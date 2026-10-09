"""Installed declared scalar capture, durability and authorized source retrieval."""
import json
import time


def run(sql,ready,ticket_from,checks):
    # PostgreSQL JSON and the serialized native SourceApply request both need
    # headroom under the 512 KiB envelope. Two individually valid large events
    # must not be dispatched together merely because their count is below 16.
    assert sql("SELECT jsonb_array_length(qdrant_internal.pack_source_events(jsonb_build_array(jsonb_build_object('body',repeat('x',300000)),jsonb_build_object('body',repeat('x',300000)))))")=='1'
    assert '54000' in sql("SELECT qdrant_internal.pack_source_events(jsonb_build_array(jsonb_build_object('body',repeat('x',500000))))",ok=False)
    literal=lambda value:"'"+value.replace("'","''")+"'"
    contract={name:dict(field=field,kind=kind) for name,field,kind in [
        ('category','Category / 汉字','keyword'),('quantity','quantity','integer'),
        ('price','price','float'),('available','available','bool')]}
    settings=dict(text=dict(fields=['body']),payload=contract)
    def register(name,source='payload_docs',payload=None):
        value=dict(settings,payload=contract if payload is None else payload)
        return 'SELECT qdrant.create_index('+literal(name)+','+literal(source)+",'id',"+literal(json.dumps(value))+')'
    def drop(name):
        task=json.loads(sql('SELECT qdrant.drop_index('+literal(name)+')'))['task_id']
        result=json.loads(sql('SELECT qdrant.await_task('+literal(task)+',60000)'))
        assert result['succeeded'] and result['physical_cleanup_completed'],result
    sql('CREATE TABLE payload_docs(id bigint PRIMARY KEY,body text NOT NULL,"Category / 汉字" text,quantity bigint,price double precision,available boolean,private_note text)')
    sql("INSERT INTO payload_docs SELECT n,'payload anchor '||n,'class '||n,CASE WHEN n=1 THEN 9223372036854775807 ELSE n END,n::float8/10,n%2=0,'private unindexed secret' FROM generate_series(1,40) n")
    sql(register('payload_docs'))
    def retrieve(key='1'):
        return json.loads(sql('SELECT qdrant.retrieve(\'payload_docs\','+literal(json.dumps([key]))+')'))['items'][0]
    def replay():
        ready('payload_docs')
        item=retrieve()
        expected=json.loads(sql("SELECT qdrant_internal.payload_projection('payload_docs',t) FROM payload_docs t WHERE id=1"))
        assert item['status']=='found' and item['attributes']==expected,item
        ledger=json.loads(sql("SELECT jsonb_build_object('payload',payload,'fp',payload_fingerprint,'body_fp',fingerprint,'revision',revision) FROM qdrant_internal.source_state WHERE index_name='payload_docs' AND tagged_key->>'value'='1'"))
        assert ledger['payload']==expected and ledger['fp']==item['payload_fingerprint'],(ledger,item)
        assert sql("SELECT count(*) FROM qdrant_internal.source_state WHERE index_name='payload_docs' AND NOT tombstone AND payload_fingerprint<>encode(sha256(convert_to(payload::text,'UTF8')),'hex')")=='0'
        assert sql("SELECT count(*) FROM qdrant_internal.outbox WHERE index_name='payload_docs' AND projection::text LIKE '%private unindexed secret%'")=='0'
    replay()
    assert retrieve()['attributes']['quantity']==9223372036854775807
    assert sql("SELECT count(*) FROM qdrant_internal.source_state WHERE index_name='payload_docs' AND NOT tombstone")=='40'
    checks.append('declared keyword/int64/float/bool payload backfill and native durable retrieval retain exact values without undeclared columns')
    before=retrieve()
    ticket=ticket_from(sql("BEGIN; UPDATE payload_docs SET quantity=-9223372036854775808,price=-12.5,available=true,\"Category / 汉字\"='changed' WHERE id=1; SELECT qdrant.track_changes('payload_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',30000)"))['durable']
    after=retrieve()
    assert after['source_fingerprint']==before['source_fingerprint'] and after['payload_fingerprint']!=before['payload_fingerprint'] and after['revision']>before['revision'],(before,after)
    assert after['attributes']==dict(category='changed',quantity=-9223372036854775808,price=-12.5,available=True),after
    sql('BEGIN; UPDATE payload_docs SET quantity=42 WHERE id=1; SAVEPOINT s; UPDATE payload_docs SET price=0 WHERE id=1; ROLLBACK TO s; ROLLBACK')
    replay()
    assert retrieve()['attributes']==after['attributes']
    for value in ["'NaN'::float8","'Infinity'::float8","'-Infinity'::float8"]:
        assert '22023' in sql('UPDATE payload_docs SET price='+value+' WHERE id=1',ok=False)
    assert '22023' in sql('UPDATE payload_docs SET "Category / 汉字"=repeat(\'x\',1025) WHERE id=1',ok=False)
    assert retrieve()['attributes']==after['attributes']
    sql('UPDATE payload_docs SET "Category / 汉字"=NULL,quantity=NULL,price=NULL,available=NULL WHERE id=2')
    ready('payload_docs')
    assert retrieve('2')['attributes']==dict(category=None,quantity=None,price=None,available=None)
    checks.append('payload-only revisions preserve text fingerprints, flush before exact ticket ACK, roll back rejected/nonfinite values and retain explicit null scalars')
    old=retrieve('3')
    sql("DELETE FROM payload_docs WHERE id=3; INSERT INTO payload_docs VALUES(3,'new payload incarnation','new',3,0.3,true,'private unindexed secret')")
    ready('payload_docs')
    new=retrieve('3')
    assert new['incarnation']!=old['incarnation'] and new['point_id']!=old['point_id'] and new['attributes']['category']=='new',(old,new)
    task=json.loads(sql("SELECT qdrant.rebuild_index('payload_docs')"))
    # Public task status and fresh active generation prove the same scalar contract
    # is built and caught up through actual shadow native delivery.
    assert json.loads(sql("SELECT qdrant.await_task('"+task['task_id']+"',30000)"))['state']=='succeeded'
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        cleanup=json.loads(sql("SELECT qdrant.task_status('"+task['task_id']+"')"))
        if cleanup['retired_storage_cleanup']: break
        time.sleep(.01)
    assert cleanup['retired_storage_cleanup'],cleanup
    replay()
    checks.append('payload primary-key reincarnation and actual shadow generation rebuild retain current attributes')
    sql('CREATE TABLE payload_invalid(id bigint PRIMARY KEY,body text NOT NULL,quantity bigint)')
    bad=[({'Alias':dict(field='quantity',kind='integer')},'22023'),
         ({'alias.path':dict(field='quantity',kind='integer')},'22023'),
         ({'x':dict(field='missing',kind='integer')},'22023'),
         ({'x':dict(field='quantity',kind='keyword')},'22023'),
         ({'x':dict(field='quantity',kind='geo')},'0A000'),
         ({'x':dict(field='quantity',kind='integer',path='/tmp')},'22023'),
         ({'x':dict(kind='integer')},'22023'),
         ({'x'+str(n):dict(field='quantity',kind='integer') for n in range(9)},'54000'),
         ([], '22023'),(None,'22023')]
    for payload,code in bad:
        value=dict(text=dict(fields=['body']),payload=payload)
        error=sql('SELECT qdrant.create_index(\'payload_invalid\',\'payload_invalid\',\'id\','+literal(json.dumps(value))+')',ok=False)
        assert code in error,(value,error)
        assert sql("SELECT count(*) FROM qdrant_internal.index_catalog WHERE index_name='payload_invalid'")=='0'
        assert sql("SELECT count(*) FROM pg_trigger WHERE tgrelid='payload_invalid'::regclass AND NOT tgisinternal")=='0'
    sql('GRANT INSERT,UPDATE,DELETE ON payload_docs TO pgq_writer')
    sql("SET ROLE pgq_writer; UPDATE payload_docs SET quantity=5")
    ready('payload_docs')
    assert retrieve('4')['attributes']['quantity']==5
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant.retrieve('payload_docs','[\"1\"]')",ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT * FROM qdrant_internal.payload_catalog",ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.payload_projection('payload_docs',t) FROM payload_docs t",ok=False)
    checks.append('invalid payload registration rolls back catalog/triggers, ordinary writers capture without private privileges and unauthorized retrieval/catalog access fails')
    sql('CREATE TABLE payload_binding(id bigint PRIMARY KEY,body text NOT NULL,category text)')
    sql(register('payload_binding','payload_binding',dict(category=dict(field='category',kind='keyword'))))
    ready('payload_binding')
    sql('ALTER TABLE payload_binding ALTER COLUMN category TYPE bigint USING NULL::bigint')
    assert '55000' in sql("SELECT qdrant.retrieve('payload_binding','[\"1\"]')",ok=False)
    drop('payload_binding')
    checks.append('payload source type changes invalidate admission and cannot serve an old native schema')
    sql('CREATE TABLE payload_large(id bigint PRIMARY KEY,body text NOT NULL,category text)')
    sql(register('payload_large','payload_large',{'c'+str(n):dict(field='category',kind='keyword') for n in range(8)}))
    assert '54000' in sql("INSERT INTO payload_large VALUES(1,'anchor',repeat('x',1024))",ok=False)
    assert sql('SELECT count(*) FROM payload_large')=='0'
    drop('payload_large')
    # Four accepted near-limit source projections exceed one IPC envelope.
    # Exact byte packing must make forward progress without dropping membership.
    sql('CREATE TABLE payload_packed(id bigint PRIMARY KEY,body text NOT NULL,category text)')
    sql(register('payload_packed','payload_packed',dict(category=dict(field='category',kind='keyword'))))
    ready('payload_packed')
    ticket=ticket_from(sql("BEGIN; INSERT INTO payload_packed SELECT n,'anchor'||repeat(chr(1),60000),'class' FROM generate_series(1,4) n; SELECT qdrant.track_changes('payload_packed'); COMMIT"))
    waited=json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',30000)"))
    assert waited['durable'] and waited['pending_events']==0,waited
    assert sql("SELECT count(*) FROM qdrant_internal.event_ack a JOIN qdrant_internal.outbox e USING(event_id) WHERE e.index_name='payload_packed'")=='4'
    assert sql("SELECT count(*) FROM qdrant.search('payload_packed','anchor')")=='4'
    task=json.loads(sql("SELECT qdrant.rebuild_index('payload_packed')"))['task_id']
    waited=json.loads(sql("SELECT qdrant.await_task('"+task+"',30000)"))
    assert waited['succeeded'],(waited,sql("SELECT qdrant.task_status('"+task+"')"),sql("SELECT qdrant.index_status('payload_packed')"))
    assert sql("SELECT count(*) FROM qdrant.search('payload_packed','anchor')")=='4'
    drop('payload_packed')
    checks.append('8 KiB scalar projection overflow rolls back, 480 KiB event prefix leaves IPC headroom and multi-envelope source/backfill batches ACK all exact events after native flush')
    return replay

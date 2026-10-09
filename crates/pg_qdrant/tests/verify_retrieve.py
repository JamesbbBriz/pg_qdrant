"""Actual Edge retrieval and current PostgreSQL source/version arbitration."""
import json
import os
import signal
import time


def run(sql,ready,ticket_from,checks,spawn=None):
    literal=lambda value:"'"+value.replace("'","''")+"'"
    def statement(keys,index='retrieve_docs',options=None):
        return 'SELECT qdrant.retrieve('+literal(index)+','+literal(json.dumps(keys))+','+literal(json.dumps(options or {}))+')'
    def call(keys,index='retrieve_docs',options=None):
        return json.loads(sql(statement(keys,index,options)))
    sql('CREATE TABLE retrieve_docs(id bigint PRIMARY KEY,body text NOT NULL)')
    sql("INSERT INTO retrieve_docs SELECT n,'retrieve original '||n FROM generate_series(1,100) n")
    sql("SELECT qdrant.create_index('retrieve_docs','retrieve_docs','id','{\"text\":{\"fields\":[\"body\"]}}')")
    def replay():
        ready('retrieve_docs')
        result=call(['7','001','999'])
        assert result['ordering']=='normalized request order' and not result['release_supported'],result
        assert [item['source_key'] for item in result['items']]==['7','1','999'],result
        assert [item['status'] for item in result['items']]==['found','found','source_missing'],result
        for item in result['items'][:2]:
            expected=json.loads(sql("SELECT jsonb_build_object('body',t.body,'revision',s.revision,'incarnation',s.incarnation,'point_id',s.point_id,'fp',s.fingerprint) FROM retrieve_docs t JOIN qdrant_internal.source_state s ON s.index_name='retrieve_docs' AND s.tagged_key->>'value'=t.id::text WHERE t.id="+item['source_key']))
            assert item['excerpt']==expected['body'][:240] and item['revision']==expected['revision'] and item['incarnation']==expected['incarnation'] and item['point_id']==expected['point_id'] and item['source_fingerprint']==expected['fp'],(item,expected)
            assert 'score' not in item and 'vector' not in item and 'body' not in item,item
        assert call(['998','999'])['items']==[dict(source_key=k,status='source_missing') for k in ['998','999']]
    replay()
    hundred=call([str(n) for n in range(100,0,-1)])
    assert len(hundred['items'])==100 and all(item['status']=='found' for item in hundred['items']),hundred
    assert [item['source_key'] for item in hundred['items']]==[str(n) for n in range(100,0,-1)]
    assert len(json.dumps(hundred).encode())<262144
    checks.append('native retrieve preserves source-key request order, exact 100-key boundary and metadata-only/source excerpts')
    for keys,options in [([] ,{}),([str(n) for n in range(101)],{}),(['1','01'],{}),([1],{}),([None],{}),(['9223372036854775808'],{}),(['x'],{}),(['1'],dict(timeout_ms=0)),(['1'],dict(timeout_ms=30001)),(['1'],dict(timeout_ms=1.5)),(['1'],dict(timeout_ms='1')),(['1'],dict(timeout_ms=None)),(['1'],dict(with_vectors=True)),(['1'],dict(filter={})),(['1'],dict(path='/tmp'))]:
        error=sql(statement(keys,options=options),ok=False)
        assert '22023' in error,(keys,options,error)
    for argument in ['NULL',"'{}'::jsonb","'null'::jsonb"]:
        assert '22023' in sql("SELECT qdrant.retrieve('retrieve_docs',"+argument+')',ok=False)
    checks.append('retrieve refuses malformed, duplicate-normalized, over-budget keys/options and arbitrary native selectors')
    before=call(['7'])['items'][0]
    ticket=ticket_from(sql("BEGIN; DELETE FROM retrieve_docs WHERE id=7; INSERT INTO retrieve_docs VALUES(7,'retrieve reused'); SELECT qdrant.track_changes('retrieve_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',30000)"))['durable']
    after=call(['7'])['items'][0]
    assert after['status']=='found' and after['excerpt']=='retrieve reused' and after['point_id']!=before['point_id'] and after['incarnation']!=before['incarnation'] and after['revision']>before['revision'],(before,after)
    sql("BEGIN; UPDATE retrieve_docs SET body='rolled back' WHERE id=1; ROLLBACK")
    replay()
    for kind,keys in [('uuid',['00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002']),('text',["quoted 'key / 汉字",'Case KEY'])]:
        index='retrieve_'+kind
        sql('CREATE TABLE '+index+'(id '+kind+' PRIMARY KEY,body text NOT NULL)')
        sql('INSERT INTO '+index+' VALUES('+literal(keys[0])+",'first source'),("+literal(keys[1])+",'second source')")
        sql('SELECT qdrant.create_index('+literal(index)+','+literal(index)+",'id','{\"text\":{\"fields\":[\"body\"]}}')")
        ready(index)
        items=call(list(reversed(keys)),index)['items']
        assert [v['source_key'] for v in items]==list(reversed(keys)) and [v['excerpt'] for v in items]==['second source','first source'],items
        sql('SELECT qdrant.drop_index('+literal(index)+')')
    checks.append('retrieve uses bigint/uuid/quoted Unicode text identities and durable deletion/reuse with new point/incarnation')
    sql('CREATE TABLE retrieve_bytes(id text PRIMARY KEY,body text NOT NULL)')
    sql("INSERT INTO retrieve_bytes SELECT repeat('k',1000)||lpad(n::text,3,'0'),'a'||repeat(chr(1),239) FROM generate_series(1,100) n")
    sql("SELECT qdrant.create_index('retrieve_bytes','retrieve_bytes','id','{\"text\":{\"fields\":[\"body\"]}}')")
    ready('retrieve_bytes')
    keys=['k'*1000+str(n).zfill(3) for n in range(1,101)]
    assert len(json.dumps(keys).encode())<131072
    error=sql(statement(keys,'retrieve_bytes'),ok=False)
    assert '54000' in error and 'Retrieve result exceeds 256 KiB' in error,error
    sql("SELECT qdrant.drop_index('retrieve_bytes')")
    replay()
    checks.append('actual native/source escaped excerpts enforce the 256 KiB retrieve response bound without returning a partial array')
    assert '42501' in sql('SET ROLE pgq_writer; '+statement(['999']),ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.p1_retrieve('{}',1000)",ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.require_readable_source('retrieve_docs')",ok=False)
    sql('CREATE ROLE pgq_retrieve_reader; GRANT builder TO pgq_retrieve_reader; GRANT SELECT ON retrieve_docs TO pgq_retrieve_reader')
    assert json.loads(sql('SET ROLE pgq_retrieve_reader; '+statement(['1'])))['items'][0]['status']=='found'
    sql('REVOKE SELECT ON retrieve_docs FROM builder,pgq_retrieve_reader')
    assert '42501' in sql('SET ROLE pgq_retrieve_reader; '+statement(['999']),ok=False)
    sql('GRANT SELECT ON retrieve_docs TO builder,pgq_retrieve_reader')
    assert '0A000' in sql('BEGIN; ALTER TABLE retrieve_docs ENABLE ROW LEVEL SECURITY; '+statement(['1']),ok=False)
    assert '55000' in sql('BEGIN; ALTER TABLE retrieve_docs DISABLE TRIGGER qdrant_p1_rows; '+statement(['1']),ok=False)
    assert '55000' in sql("BEGIN; UPDATE qdrant_internal.consumer_state SET state='dirty' WHERE index_name='retrieve_docs'; "+statement(['1']),ok=False)
    replay()
    checks.append('retrieve rechecks index/source/column permission and refuses RLS, capture drift, dirty generation and private entry access')
    if spawn is not None:
        def wait_active():
            end=time.monotonic()+10
            while time.monotonic()<end:
                state=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if (state.get('active') or {}).get('operation')=='source_retrieve':return state
                time.sleep(.02)
            raise AssertionError('native source retrieve was not admitted')
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'));pid=owner['engine_pid']
        os.kill(pid,signal.SIGSTOP)
        try:
            waiting=spawn(statement(['1']),'pgq_retrieve_cancel');wait_active()
            assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='pgq_retrieve_cancel'")=='t'
            out,err=waiting.communicate(timeout=10)
            assert waiting.returncode and '57014' in err,(out,err)
            retained=wait_active()
            assert retained['engine_pid']==pid and retained['engine_instance']==owner['engine_instance'],retained
        finally:os.kill(pid,signal.SIGCONT)
        end=time.monotonic()+10
        while time.monotonic()<end:
            completed=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
            if not completed.get('active'):break
            time.sleep(.02)
        assert not completed.get('active'),completed
        replay()
        os.kill(pid,signal.SIGSTOP)
        try:
            waiting=spawn(statement(['1']),'pgq_retrieve_change');wait_active()
            sql("UPDATE retrieve_docs SET body='concurrent new source' WHERE id=1")
        finally:os.kill(pid,signal.SIGCONT)
        out,err=waiting.communicate(timeout=15)
        if waiting.returncode:
            assert '55000' in err,(out,err)
        else:
            item=json.loads(out)['items'][0]
            assert item['status'] in ['native_stale','found'],item
            if item['status']=='found':assert item['excerpt']=='concurrent new source',item
            else:assert set(item)=={'source_key','status'},item
        ready('retrieve_docs');replay()
        checks.append('cancelled native retrieve retains sole owner; committed concurrent source update never exposes old text/version')
    return replay

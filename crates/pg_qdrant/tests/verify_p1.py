"""Actual installed PostgreSQL -> embedded Edge -> flush -> exact ACK checks."""
import json
import hashlib
import os
import pathlib
import subprocess
import time

checks = []
crash_matrix = []
PSQL = ['psql','-X','-A','-t','-q','-v','ON_ERROR_STOP=1','-v','VERBOSITY=verbose']

def spawn(query, name):
    env = dict(os.environ, PGAPPNAME=name)
    return subprocess.Popen(PSQL+['-c',query],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)

def finish(process):
    out,err=process.communicate(timeout=15)
    assert process.returncode==0,(out,err)
    return out.strip()

def ticket_from(output):
    return next(line for line in output.splitlines() if len(line)==36 and line.count('-')==4)

def wait_session(name, event):
    end=time.monotonic()+10
    while time.monotonic()<end:
        if sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='"+name+"' AND "+event)=='1':
            return
        time.sleep(.01)
    raise AssertionError('session did not reach '+event)
def sql(query, ok=True):
    p = subprocess.run(PSQL+['-c',query],text=True,capture_output=True,timeout=130)
    if ok and p.returncode:
        raise AssertionError(query + '\n' + p.stderr)
    if not ok:
        assert p.returncode, 'expected SQL error: ' + query
        return p.stderr
    return p.stdout.strip()

def ready(index='docs', timeout=60):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        status=json.loads(sql("SELECT qdrant.index_status('"+index+"')"))
        if status.get('engine_index_ready') and status['pending_events']==0:
            return status
        time.sleep(.02)
    raise AssertionError(status)

def hits(q, index='docs'):
    return sql("SELECT coalesce(jsonb_agg(source_key ORDER BY rank),'[]') FROM qdrant.search('"+index+"','"+q+"')")

sql('CREATE EXTENSION pg_qdrant')
sql('CREATE TABLE docs(id bigint PRIMARY KEY, body text NOT NULL, ignored text)')
sql("INSERT INTO docs SELECT n,'transaction recovery '||n,repeat('x',100000) FROM generate_series(1,1100)n")
sql("SELECT qdrant.create_index('docs','docs','id','{\"text\":{\"fields\":[\"body\"]}}')")
ready()
assert sql("SELECT count(*) FROM qdrant_internal.source_state WHERE index_name='docs'")=='1100'
assert hits('recovery')!='[]'
checks += ['CREATE EXTENSION install','online backfill beyond 1000','real offline Edge BM25','narrow field capture']

sql("BEGIN; INSERT INTO docs VALUES(5000,'rollback ghost',NULL); SAVEPOINT s; UPDATE docs SET body='ghost' WHERE id=1; ROLLBACK TO s; ROLLBACK")
assert sql("SELECT count(*) FROM qdrant_internal.outbox WHERE projection->>'body' LIKE '%ghost%'")=='0'
sql("BEGIN; UPDATE docs SET body='changed committed' WHERE id=1; SELECT qdrant.track_changes('docs') AS t; COMMIT")
ticket=sql("SELECT ticket_id FROM qdrant_internal.change_tickets ORDER BY source_xid DESC LIMIT 1")
result=json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))
assert result['durable'] and result['pending_events']==0, result
sql("BEGIN; INSERT INTO docs VALUES(5001,'uncommitted',NULL); SELECT qdrant.await_changes(qdrant.track_changes('docs'),100); COMMIT",ok=False)
checks += ['rollback/savepoint','committed fixed ticket durable','own uncommitted wait rejection']

# An earlier allocated event commits later than an already durable higher ID.
slow=spawn("BEGIN; INSERT INTO docs VALUES(7001,'slowcommit order',NULL); SELECT qdrant.track_changes('docs'); SELECT pg_sleep(5); COMMIT",'pgq_commit_inversion')
wait_session('pgq_commit_inversion',"wait_event='PgSleep'")
fast_ticket=ticket_from(sql("BEGIN; INSERT INTO docs VALUES(7002,'fastcommit order',NULL); SELECT qdrant.track_changes('docs'); COMMIT"))
fast=json.loads(sql("SELECT qdrant.await_changes('"+fast_ticket+"',4000)"))
assert fast['durable'],fast
assert slow.poll() is None,'higher event must ACK before the earlier transaction commits'
slow_ticket=ticket_from(finish(slow))
assert json.loads(sql("SELECT qdrant.await_changes('"+slow_ticket+"',60000)"))['durable']
assert hits('slowcommit')=='["7001"]' and hits('fastcommit')=='["7002"]'
checks += ['commit inversion uses exact event sets']

first=spawn("BEGIN; UPDATE docs SET body='firstserial' WHERE id=2; SELECT pg_sleep(2); COMMIT",'pgq_update_first')
wait_session('pgq_update_first',"wait_event='PgSleep'")
second=spawn("UPDATE docs SET body='secondserial' WHERE id=2",'pgq_update_second')
wait_session('pgq_update_second',"wait_event_type='Lock'")
finish(first); finish(second); ready()
assert hits('firstserial')=='[]' and hits('secondserial')=='["2"]'
checks += ['same-key concurrent updates serialize']

# Maximum admitted source text exceeds frontend IPC and escapes heavily in JSON.
sql("INSERT INTO docs VALUES(7100,repeat(chr(1),64000)||' longbody',NULL)")
ready()
assert hits('longbody')=='["7100"]'
sql("UPDATE docs SET body='copybatch' WHERE id IN (3,4,5)")
copy=subprocess.run(PSQL+['-c','COPY docs(id,body) FROM STDIN'],input='7200\tcopybatch\n7201\tcopybatch\n',text=True,capture_output=True)
assert copy.returncode==0,copy.stderr
ready()
assert len(json.loads(hits('copybatch')))==5
checks += ['64 KiB escaped-text consumer frame','COPY and multi-row UPDATE reach Edge']

old=sql("SELECT point_id FROM qdrant_internal.source_state WHERE index_name='docs' AND tagged_key->>'value'='1'")
sql("DELETE FROM docs WHERE id=1; INSERT INTO docs VALUES(1,'reused incarnation',NULL)")
ready()
new=sql("SELECT point_id FROM qdrant_internal.source_state WHERE index_name='docs' AND tagged_key->>'value'='1'")
assert old!=new
assert hits('changed')=='[]'
assert hits('reused')=='["1"]'
sql('UPDATE docs SET id=5002 WHERE id=1')
ready()
assert hits('reused')=='["5002"]'
checks += ['old point deletion after primary-key reuse','primary-key update']

sql('CREATE ROLE pgq_writer; GRANT INSERT,UPDATE,DELETE ON docs TO pgq_writer')
sql("SET ROLE pgq_writer; INSERT INTO docs VALUES(6000,'ordinary writer',NULL)")
sql("SET ROLE pgq_writer; SELECT * FROM qdrant.search('docs','reused')",ok=False)
sql("SET ROLE pgq_writer; SELECT * FROM qdrant_internal.outbox",ok=False)
sql("SET ROLE pgq_writer; SELECT qdrant.create_index('forbidden','docs','id','{\"text\":{\"fields\":[\"body\"]}}')",ok=False)
for query in ["qdrant.rebuild_index('docs')","qdrant.rebuild_status('docs')","qdrant.cancel_rebuild('docs',2)"]:
    assert '42501' in sql('SET ROLE pgq_writer; SELECT '+query,ok=False)
ready()
checks += ['ordinary writer needs no ledger privileges','effective role access rejection']

for keytype,key in [('uuid',"'12345678-1234-1234-1234-123456789abc'"),('text',"'你好 key'")]:
    name='keys_'+keytype
    sql('CREATE TABLE '+name+'(id '+keytype+' PRIMARY KEY,body text NOT NULL)')
    sql("INSERT INTO "+name+" VALUES("+key+",'identity roundtrip')")
    sql("SELECT qdrant.create_index('"+name+"','"+name+"','id','{\"text\":{\"fields\":[\"body\"]}}')")
    ready(name)
    assert hits('roundtrip',name)!='[]'
checks += ['uuid and Unicode text identity']

# Invalidation survives re-enabling capture; source writes stay authoritative.
sql('ALTER TABLE keys_text DISABLE TRIGGER qdrant_p1_rows')
sql("UPDATE keys_text SET body='missedcapture' WHERE id='你好 key'")
sql('ALTER TABLE keys_text ENABLE TRIGGER qdrant_p1_rows')
assert json.loads(sql("SELECT qdrant.index_status('keys_text')"))['state']=='degraded'
assert '55000' in sql("SELECT * FROM qdrant.search('keys_text','roundtrip')",ok=False)
sql("SELECT qdrant.drop_index('keys_text')")
sql("SELECT qdrant.create_index('keys_text','keys_text','id','{\"text\":{\"fields\":[\"body\"]}}')")
ready('keys_text')
assert hits('missedcapture','keys_text')!='[]'
sql('DROP TABLE keys_text')
assert not json.loads(sql("SELECT qdrant.index_status('keys_text')"))['source_exists']
sql("SELECT qdrant.drop_index('keys_text')")
sql('ALTER TABLE keys_uuid DROP CONSTRAINT keys_uuid_pkey')
assert json.loads(sql("SELECT qdrant.index_status('keys_uuid')"))['state']=='degraded'
assert '55000' in sql("SELECT * FROM qdrant.search('keys_uuid','identity')",ok=False)
sql("SELECT qdrant.drop_index('keys_uuid')")
checks += ['durable DDL invalidation and explicit reconstruction','source DROP and primary-key drift fail closed']

sql('TRUNCATE docs')
ready()
assert hits('recovery')=='[]'
sql("INSERT INTO docs VALUES(1,'final recovery',NULL)")
ready()
checks += ['TRUNCATE tombstones']

status=ready()
# Kill actual managed helper and verify complete authoritative reconstruction.
pid=json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
os.kill(pid,9)
end=time.monotonic()+60
while time.monotonic()<end:
    status2=ready()
    if status2['storage_epoch']!=status['storage_epoch']:
        break
    time.sleep(.05)
assert status2['storage_epoch']!=status['storage_epoch']
assert hits('final')=='["1"]'
checks += ['helper SIGKILL new storage epoch reconstruction']

faults=sql('SELECT (qdrant.build_info()->\'features\'->>\'p0_fault_injection\')::boolean')=='t'
if faults:
    data=pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA'])
    # SourceOwner fault marker is sibling of its database-scoped storage root.
    owner=next(data.glob('pg_qdrant*/db-*.engine-owner'),None)
    if owner is None:
        owner=next(data.rglob('db-*.engine-owner'))
    marker=owner.with_suffix('.fault')
    for cut in ['before_apply','before_flush','after_flush']:
        before=ready()
        marker.write_text(cut)
        ticket_output=sql("BEGIN; UPDATE docs SET body='"+cut+" durable recovery' WHERE id=1; SELECT qdrant.track_changes('docs'); COMMIT")
        ticket=next(line for line in ticket_output.splitlines() if len(line)==36 and line.count('-')==4)
        pending=json.loads(sql("SELECT qdrant_internal.ticket_status('"+ticket+"')"))
        assert not pending['durable'] and pending['pending_events']>0,pending
        result=json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))
        assert result['durable'],result
        after=ready()
        assert not marker.exists(),'native crash marker was not consumed'
        assert before['storage_epoch']!=after['storage_epoch'],'native fault did not replace the owner'
        assert hits(cut)=='["1"]'
        crash_matrix.append({'cut':cut,'before':before,'ticket_pending':pending,
            'after':after,'ticket_durable':result,'final_source_keys':json.loads(hits(cut))})
        checks.append('native crash '+cut+' / exact-set replay')

for fixture in ['p3_reservations.sql','p4_advanced.sql']:
    fixture_result=subprocess.run(PSQL+['-f','/src/crates/pg_qdrant/tests/'+fixture],text=True,capture_output=True,timeout=20)
    assert fixture_result.returncode==0,(fixture,fixture_result.stderr)
checks += ['retained P3 reservations and P4 shape validators (no native execution claim)']

print(json.dumps({'status':'passed','checks':checks,'fault_build':faults,
    'source_revision':os.environ.get('PG_QDRANT_SOURCE_SHA','unrecorded'),
    'cargo_lock_sha256':hashlib.sha256(pathlib.Path('/src/Cargo.lock').read_bytes()).hexdigest(),
    'build_info':json.loads(sql('SELECT qdrant.build_info()')), 'crash_matrix':crash_matrix,
    'postgres_version':sql('SHOW server_version'), 'release_supported':False},indent=2))

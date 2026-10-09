"""Actual installed PostgreSQL -> embedded Edge -> flush -> exact ACK checks."""
import json
import hashlib
import os
import pathlib
import resource
import signal
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
        diagnostic=subprocess.run(PSQL+['-c',"SELECT jsonb_agg(jsonb_build_object('pid',pid,'backend_type',backend_type,'state',state,'wait_type',wait_event_type,'wait',wait_event,'query',left(query,300),'blocked_by',pg_blocking_pids(pid))) FROM pg_stat_activity"],text=True,capture_output=True,timeout=5)
        processes=subprocess.run(['ps','-eLo','pid,tid,ppid,stat,wchan:24,comm'],text=True,capture_output=True,timeout=5)
        raise AssertionError(query + '\n' + p.stderr+'\n'+diagnostic.stdout+'\n'+processes.stdout)
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

def native_hits(q, index='docs'):
    # Inspect native candidates before SQL source rechecking can hide an old ID.
    request="(SELECT jsonb_build_object('operation','source_search','index_id',i.index_id,'generation',i.generation,'storage_epoch',c.storage_epoch,'q','"+q+"','top_k',1000) FROM qdrant_internal.index_catalog i JOIN qdrant_internal.consumer_state c USING(index_name) WHERE i.index_name='"+index+"')"
    return json.loads(sql('SELECT qdrant_internal.p1_search('+request+',30000)'))

disposable=pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA']).resolve()
assert disposable.parent.parent==pathlib.Path('/tmp') and disposable.parent.name.startswith('pgq-p1-product.')
assert pathlib.Path(sql('SHOW data_directory')).resolve()==disposable
sql('CREATE EXTENSION pg_qdrant')
faults=sql('SELECT (qdrant.build_info()->\'features\'->>\'p0_fault_injection\')::boolean')=='t'
capabilities=json.loads(sql('SELECT qdrant.capabilities()'))
assert capabilities['index_catalog_available']
assert capabilities['bounded_search_page_available']
assert capabilities['source_retrieve_available']
assert capabilities['source_statistics_available']
assert capabilities['source_matrix_available']
assert capabilities['source_groups_available']
assert capabilities['bounded_lexical_available']
assert [item['id'] for item in capabilities['capabilities'] if item['sql_product_interface']]==['F01','F02','F04','F05','F06','F07','F08','F09','F10','F11','F13','F14','F15','F17','F19','V01','V02','V03','Q02','Q03','Q04','Q05','Q06','Q07','Q08','Q09','Q10','Q11','Q12']
assert not any(item['release_supported'] for item in capabilities['capabilities'])
if faults:
    import verify_transport
    verify_transport.run(sql,ready,ticket_from,checks,crash_matrix)
sql('CREATE TABLE docs(id bigint PRIMARY KEY, body text NOT NULL, ignored text)')
sql("INSERT INTO docs SELECT n,'transaction recovery '||n,repeat('x',100000) FROM generate_series(1,1100)n")
sql("SELECT qdrant.create_index('docs','docs','id','{\"text\":{\"fields\":[\"body\"]}}')")
ready()
idle_version=sql("SELECT xmax::text FROM qdrant_internal.consumer_state WHERE index_name='docs'")
time.sleep(.2)
assert sql("SELECT xmax::text FROM qdrant_internal.consumer_state WHERE index_name='docs'")==idle_version,'idle polling must not repeatedly lock the consumer tuple'
assert sql("SELECT count(*) FROM qdrant_internal.source_state WHERE index_name='docs'")=='1100'
assert hits('recovery')!='[]'
checks += ['CREATE EXTENSION install','online backfill beyond 1000','real offline Edge BM25','narrow field capture','idle consumer does not rewrite tuple locks']
import verify_dispatch
verify_dispatch.run(sql,ready,checks)

guard_before = json.loads(sql('SELECT qdrant_internal.p0_ping()'))
guard_pid = guard_before['engine_pid']
guard_limits = guard_before['helper_resource_limits']
assert guard_limits['address_space_limit_enforced'] and not guard_limits['rss_limit_enforced']
guard_bytes = guard_limits['address_space_soft_bytes']
assert 512*1024**2 <= guard_bytes <= 8*1024**3 and guard_limits['address_space_hard_bytes'] == guard_bytes
assert resource.prlimit(guard_pid,resource.RLIMIT_AS) == (guard_bytes,guard_bytes)
supervisor_as = resource.prlimit(guard_before['worker_pid'],resource.RLIMIT_AS)[0]
assert supervisor_as == resource.RLIM_INFINITY or supervisor_as > guard_bytes
if json.loads(sql('SELECT qdrant.build_info()'))['features']['p0_fault_injection']:
    guard_probe = json.loads(sql("SELECT qdrant_internal.p0_fault('address_space')"))
    assert guard_probe['mapping_refused'] and guard_probe['errno'] == 12 and guard_probe['requested_mapping_bytes'] > guard_bytes
    assert guard_probe['physical_pages_touched'] == 0 and not guard_probe['kernel_oom']
    assert json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid'] == guard_pid
    assert hits('recovery') != '[]'
    checks.append('actual helper RLIMIT_AS/ENOMEM probe preserves native queries; PostgreSQL supervisor has separate limits')
else:
    checks.append('actual helper RLIMIT_AS observed independently; PostgreSQL supervisor has separate limits')

sql("BEGIN; INSERT INTO docs VALUES(5000,'rollback ghost',NULL); SAVEPOINT s; UPDATE docs SET body='ghost' WHERE id=1; ROLLBACK TO s; ROLLBACK")
assert sql("SELECT count(*) FROM qdrant_internal.outbox WHERE projection->>'body' LIKE '%ghost%'")=='0'
sql("BEGIN; UPDATE docs SET body='changed committed' WHERE id=1; SELECT qdrant.track_changes('docs') AS t; COMMIT")
ticket=sql("SELECT ticket_id FROM qdrant_internal.change_tickets ORDER BY source_xid DESC LIMIT 1")
result=json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))
assert result['durable'] and result['pending_events']==0, result
sql("BEGIN; INSERT INTO docs VALUES(5001,'uncommitted',NULL); SELECT qdrant.await_changes(qdrant.track_changes('docs'),100); COMMIT",ok=False)
checks += ['rollback/savepoint','committed fixed ticket durable','own uncommitted wait rejection']

# Later writes in the same transaction belong to a different sealed ticket.
sealed=sql("BEGIN; INSERT INTO docs VALUES(8100,'sealedfirst',NULL); SELECT qdrant.track_changes('docs'); UPDATE docs SET body='sealedsecond' WHERE id=8100; SELECT qdrant.track_changes('docs'); COMMIT")
tickets=[line for line in sealed.splitlines() if len(line)==36 and line.count('-')==4]
assert len(tickets)==2 and tickets[0]!=tickets[1]
for sealed_ticket in tickets:
    assert sql("SELECT sealed_events FROM qdrant_internal.change_tickets WHERE ticket_id='"+sealed_ticket+"'")=='1'
    assert sql("SELECT count(*) FROM qdrant_internal.outbox WHERE ticket_id='"+sealed_ticket+"'")=='1'
    assert json.loads(sql("SELECT qdrant.await_changes('"+sealed_ticket+"',60000)"))['durable']
assert hits('sealedfirst')=='[]' and hits('sealedsecond')=='["8100"]'
empty_ticket=sql("SELECT qdrant.track_changes('docs')")
empty=json.loads(sql("SELECT qdrant.await_changes('"+empty_ticket+"',5000)"))
assert empty['durable'] and empty['pending_events']==0,empty
assert '25001' in sql("BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT qdrant.await_changes('"+empty_ticket+"',10)",ok=False)
checks += ['fixed membership across later writes, empty ticket and snapshot rejection']

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

# A locked first key cannot strand other indexes or advance a backfill cursor.
sql('CREATE TABLE online_build(id bigint PRIMARY KEY,body text NOT NULL)')
sql("INSERT INTO online_build SELECT n,'onlineinitial '||n FROM generate_series(1,96)n")
blocked_scan=spawn("BEGIN; SELECT id FROM online_build WHERE id=1 FOR UPDATE; SELECT pg_sleep(5); UPDATE online_build SET body='onlinechanged' WHERE id=1; DELETE FROM online_build WHERE id=2; UPDATE online_build SET id=200 WHERE id=3; INSERT INTO online_build VALUES(100,'onlinenew'); COMMIT",'pgq_backfill_locked_key')
wait_session('pgq_backfill_locked_key',"wait_event='PgSleep'")
sql("SELECT qdrant.create_index('online_build','online_build','id','{\"text\":{\"fields\":[\"body\"]}}')")
other_ticket=ticket_from(sql("BEGIN; UPDATE docs SET body='otherindexprogress' WHERE id=6; SELECT qdrant.track_changes('docs'); COMMIT"))
assert json.loads(sql("SELECT qdrant.await_changes('"+other_ticket+"',4000)"))['durable']
assert blocked_scan.poll() is None,'backfill must not block another index on the source row lock'
assert sql("SELECT backfill_cursor IS NULL AND NOT backfill_done FROM qdrant_internal.index_catalog WHERE index_name='online_build'")=='t'
finish(blocked_scan); ready('online_build')
assert hits('onlinechanged','online_build')=='["1"]'
assert hits('onlinenew','online_build')=='["100"]'
assert sql("SELECT count(*) FROM qdrant_internal.source_state WHERE index_name='online_build' AND NOT tombstone")=='96'
assert '2' not in [str(hit['payload']['source_key']['value']) for hit in native_hits('onlineinitial','online_build')]
assert '200' in [str(hit['payload']['source_key']['value']) for hit in native_hits('onlineinitial','online_build')]
sql("SELECT qdrant.drop_index('online_build')")
checks += ['nonblocking backfill retains locked keys and captures build-time mutations']

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

sql("INSERT INTO docs SELECT n,repeat('budgetedlongbody ',3800),NULL FROM generate_series(7300,7319)n")
ready()
native=native_hits('budgetedlongbody')
assert len(native)==20 and all('body' not in hit['payload'] for hit in native)
assert sql("SELECT count(*) FROM qdrant.search('docs','budgetedlongbody','text',20,'{}','{\"candidate_limit\":20}')")=='20'
checks += ['long-body candidates use metadata-only IPC and authorized source excerpts']

old=sql("SELECT point_id FROM qdrant_internal.source_state WHERE index_name='docs' AND tagged_key->>'value'='1'")
assert old in [str(hit['id']) for hit in native_hits('changed')]
sql("DELETE FROM docs WHERE id=1; INSERT INTO docs VALUES(1,'reused incarnation',NULL)")
ready()
new=sql("SELECT point_id FROM qdrant_internal.source_state WHERE index_name='docs' AND tagged_key->>'value'='1'")
assert old!=new
assert native_hits('changed')==[], 'obsolete native point cannot be hidden by the source JOIN'
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

# A source DDL change cannot cross the native-query permission boundary.
sql('CREATE TABLE query_binding(id bigint PRIMARY KEY,body text NOT NULL)')
sql("INSERT INTO query_binding VALUES(1,'querybinding token')")
sql("SELECT qdrant.create_index('query_binding','query_binding','id','{\"text\":{\"fields\":[\"body\"]}}')")
ready('query_binding')
pid=json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
os.kill(pid,signal.SIGSTOP)
try:
    query=spawn("SELECT source_key FROM qdrant.search('query_binding','querybinding')",'pgq_search_binding')
    end=time.monotonic()+10
    while time.monotonic()<end:
        if (json.loads(sql('SELECT qdrant_internal.p0_ping()')).get('active') or {}).get('operation')=='source_search':
            break
        time.sleep(.01)
    else:
        raise AssertionError('native source search was not admitted')
    assert sql("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING(pid) WHERE a.application_name='pgq_search_binding' AND l.relation='query_binding'::regclass AND l.mode='AccessShareLock' AND l.granted")=='1'
    ddl=spawn('ALTER TABLE query_binding ENABLE ROW LEVEL SECURITY','pgq_source_rls_change')
    wait_session('pgq_source_rls_change',"wait_event_type='Lock'")
    assert query.poll() is None and ddl.poll() is None
finally:
    os.kill(pid,signal.SIGCONT)
assert finish(query)=='1'
finish(ddl)
assert '0A000' in sql("SELECT * FROM qdrant.search('query_binding','querybinding')",ok=False)
sql("SELECT qdrant.drop_index('query_binding')")
checks += ['source binding lock spans native search and concurrent RLS change']

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
assert native_hits('recovery')==[], 'TRUNCATE must delete native points, not just hide missing source rows'
sql("INSERT INTO docs VALUES(1,'final recovery',NULL)")
ready()
checks += ['TRUNCATE tombstones']

status=ready()
assert len(status['engine_instance'])==32
assert sql("SELECT qdrant_internal.p1_service_ready(repeat('0',32))")=='f'
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
assert status2['engine_instance']!=status['engine_instance']
assert sql("SELECT qdrant_internal.p1_service_ready('"+status['engine_instance']+"')")=='f'
assert hits('final')=='["1"]'
checks += ['helper SIGKILL new storage epoch reconstruction']

# A caller cancelling its wait does not relinquish a native write's owner.
pid=json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
os.kill(pid,signal.SIGSTOP)
try:
    ticket=ticket_from(sql("BEGIN; UPDATE docs SET body='cancelledwait recovery' WHERE id=1; SELECT qdrant.track_changes('docs'); COMMIT"))
    timed=json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',100)"))
    assert timed['timed_out'] and not timed['durable'] and timed['pending_events']>0,timed
    waiting=spawn("SELECT qdrant.await_changes('"+ticket+"',60000)",'pgq_cancelled_wait')
    wait_session('pgq_cancelled_wait',"wait_event='PgSleep'")
    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='pgq_cancelled_wait'")=='t'
    out,err=waiting.communicate(timeout=10)
    assert waiting.returncode!=0 and '57014' in err,(out,err)
    owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
    assert owner['engine_pid']==pid and owner['active']['operation']=='source_apply',owner
finally:
    os.kill(pid,signal.SIGCONT)
assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))['durable']
assert hits('cancelledwait')=='["1"]'
checks += ['timeout and cancelled wait retain the in-flight native write owner']

# A management rollback cannot strand an in-flight flush receipt or its ticket.
pid=json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
os.kill(pid,signal.SIGSTOP)
try:
    ticket=ticket_from(sql("BEGIN; UPDATE docs SET body='managementrollback recovery' WHERE id=1; SELECT qdrant.track_changes('docs'); COMMIT"))
    assert not json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',100)"))['durable']
    management=spawn("BEGIN; SELECT qdrant.drop_index('docs'); SELECT pg_sleep(2); ROLLBACK",'pgq_management_rollback')
    wait_session('pgq_management_rollback',"wait_event='PgSleep'")
finally:
    os.kill(pid,signal.SIGCONT)
finish(management)
assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))['durable']
assert hits('managementrollback')=='["1"]'
assert json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']==pid
checks += ['management rollback replays unacknowledged in-flight receipts']

# Replacement may initially skip a management-locked catalog row. It must
# rotate that row's owner fence once the lock is released, before serving it.
before=ready()
locked=spawn("BEGIN; SELECT index_name FROM qdrant_internal.index_catalog WHERE index_name='docs' FOR UPDATE; SELECT pg_sleep(2); ROLLBACK",'pgq_owner_rotation_lock')
wait_session('pgq_owner_rotation_lock',"wait_event='PgSleep'")
os.kill(json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid'],signal.SIGKILL)
finish(locked)
after=ready()
assert after['engine_instance']!=before['engine_instance'] and after['storage_epoch']!=before['storage_epoch']
assert hits('managementrollback')=='["1"]'
crash_matrix.append({'cut':'owner_replacement_with_locked_catalog','before':before,
    'after':after,'final_source_keys':json.loads(hits('managementrollback'))})
checks += ['replacement owner rotates catalog rows skipped during management']

import verify_models
verify_dense_replay=verify_models.run(sql,ready,ticket_from,checks)
import verify_sparse
verify_sparse_replay=verify_sparse.run(sql,ready,ticket_from,checks)
import verify_tokens
verify_tokens_replay=verify_tokens.run(sql,ready,ticket_from,checks)
import verify_lexical
verify_lexical_replay=verify_lexical.run(sql,ready,ticket_from,checks,faults,crash_matrix)
import verify_bounded_lexical
verify_bounded_lexical_replay=verify_bounded_lexical.run(sql,ready,ticket_from,checks,spawn)
import verify_paging
verify_paging_replay=verify_paging.run(sql,ready,ticket_from,checks,spawn,finish,wait_session)
import verify_discovery
verify_discovery_replay=verify_discovery.run(sql,ready,ticket_from,checks)
import verify_numeric
verify_numeric_replay=verify_numeric.run(sql,ready,ticket_from,checks)
import verify_recommendations
verify_recommendations_replay=verify_recommendations.run(sql,ready,ticket_from,checks,spawn)
import verify_context_discovery
verify_context_discovery_replay=verify_context_discovery.run(sql,ready,ticket_from,checks,spawn)
import verify_feedback
verify_feedback_replay=verify_feedback.run(sql,ready,ticket_from,checks,spawn)
import verify_mmr
verify_mmr_replay=verify_mmr.run(sql,ready,ticket_from,checks,spawn)
import verify_formula
verify_formula_replay=verify_formula.run(sql,ready,ticket_from,checks,spawn)
import verify_retrieve
verify_retrieve_replay=verify_retrieve.run(sql,ready,ticket_from,checks,spawn)
import verify_payload
verify_payload_replay=verify_payload.run(sql,ready,ticket_from,checks)
import verify_filters
verify_filters_replay=verify_filters.run(sql,ready,ticket_from,checks,spawn)
import verify_fusion
verify_fusion_replay=verify_fusion.run(sql,ready,checks)
import verify_statistics
verify_statistics_replay=verify_statistics.run(sql,ready,ticket_from,checks,spawn)
import verify_matrix
verify_matrix_replay=verify_matrix.run(sql,ready,ticket_from,checks,spawn)
import verify_groups
verify_groups_replay=verify_groups.run(sql,ready,ticket_from,checks,spawn)
def verify_model_replay():
    verify_dense_replay()
    verify_sparse_replay()
    verify_tokens_replay()
    verify_lexical_replay()
    verify_bounded_lexical_replay()
    verify_paging_replay()
    verify_discovery_replay()
    verify_numeric_replay()
    verify_recommendations_replay()
    verify_context_discovery_replay()
    verify_feedback_replay()
    verify_mmr_replay()
    verify_formula_replay()
    verify_retrieve_replay()
    verify_payload_replay()
    verify_filters_replay()
    verify_fusion_replay()
    verify_statistics_replay()
    verify_matrix_replay()
    verify_groups_replay()
# PostgreSQL cancellation must not release an executing fused native query's owner.
ready('model_docs')
hybrid_vector=json.dumps({'dense':{'model_id':'fixture-model','model_version':'r1','vector':[1,0]}})
pid=json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
os.kill(pid,signal.SIGSTOP)
try:
    waiting=spawn("SELECT * FROM qdrant.search('model_docs','whale','hybrid',10,'"+hybrid_vector+"')",'pgq_hybrid_cancel')
    end=time.monotonic()+10
    while time.monotonic()<end:
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
        if (owner.get('active') or {}).get('operation')=='source_search': break
        time.sleep(.02)
    assert (owner.get('active') or {}).get('operation')=='source_search',owner
    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='pgq_hybrid_cancel'")=='t'
    out,err=waiting.communicate(timeout=10)
    assert waiting.returncode!=0 and '57014' in err,(out,err)
    retained=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
    assert retained['engine_pid']==pid and retained['active']['operation']=='source_search',retained
finally:
    os.kill(pid,signal.SIGCONT)
verify_model_replay()
assert json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']==pid
checks += ['cancelled native hybrid query retains sole owner until actual completion']
if faults:
    data=pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA'])
    # SourceOwner fault marker is sibling of its database-scoped storage root.
    owner=next(data.glob('pg_qdrant*/db-*.engine-owner'),None)
    if owner is None:
        owner=next(data.rglob('db-*.engine-owner'))
    marker=owner.with_suffix('.fault')
    for cut in ['before_apply','after_point_delete','before_flush','after_flush']:
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
        verify_model_replay()
        crash_matrix.append({'cut':cut,'before':before,'ticket_pending':pending,
            'after':after,'ticket_durable':result,'final_source_keys':json.loads(hits(cut))})
        checks.append('native crash '+cut+' / exact-set replay')

for fixture in ['p3_reservations.sql','p4_advanced.sql']:
    fixture_result=subprocess.run(PSQL+['-f','/src/crates/pg_qdrant/tests/'+fixture],text=True,capture_output=True,timeout=20)
    assert fixture_result.returncode==0,(fixture,fixture_result.stderr)
checks += ['transactional P3 task admission/cancellation and retained P4 shape validators']

import verify_generations
verify_generations.run(sql,ready,spawn,finish,wait_session,ticket_from,native_hits,verify_model_replay,checks,faults,crash_matrix)

# Restart the actual fsync-enabled PostgreSQL cluster with committed ACKs and
# a committed pending event. Historical ACKs cannot authorize a new helper.
before=ready()
ticket=ticket_from(sql("BEGIN; UPDATE docs SET body='postgresrestart durable recovery' WHERE id=1; SELECT qdrant.track_changes('docs'); COMMIT"))
data=pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA'])
bindir=subprocess.check_output(['pg_config','--bindir'],text=True).strip()
subprocess.run([bindir+'/pg_ctl','-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True,text=True)
restart_log=pathlib.Path(os.environ.get('PG_QDRANT_ARTIFACT_DIR','/src/artifacts'))/'p1-product-restart.log'
start=subprocess.run([bindir+'/pg_ctl','-D',str(data),'-l',str(restart_log),'-o',
    "-c listen_addresses='' -c unix_socket_directories='"+os.environ['PGHOST']+"' -c port="+os.environ['PGPORT']+" -c fsync=on -c synchronous_commit=on",'-w','start'],capture_output=True,text=True)
assert start.returncode==0,(start.stdout,start.stderr,restart_log.read_text())
result=json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))
after=ready()
assert result['durable'] and after['storage_epoch']!=before['storage_epoch'],(result,after)
assert hits('postgresrestart')=='["1"]'
verify_model_replay()
crash_matrix.append({'cut':'postgres_immediate_stop','before':before,'after':after,
    'ticket_durable':result,'final_source_keys':json.loads(hits('postgresrestart'))})
checks += ['PostgreSQL immediate-stop WAL recovery and new-owner exact replay']

import verify_abandoned
verify_abandoned.run(sql,ready,spawn,wait_session,checks,faults,crash_matrix)

import verify_retirements
verify_retirements.run(sql,ready,checks,faults,crash_matrix)

# Uninstall while the supervisor is alive. It must avoid calling removed SQL
# functions; installation rollback and reinstallation preserve source facts.
build_info=json.loads(sql('SELECT qdrant.build_info()'))
worker=json.loads(sql('SELECT qdrant_internal.p0_ping()'))['worker_pid']
observer=spawn('SELECT pg_sleep(2)','pgq_extension_drop_observer')
sql("SELECT qdrant.drop_index('bounded_lexical'); SELECT qdrant.drop_index('payload_docs'); SELECT qdrant.drop_index('retrieve_docs'); SELECT qdrant.drop_index('recommendation_docs'); SELECT qdrant.drop_index('numeric_dense'); SELECT qdrant.drop_index('numeric_sparse'); SELECT qdrant.drop_index('discovery_docs'); SELECT qdrant.drop_index('page_docs'); SELECT qdrant.drop_index('lexical_docs'); SELECT qdrant.drop_index('token_docs'); SELECT qdrant.drop_index('sparse_docs'); SELECT qdrant.drop_index('model_docs'); SELECT qdrant.drop_index('docs'); DROP EXTENSION pg_qdrant")
assert sql("SELECT count(*) FROM pg_trigger WHERE tgrelid='model_docs'::regclass AND NOT tgisinternal")=='0'
finish(observer)
assert sql('SELECT count(*) FROM docs')=='1'
assert sql("SELECT count(*) FROM pg_stat_activity WHERE pid="+str(worker))=='1','consumer must stay alive without its extension catalog'
sql('BEGIN; CREATE EXTENSION pg_qdrant; ROLLBACK')
assert sql("SELECT count(*) FROM pg_extension WHERE extname='pg_qdrant'")=='0'
sql('CREATE EXTENSION pg_qdrant')
sql("SELECT qdrant.create_index('docs','docs','id','{\"text\":{\"fields\":[\"body\"]}}')")
ready()
assert hits('postgresrestart')=='["1"]'
checks += ['live-consumer DROP, installation rollback and reinstallation']

import verify_logical_restore
verify_logical_restore.run(sql,ready,ticket_from,checks,spawn,finish,wait_session)

print(json.dumps({'status':'passed','checks':checks,'fault_build':faults,
    'source_revision':os.environ.get('PG_QDRANT_SOURCE_SHA','unrecorded'),
    'cargo_lock_sha256':hashlib.sha256(pathlib.Path('/src/Cargo.lock').read_bytes()).hexdigest(),
    'build_info':build_info, 'crash_matrix':crash_matrix,
    'postgres_version':sql('SHOW server_version'), 'release_supported':False},indent=2))

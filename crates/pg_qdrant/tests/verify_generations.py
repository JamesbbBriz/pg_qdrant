"""Committed shadow builds, source-write fencing and actual serving switches."""
import json
import os
import pathlib
import time


def run(sql, ready, spawn, finish, wait_session, ticket_from, native_hits, model_replay, checks, faults, crash_matrix):
    sql('CREATE TABLE generation_docs(id bigint PRIMARY KEY,body text NOT NULL)')
    sql("INSERT INTO generation_docs SELECT n,'generation fixture '||n FROM generate_series(1,256)n")
    sql("SELECT qdrant.create_index('generation_docs','generation_docs','id','{\"text\":{\"fields\":[\"body\"]}}')")
    before=ready('generation_docs')
    old_generation=before['generation']
    storage_root=next(pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA']).rglob('db-*.engine-owner')).with_suffix('.indexes')
    index_id=sql("SELECT index_id FROM qdrant_internal.index_catalog WHERE index_name='generation_docs'")
    old_path=storage_root/(index_id+'-'+old_generation+'-'+before['storage_epoch'])
    assert old_path.is_dir(),old_path
    obsolete=sql("SELECT point_id FROM qdrant_internal.source_state WHERE index_name='generation_docs' AND tagged_key->>'value'='2'")
    slow=spawn("BEGIN; INSERT INTO generation_docs VALUES(1001,'latecommit shadow'); SELECT qdrant.track_changes('generation_docs'); SELECT pg_sleep(5); COMMIT",'pgq_generation_latecommit')
    wait_session('pgq_generation_latecommit',"wait_event='PgSleep'")
    task=json.loads(sql("SELECT qdrant.rebuild_index('generation_docs')"))['task_id']
    assert '55006' in sql("SELECT qdrant.rebuild_index('generation_docs')",ok=False)
    fast=ticket_from(sql("BEGIN; UPDATE generation_docs SET body='new shadow' WHERE id=1; DELETE FROM generation_docs WHERE id=2; INSERT INTO generation_docs VALUES(2,'reused shadow'); SELECT qdrant.track_changes('generation_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+fast+"',4000)"))['durable']
    assert slow.poll() is None,'source consumer must progress while shadow cannot switch'
    assert sql("SELECT generation FROM qdrant_internal.index_catalog WHERE index_name='generation_docs'")==old_generation
    assert sql("SELECT source_key FROM qdrant.search('generation_docs','reused')")=='2'
    waiting=json.loads(sql("SELECT qdrant.await_task('"+task+"',0)"))
    assert waiting['timed_out'] and not waiting['succeeded'],waiting
    slow_ticket=ticket_from(finish(slow))
    result=json.loads(sql("SELECT qdrant.await_task('"+task+"',60000)"))
    assert result['succeeded'] and result['state']=='succeeded' and result['generation']!=old_generation,result
    assert result['active_generation']==result['generation'] and result['pending_events']==0,result
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        cleanup=json.loads(sql("SELECT qdrant.task_status('"+task+"')"))
        if cleanup['retired_storage_cleanup']: break
        time.sleep(.01)
    assert cleanup['retired_storage_cleanup'] and not old_path.exists(),cleanup
    assert (storage_root/(index_id+'-'+result['generation']+'-'+result['storage_epoch'])).is_dir()
    ready('generation_docs')
    assert sql("SELECT source_key FROM qdrant.search('generation_docs','latecommit')")=='1001'
    assert sql("SELECT source_key FROM qdrant.search('generation_docs','reused')")=='2'
    assert not any(str(hit['id'])==obsolete for hit in native_hits('fixture','generation_docs'))
    assert json.loads(sql("SELECT qdrant.await_changes('"+slow_ticket+"',0)"))['error']=='generation_invalidated'
    new_ticket=sql("SELECT qdrant.track_changes('generation_docs')")
    assert json.loads(sql("SELECT qdrant.await_changes('"+new_ticket+"',10000)"))['durable']
    checks += ['actual shadow generation flush/exact receipts and atomic switch with long commit inversion',
               'old serving queries during build, DELETE/key reuse cleanup and ticket generation invalidation',
               'retired native owner drops references and removes only the superseded generation directory']

    # An uncommitted task must never start or be awaited successfully.
    assert '55000' in sql("BEGIN; SELECT qdrant.await_task((qdrant.rebuild_index('generation_docs')->>'task_id')::uuid,1); COMMIT",ok=False)
    count=sql("SELECT count(*) FROM qdrant_internal.generation_reservations j JOIN qdrant_internal.index_catalog i USING(index_id) WHERE i.index_name='generation_docs'")
    sql("BEGIN; SELECT qdrant.rebuild_index('generation_docs'); ROLLBACK")
    assert sql("SELECT count(*) FROM qdrant_internal.generation_reservations j JOIN qdrant_internal.index_catalog i USING(index_id) WHERE i.index_name='generation_docs'")==count
    checks += ['uncommitted task wait refusal and transactional build rollback']

    # Hold the serving binding to observe committed cancellation before cutover.
    held=spawn("BEGIN; SELECT index_name FROM qdrant_internal.index_catalog WHERE index_name='generation_docs' FOR KEY SHARE; SELECT pg_sleep(2); ROLLBACK",'pgq_generation_cancel_lock')
    wait_session('pgq_generation_cancel_lock',"wait_event='PgSleep'")
    cancelled=json.loads(sql("SELECT qdrant.rebuild_index('generation_docs')"))['task_id']
    assert sql("SELECT qdrant.cancel_task('"+cancelled+"')")=='t'
    assert sql("SELECT qdrant.cancel_task('"+cancelled+"')")=='f'
    assert json.loads(sql("SELECT qdrant.await_task('"+cancelled+"',1000)"))['state']=='cancelled'
    finish(held)
    assert sql("SELECT generation FROM qdrant_internal.index_catalog WHERE index_name='generation_docs'")==result['generation']
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant.task_status('"+task+"')",ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant.cancel_task('"+task+"')",ok=False)
    assert '25001' in sql("BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT qdrant.await_task('"+task+"',0)",ok=False)
    checks += ['task cancellation preserves serving generation and owner/snapshot admission']

    if faults:
        marker=next(pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA']).rglob('db-*.engine-owner')).with_suffix('.fault')
        for cut in ['before_flush','after_flush']:
            before_failure=ready('generation_docs')
            index_id=sql("SELECT index_id FROM qdrant_internal.index_catalog WHERE index_name='generation_docs'")
            marker.write_text('shadow:'+index_id+':'+cut)
            failed_task=json.loads(sql("SELECT qdrant.rebuild_index('generation_docs')"))['task_id']
            failed=json.loads(sql("SELECT qdrant.await_task('"+failed_task+"',60000)"))
            assert failed['state']=='failed' and not failed['succeeded'] and failed['pending_events']>0,failed
            assert not marker.exists(),'shadow did not reach the actual native fault'
            assert sql("SELECT count(*) FROM qdrant_internal.generation_receipts WHERE task_id='"+failed_task+"'")=='0'
            after_failure=ready('generation_docs')
            assert after_failure['generation']==before_failure['generation']
            assert after_failure['storage_epoch']!=before_failure['storage_epoch']
            assert sql("SELECT source_key FROM qdrant.search('generation_docs','reused')")=='2'
            crash_matrix.append({'cut':'shadow_'+cut,'before':before_failure,'failed_task':failed,
                                 'after':after_failure,'final_source_keys':['2']})
        retry=json.loads(sql("SELECT qdrant.rebuild_index('generation_docs')"))['task_id']
        assert json.loads(sql("SELECT qdrant.await_task('"+retry+"',60000)"))['succeeded']
        checks += ['failed native shadow flush has no receipts, preserves old logical generation, and fresh retry succeeds']

    # Model contracts/points survive a real native generation replacement.
    model_replay()
    model_task=json.loads(sql("SELECT qdrant.rebuild_index('model_docs')"))['task_id']
    assert json.loads(sql("SELECT qdrant.await_task('"+model_task+"',60000)"))['succeeded']
    model_replay()
    checks += ['dense and RRF/DBSF remain native-queryable after serving generation replacement']
    sql("SELECT qdrant.drop_index('generation_docs')")
    sql('DROP TABLE generation_docs')

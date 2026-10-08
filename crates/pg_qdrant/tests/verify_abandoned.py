"""Native cancellation cleanup keeps the serving generation and original outcome."""
import json
import os
import pathlib
import signal
import time


def run(sql, ready, spawn, wait_session, checks, faults, crash_matrix):
    sql('CREATE TABLE abandoned_docs(id bigint PRIMARY KEY,body text NOT NULL)')
    sql("INSERT INTO abandoned_docs SELECT n,'abandoned cleanup fixture '||n FROM generate_series(1,128)n")
    sql("SELECT qdrant.create_index('abandoned_docs','abandoned_docs','id','{\"text\":{\"fields\":[\"body\"]}}')")
    serving = ready('abandoned_docs')
    index_id = sql("SELECT index_id FROM qdrant_internal.index_catalog WHERE index_name='abandoned_docs'")
    root = next(pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA']).rglob('db-*.engine-owner')).with_suffix('.indexes')
    serving_path = root/(index_id+'-'+serving['generation']+'-'+serving['storage_epoch'])
    assert '55000' in sql("BEGIN; SELECT qdrant.await_task((qdrant.rebuild_index('abandoned_docs')->>'task_id')::uuid,0,true)", ok=False)
    assert '22023' in sql("SELECT qdrant.await_task(gen_random_uuid(),0,NULL)", ok=False)
    held = spawn("BEGIN; SELECT index_name FROM qdrant_internal.index_catalog WHERE index_name='abandoned_docs' FOR KEY SHARE; SELECT pg_sleep(90); ROLLBACK", 'pgq_abandoned_cutover_lock')
    wait_session('pgq_abandoned_cutover_lock', "wait_event='PgSleep'")
    try:
        for cycle in range(40):
            task = json.loads(sql("SELECT qdrant.rebuild_index('abandoned_docs')"))['task_id']
            deadline = time.monotonic()+10
            while True:
                status = json.loads(sql("SELECT qdrant.task_status('"+task+"')"))
                path = root/(index_id+'-'+status['generation']+'-'+status['storage_epoch'])
                if path.is_dir() and int(sql("SELECT count(*) FROM qdrant_internal.generation_receipts WHERE task_id='"+task+"'")) > 0:
                    break
                assert time.monotonic() < deadline, status
                time.sleep(.01)
            if cycle == 0:
                sql("BEGIN; SELECT qdrant.cancel_task('"+task+"'); ROLLBACK")
                assert json.loads(sql("SELECT qdrant.task_status('"+task+"')"))['state'] != 'cancelled'
                assert path.is_dir() and serving_path.is_dir()
                pid = json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
                os.kill(pid, signal.SIGSTOP)
                try:
                    assert sql("SELECT qdrant.cancel_task('"+task+"')") == 't'
                    pending = json.loads(sql("SELECT qdrant.await_task('"+task+"',0,true)"))
                    assert pending['state'] == 'cancelled' and pending['timed_out'] and pending['cleanup_timed_out'], pending
                    assert not pending['succeeded'] and not pending['abandoned_storage_cleanup'] and path.is_dir()
                    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant.await_task('"+task+"',0,true)", ok=False)
                    assert '25001' in sql("BEGIN ISOLATION LEVEL REPEATABLE READ; SELECT qdrant.await_task('"+task+"',0,true)", ok=False)
                    sql("SELECT qdrant_internal.ack_retirement_batch(jsonb_build_object('index_id',"+index_id+
                        ",'task_id','"+task+"','generation','"+status['generation']+"','storage_epoch','"+status['storage_epoch']+"','consumer_id',gen_random_uuid()))")
                    assert not json.loads(sql("SELECT qdrant.task_status('"+task+"')"))['abandoned_storage_cleanup']
                finally:
                    os.kill(pid, signal.SIGCONT)
            else:
                assert sql("SELECT qdrant.cancel_task('"+task+"')") == 't'
            result = json.loads(sql("SELECT qdrant.await_task('"+task+"',60000,true)"))
            assert result['state'] == 'cancelled' and not result['succeeded'] and result['abandoned_storage_cleanup'], result
            assert result['abandoned_pending_epochs'] == 0 and not path.exists() and serving_path.is_dir(), (cycle, result)
            assert sql("SELECT qdrant.cancel_task('"+task+"')") == 'f'
            assert sql("SELECT generation FROM qdrant_internal.index_catalog WHERE index_name='abandoned_docs'") == serving['generation']
            assert sql("SELECT source_key FROM qdrant.search('abandoned_docs','fixture', 'text',1)")
    finally:
        sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='pgq_abandoned_cutover_lock'")
        out, err = held.communicate(timeout=10)
        assert held.returncode != 0 and '57014' in err, (out, err)
    checks += ['cancel rollback, owner/snapshot/exact receipt admission and cleanup timeout preserve native ownership',
               '40 actual flushed shadow cancellations release native references without changing the serving generation']

    if faults:
        before = ready('abandoned_docs')
        pid = json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
        marker = next(pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA']).rglob('db-*.engine-owner')).with_suffix('.fault')
        marker.write_text('shadow:'+index_id+':shadow_apply_error')
        failed_task = json.loads(sql("SELECT qdrant.rebuild_index('abandoned_docs')"))['task_id']
        failed = json.loads(sql("SELECT qdrant.await_task('"+failed_task+"',60000,true)"))
        assert failed['state'] == 'failed' and not failed['succeeded'] and 'injected shadow error' in failed['error'], failed
        assert failed['pending_events'] > 0 and failed['abandoned_storage_cleanup'] and failed['abandoned_pending_epochs'] == 0, failed
        assert sql("SELECT count(*) FROM qdrant_internal.generation_receipts WHERE task_id='"+failed_task+"'") == '0'
        assert not marker.exists() and not (root/(index_id+'-'+failed['generation']+'-'+failed['storage_epoch'])).exists()
        assert json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid'] == pid
        after = ready('abandoned_docs')
        assert after['generation'] == before['generation'] and serving_path.is_dir()
        assert sql("SELECT source_key FROM qdrant.search('abandoned_docs','fixture','text',1)")
        crash_matrix.append({'cut': 'shadow_nonfatal_error_after_native_apply', 'injected_fault': True,
                             'before': before, 'failed_task': failed, 'after': after,
                             'same_helper_pid': True, 'native_directory_cleaned': True})
        checks.append('actual partial native apply followed by injected error has no event receipts; owned failed shadow retires without masking its failure')
        retry = json.loads(sql("SELECT qdrant.rebuild_index('abandoned_docs')"))['task_id']
        retried = json.loads(sql("SELECT qdrant.await_task('"+retry+"',60000,true)"))
        assert retried['succeeded'] and retried['retired_storage_cleanup'] and not serving_path.exists(), retried
        assert json.loads(sql("SELECT qdrant.task_status('"+failed_task+"')"))['state'] == 'failed'
        assert sql("SELECT source_key FROM qdrant.search('abandoned_docs','fixture','text',1)")
        checks.append('fresh build retry after failed-shadow cleanup succeeds, retires superseded serving storage and retains historical failure')

    # A build cancelled in its creation transaction never owned native storage.
    output = sql("BEGIN; SELECT qdrant.rebuild_index('abandoned_docs'); SELECT qdrant.cancel_task((SELECT task_id FROM qdrant_internal.generation_reservations WHERE index_id="+index_id+" ORDER BY generation_no DESC LIMIT 1)); COMMIT")
    task = json.loads(output.splitlines()[0])['task_id']
    result = json.loads(sql("SELECT qdrant.await_task('"+task+"',60000,true)"))
    assert result['state'] == 'cancelled' and result['abandoned_storage_cleanup'], result
    assert sql("SELECT engine_instance IS NULL FROM qdrant_internal.generation_reservations WHERE task_id='"+task+"'") == 't'
    drop_task = json.loads(sql("SELECT qdrant.drop_index('abandoned_docs')"))['task_id']
    assert json.loads(sql("SELECT qdrant.await_task('"+drop_task+"',60000,true)"))['physical_cleanup_completed']
    archived = json.loads(sql("SELECT qdrant.task_status('"+task+"')"))
    assert archived['state'] == 'cancelled' and archived['abandoned_storage_cleanup'] and not archived['succeeded']
    assert '55000' in sql("SELECT qdrant.await_task('"+task+"',0,true)", ok=False)
    sql('DROP TABLE abandoned_docs')
    checks.append('unstarted cancellation needs no native shard; index DROP skips verified clean shadows and archives their cancellation outcome')

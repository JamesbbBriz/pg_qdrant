"""Committed native retirement, name reuse and fail-closed ownership changes."""
import json
import os
import pathlib
import signal
import time


def run(sql, ready, checks, faults, crash_matrix):
    settings = '{"text":{"fields":["body"]}}'
    sql('CREATE TABLE retirement_docs(id bigint PRIMARY KEY,body text NOT NULL)')
    sql("INSERT INTO retirement_docs VALUES(1,'retirement original')")
    root = next(pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA']).rglob('db-*.engine-owner')).with_suffix('.indexes')
    sentinel = root/'unrelated-retirement-sentinel'
    sentinel.mkdir()
    (sentinel/'preserve').write_text('unrelated storage')

    def create():
        sql("SELECT qdrant.create_index('retirement_docs','retirement_docs','id','"+settings+"')")
        status = ready('retirement_docs')
        index_id = sql("SELECT index_id FROM qdrant_internal.index_catalog WHERE index_name='retirement_docs'")
        path = root/(index_id+'-'+status['generation']+'-'+status['storage_epoch'])
        assert path.is_dir(), path
        return index_id, path

    def drop():
        result = json.loads(sql("SELECT qdrant.drop_index('retirement_docs')"))
        assert result['kind'] == 'drop' and result['logical_index_removed'], result
        return result['task_id']

    def wait(task, success=True):
        result = json.loads(sql("SELECT qdrant.await_task('"+task+"',60000)"))
        assert result['completed'] and result['succeeded'] is success, result
        assert result['physical_cleanup_completed'] is success, result
        return result

    index_id, old_path = create()
    rolled = json.loads(sql("BEGIN; SELECT qdrant.drop_index('retirement_docs'); ROLLBACK"))['task_id']
    assert sql("SELECT count(*) FROM qdrant_internal.drop_tasks WHERE task_id='"+rolled+"'") == '0'
    assert old_path.is_dir() and sql("SELECT source_key FROM qdrant.search('retirement_docs','original')") == '1'
    assert '55000' in sql("BEGIN; SELECT qdrant.await_task((qdrant.drop_index('retirement_docs')->>'task_id')::uuid,0)", ok=False)
    assert sql("SELECT index_id FROM qdrant_internal.index_catalog WHERE index_name='retirement_docs'") == index_id
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant.drop_index('retirement_docs')", ok=False)
    checks.append('DROP rollback/uncommitted wait keeps the live native generation and owner admission')

    pid = json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
    os.kill(pid, signal.SIGSTOP)
    try:
        task = drop()
        pending = json.loads(sql("SELECT qdrant.await_task('"+task+"',0)"))
        assert pending.get('timed_out') and not pending['completed'] and old_path.is_dir(), pending
        assert '0A000' in sql("SELECT qdrant.cancel_task('"+task+"')", ok=False)
        assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant.task_status('"+task+"')", ok=False)
        assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant.cancel_task('"+task+"')", ok=False)
        # A forged epoch/consumer receipt must not authorize physical success.
        sql("SELECT qdrant_internal.ack_retirement_batch(jsonb_build_object('index_id',"+index_id+
            ",'task_id','"+task+"','generation',gen_random_uuid(),'storage_epoch',gen_random_uuid(),'consumer_id',gen_random_uuid()))")
        assert not json.loads(sql("SELECT qdrant.task_status('"+task+"')"))['succeeded']
        sql("SELECT qdrant.create_index('retirement_docs','retirement_docs','id','"+settings+"')")
        replacement_id = sql("SELECT index_id FROM qdrant_internal.index_catalog WHERE index_name='retirement_docs'")
        assert replacement_id != index_id
        sql("UPDATE retirement_docs SET body='retirement replacement' WHERE id=1")
    finally:
        os.kill(pid, signal.SIGCONT)
    wait(task)
    status = ready('retirement_docs')
    replacement_path = root/(replacement_id+'-'+status['generation']+'-'+status['storage_epoch'])
    assert not old_path.exists() and replacement_path.is_dir()
    assert sql("SELECT source_key FROM qdrant.search('retirement_docs','replacement')") == '1'
    assert sql("SELECT qdrant.cancel_task('"+task+"')") == 'f'
    checks.append('committed DROP timeout/exact receipt and same-name new identity preserves the replacement native shard')

    historical = json.loads(sql("SELECT qdrant.rebuild_index('retirement_docs')"))['task_id']
    assert json.loads(sql("SELECT qdrant.await_task('"+historical+"',60000)"))['succeeded']
    task = drop()
    wait(task)
    archived = json.loads(sql("SELECT qdrant.task_status('"+historical+"')"))
    assert archived['state'] == 'succeeded' and archived['succeeded'] and archived['logical_index_removed'], archived
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant.task_status('"+historical+"')", ok=False)
    checks.append('historical rebuild outcome survives catalog removal with original owner permission')
    if faults:
        # Earlier actual shadow crash cuts were followed by index removal.
        # Their pending receipts and failed outcome must survive that removal.
        archived_failures = json.loads(sql("SELECT jsonb_agg(qdrant.task_status(task_id)) FROM qdrant_internal.task_archive "
            "WHERE status->>'index_name'='generation_docs' AND status->>'state'='failed'"))
        assert len(archived_failures) == 2 and all(not a['succeeded'] and a['pending_events'] > 0 for a in archived_failures), archived_failures
        checks.append('DROP archives real failed shadow outcomes and pending receipts without rewriting failure as cancellation')

    for cycle in range(40):
        _, path = create()
        task = drop()
        wait(task)
        assert not path.exists(), (cycle, path)
        assert sql("SELECT count(*) FROM pg_trigger WHERE tgrelid='retirement_docs'::regclass AND NOT tgisinternal") == '0'
        assert (sentinel/'preserve').read_text() == 'unrelated storage'
    checks.append('40 installed create/flush/drop cycles release native references beyond the 32-open-shard ceiling')

    # Actual helper replacement invalidates ownership; it must preserve the
    # old canonical directory and never claim cleanup based on an old nonce.
    _, uncertain_path = create()
    before = ready('retirement_docs')
    pid = json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
    os.kill(pid, signal.SIGSTOP)
    task = drop()
    os.kill(pid, signal.SIGKILL)
    failure = wait(task, success=False)
    # If dispatch races SIGKILL, the exact native request can observe EPIPE
    # before the next dispatch observes a different ownership nonce.
    assert any(reason in failure['error'] for reason in ['owner_changed', 'transport: Broken pipe']) and failure['pending_epochs'] > 0, failure
    assert uncertain_path.is_dir() and (sentinel/'preserve').is_file()
    # A fresh index remains usable despite the explicit failed cleanup task.
    _, path = create()
    after = ready('retirement_docs')
    assert json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid'] != pid
    assert sql("SELECT source_key FROM qdrant.search('retirement_docs','replacement')") == '1'
    task = drop()
    wait(task)
    assert not path.exists() and uncertain_path.is_dir()
    crash_matrix.append({'cut': 'drop_owner_sigkill', 'before': before, 'failed_drop': failure,
                         'after': after, 'uncertain_directory_preserved': uncertain_path.is_dir(),
                         'fresh_directory_cleaned': not path.exists(), 'final_source_keys': ['1']})
    checks.append('SIGKILL owner replacement fails cleanup closed, preserves uncertain storage and permits fresh trusted-source registration')

    _, prior_path = create()
    before = ready('retirement_docs')
    pid = json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic()+60
    while True:
        after = ready('retirement_docs')
        if before['storage_epoch'] != after['storage_epoch'] or time.monotonic() >= deadline:
            break
        time.sleep(.02)
    assert before['storage_epoch'] != after['storage_epoch'] and prior_path.is_dir(), (before, after)
    index_id = sql("SELECT index_id FROM qdrant_internal.index_catalog WHERE index_name='retirement_docs'")
    current_path = root/(index_id+'-'+after['generation']+'-'+after['storage_epoch'])
    assert current_path.is_dir()
    assert sql("SELECT source_key FROM qdrant.search('retirement_docs','replacement')") == '1'
    failure = wait(drop(), success=False)
    assert 'owner_changed' in failure['error'] and failure['pending_epochs'] == 1, failure
    assert prior_path.is_dir() and not current_path.exists(), (prior_path, current_path, failure)
    sql('DROP TABLE retirement_docs')
    crash_matrix.append({'cut': 'drop_prior_owner_history', 'before': before, 'after': after,
                         'failed_drop': failure, 'prior_directory_preserved': prior_path.is_dir(),
                         'current_directory_cleaned': not current_path.exists(), 'final_source_keys': ['1']})
    checks.append('epoch rotation retains prior ownership: DROP cleans current epoch but reports preserved old epoch as incomplete')

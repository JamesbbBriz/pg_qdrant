"""Installed native word completion with complete source/version admission."""
import json
import os
import signal
import time


def run(sql, ready, ticket_from, checks, spawn=None):
    def literal(value):
        return "'" + value.replace("'", "''") + "'"
    sql('CREATE TABLE suggestion_docs(id bigint PRIMARY KEY,body text NOT NULL,category text)')
    sql("INSERT INTO suggestion_docs VALUES(1,'transaction transaction transfer','Allow'),"
        "(2,'transfer','Allow'),(3,'transit','Allow'),(4,'transsecret','Denied'),"
        "(5,'数据库 数据集','Allow'),(6,'transfer123','Allow')")
    config = '{"text":{"fields":["body"]},"payload":{"category":{"kind":"keyword","field":"category"}}}'
    register = "SELECT qdrant.create_index('suggestion_docs','suggestion_docs','id'," + literal(config) + ")"
    sql(register)
    allow = {'filter': {'field': 'category', 'eq': 'Allow'}}
    def statement(prefix='TRAN', limit=10, options=None):
        return 'SELECT qdrant.suggest(\'suggestion_docs\',' + literal(prefix) + ',' + str(limit) + ',' + literal(json.dumps(allow if options is None else options)) + ')'
    def query(**kw):
        return json.loads(sql(statement(**kw)))
    def replay():
        ready('suggestion_docs')
        result = query()
        assert result['items'] == [
            {'term': 'transfer', 'point_count': 2},
            {'term': 'transaction', 'point_count': 1},
            {'term': 'transit', 'point_count': 1}], result
        assert result['total_terms'] == 3 and result['terms_complete'] and result['counts_exact'], result
        assert result['live_points'] == 6 and result['filtered_points'] == 5, result
        assert result['prefix'] == 'tran' and not result['release_supported'], result
        assert 'hits' not in result and 'proof' not in result, result
        small = query(limit=1)
        assert small['total_terms'] == 3 and not small['terms_complete'] and small['items'] == result['items'][:1], small
        assert query(options={})['total_terms'] == 4
        assert query(prefix='数据')['items'] == [
            {'term': '数据库', 'point_count': 1}, {'term': '数据集', 'point_count': 1}]
        empty = query(prefix='zz')
        assert empty['items'] == [] and empty['total_terms'] == 0 and empty['terms_complete'], empty
        assert query(options={'filter': {'field': 'category', 'eq': 'Missing'}})['total_terms'] == 0
    replay()
    checks.append('native term dictionary completion ranks the complete filtered prefix domain by exact source-point frequency with deterministic ties, Unicode literals, top-k coverage and zero-result scope')
    owner = json.loads(sql('SELECT qdrant_internal.p0_ping()'))
    for prefix in ['', 'a', 'a b', 'trans*', 'SKU-123', 'cafe\u0301', 'a' * 33]:
        assert '22023' in sql(statement(prefix=prefix), ok=False), prefix
    for limit in [0, 21, -1]:
        assert '22023' in sql(statement(limit=limit), ok=False), limit
    for invalid in [
        "SELECT qdrant.suggest('suggestion_docs',NULL)",
        "SELECT qdrant.suggest('suggestion_docs','tran',NULL)",
        "SELECT qdrant.suggest('suggestion_docs','tran',10,NULL)",
        statement(options={'unknown': True}),
    ]:
        assert '22023' in sql(invalid, ok=False), invalid
    assert json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_instance'] == owner['engine_instance']
    terms = ['ab' + chr(97 + n // 26) + chr(97 + n % 26) for n in range(129)]
    sql('UPDATE suggestion_docs SET body=' + literal(' '.join(terms[:128])) + ' WHERE id=1')
    ready('suggestion_docs')
    exact = query(prefix='ab', limit=1)
    assert exact['total_terms'] == 128 and len(exact['items']) == 1 and not exact['terms_complete'], exact
    sql('UPDATE suggestion_docs SET body=' + literal(' '.join(terms)) + ' WHERE id=1')
    ready('suggestion_docs')
    assert '54000' in sql(statement(prefix='ab', limit=1), ok=False)
    sql("UPDATE suggestion_docs SET body='transaction transaction transfer' WHERE id=1")
    replay()
    checks.append('suggestion inputs protect identifier/operator boundaries; exact 128-term enumeration succeeds and overflow refuses before top-k without truncating candidates')
    assert '42501' in sql('SET ROLE pgq_writer; ' + statement(), ok=False)
    sql('CREATE ROLE pgq_suggestion_reader; GRANT SELECT ON suggestion_docs TO pgq_suggestion_reader')
    assert '42501' in sql('SET ROLE pgq_suggestion_reader; ' + statement(), ok=False)
    sql('GRANT builder TO pgq_suggestion_reader')
    assert json.loads(sql('SET ROLE pgq_suggestion_reader; ' + statement()))['total_terms'] == 3
    sql('REVOKE builder FROM pgq_suggestion_reader')
    assert '0A000' in sql('BEGIN ISOLATION LEVEL REPEATABLE READ; ' + statement(), ok=False)
    assert '55000' in sql("BEGIN; UPDATE suggestion_docs SET body='uncommitted' WHERE id=1; " + statement(), ok=False)
    checks.append('suggestions reuse registered/inherited-owner and source SELECT admission, refuse table-only/writer roles and unsupported or uncommitted snapshots')
    ticket = ticket_from(sql("BEGIN; DELETE FROM suggestion_docs WHERE id=2; INSERT INTO suggestion_docs VALUES(2,'different','Allow'); SELECT qdrant.track_changes('suggestion_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('" + ticket + "',60000)"))['durable']
    changed = {i['term']: i['point_count'] for i in query()['items']}
    assert changed['transfer'] == 1 and changed['transaction'] == 1, changed
    sql("UPDATE suggestion_docs SET body='transfer' WHERE id=2")
    replay()
    checks.append('committed ordinary DML and primary-key reuse update suggestion frequencies only with exact durable source receipts')
    if spawn is not None:
        for change in ['source', 'cancel']:
            owner = json.loads(sql('SELECT qdrant_internal.p0_ping()')); pid = owner['engine_pid']
            os.kill(pid, signal.SIGSTOP)
            try:
                application = 'pgq_suggestions_' + change
                waiting = spawn(statement(), application)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    busy = json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    if (busy.get('active') or {}).get('operation') == 'source_statistics': break
                    time.sleep(.02)
                assert (busy.get('active') or {}).get('operation') == 'source_statistics', busy
                if change == 'source': sql("UPDATE suggestion_docs SET body='changed' WHERE id=1")
                else:
                    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='" + application + "'") == 't'
                    out, err = waiting.communicate(timeout=10)
                    assert waiting.returncode and '57014' in err, (out, err)
                    retained = json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    assert retained.get('active') and retained['engine_instance'] == owner['engine_instance'], retained
            finally: os.kill(pid, signal.SIGCONT)
            if change == 'source':
                out, err = waiting.communicate(timeout=10)
                assert waiting.returncode and '55000' in err, (out, err)
                sql("UPDATE suggestion_docs SET body='transaction transaction transfer' WHERE id=1")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                completed = json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if not completed.get('active'): break
                time.sleep(.02)
            assert not completed.get('active'), completed
            replay()
        checks.append('in-flight suggestion source mutation refuses the whole response; SQL cancellation retains ownership until native execution finishes')
    sql('ALTER TABLE suggestion_docs ENABLE ROW LEVEL SECURITY')
    assert '0A000' in sql(statement(), ok=False)
    sql('ALTER TABLE suggestion_docs DISABLE ROW LEVEL SECURITY')
    task = json.loads(sql("SELECT qdrant.drop_index('suggestion_docs')"))
    assert json.loads(sql("SELECT qdrant.await_task('" + task['task_id'] + "',60000)"))['succeeded']
    sql(register); replay()
    checks.append('RLS suggestions explicitly refuse; trusted DDL re-registration reconstructs the same term domain and counts')
    return replay

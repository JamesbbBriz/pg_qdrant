"""Installed immutable candidate pages, permissions, freshness and quotas."""
import json
import os
import signal
import time


def run(sql, ready, ticket_from, checks, spawn, finish, wait_session):
    sql('CREATE TABLE page_docs(id text PRIMARY KEY,body text NOT NULL)')
    sql("INSERT INTO page_docs SELECT n::text,'page anchor transaction recovery '||n FROM generate_series(1,23)n")
    sql("SELECT qdrant.create_index('page_docs','page_docs','id','{\"text\":{\"fields\":[\"body\"]}}')")
    ready('page_docs')

    def page(cursor=None, size=7, query='anchor', options=None, prefix='', ok=True):
        args = ["'page_docs'", "'"+query+"'", "'text'", str(size), "'{}'",
                "'"+json.dumps(options or {})+"'", 'NULL' if cursor is None else "'"+json.dumps(cursor)+"'"]
        result = sql(prefix+'SELECT qdrant.search_page('+','.join(args)+')', ok=ok)
        return json.loads(result) if ok else result

    first = page()
    assert len(first['hits']) == 7 and first['captured_count'] == 23 and first['native_query_executed']
    assert first['global_match_count'] is None and not first['global_coverage_verified'] and first['source_rechecked']
    cursor = first['next_cursor']
    assert page(cursor) == page(cursor), 'A repeated immutable cursor changed or advanced its page'
    all_hits = list(first['hits'])
    while cursor:
        current = page(cursor)
        assert not current['native_query_executed'] and current['snapshot'] == first['snapshot']
        all_hits += current['hits']
        cursor = current['next_cursor']
    assert len(all_hits) == 23 and len({hit['source_key'] for hit in all_hits}) == 23
    assert [h['rank'] for h in all_hits] == list(range(1,24))
    checks.append('immutable paging retries return identical pages and enumerate a captured bounded domain without duplicates')

    for bad in [None, 0, 101]:
        size = 'NULL' if bad is None else str(bad)
        assert '22023' in sql("SELECT qdrant.search_page('page_docs','anchor','text',"+size+")", ok=False)
    for bad in [{}, {'snapshot': first['snapshot']}, {'snapshot': first['snapshot'], 'offset': -1},
                {'snapshot': 'invalid', 'offset': 7}, {'snapshot': first['snapshot'], 'offset': 1.5},
                {'snapshot': first['snapshot'], 'offset': 24}, dict(first['next_cursor'], tenant='forged')]:
        assert '22023' in page(bad, ok=False), bad
    assert '22023' in page(first['next_cursor'], query='different', ok=False)
    assert '42501' in page(first['next_cursor'], prefix='SET ROLE pgq_writer; ', ok=False)
    assert '42501' in sql('SET ROLE pgq_writer; SELECT * FROM qdrant_internal.search_snapshots', ok=False)
    sql('CREATE ROLE pgq_page_reader; GRANT builder TO pgq_page_reader; GRANT SELECT ON page_docs TO pgq_page_reader')
    # The second role has valid source/index access but cannot reuse an
    # otherwise valid cursor created by another effective PostgreSQL identity.
    reader = page(prefix='SET ROLE pgq_page_reader; ')
    assert '42501' in page(first['next_cursor'], prefix='SET ROLE pgq_page_reader; ', ok=False)
    # The reader inherits table-owner privileges through its valid index-owner
    # domain. Revoke the inherited owner SELECT as well as the direct grant;
    # the session's superuser can continue managing the disposable fixture.
    sql('REVOKE SELECT ON page_docs FROM builder,pgq_page_reader')
    assert sql("SELECT has_table_privilege('pgq_page_reader','page_docs','SELECT')") == 'f'
    assert '42501' in page(reader['next_cursor'], prefix='SET ROLE pgq_page_reader; ', ok=False)
    sql('GRANT SELECT ON page_docs TO builder,pgq_page_reader')
    # Permission changes and metadata drift must also invalidate cached pages.
    sql('ALTER TABLE page_docs ENABLE ROW LEVEL SECURITY')
    assert '55000' in page(first['next_cursor'], ok=False)
    sql('ALTER TABLE page_docs DISABLE ROW LEVEL SECURITY')
    # Use a new index because conservative DDL drift remains deliberately closed.
    drop = json.loads(sql("SELECT qdrant.drop_index('page_docs')"))['task_id']
    assert sql("SELECT count(*) FROM qdrant_internal.search_snapshots WHERE index_name='page_docs'") == '0'
    assert json.loads(sql("SELECT qdrant.await_task('"+drop+"',60000,true)"))['physical_cleanup_completed']
    sql("SELECT qdrant.create_index('page_docs','page_docs','id','{\"text\":{\"fields\":[\"body\"]}}')")
    ready('page_docs')
    assert '55000' in page(first['next_cursor'], ok=False)
    checks.append('cursor shape/query/role/private-table/RLS and same-name index replacement fail closed')

    before = page()
    changed = ticket_from(sql("BEGIN; UPDATE page_docs SET body='page anchor changed' WHERE id='1'; SELECT qdrant.track_changes('page_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+changed+"',10000)"))['durable']
    assert '55000' in page(before['next_cursor'], ok=False)
    reused = page()
    ticket = ticket_from(sql("BEGIN; DELETE FROM page_docs WHERE id='1'; INSERT INTO page_docs VALUES('1','page anchor transaction recovery'); SELECT qdrant.track_changes('page_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    assert '55000' in page(reused['next_cursor'], ok=False)
    old = page()
    task = json.loads(sql("SELECT qdrant.rebuild_index('page_docs')"))['task_id']
    assert json.loads(sql("SELECT qdrant.await_task('"+task+"',60000)"))['succeeded']
    assert '55000' in page(old['next_cursor'], ok=False)
    expired = page()
    sql("UPDATE qdrant_internal.search_snapshots SET expires_at=clock_timestamp()-interval '1 second' WHERE snapshot_id='"+expired['snapshot']+"'")
    assert '55000' in page(expired['next_cursor'], ok=False)
    checks.append('captured source revision/incarnation, actual generation cutover and TTL expiry invalidate cursors')
    # A tiny candidate domain stays honest instead of claiming global totals.
    limited = page(size=7, options=dict(candidate_limit=3))
    assert limited['captured_count'] == 3 and limited['captured_limit'] == 3 and limited['next_cursor'] is None
    assert limited['global_match_count'] is None and not limited['global_coverage_verified']
    empty = page(query='unmatchedword')
    assert empty['hits'] == [] and empty['captured_count'] == 0 and empty['next_cursor'] is None
    checks.append('tiny and zero-result page domains report captured counts with unknown global count/coverage')
    count_before = sql('SELECT count(*) FROM qdrant_internal.search_snapshots')
    sql("BEGIN; SELECT qdrant.search_page('page_docs','anchor'); ROLLBACK")
    assert sql('SELECT count(*) FROM qdrant_internal.search_snapshots') == count_before
    locked = spawn('BEGIN; SELECT pg_advisory_xact_lock(1885824362,2); SELECT pg_sleep(2); ROLLBACK', 'pgq_page_admission')
    wait_session('pgq_page_admission', "wait_event='PgSleep'")
    assert '55P03' in page(ok=False)
    finish(locked)
    # Quota rejection happens before native execution; it cannot create a
    # 129th live snapshot. Rollback and expiry release admission capacity.
    sql('DELETE FROM qdrant_internal.search_snapshots')
    for _ in range(128):
        page()
    assert sql('SELECT count(*) FROM qdrant_internal.search_snapshots') == '128'
    assert '54000' in page(ok=False)
    assert sql('SELECT count(*) FROM qdrant_internal.search_snapshots') == '128'
    sql("UPDATE qdrant_internal.search_snapshots SET expires_at=clock_timestamp()-interval '1 second'")
    assert page()['captured_count'] == 23
    assert sql('SELECT count(*) FROM qdrant_internal.search_snapshots') == '1'
    checks.append('paging rollback/admission contention/live snapshot quota and expired capacity reclamation are enforced')
    # Reject an oversized real result snapshot without persisting part of it.
    changed = ticket_from(sql("BEGIN; INSERT INTO page_docs SELECT n::text,'page anchor transaction recovery '||n FROM generate_series(24,100)n; SELECT qdrant.track_changes('page_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+changed+"',10000)"))['durable']
    bytes_count_before = sql('SELECT count(*) FROM qdrant_internal.search_snapshots')
    large = dict(matching=dict(all='anchor '*290, any='anchor '*290, exclude='unmatchedword '*140))
    assert max(len(value.encode()) for value in large['matching'].values()) <= 2048
    oversized_error = page(options=large, ok=False)
    assert '54000' in oversized_error, oversized_error
    assert sql('SELECT count(*) FROM qdrant_internal.search_snapshots') == bytes_count_before
    changed = ticket_from(sql("BEGIN; DELETE FROM page_docs WHERE id::integer>=24; SELECT qdrant.track_changes('page_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+changed+"',10000)"))['durable']
    checks.append('actual native results exceeding the snapshot byte budget roll back without partial cache rows')
    cancel_count_before = sql('SELECT count(*) FROM qdrant_internal.search_snapshots')
    pid = json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
    os.kill(pid, signal.SIGSTOP)
    try:
        waiting = spawn("SELECT qdrant.search_page('page_docs','anchor')", 'pgq_page_cancel')
        end = time.monotonic()+10
        while time.monotonic()<end:
            if (json.loads(sql('SELECT qdrant_internal.p0_ping()')).get('active') or {}).get('operation') == 'source_search':
                break
            time.sleep(.02)
        else:
            raise AssertionError('Native page query was not admitted')
        assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='pgq_page_cancel'") == 't'
        out, err = waiting.communicate(timeout=10)
        assert waiting.returncode and '57014' in err, (out, err)
        assert json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid'] == pid
    finally:
        os.kill(pid, signal.SIGCONT)
    ready('page_docs')
    assert sql('SELECT count(*) FROM qdrant_internal.search_snapshots') == cancel_count_before
    assert page()['captured_count'] == 23
    checks.append('cancelled executing native page query retains ownership and rolls back its snapshot before reuse')

    # A successful first admission cannot override a later preflight returning
    # not-ready. Inject only that SQL result, retaining real native execution.
    cached = page()
    original_preflight = sql("SELECT pg_get_functiondef('qdrant.explain_search(text,text,text,integer,jsonb,jsonb)'::regprocedure)")
    saved_preflight = original_preflight.replace('FUNCTION qdrant.explain_search(', 'FUNCTION qdrant.page_preflight_saved(')
    saved_preflight = saved_preflight.replace('explain_search.', 'page_preflight_saved.')
    sql(saved_preflight)
    try:
        sql('CREATE TABLE qdrant_internal.page_preflight_fault(calls integer NOT NULL,cut integer NOT NULL); '
            'INSERT INTO qdrant_internal.page_preflight_fault VALUES(0,2)')
        sql("""CREATE OR REPLACE FUNCTION qdrant.explain_search(index_name text,q text,mode text DEFAULT 'text',top_k integer DEFAULT 10,
          query_vectors jsonb DEFAULT '{}',options jsonb DEFAULT '{}') RETURNS jsonb
          LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
          DECLARE plan jsonb; fault boolean;
          BEGIN
            plan:=qdrant.page_preflight_saved(index_name,q,mode,top_k,query_vectors,options);
            UPDATE qdrant_internal.page_preflight_fault SET calls=calls+1 RETURNING calls=cut INTO fault;
            IF fault THEN plan:=plan||jsonb_build_object('search_executable',false); END IF;
            RETURN plan;
          END $$""")
        cached_error = page(cached['next_cursor'], ok=False)
        assert '55000' in cached_error and 'readiness changed during paging' in cached_error, cached_error
        assert sql('SELECT calls FROM qdrant_internal.page_preflight_fault') == '0', 'Failed page did not roll back its injected counter'
        sql('UPDATE qdrant_internal.page_preflight_fault SET calls=0,cut=3')
        native_error = sql("SELECT * FROM qdrant.search('page_docs','anchor')", ok=False)
        assert '55000' in native_error and 'readiness changed during search' in native_error, native_error
    finally:
        sql(original_preflight)
        sql('DROP FUNCTION IF EXISTS qdrant.page_preflight_saved(text,text,text,integer,jsonb,jsonb); '
            'DROP TABLE IF EXISTS qdrant_internal.page_preflight_fault')
    assert page(cached['next_cursor'])['snapshot'] == cached['snapshot']
    checks.append('native search and cached pages reject an injected later not-ready preflight without exposing hits')

    previous_page = None
    def replay():
        nonlocal previous_page
        ready('page_docs')
        current = page()
        if previous_page and (current['storage_epoch'], current['generation']) != (previous_page['storage_epoch'], previous_page['generation']):
            assert '55000' in page(previous_page['next_cursor'], ok=False)
        assert current['captured_count'] == 23
        assert page(current['next_cursor'])['snapshot'] == current['snapshot']
        assert all(hit['provenance']['point_id'] for hit in current['hits'])
        previous_page = current
    replay()
    return replay

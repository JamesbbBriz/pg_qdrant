"""Disposable P0 key/recheck tests; no product ingestion or RLS support claim."""

import json


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def exercise_identity_and_source(scalar, jsql, run, spawn, finish, wait_for, expect_error, record):
    uuid = "12345678-1234-5678-9abc-123456789abc"
    replacement = "22345678-1234-5678-9abc-123456789abc"
    fingerprint = "a" * 64
    for integer in [-9223372036854775808, 9007199254740993, 9223372036854775807]:
        value = jsql(f"SELECT qdrant_internal.p0_identity_roundtrip({literal(str(integer))}::bigint,"
                     f"'{uuid.upper()}'::uuid,'知识库')")
        assert value["keys"] == [{"type": "bigint", "value": str(integer)},
                                  {"type": "uuid", "value": uuid},
                                  {"type": "text", "value": "知识库"}]
        assert bytes(value["uuid_bytes"]).hex() == uuid.replace("-", "")
        assert value["text_utf8_bytes"] == len("知识库".encode())
        assert value["roundtrip_verified"] and not value["point_id_mapping"]
    texts = ["", "1", "é", "e\u0301", "名" * 341, "x" * 1024]
    observed = [jsql(f"SELECT qdrant_internal.p0_identity_roundtrip(1,'{uuid}',{literal(text)})")["keys"][2]
                for text in texts]
    assert [x["value"] for x in observed] == texts and observed[2] != observed[3]
    for args in [f"NULL,'{uuid}','x'", "1,NULL,'x'", f"1,'{uuid}',NULL"]:
        expect_error(f"SELECT qdrant_internal.p0_identity_roundtrip({args})", "22004")
    expect_error(f"SELECT qdrant_internal.p0_identity_roundtrip(1,'{uuid}',repeat('名',342))", "53400")
    assert scalar("SELECT NOT proisstrict AND provolatile='v' AND proparallel='u' AND NOT prosecdef "
                  "FROM pg_proc WHERE oid='qdrant_internal.p0_identity_roundtrip(bigint,uuid,text)'::regprocedure") == "t"
    record("tagged_bigint_uuid_text_identity", bigint_exact_above_javascript_range=True,
           unicode_normalization=False, stable_point_id_allocated=False)

    run("CREATE SCHEMA pgq_p0_sources; "
        "CREATE TABLE pgq_p0_sources.bigints(id bigint PRIMARY KEY, revision bigint NOT NULL, "
        "incarnation uuid NOT NULL, fingerprint text NOT NULL); "
        "CREATE TABLE pgq_p0_sources.uuids(id uuid PRIMARY KEY, revision bigint NOT NULL, "
        "incarnation uuid NOT NULL, fingerprint text NOT NULL); "
        "CREATE TABLE pgq_p0_sources.texts(id text PRIMARY KEY, revision bigint NOT NULL, "
        "incarnation uuid NOT NULL, fingerprint text NOT NULL)")
    run(f"INSERT INTO pgq_p0_sources.bigints VALUES (1,1,'{uuid}','{fingerprint}'); "
        f"INSERT INTO pgq_p0_sources.uuids VALUES ('{uuid}',1,'{uuid}','{fingerprint}'); "
        f"INSERT INTO pgq_p0_sources.texts VALUES ('txn''编号',1,'{uuid}','{fingerprint}')")

    def candidate(key="1", tag="bigint", revision="1", incarnation=uuid, fp=fingerprint):
        return {"key": {"type": tag, "value": key}, "revision": revision,
                "incarnation": incarnation, "fingerprint": fp}

    def sql(candidates=None, relation="pgq_p0_sources.bigints", key="id"):
        if candidates is None:
            candidates = [candidate()]
        return ("SELECT qdrant_internal.p0_recheck_candidates(" + literal(relation) + "::regclass,"
                + literal(key) + "," + literal(json.dumps(candidates, ensure_ascii=False)) + "::jsonb)")

    def check(candidates=None, relation="pgq_p0_sources.bigints", key="id"):
        result = jsql(sql(candidates, relation, key))
        assert result["snapshot_scope"] == "active_statement_snapshot"
        assert result["spi_read_only"] and result["source_select_count"] == 1
        assert not result["production_authorization"] and not result["rls_supported"]
        assert not result["outbox_implemented"] and not result["engine_mvcc_top_k"]
        return [row["status"] for row in result["rows"]]

    assert check([candidate("999"), candidate()]) == ["missing", "matched"]
    assert check([candidate(uuid, "uuid")], "pgq_p0_sources.uuids") == ["matched"]
    assert check([candidate("txn'编号", "text")], "pgq_p0_sources.texts") == ["matched"]
    assert check([candidate(revision="2")]) == ["stale"]
    assert check([candidate(incarnation=replacement)]) == ["stale"]
    assert check([candidate(fp="b" * 64)]) == ["stale"]
    run(f"UPDATE pgq_p0_sources.bigints SET fingerprint='invalid'")
    assert check() == ["invalid_fixture"]
    run(f"UPDATE pgq_p0_sources.bigints SET fingerprint='{fingerprint}'")
    run("DELETE FROM pgq_p0_sources.bigints WHERE id=1")
    assert check() == ["missing"]
    run(f"INSERT INTO pgq_p0_sources.bigints VALUES(1,1,'{replacement}','{fingerprint}')")
    assert check() == ["stale"]
    assert check([candidate(incarnation=replacement)]) == ["matched"]
    run(f"UPDATE pgq_p0_sources.bigints SET incarnation='{uuid}'")
    # Identifiers come from a locked relation/catalog and are quoted; values bind.
    quoted_table = 'pgq_p0_sources."odd""; SELECT 1--"'
    quoted_key = 'key"; SELECT 1--'
    run(f'CREATE TABLE {quoted_table} ("key""; SELECT 1--" text PRIMARY KEY, '
        'revision bigint NOT NULL, incarnation uuid NOT NULL, fingerprint text NOT NULL)')
    run(f"INSERT INTO {quoted_table} VALUES('quoted''value',1,'{uuid}','{fingerprint}')")
    assert check([candidate("quoted'value", "text")], quoted_table, quoted_key) == ["matched"]
    assert jsql("SET search_path=pgq_p0_sources; " + sql())["rows"][0]["status"] == "matched"
    record("source_key_version_incarnation_and_quoted_binding", returned_source_text=False,
           allocated_incarnations=False, bounded_candidates=32)

    invalid = [[], [candidate()] * 33, [candidate(), candidate()], [candidate(tag="text")],
               [candidate(key="01")], [candidate(key="9223372036854775808")],
               [candidate(revision="0")], [candidate(revision="01")],
               [candidate(incarnation="bad")], [candidate(incarnation=uuid.upper())],
               [candidate(fp="A" * 64)], [candidate(fp="a" * 63)],
               [{**candidate(), "extra": True}], [dict(candidate(), revision=1)],
               [dict(candidate(), key={"type": "bigint", "value": 1})],
               [dict(candidate(), key={"type": "bigint", "value": "1", "extra": 2})]]
    for index, payload in enumerate(invalid):
        expect_error(sql(payload), "53400" if index < 2 else "22023")
    expect_error("SELECT qdrant_internal.p0_recheck_candidates(NULL,'id','[]')", "22004")
    expect_error("SELECT qdrant_internal.p0_recheck_candidates('pgq_p0_sources.bigints',NULL,'[]')", "22004")
    expect_error("SELECT qdrant_internal.p0_recheck_candidates('pgq_p0_sources.bigints','id',NULL)", "22004")
    for definition in [
        "id bigint, revision bigint NOT NULL, incarnation uuid NOT NULL, fingerprint text NOT NULL",
        "id bigint PRIMARY KEY DEFERRABLE, revision bigint NOT NULL, incarnation uuid NOT NULL, fingerprint text NOT NULL",
        "id integer PRIMARY KEY, revision bigint NOT NULL, incarnation uuid NOT NULL, fingerprint text NOT NULL",
        "id bigint PRIMARY KEY, revision bigint, incarnation uuid NOT NULL, fingerprint text NOT NULL",
        "id bigint PRIMARY KEY, revision text NOT NULL, incarnation uuid NOT NULL, fingerprint text NOT NULL",
        "id bigint PRIMARY KEY, revision bigint NOT NULL, incarnation text NOT NULL, fingerprint text NOT NULL",
        "id bigint PRIMARY KEY, revision bigint NOT NULL, incarnation uuid NOT NULL, fingerprint bytea NOT NULL",
    ]:
        run("CREATE TABLE pgq_p0_sources.bad(" + definition + ")")
        expect_error(sql(relation="pgq_p0_sources.bad"), "0A000")
        run("DROP TABLE pgq_p0_sources.bad")
    run("ALTER TABLE pgq_p0_sources.bigints ENABLE ROW LEVEL SECURITY")
    expect_error(sql(), "0A000")
    run("ALTER TABLE pgq_p0_sources.bigints FORCE ROW LEVEL SECURITY")
    expect_error(sql(), "0A000")
    run("ALTER TABLE pgq_p0_sources.bigints DISABLE ROW LEVEL SECURITY; "
        "ALTER TABLE pgq_p0_sources.bigints NO FORCE ROW LEVEL SECURITY")
    run("CREATE VIEW pgq_p0_sources.source_view AS SELECT * FROM pgq_p0_sources.bigints")
    expect_error(sql(relation="pgq_p0_sources.source_view"), "0A000")
    expect_error(sql(key='id"; DROP TABLE pgq_p0_sources.bigints;--'), "0A000")
    # A caught error after the catalog guard is acquired must release that guard.
    run("DO $body$ BEGIN BEGIN PERFORM qdrant_internal.p0_recheck_candidates("
        "'pgq_p0_sources.bigints','not_a_column'," + literal(json.dumps([candidate()])) + "::jsonb); "
        "RAISE EXCEPTION 'recheck unexpectedly passed'; EXCEPTION WHEN feature_not_supported THEN NULL; END; "
        "IF EXISTS(SELECT FROM pg_locks WHERE pid=pg_backend_pid() AND relation='pg_namespace'::regclass "
        "AND mode='ShareLock' AND granted) THEN RAISE EXCEPTION 'catalog guard leaked after ERROR'; END IF; END $body$")
    assert scalar("BEGIN; " + sql() + "; SELECT NOT EXISTS(SELECT FROM pg_locks "
                  "WHERE pid=pg_backend_pid() AND relation='pg_namespace'::regclass "
                  "AND mode='ShareLock' AND granted); ROLLBACK").splitlines()[-1] == "t"
    record("source_recheck_definition_input_and_error_lock_cleanup", rls="explicitly_rejected")

    # READ COMMITTED statement visibility even when the caller already has an XID.
    before = ("SET application_name='pgq_recheck_statement'; BEGIN; SELECT pg_current_xact_id(); "
              "WITH gate AS MATERIALIZED (SELECT pg_sleep(1.5)) "
              + sql() + " FROM gate; COMMIT")
    reader = spawn(before)
    wait_for(lambda: scalar("SELECT EXISTS(SELECT FROM pg_stat_activity WHERE application_name="
                            "'pgq_recheck_statement' AND wait_event='PgSleep')") == "t")
    run("UPDATE pgq_p0_sources.bigints SET revision=2")
    out, _ = finish(reader)
    result = json.loads([line for line in out.splitlines() if line.startswith('{')][-1])
    assert result["rows"][0]["status"] == "matched", result
    assert result["isolation"] == "read committed"
    assert check() == ["stale"]
    # A separate new statement inside REPEATABLE READ retains its established snapshot.
    run("UPDATE pgq_p0_sources.bigints SET revision=1")
    reader = spawn("SET application_name='pgq_recheck_repeatable'; BEGIN ISOLATION LEVEL REPEATABLE READ; "
                   + sql() + "; SELECT pg_sleep(1.5); " + sql() + "; COMMIT")
    wait_for(lambda: scalar("SELECT EXISTS(SELECT FROM pg_stat_activity WHERE application_name="
                            "'pgq_recheck_repeatable' AND wait_event='PgSleep')") == "t")
    run("UPDATE pgq_p0_sources.bigints SET revision=2")
    out, _ = finish(reader)
    results = [json.loads(line) for line in out.splitlines() if line.startswith('{')]
    assert len(results) == 2 and all(x["rows"][0]["status"] == "matched" for x in results)
    assert all(x["isolation"] == "repeatable read" for x in results)
    assert check() == ["stale"]
    run("UPDATE pgq_p0_sources.bigints SET revision=1")
    writer = spawn("SET application_name='pgq_recheck_uncommitted'; BEGIN; "
                   "UPDATE pgq_p0_sources.bigints SET revision=2; SELECT pg_sleep(1.5); ROLLBACK")
    wait_for(lambda: scalar("SELECT EXISTS(SELECT FROM pg_stat_activity WHERE application_name="
                            "'pgq_recheck_uncommitted' AND wait_event='PgSleep')") == "t")
    assert check() == ["matched"]
    finish(writer)
    assert check() == ["matched"]
    record("source_recheck_active_statement_and_repeatable_snapshots", caller_xid_assigned=True,
           read_committed_new_statement_observes_commit=True, uncommitted_update_invisible=True)

    # Observe the normal PostgreSQL SELECT lock; it can last to transaction end
    # independently of the probe-owned PgRelation/catalog guard lifetimes.
    holder = spawn("SET application_name='pgq_recheck_ddl'; BEGIN; " + sql()
                   + "; SELECT pg_sleep(1.5); COMMIT")
    wait_for(lambda: scalar("SELECT EXISTS(SELECT FROM pg_stat_activity WHERE application_name="
                            "'pgq_recheck_ddl' AND wait_event='PgSleep')") == "t")
    expect_error("SET lock_timeout='100ms'; ALTER TABLE pgq_p0_sources.bigints ADD COLUMN blocked integer", "55P03")
    finish(holder)
    run("ALTER TABLE pgq_p0_sources.bigints ADD COLUMN ddl_after_release integer")
    # Exercise the conditional catalog guard without unsafe system-catalog writes.
    blocker = spawn("SET application_name='pgq_recheck_namespace'; BEGIN; "
                    "LOCK TABLE pg_catalog.pg_namespace IN ROW EXCLUSIVE MODE; SELECT pg_sleep(1.5); COMMIT")
    wait_for(lambda: scalar("SELECT EXISTS(SELECT FROM pg_stat_activity WHERE application_name="
                            "'pgq_recheck_namespace' AND wait_event='PgSleep')") == "t")
    expect_error(sql(), "55P03")
    finish(blocker)
    assert check() == ["matched"]
    # A PostgreSQL-origin cancellation after namespace acquisition exercises
    # native error unwinding, before SPI. Reindex keeps only this fixture's
    # primary index exclusively locked while its ordinary table lock permits AS.
    holder = spawn("SET application_name='pgq_recheck_cancel_holder'; BEGIN; "
                   "REINDEX INDEX pgq_p0_sources.bigints_pkey; SELECT pg_sleep(5); COMMIT")
    canceled = None
    try:
        wait_for(lambda: scalar("SELECT EXISTS(SELECT FROM pg_stat_activity WHERE application_name="
                                "'pgq_recheck_cancel_holder' AND wait_event='PgSleep')") == "t")
        canceled = spawn(
            "SET application_name='pgq_recheck_cancel_waiter'; DO $body$ BEGIN BEGIN "
            + sql().replace("SELECT ", "PERFORM ", 1)
            + "; RAISE EXCEPTION 'recheck did not wait for cancellation'; "
            "EXCEPTION WHEN query_canceled THEN NULL; END; "
            "IF EXISTS(SELECT FROM pg_locks WHERE pid=pg_backend_pid() "
            "AND relation='pg_catalog.pg_namespace'::regclass AND mode='ShareLock' AND granted) "
            "THEN RAISE EXCEPTION 'catalog guard leaked after native cancellation'; END IF; "
            "END $body$; SELECT pg_backend_pid()")
        waiter_pid = wait_for(lambda: scalar(
            "SELECT a.pid FROM pg_stat_activity a WHERE a.application_name='pgq_recheck_cancel_waiter' "
            "AND EXISTS(SELECT FROM pg_locks l WHERE l.pid=a.pid "
            "AND l.relation='pgq_p0_sources.bigints_pkey'::regclass "
            "AND l.mode='AccessShareLock' AND NOT l.granted) "
            "AND EXISTS(SELECT FROM pg_locks n WHERE n.pid=a.pid "
            "AND n.relation='pg_catalog.pg_namespace'::regclass AND n.mode='ShareLock' AND n.granted)"))
        assert scalar(f"SELECT pg_cancel_backend({int(waiter_pid)})") == "t"
        out, _ = finish(canceled)
        assert out.strip() == waiter_pid, (out, waiter_pid)
        finish(holder)
    finally:
        for process in [canceled, holder]:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
    assert check() == ["matched"]
    record("source_recheck_ddl_and_conditional_namespace_guard", production_locking_design=False,
           native_cancellation_stage="primary_index_lock_before_SPI", same_backend_survived=True)

    run("CREATE ROLE pgq_p0_source_unprivileged")
    expect_error("SET ROLE pgq_p0_source_unprivileged; " + sql(), "42501")
    run("GRANT USAGE ON SCHEMA qdrant_internal,pgq_p0_sources TO pgq_p0_source_unprivileged; "
        "GRANT EXECUTE ON FUNCTION qdrant_internal.p0_recheck_candidates(regclass,text,jsonb), "
        "qdrant_internal.p0_identity_roundtrip(bigint,uuid,text) TO pgq_p0_source_unprivileged")
    expect_error("SET ROLE pgq_p0_source_unprivileged; " + sql(), "42501")
    expect_error("SET ROLE pgq_p0_source_unprivileged; "
                 "SELECT qdrant_internal.p0_identity_roundtrip(NULL,NULL,NULL)", "42501")
    expect_error("SET ROLE pgq_p0_source_unprivileged; "
                 "SELECT qdrant_internal.p0_recheck_candidates(NULL,NULL,NULL)", "42501")
    run("DROP OWNED BY pgq_p0_source_unprivileged; DROP ROLE pgq_p0_source_unprivileged; "
        "DROP SCHEMA pgq_p0_sources CASCADE")
    record("source_identity_recheck_acl_and_runtime_superuser", production_source_select_authorization=False)

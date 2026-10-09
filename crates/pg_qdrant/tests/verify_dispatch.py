"""Measure the installed consumer's idle binding-validation work."""
import json


def run(sql, ready, checks):
    ready('docs')
    function="'qdrant_internal.p1_index_status(text)'::regprocedure::oid"
    calls="SELECT coalesce((SELECT calls FROM pg_stat_user_functions WHERE funcid="+function+"),0)"
    # Per-backend tracking measures actual calls, with a positive control. The
    # worker retains its normal settings; no consumer function is replaced.
    sql("SET track_functions='all'; SELECT pg_stat_reset_single_function_counters("+function+"); "
        "SELECT qdrant_internal.p1_index_status('docs'); SELECT pg_stat_force_next_flush()")
    assert int(sql(calls))==1,'binding validator must be visible to function statistics'
    before=json.loads(sql("SELECT to_jsonb(c) FROM qdrant_internal.consumer_state c WHERE index_name='docs'"))
    result=sql("SET track_functions='all'; SELECT pg_stat_reset_single_function_counters("+function+"); "
        "SELECT qdrant_internal.next_source_batch(qdrant_internal.p0_ping()->>'engine_instance') IS NULL; "
        "SELECT pg_stat_force_next_flush()")
    assert result=='t',result
    assert int(sql(calls))==0,'an idle dispatch must not scan source bindings or their history counts'
    after=json.loads(sql("SELECT to_jsonb(c) FROM qdrant_internal.consumer_state c WHERE index_name='docs'"))
    assert before==after,'idle dispatch must not rewrite the serving consumer'
    checks.append('installed idle consumer dispatch performs zero source binding validations; positive control observes the real validator')

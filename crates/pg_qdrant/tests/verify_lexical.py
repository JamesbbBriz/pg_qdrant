"""Installed shared analyzer and native matching; no lexical quality claim."""
import json
import os
import pathlib


def run(sql, ready, ticket_from, checks, faults, crash_matrix):
    def literal(value):
        return "'"+json.dumps(value, ensure_ascii=False).replace("'", "''")+"'"

    sql('CREATE TABLE lexical_docs(id text PRIMARY KEY,body text NOT NULL)')
    rows = [('PG-001', 'anchor transaction recovery'), ('PG-002', 'anchor transaction safe recovery'),
            ('PG-003', 'anchor recovery transaction'), ('PG-004', 'anchor catalog'),
            ('pg-001', 'anchor transactions'), ('CN-001', 'anchor 事务回滚 支持中文'),
            ('U-001', 'anchor café'), ('U-002', 'anchor cafe')]
    sql('INSERT INTO lexical_docs VALUES '+','.join("('"+k+"','"+v+"')" for k, v in rows))
    sql("SELECT qdrant.create_index('lexical_docs','lexical_docs','id','{\"text\":{\"fields\":[\"body\"]}}')")
    ready('lexical_docs')

    def hits(matching, q='anchor', ok=True):
        return sql("SELECT coalesce(jsonb_agg(source_key ORDER BY source_key),'[]') FROM "
                   "qdrant.search('lexical_docs','"+q+"','text',10,'{}',"+literal({'matching': matching})+")", ok=ok)

    def goldens():
        ready('lexical_docs')
        fixtures = [({'all': 'transaction recovery'}, ['PG-001', 'PG-002', 'PG-003']),
                    ({'any': 'catalog transaction'}, ['PG-001', 'PG-002', 'PG-003', 'PG-004']),
                    ({'all': 'transaction recovery', 'exclude': 'safe catalog'}, ['PG-001', 'PG-003']),
                    ({'phrase': 'TRANSACTION recovery'}, ['PG-001']),
                    ({'phrase': 'recovery transaction'}, ['PG-003']),
                    ({'token_prefix': 'TRAN'}, ['PG-001', 'PG-002', 'PG-003', 'pg-001']),
                    ({'key_exact': 'PG-001'}, ['PG-001']), ({'key_prefix': 'PG-'}, ['PG-001', 'PG-002', 'PG-003', 'PG-004']),
                    ({'key_prefix': 'pg-'}, ['pg-001']), ({'key_exact': 'PG'}, []),
                    ({'all': 'transactions'}, ['pg-001']), ({'all': 'café'}, ['U-001', 'U-002']),
                    ({'all': 'cafe'}, ['U-001', 'U-002']), ({'token_prefix': 'café'}, ['U-001']),
                    ({'all': '事务回滚', 'key_prefix': 'CN-'}, ['CN-001']),
                    ({'phrase': 'transaction recovery', 'key_prefix': 'pg-'}, [])]
        for matching, expected in fixtures:
            result = json.loads(hits(matching))
            assert result == expected, (matching, result, expected)

    goldens()
    for matching in [None, [], 'phrase', {'tenant_id': 'anything'}, {'phrase': None}, {'all': []},
                     {'token_prefix': 'a'}, {'token_prefix': 'a b'}, {'token_prefix': 'a'*33},
                     {'all': ''}, {'phrase': 'x'*2049}, {'key_exact': 'x'*1025},
                     {'all': 'x'*2048, 'any': 'x'*2048, 'exclude': 'x'*2048, 'phrase': 'x'*2048, 'key_prefix': 'a'}]:
        assert '22023' in hits(matching, ok=False), matching
    rejected = hits({'phrase': '!!!'}, ok=False)
    assert 'matching clause requires' in rejected, rejected
    explain = json.loads(sql("SELECT qdrant.explain_search('lexical_docs','anchor','text',10,'{}',"+
                             literal({'matching': {'phrase': 'transaction recovery'}})+")"))
    assert explain['native_predicates_available'] and not explain['native_predicates_compiled']
    assert explain['matching_contract']['idf_scope'].startswith('entire live')
    assert '42501' in sql("SET ROLE pgq_writer; SELECT * FROM qdrant.search('lexical_docs','anchor','text',10,'{}',"+
                         literal({'matching': {'key_prefix': 'PG-'}})+")", ok=False)
    # Ranking and payload matching independently compile the same policy.
    assert json.loads(hits({'all': 'TRANSACTION'}, 'TRANSACTION')) == ['PG-001', 'PG-002', 'PG-003']
    assert json.loads(hits({'all': 'transactions'}, 'transactions')) == ['pg-001']
    assert json.loads(hits({'all': 'café'}, 'café')) == ['U-001', 'U-002']
    assert json.loads(hits({'all': 'cafe'}, 'cafe')) == ['U-001', 'U-002']
    assert json.loads(hits({'all': '事务回滚'}, '事务回滚')) == ['CN-001']
    unfiltered = float(sql("SELECT score FROM qdrant.search('lexical_docs','anchor','text',10) WHERE source_key='PG-001'"))
    filtered = float(sql("SELECT score FROM qdrant.search('lexical_docs','anchor','text',10,'{}',"+
                         literal({'matching': {'key_exact': 'PG-001'}})+")"))
    assert abs(unfiltered-filtered) < 1e-8, (unfiltered, filtered)
    checks += ['shared BM25/payload analyzer case, no stemming, native multilingual accent normalization and mixed Chinese fixtures',
               'native AND/OR/exclusion/contiguous phrase, separate token and exact whole-key prefixes',
               'typed matching rejects unknown/null/over-budget clauses and unauthorized source access']
    checks += ['candidate matching preserves whole-live-generation BM25 IDF statistics']
    # Ordinary DML must replace positional/prefix and keyword postings, including
    # deletion followed by reusing a primary key with a new incarnation.
    changed = ticket_from(sql("BEGIN; UPDATE lexical_docs SET body='anchor changed' WHERE id='PG-001'; "
                              "SELECT qdrant.track_changes('lexical_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+changed+"',10000)"))['durable']
    assert json.loads(hits({'phrase': 'transaction recovery'})) == []
    assert json.loads(hits({'token_prefix': 'tran', 'key_exact': 'PG-001'})) == []
    reused = ticket_from(sql("BEGIN; DELETE FROM lexical_docs WHERE id='PG-001'; "
                             "INSERT INTO lexical_docs VALUES('PG-001','anchor transaction recovery'); "
                             "SELECT qdrant.track_changes('lexical_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+reused+"',10000)"))['durable']
    goldens()
    if faults:
        before = ready('lexical_docs')
        pid = json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid']
        index_id = sql("SELECT index_id FROM qdrant_internal.index_catalog WHERE index_name='lexical_docs'")
        marker = next(pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA']).rglob('db-*.engine-owner')).with_suffix('.fault')
        marker.write_text('shadow:'+index_id+':lexical_schema_error')
        failed_task = json.loads(sql("SELECT qdrant.rebuild_index('lexical_docs')"))['task_id']
        failed = json.loads(sql("SELECT qdrant.await_task('"+failed_task+"',60000,true)"))
        assert failed['state'] == 'failed' and not failed['succeeded'] and 'injected lexical schema error' in failed['error'], failed
        assert failed['pending_events'] > 0 and failed['abandoned_storage_cleanup'], failed
        assert sql("SELECT count(*) FROM qdrant_internal.generation_receipts WHERE task_id='"+failed_task+"'") == '0'
        root = marker.with_suffix('.indexes')
        assert not marker.exists() and not (root/(index_id+'-'+failed['generation']+'-'+failed['storage_epoch'])).exists()
        assert json.loads(sql('SELECT qdrant_internal.p0_ping()'))['engine_pid'] == pid
        assert ready('lexical_docs')['generation'] == before['generation']
        goldens()
        crash_matrix.append({'cut': 'shadow_partial_native_lexical_schema_error', 'injected_fault': True,
                             'before': before, 'failed_task': failed, 'same_helper_pid': True,
                             'event_receipts': 0, 'native_directory_cleaned': True})
        checks.append('partial native lexical schema creation never ACKs or switches; failed owned shard cleans up while serving generation remains usable')
    task = json.loads(sql("SELECT qdrant.rebuild_index('lexical_docs')"))['task_id']
    assert json.loads(sql("SELECT qdrant.await_task('"+task+"',60000)"))['succeeded']
    goldens()
    checks += ['lexical positional/prefix postings survive durable source edits/key reuse and actual generation switch']
    return goldens

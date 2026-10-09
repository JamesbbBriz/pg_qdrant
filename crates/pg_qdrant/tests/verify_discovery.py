"""Installed mode registry and permissioned representation-readiness checks."""
import json


def run(sql, ready, ticket_from, checks):
    global_registry = json.loads(sql('SELECT qdrant.capabilities()'))
    assert global_registry['registry_schema_version'] == 2
    expected = {'text', 'semantic', 'sparse', 'hybrid', 'maxsim', 'precision'}
    assert {m['mode'] for m in global_registry['query_modes']} == expected
    assert len(global_registry['capabilities']) == 54
    assert all(m['adapter_available'] and m['admission_ready'] is None and not m['release_validation_passed']
               and not m['release_supported'] and not m['native_query_executed'] for m in global_registry['query_modes'])
    for query in ['SELECT * FROM qdrant_internal.query_modes', 'SELECT qdrant_internal.capability_registry_base(NULL)']:
        assert '42501' in sql('SET ROLE pgq_writer; '+query, ok=False)

    fields, slots = [], {}
    for name, kind in [('dense', 'dense'), ('sparse', 'learned_sparse'), ('tokens', 'token_vectors')]:
        c = dict(kind=kind, model_id='discovery-'+name, model_version='r1', tokenizer='fixture-r1',
                 dimensions=2 if kind != 'learned_sparse' else 100, distance='dot',
                 normalization='none', storage_precision='float32', vector_field=name+'_vector',
                 fingerprint_field=name+'_source', incarnation_field=name+'_inc',
                 model_id_field=name+'_model', model_version_field=name+'_version')
        if kind == 'token_vectors':
            c.update(max_tokens=4, comparator='maxsim')
        if kind == 'learned_sparse':
            c.update(vocabulary='discovery-r1', idf_policy='none', idf_revision='not-applied')
        slots[name] = c
        fields += [name+'_vector jsonb', name+'_source text', name+'_inc uuid', name+'_model text', name+'_version text']
    sql('CREATE TABLE discovery_docs(id bigint PRIMARY KEY,body text NOT NULL,'+','.join(fields)+')')
    sql("INSERT INTO discovery_docs(id,body) VALUES(1,'alpha'),(2,'beta')")
    settings = json.dumps({'text': {'fields': ['body']}, 'representations': slots})
    sql("SELECT qdrant.create_index('discovery_docs','discovery_docs','id','"+settings+"')")
    ready('discovery_docs')
    assert '22023' in sql("SELECT qdrant.capabilities('missing')", ok=False)
    assert '22023' in sql("BEGIN; DELETE FROM qdrant_internal.query_modes WHERE mode='text'; SELECT qdrant.explain_search('discovery_docs','alpha');", ok=False)
    assert {m['mode'] for m in json.loads(sql('SELECT qdrant.capabilities()'))['query_modes']} == expected

    def registry(role=None):
        value = json.loads(sql(('SET ROLE '+role+'; ' if role else '')+"SELECT qdrant.capabilities('discovery_docs')"))
        assert 'owner_status' not in value['index_state']
        return {m['mode']: m for m in value['query_modes']}

    def expect(enabled):
        values = registry()
        assert {mode for mode, item in values.items() if item['admission_ready']} == set(enabled), values

    expect(['text'])
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant.capabilities('discovery_docs')", ok=False)
    sql('CREATE ROLE pgq_discovery_reader; GRANT builder TO pgq_discovery_reader; GRANT SELECT ON discovery_docs TO pgq_discovery_reader')
    assert registry('pgq_discovery_reader')['text']['admission_ready']
    sql('REVOKE SELECT ON discovery_docs FROM builder,pgq_discovery_reader')
    assert sql("SELECT has_table_privilege('pgq_discovery_reader','discovery_docs','SELECT')") == 'f'
    assert '42501' in sql("SET ROLE pgq_discovery_reader; SELECT qdrant.capabilities('discovery_docs')", ok=False)
    sql('GRANT SELECT ON discovery_docs TO builder,pgq_discovery_reader')

    def fill(name):
        c = slots[name]
        for row in json.loads(sql("SELECT qdrant.encoding_inputs('discovery_docs','"+name+"')")):
            vector = [[1, 0]] if name == 'tokens' else ({'indices': [7], 'values': [1]} if name == 'sparse' else [1, 0])
            values = {c['vector_field']: json.dumps(vector), c['fingerprint_field']: row['source_fingerprint'],
                      c['incarnation_field']: row['incarnation'], c['model_id_field']: c['model_id'], c['model_version_field']: c['model_version']}
            ticket = ticket_from(sql('BEGIN; UPDATE discovery_docs SET '+','.join(k+"='"+v+"'" for k, v in values.items())+
                                     ' WHERE id='+row['key']['value']+"; SELECT qdrant.track_changes('discovery_docs'); COMMIT"))
            assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))['durable']
        ready('discovery_docs')

    fill('dense'); expect(['text', 'semantic', 'hybrid'])
    fill('sparse'); expect(['text', 'semantic', 'sparse', 'hybrid'])
    fill('tokens'); expect(expected)
    ticket = ticket_from(sql("BEGIN; UPDATE discovery_docs SET body='changed' WHERE id=1; SELECT qdrant.track_changes('discovery_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))['durable']
    expect(['text'])
    for name in slots:
        fill(name)
    expect(expected)
    mismatch = sql("BEGIN; UPDATE qdrant_internal.representation_state SET incarnation=gen_random_uuid() WHERE index_name='discovery_docs' AND name='dense'; SELECT value->>'admission_ready' FROM jsonb_array_elements(qdrant.capabilities('discovery_docs')->'query_modes') WHERE value->>'mode'='semantic'; ROLLBACK")
    assert mismatch == 'false', mismatch
    # A stale identity must not masquerade as complete merely because row counts match.
    mismatch = sql("BEGIN; UPDATE qdrant_internal.representation_state SET source_fingerprint='wrong' WHERE index_name='discovery_docs' AND name='dense'; SELECT value->>'admission_ready' FROM jsonb_array_elements(qdrant.capabilities('discovery_docs')->'query_modes') WHERE value->>'mode'='semantic'; ROLLBACK")
    assert mismatch == 'false', mismatch
    expect(expected)
    assert '0A000' in sql("BEGIN; ALTER TABLE discovery_docs ENABLE ROW LEVEL SECURITY; SELECT qdrant.capabilities('discovery_docs')", ok=False)
    expect(expected)
    task = json.loads(sql("SELECT qdrant.rebuild_index('discovery_docs')"))['task_id']
    rebuilt = json.loads(sql("SELECT qdrant.await_task('"+task+"',60000)"))
    assert rebuilt['succeeded'], rebuilt
    ready('discovery_docs')
    expect(expected)
    checks.append('global and index-specific discovery separates retained requirements, adapter modes, release validation and actual representation freshness with owner/source permissions')
    checks.append('one private mode registry governs public search admission and capability discovery; rollback restores its exact six-mode membership')

    def replay():
        ready('discovery_docs')
        expect(expected)
        snapshot = json.loads(sql("SELECT qdrant.capabilities('discovery_docs')"))
        assert snapshot['index_state']['pending_events'] == 0
        assert len(snapshot['index_state']['generation']) == 36
    replay()
    return replay

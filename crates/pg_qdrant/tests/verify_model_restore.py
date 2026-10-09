"""Fresh BYOV identities after a real source dump, never imported readiness."""
import json


def declaration(slots):
    return "qdrant.create_index('restored_models','restored_models','id','" + json.dumps(
        {'text': {'fields': ['body']}, 'representations': slots}) + "')"


def output(sql, ticket_from, request, vector, ok=True):
    c = request['model']
    values = {c['vector_field']: json.dumps(vector), c['fingerprint_field']: request['source_fingerprint'],
              c['incarnation_field']: request['incarnation'], c['model_id_field']: c['model_id'],
              c['model_version_field']: c['model_version']}
    result = sql('BEGIN; UPDATE restored_models SET ' + ','.join(k + "='" + v + "'" for k, v in values.items()) +
                 ' WHERE id=' + request['key']['value'] + "; SELECT qdrant.track_changes('restored_models'); COMMIT", ok=ok)
    if not ok:
        return result
    ticket = ticket_from(result)
    assert json.loads(sql("SELECT qdrant.await_changes('" + ticket + "',60000)"))['durable']


def vector(name, key):
    dense = [1, 0] if key == '1' else [0, 1]
    if name == 'tokens':
        return [dense]
    if name == 'learned':
        return {'indices': [7 if key == '1' else 9], 'values': [1]}
    return dense


def prepare(sql, ready, ticket_from):
    slots = {}
    fields = []
    for name, kind in [('dense', 'dense'), ('learned', 'learned_sparse'), ('tokens', 'token_vectors')]:
        c = dict(kind=kind, model_id='restore-' + name, model_version='r1', tokenizer='restore-r1',
                 dimensions=100 if name == 'learned' else 2, distance='dot', normalization='none',
                 storage_precision='float32', vector_field=name + '_vector', fingerprint_field=name + '_source',
                 incarnation_field=name + '_inc', model_id_field=name + '_model', model_version_field=name + '_version')
        if name == 'learned':
            c.update(vocabulary='restore-vocabulary-r1', idf_policy='none', idf_revision='not-applied')
        if name == 'tokens':
            c.update(max_tokens=4, comparator='maxsim')
        slots[name] = c
        fields += [name + '_vector jsonb', name + '_source text', name + '_inc uuid',
                   name + '_model text', name + '_version text']
    sql('CREATE TABLE restored_models(id bigint PRIMARY KEY,body text NOT NULL,' + ','.join(fields) + ')')
    sql("INSERT INTO restored_models(id,body) VALUES(1,'model backup alpha'),(2,'model backup beta'); SELECT " + declaration(slots))
    status = ready('restored_models')
    requests = {}
    for name in slots:
        requests[name] = json.loads(sql("SELECT qdrant.encoding_inputs('restored_models','" + name + "')"))
        for request in requests[name]:
            output(sql, ticket_from, request, vector(name, request['key']['value']))
    rows = json.loads(sql("SELECT qdrant.index_status('restored_models')"))['representations']
    assert all(rows[name]['rows']['ready'] == 2 for name in slots), rows
    return slots, requests, status['generation']


def recover(sql, ready, ticket_from, checks, fixture):
    slots, old_requests, old_generation = fixture
    full = declaration(slots)

    def triggers():
        return sql("SELECT jsonb_object_agg(tgname,oid::text) FROM pg_trigger WHERE "
                   "tgrelid='restored_models'::regclass AND NOT tgisinternal")

    before = triggers()
    assert len(json.loads(before)) == 5
    assert sql('SELECT count(*) FROM restored_models WHERE dense_vector IS NOT NULL AND learned_vector IS NOT NULL AND tokens_vector IS NOT NULL') == '2'
    assert sql("SELECT count(*) FROM qdrant_internal.index_catalog WHERE index_name='restored_models'") == '0'
    omitted = {name: c for name, c in slots.items() if name != 'tokens'}
    assert '55000' in sql('SELECT ' + declaration(omitted), ok=False)
    assert triggers() == before
    fields = 'dense_vector,dense_source,dense_inc,dense_model,dense_version'
    prefix = 'DROP TRIGGER qdrant_model_dense ON restored_models; CREATE TRIGGER qdrant_model_dense '
    suffix = " ON restored_models FOR EACH ROW EXECUTE FUNCTION qdrant_internal.model_output_guard('restored_models','dense')"
    negatives = [
        'ALTER TABLE restored_models DISABLE TRIGGER qdrant_model_dense',
        prefix + 'BEFORE UPDATE OF ' + fields + " ON restored_models FOR EACH ROW EXECUTE FUNCTION qdrant_internal.model_output_guard('wrong','dense')",
        prefix + 'BEFORE UPDATE OF ' + fields + " ON restored_models FOR EACH ROW EXECUTE FUNCTION qdrant_internal.model_output_guard('restored_models','wrong')",
        prefix + 'AFTER UPDATE OF ' + fields + suffix,
        prefix + 'BEFORE UPDATE OF dense_vector,dense_source,dense_inc,dense_model,body' + suffix,
        prefix + 'BEFORE UPDATE' + suffix,
        prefix + 'BEFORE UPDATE OF ' + fields + " ON restored_models FOR EACH ROW WHEN (true) EXECUTE FUNCTION qdrant_internal.model_output_guard('restored_models','dense')",
        prefix + 'BEFORE UPDATE OF ' + fields + ' ON restored_models FOR EACH ROW EXECUTE FUNCTION public.source_audit()',
    ]
    for change in negatives:
        assert '55000' in sql('BEGIN; ' + change + '; SELECT ' + full, ok=False), change
        assert triggers() == before
        assert sql("SELECT count(*) FROM qdrant_internal.index_catalog WHERE index_name='restored_models'") == '0'
    bad = {name: dict(c) for name, c in slots.items()}
    bad['tokens']['vector_field'] = 'absent_column'
    assert '22023' in sql('SELECT ' + declaration(bad), ok=False)
    assert triggers() == before
    sql('BEGIN; SELECT ' + full + '; ROLLBACK')
    assert triggers() == before
    checks.append('actual BYOV dump retains all three output kinds; omitted and eight altered model guards fail closed, and invalid later slots/explicit rollback preserve original trigger identities')

    sql('SELECT ' + full)
    status = ready('restored_models')
    assert status['generation'] != old_generation
    assert all(status['representations'][name]['rows']['stale'] == 2 and
               status['representations'][name]['rows'].get('ready', 0) == 0 for name in slots), status
    assert sql("SELECT count(*) FROM qdrant.search('restored_models','backup')") == '2'
    queries = {}
    for name, c in slots.items():
        query = dict(model_id=c['model_id'], model_version=c['model_version'], vector=vector(name, '1'))
        if name == 'learned':
            query.update(vocabulary=c['vocabulary'], idf_revision=c['idf_revision'])
        mode = {'dense': 'semantic', 'learned': 'sparse', 'tokens': 'maxsim'}[name]
        queries[name] = "SELECT coalesce(jsonb_agg(source_key ORDER BY rank),'[]') FROM qdrant.search('restored_models','backup','" + mode + "',10,'" + json.dumps({name: query}) + "')"
        assert '55000' in sql(queries[name], ok=False)
        assert '55000' in output(sql, ticket_from, old_requests[name][0], vector(name, '1'), ok=False)
    checks.append('fresh restored incarnations keep retained dense/sparse/token outputs stale, permit BM25 and reject native model queries and late pre-backup identity outputs')

    for name in slots:
        requests = json.loads(sql("SELECT qdrant.encoding_inputs('restored_models','" + name + "')"))
        assert len(requests) == 2
        old = {r['key']['value']: r for r in old_requests[name]}
        for request in requests:
            assert request['model'] == slots[name]
            assert request['incarnation'] != old[request['key']['value']]['incarnation']
            assert request['source_fingerprint'] == old[request['key']['value']]['source_fingerprint']
            output(sql, ticket_from, request, vector(name, request['key']['value']))
        results = json.loads(sql(queries[name]))
        assert results and results[0] == '1', (name, results)
    status = ready('restored_models')
    assert all(status['representations'][name]['rows']['ready'] == 2 for name in slots), status
    assert triggers() != before
    for name in slots:
        assert json.loads(triggers())['qdrant_model_' + name] == json.loads(before)['qdrant_model_' + name]
    checks.append('current encoding inputs and ordinary model-column UPDATEs rebuild all three native representations through exact durable tickets; real dense/sparse/MaxSim searches rank the expected restored source')
    active_triggers = triggers()
    changed = json.loads(sql("BEGIN; DROP TRIGGER qdrant_model_dense ON restored_models; "
                             "SELECT qdrant.index_status('restored_models'); ROLLBACK"))
    assert changed['capture_state'] == 'degraded' and not changed['engine_index_ready'], changed
    assert '55000' in sql('BEGIN; DROP TRIGGER qdrant_model_dense ON restored_models; ' + queries['dense'], ok=False)
    assert triggers() == active_triggers
    assert ready('restored_models')['capture_state'] == 'capturing'
    checks.append('a later restored model-guard drop still invalidates capture and native reads; rollback restores the active guard and serving state')

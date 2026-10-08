"""Real native token MaxSim, bounded prefetch domain, model and replay gates."""
import json


def run(sql, ready, ticket_from, checks):
    fields = []
    slots = {}
    for name, kind, width in [('tokens', 'token_vectors', 2), ('dense', 'dense', 2),
                              ('learned', 'learned_sparse', 100), ('budget', 'token_vectors', 128)]:
        c = dict(kind=kind, model_id='fixture-'+name, model_version='r1', tokenizer='fixture-r1',
                 dimensions=width, distance='dot', normalization='none', storage_precision='float32',
                 vector_field=name+'_vector', fingerprint_field=name+'_source', incarnation_field=name+'_inc',
                 model_id_field=name+'_model', model_version_field=name+'_version')
        if kind == 'token_vectors':
            c.update(max_tokens=4 if name == 'tokens' else 128, comparator='maxsim')
        if kind == 'learned_sparse':
            c.update(vocabulary='fixture-vocabulary-r1', idf_policy='none', idf_revision='not-applied')
        slots[name] = c
        fields += [name+'_vector jsonb', name+'_source text', name+'_inc uuid', name+'_model text', name+'_version text']
    sql('CREATE TABLE token_docs(id bigint PRIMARY KEY,body text NOT NULL,'+','.join(fields)+')')
    sql("INSERT INTO token_docs(id,body) VALUES(1,'alpha alpha'),(2,'alpha beta'),(3,'outsider'),(4,'separate')")
    sql("INSERT INTO token_docs(id,body) SELECT n,'unrelated '||n FROM generate_series(5,10)n")
    for field, value in [('distance', 'cosine'), ('max_tokens', 0), ('max_tokens', 129),
                         ('max_tokens', 1.5), ('comparator', 'unknown')]:
        bad = dict(slots['tokens']); bad[field] = value
        settings = json.dumps({'text': {'fields': ['body']}, 'representations': {'tokens': bad}})
        code = '22023' if field == 'max_tokens' else '0A000'
        assert code in sql("SELECT qdrant.create_index('bad_token','token_docs','id','"+settings+"')", ok=False)
    assert sql("SELECT count(*) FROM qdrant_internal.index_catalog WHERE index_name='bad_token'") == '0'
    settings = json.dumps({'text': {'fields': ['body']}, 'representations': slots})
    sql("SELECT qdrant.create_index('token_docs','token_docs','id','"+settings+"')")
    ready('token_docs')

    def vectors(name='tokens', vector=None):
        c = slots[name]
        if vector is None:
            vector = [[1, 0], [0, 1]] if name == 'tokens' else (
                {'indices': [7], 'values': [1]} if name == 'learned' else [1, 0])
        result = dict(model_id=c['model_id'], model_version=c['model_version'], vector=vector)
        if name == 'learned':
            result.update(vocabulary=c['vocabulary'], idf_revision=c['idf_revision'])
        return {name: result}

    def hits(mode='maxsim', queries=None, cap=10, top=10, fusion=None, ok=True, matching=None):
        options = dict(candidate_limit=cap)
        if fusion:
            options['fusion'] = fusion
        if matching is not None:
            options['matching'] = matching
        return sql("SELECT coalesce(jsonb_agg(jsonb_build_object('key',source_key,'score',score,'plan',provenance->>'plan') ORDER BY rank),'[]') "
                   "FROM qdrant.search('token_docs','alpha','"+mode+"',"+str(top)+",'"+
                   json.dumps(queries if queries is not None else vectors())+"','"+json.dumps(options)+"')", ok=ok)

    def output(row, name, vector, ok=True):
        c = slots[name]
        values = {c['vector_field']: json.dumps(vector), c['fingerprint_field']: row['source_fingerprint'],
                  c['incarnation_field']: row['incarnation'], c['model_id_field']: c['model_id'],
                  c['model_version_field']: c['model_version']}
        return sql('BEGIN; UPDATE token_docs SET '+','.join(k+"='"+v+"'" for k, v in values.items())+
                   ' WHERE id='+row['key']['value']+"; SELECT qdrant.track_changes('token_docs'); COMMIT", ok=ok)

    token_values = {'1': [[1, 0], [0, 1]], '2': [[2, 0], [0, 2]], '3': [[10, 10]],
                    **{str(k): [[.25/k, .25/k]] for k in range(4, 11)}}

    def fill():
        for name in slots:
            for row in json.loads(sql("SELECT qdrant.encoding_inputs('token_docs','"+name+"')")):
                key = row['key']['value']
                vector = token_values[key] if name == 'tokens' else (
                    [[1]+[0]*127] if name == 'budget' else (
                        {'indices': [7], 'values': [12-int(key)]} if name == 'learned' else [12-int(key), 0]))
                ticket = ticket_from(output(row, name, vector))
                waited = json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))
                assert waited['durable'], (name, key, waited, sql("SELECT qdrant.index_status('token_docs')"))

    assert '55000' in hits(ok=False)
    first = json.loads(sql("SELECT qdrant.encoding_inputs('token_docs','tokens')"))[0]
    for wrong in [[], [1, 0], [[1]], [[1, 0], [1]], [[1, 0]]*5, [[1e100, 0]], [[1e30, 0]], [[None, 0]]]:
        assert '22023' in output(first, 'tokens', wrong, ok=False), wrong
    fill()

    def goldens():
        ready('token_docs')
        results = json.loads(hits())
        # Independent sum(query token max document-token dot product).
        expected = {k: sum(max(sum(a*b for a, b in zip(q, d)) for d in doc)
                           for q in [[1, 0], [0, 1]]) for k, doc in token_values.items()}
        assert [h['key'] for h in results] == ['3', '2', '1']+[str(k) for k in range(4, 11)], results
        assert all(abs(h['score']-expected[h['key']]) < .00001 for h in results), (results, expected)
        one = json.loads(hits('precision', cap=1, top=1))
        assert [h['key'] for h in one] == ['1'] and one[0]['score'] == 2, one
        two = json.loads(hits('precision', cap=2, top=2))
        assert [h['key'] for h in two] == ['2', '1'], two
        assert [h['score'] for h in two] == [4, 2] and all(h['plan'] == 'precision_bm25_maxsim' for h in two)
        for name in ['dense', 'learned']:
            for fusion in ['rrf', 'dbsf']:
                result = json.loads(hits('precision', dict(vectors(), **vectors(name)), cap=2, top=2, fusion=fusion))
                assert [h['key'] for h in result] == ['2', '1'], result
                assert [h['score'] for h in result] == [4, 2], result
                kind = 'dense' if name == 'dense' else 'learned_sparse'
                assert all(h['plan'] == 'precision_bm25_'+kind+'_'+fusion+'_maxsim' for h in result), result
        # Higher scoring outsiders occur in lexical, dense/sparse and tokens.
        # A cap of one forces the predicate into each recall branch, before
        # truncation, and into the nested candidate/reranking stages.
        plans = [('text', {}, None), ('semantic', vectors('dense'), None),
                 ('sparse', vectors('learned'), None), ('maxsim', vectors(), None),
                 ('precision', vectors(), None)]
        for name in ['dense', 'learned']:
            for fusion in ['rrf', 'dbsf']:
                plans += [('hybrid', vectors(name), fusion),
                          ('precision', dict(vectors(), **vectors(name)), fusion)]
        for mode, queries, fusion in plans:
            result = json.loads(hits(mode, queries, cap=1, top=1, fusion=fusion, matching={'phrase': 'alpha beta'}))
            assert [h['key'] for h in result] == ['2'], (mode, fusion, result)
            # Check native candidate identities without relying on final JOIN.
            options = dict(candidate_limit=1, matching={'phrase': 'alpha beta'})
            if fusion:
                options['fusion'] = fusion
            admitted = json.loads(sql("SELECT qdrant.explain_search('token_docs','alpha','"+mode+"',1,'"+
                                      json.dumps(queries)+"','"+json.dumps(options)+"')"))
            request = dict(operation='source_search', q='alpha', top_k=1,
                           representation_query=admitted['representation_query'],
                           rerank_query=admitted['rerank_query'], fusion=admitted['fusion'],
                           predicates=admitted['matching'])
            native = json.loads(sql("SELECT qdrant_internal.p1_search('"+json.dumps(request)+"'::jsonb || "
                                    "(SELECT jsonb_build_object('index_id',i.index_id,'generation',i.generation,'storage_epoch',c.storage_epoch) "
                                    "FROM qdrant_internal.index_catalog i JOIN qdrant_internal.consumer_state c USING(index_name) "
                                    "WHERE i.index_name='token_docs'),5000)"))
            assert [h['payload']['source_key']['value'] for h in native] == ['2'], (mode, fusion, native)
            paged = json.loads(sql("SELECT qdrant.search_page('token_docs','alpha','"+mode+"',1,'"+
                                   json.dumps(queries)+"','"+json.dumps(options)+"')"))
            assert [h['source_key'] for h in paged['hits']] == ['2'], (mode, fusion, paged)
            assert paged['captured_count'] == 1 and paged['next_cursor'] is None
            assert paged['global_match_count'] is None and not paged['global_coverage_verified']
            sql("DELETE FROM qdrant_internal.search_snapshots WHERE snapshot_id='"+paged['snapshot']+"'")
        paged = json.loads(sql("SELECT qdrant.search_page('token_docs','alpha','maxsim',2,'"+json.dumps(vectors())+"')"))
        assert [h['source_key'] for h in paged['hits']] == ['3', '2']
        assert '22023' in sql("SELECT qdrant.search_page('token_docs','alpha','maxsim',2,'"+
                              json.dumps(vectors(vector=[[0, 1]]))+"','{}','"+json.dumps(paged['next_cursor'])+"')", ok=False)
        second_page = json.loads(sql("SELECT qdrant.search_page('token_docs','alpha','maxsim',2,'"+
                                     json.dumps(vectors())+"','{}','"+json.dumps(paged['next_cursor'])+"')"))
        assert [h['source_key'] for h in second_page['hits']] == ['1', '4']
        assert not second_page['native_query_executed']
        sql("DELETE FROM qdrant_internal.search_snapshots WHERE snapshot_id='"+paged['snapshot']+"'")

    goldens()
    for wrong in [[], [[1]], [[1, 0]]*5, [[1e100, 0]], [[1e30, 0]]]:
        assert '22023' in hits(queries=vectors(vector=wrong), ok=False)
    assert '22023' in hits(mode='semantic', ok=False)
    assert '22023' in hits(mode='precision', queries=vectors('dense'), ok=False)
    assert '22023' in hits(mode='precision', queries=dict(vectors(), **vectors('dense'), **vectors('learned')), ok=False)
    assert '22023' in hits(mode='precision', queries=dict(vectors(), **vectors('budget', [[1]+[0]*127])), ok=False)
    assert '22023' in hits(mode='precision', fusion='rrf', ok=False)
    bad = vectors(); bad['tokens']['model_version'] = 'r2'
    assert '22023' in hits(queries=bad, ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT * FROM qdrant.search('token_docs','alpha','maxsim',3,'"+json.dumps(vectors())+"')", ok=False)
    budget = vectors('budget', [[1]+[0]*127]*128)
    assert 'scalar-work budget' in hits('precision', budget, cap=1000, top=1, ok=False)
    rejected = hits('maxsim', budget, ok=False)
    assert 'scalar-work budget' in rejected, rejected[:2000]
    assert len(json.loads(hits('precision', budget, cap=1, top=1))) == 1
    explain = json.loads(sql("SELECT qdrant.explain_search('token_docs','alpha','precision',1,'"+json.dumps(vectors())+"','{\"candidate_limit\":1}')"))
    assert explain['maxsim_contract']['scalar_work_upper_bound'] == 16
    assert explain['maxsim_contract']['scalar_work_limit'] == 20000000
    checks += ['native token MaxSim matches independent per-token dot-product goldens',
               'native phrase filter precedes cap-one text/dense/sparse/fusion/nested MaxSim truncation and source JOIN',
               'bounded pages preserve every native recall/rerank plan and reject altered valid model queries',
               'precision respects bounded lexical/hybrid candidate domain and excludes higher-scoring outsiders',
               'token shape/model/permission and real native scalar-work budget fail closed']

    old = json.loads(sql("SELECT jsonb_build_object('key',tagged_key,'incarnation',incarnation,'source_fingerprint',fingerprint) "
                        "FROM qdrant_internal.source_state WHERE index_name='token_docs' AND tagged_key->>'value'='1'"))
    ticket = ticket_from(sql("BEGIN; UPDATE token_docs SET body='alpha alpha changed' WHERE id=1; SELECT qdrant.track_changes('token_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    assert '55000' in hits(ok=False)
    assert '55000' in output(old, 'tokens', token_values['1'], ok=False)
    # Inspect native points before the source JOIN/completeness preflight.
    native_query = dict(representation='tokens', **vectors()['tokens'])
    request = "(SELECT jsonb_build_object('operation','source_search','index_id',i.index_id,'generation',i.generation," \
              "'storage_epoch',c.storage_epoch,'q','alpha','top_k',10,'representation_query','"+json.dumps(native_query)+"'::jsonb) " \
              "FROM qdrant_internal.index_catalog i JOIN qdrant_internal.consumer_state c USING(index_name) WHERE i.index_name='token_docs')"
    native = json.loads(sql('SELECT qdrant_internal.p1_search('+request+',5000)'))
    assert {h['payload']['source_key']['value'] for h in native} == set(token_values)-{'1'}, native
    fill(); goldens()
    sql("BEGIN; UPDATE token_docs SET body='rolled back' WHERE id=1; ROLLBACK")
    goldens()
    old = json.loads(sql("SELECT jsonb_build_object('key',tagged_key,'incarnation',incarnation,'source_fingerprint',fingerprint) "
                        "FROM qdrant_internal.source_state WHERE index_name='token_docs' AND tagged_key->>'value'='1'"))
    ticket = ticket_from(sql("BEGIN; DELETE FROM token_docs WHERE id=1; INSERT INTO token_docs(id,body) VALUES(1,'alpha alpha'); SELECT qdrant.track_changes('token_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    assert '55000' in output(old, 'tokens', token_values['1'], ok=False)
    fill(); goldens()
    task = json.loads(sql("SELECT qdrant.rebuild_index('token_docs')"))['task_id']
    assert json.loads(sql("SELECT qdrant.await_task('"+task+"',60000)"))['succeeded']
    goldens()
    checks += ['token source edits, rollback and key reuse reject stale output and retain correct MaxSim',
               'actual token-vector generation build/switch retains standalone and nested precision scores']
    return goldens

"""Named dense BYOV through source DML, real Edge writes and source rechecks."""
import json


def run(sql, ready, ticket_from, checks):
    contract = dict(kind='dense', model_id='fixture-model', model_version='r1',
                    tokenizer='fixture-tokenizer-r1', dimensions=2, distance='dot',
                    normalization='unit', storage_precision='float32', vector_field='embedding',
                    fingerprint_field='embedding_source', incarnation_field='embedding_incarnation',
                    model_id_field='embedding_model', model_version_field='embedding_version')
    settings = {'text': {'fields': ['body']}, 'representations': {'dense': contract}}
    second = dict(contract, model_version='r2', vector_field='embedding2',fingerprint_field='embedding_source2',
                  incarnation_field='embedding_incarnation2',model_id_field='embedding_model2',model_version_field='embedding_version2')
    settings['representations']['second']=second
    sql('CREATE TABLE model_docs(id bigint PRIMARY KEY,body text NOT NULL,embedding jsonb,'
        'embedding_source text,embedding_incarnation uuid,embedding_model text,embedding_version text,ignored text,'
        'embedding2 jsonb,embedding_source2 text,embedding_incarnation2 uuid,embedding_model2 text,embedding_version2 text)')
    sql("INSERT INTO model_docs(id,body,ignored) VALUES(1,'blue whale','private unrelated value'),(2,'orange tree',NULL)")
    bad = json.loads(json.dumps(settings))
    bad['representations']['dense']['dimensions'] = 5000
    assert '0A000' in sql("SELECT qdrant.create_index('bad_model','model_docs','id','"+json.dumps(bad)+"')",ok=False)
    assert sql("SELECT count(*) FROM qdrant_internal.index_catalog WHERE index_name='bad_model'") == '0'
    assert sql("SELECT count(*) FROM pg_trigger WHERE tgrelid='model_docs'::regclass AND NOT tgisinternal") == '0'
    sql("SELECT qdrant.create_index('model_docs','model_docs','id','"+json.dumps(settings)+"')")
    ready('model_docs')
    requests = json.loads(sql("SELECT qdrant.encoding_inputs('model_docs','dense')"))
    assert len(requests) == 2 and all(r['model'] == contract for r in requests)
    assert all('ignored' not in r for r in requests)
    query = json.dumps({'dense': {'model_id': 'fixture-model', 'model_version': 'r1', 'vector': [1, 0]}})

    def semantic():
        return sql("SELECT coalesce(jsonb_agg(source_key ORDER BY rank),'[]') FROM qdrant.search('model_docs','model','semantic',10,'"+query+"')")

    assert '55000' in sql("SELECT * FROM qdrant.search('model_docs','model','semantic',10,'"+query+"')",ok=False)

    def write_output(request, vector, ok=True):
        return sql("BEGIN; UPDATE model_docs SET embedding='"+json.dumps(vector)+"',embedding_source='"+
                   request['source_fingerprint']+"',embedding_incarnation='"+request['incarnation']+
                   "',embedding_model='fixture-model',embedding_version='r1' WHERE id="+request['key']['value']+
                   "; SELECT qdrant.track_changes('model_docs'); COMMIT",ok=ok)

    for request in requests:
        vector = [1, 0] if request['key']['value'] == '1' else [0, 1]
        ticket = ticket_from(write_output(request, vector))
        assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    assert semantic() == '["1", "2"]',semantic()
    status = json.loads(sql("SELECT qdrant.index_status('model_docs')"))['representations']['dense']
    assert status['contract']==contract and status['rows']['ready']==2,status
    assert sql("SELECT count(*) FROM qdrant_internal.representation_state WHERE index_name='model_docs' AND representation_fingerprint ~ '^[0-9a-f]{64}$'")=='2'
    assert sql("SELECT count(*) FROM qdrant_internal.outbox WHERE index_name='model_docs' AND projection::text LIKE '%private unrelated value%'")=='0'
    checks += ['declared dense contracts, failed registration rollback, encoding inputs and real Edge semantic SQL']

    # A text-only update retains source model columns but removes their vector.
    old = next(r for r in requests if r['key']['value']=='1')
    ticket = ticket_from(sql("BEGIN; UPDATE model_docs SET body='purple whale' WHERE id=1; SELECT qdrant.track_changes('model_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    status=json.loads(sql("SELECT qdrant.index_status('model_docs')"))['representations']['dense']['rows']
    assert status['stale']==1 and status['ready']==1,status
    assert '55000' in write_output(old,[1,0],ok=False)
    secondary=json.loads(sql("SELECT qdrant.encoding_inputs('model_docs','second')"))[0]
    ticket=ticket_from(sql("BEGIN; UPDATE model_docs SET embedding2='[0,1]',embedding_source2='"+
       secondary['source_fingerprint']+"',embedding_incarnation2='"+secondary['incarnation']+
       "',embedding_model2='fixture-model',embedding_version2='r2' WHERE id="+secondary['key']['value']+
       "; SELECT qdrant.track_changes('model_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    assert sql("SELECT count(*) FROM qdrant_internal.representation_state WHERE index_name='model_docs' AND name='second' AND state='ready'")=='1'
    assert '55000' in sql("SELECT * FROM qdrant.search('model_docs','model','semantic',10,'"+query+"')",ok=False)
    # Inspect actual native candidates; SQL admission cannot hide a retained slot.
    native = "SELECT qdrant_internal.p1_search((SELECT jsonb_build_object('operation','source_search','index_id',i.index_id,'generation',i.generation,'storage_epoch',c.storage_epoch,'q','model','top_k',10,'dense_query',jsonb_build_object('representation','dense','model_id','fixture-model','model_version','r1','vector','[1,0]'::jsonb)) FROM qdrant_internal.index_catalog i JOIN qdrant_internal.consumer_state c USING(index_name) WHERE i.index_name='model_docs'),10000)"
    candidates=json.loads(sql(native))
    assert [h['payload']['source_key']['value'] for h in candidates]==['2'],candidates
    current=json.loads(sql("SELECT qdrant.encoding_inputs('model_docs','dense')"))[0]
    assert current['text']=='purple whale' and current['source_fingerprint']!=old['source_fingerprint']
    assert '22023' in write_output(current,[1,0,0],ok=False)
    assert '22023' in write_output(current,[2,0],ok=False)
    assert '22023' in write_output(current,[1e100,0],ok=False)
    wrong=query.replace('"r1"','"r2"')
    assert '22023' in sql("SELECT * FROM qdrant.search('model_docs','model','semantic',10,'"+wrong+"')",ok=False)
    ticket=ticket_from(write_output(current,[1,0]))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    assert semantic()=='["1", "2"]'
    checks += ['text changes mark dense stale, native vector removal, late/dimension/norm/range/model rejection',
               'independent named model output does not require unrelated stale slots to be ready']

    # DELETE/reuse must fence both old model completions and old native points.
    ticket=ticket_from(sql("BEGIN; DELETE FROM model_docs WHERE id=1; INSERT INTO model_docs(id,body) VALUES(1,'new whale'); SELECT qdrant.track_changes('model_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    assert '55000' in write_output(current,[1,0],ok=False)
    current=json.loads(sql("SELECT qdrant.encoding_inputs('model_docs','dense')"))[0]
    assert current['incarnation']!=old['incarnation']
    ticket=ticket_from(write_output(current,[1,0]))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    assert semantic()=='["1", "2"]'
    checks += ['dense output incarnation fencing across DELETE/key reuse']

    # Failed model writes roll back the source and all representation metadata.
    revision=sql("SELECT revision FROM qdrant_internal.source_state WHERE index_name='model_docs' AND tagged_key->>'value'='1'")
    sql("BEGIN; UPDATE model_docs SET embedding=NULL WHERE id=1; ROLLBACK")
    assert sql("SELECT revision FROM qdrant_internal.source_state WHERE index_name='model_docs' AND tagged_key->>'value'='1'")==revision
    assert semantic()=='["1", "2"]'
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant.encoding_inputs('model_docs','dense')",ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT * FROM qdrant.search('model_docs','model','semantic',10,'"+query+"')",ok=False)
    checks += ['dense rollback and unauthorized encoding/search rejection']

    # The declared maximum dimension must be usable across both IPC boundaries.
    wide=dict(contract,dimensions=4096)
    sql('CREATE TABLE wide_docs (LIKE model_docs INCLUDING ALL)')
    sql("INSERT INTO wide_docs(id,body) VALUES(1,'wide representation')")
    wide_slots={'dense':wide, **{metric:dict(wide,distance=metric) for metric in ['cosine','euclid','manhattan']}}
    sql("SELECT qdrant.create_index('wide_docs','wide_docs','id','"+json.dumps({'text':{'fields':['body']},'representations':wide_slots})+"')")
    ready('wide_docs')
    wide_request=json.loads(sql("SELECT qdrant.encoding_inputs('wide_docs','dense')"))[0]
    vector=json.dumps([0.015625]*4096)
    ticket=ticket_from(sql("BEGIN; UPDATE wide_docs SET embedding='"+vector+"',embedding_source='"+
        wide_request['source_fingerprint']+"',embedding_incarnation='"+wide_request['incarnation']+
        "',embedding_model='fixture-model',embedding_version='r1' WHERE id=1; SELECT qdrant.track_changes('wide_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    wide_query=json.dumps({'dense':{'model_id':'fixture-model','model_version':'r1','vector':[0.015625]*4096}})
    assert len(wide_query)>16384
    assert sql("SELECT source_key FROM qdrant.search('wide_docs','wide','semantic',10,'"+wide_query+"')")=='1'
    for metric, expected in [('dense',1),('cosine',1),('euclid',0),('manhattan',0)]:
        metric_query=json.dumps({metric:{'model_id':'fixture-model','model_version':'r1','vector':[0.015625]*4096}})
        score=float(sql("SELECT score FROM qdrant.search('wide_docs','wide','semantic',10,'"+metric_query+"')"))
        assert abs(score-expected)<0.0001,(metric,score)
    sql("SELECT qdrant.drop_index('wide_docs')")
    checks += ['4096-dimensional model output and native query use bounded source/search IPC profiles',
               'native dot, cosine, Euclidean and Manhattan distances match declared dense schemas']

    def replayed():
        ready('model_docs')
        assert semantic()=='["1", "2"]'
    return replayed

"""Installed native MMR rank, original score, source security and retained owner."""
import json
import math
import os
import signal
import time


def run(sql,ready,ticket_from,checks,spawn=None):
    vectors={'1':[1,0],'2':[0,1],'3':[.9,.1],'4':[.1,.9],'5':[.5,.5],'6':[-1,0],'7':[0,0]}
    literal=lambda value:"'"+value.replace("'","''")+"'"
    def query(value=.25):
        return dict(dense=dict(model_id='recommendation',model_version='r1',strategy='mmr',target='3',**{'lambda':value}))
    def statement(request=None,options=None,top_k=7,expression="jsonb_agg(jsonb_build_object('key',source_key,'score',score,'rank',rank,'provenance',provenance) ORDER BY rank)"):
        return 'SELECT '+expression+" FROM qdrant.search('recommendation_docs','','explore',"+str(top_k)+','+literal(json.dumps(request or query()))+','+literal(json.dumps(options or dict(candidate_limit=7)))+')'
    def replay():
        dot=lambda a,b:sum(x*y for x,y in zip(a,b))
        for weight in [0,.25,1]:
            hits=json.loads(sql(statement(query(weight))))
            remaining=set(vectors)-{'3'};selected=[]
            assert len(hits)==6 and [h['rank'] for h in hits]==list(range(1,7)),hits
            for hit in hits:
                key=hit['key'];assert key in remaining
                def objective(k):
                    relevance=dot(vectors[k],vectors['3'])
                    return relevance if not selected else weight*relevance-(1-weight)*max(dot(vectors[k],vectors[s]) for s in selected)
                assert math.isclose(objective(key),max(objective(k) for k in remaining),abs_tol=1e-5),(weight,hits,key)
                assert math.isclose(hit['score'],dot(vectors[key],vectors['3']),abs_tol=1e-5),hit
                assert 'vector' not in hit and 'vector' not in hit['provenance']
                remaining.remove(key);selected.append(key)
        bounded=json.loads(sql(statement(options=dict(candidate_limit=3),top_k=3)))
        assert [h['key'] for h in bounded]==['1','4','5'],bounded
        assert bounded[1]['score']<bounded[2]['score'],bounded
    ready('recommendation_docs');replay()
    assert sql(statement(options=dict(candidate_limit=1,matching=dict(key_exact='4')),top_k=1,expression='source_key'))=='4'
    assert sql(statement(options=dict(candidate_limit=1,matching=dict(key_exact='3')),top_k=1,expression='count(*)'))=='0'
    plan=json.loads(sql("SELECT qdrant.explain_search('recommendation_docs','','explore',7,"+literal(json.dumps(query()))+",'{\"candidate_limit\":7}')"))
    assert plan['effective_plan']=='explore_dense_mmr' and plan['mmr_contract']['candidate_limit']==7
    assert 'not descending score' in plan['mmr_contract']['score_domain'] and plan['seed_digest']
    assert [v['role'] for v in plan['mmr_contract']['seed_versions']]==['target']
    registry=json.loads(sql("SELECT qdrant.capabilities('recommendation_docs')"))
    explore=next(m for m in registry['query_modes'] if m['mode']=='explore')
    assert 'Q07' in explore['capability_ids'] and explore['admission_ready'] and not explore['native_query_executed']
    checks.append('native dense MMR independent greedy objective, candidate-domain truncation, original scores and rank preservation with before-cap predicates/target exclusion')

    for field,value in [('target',None),('target','unknown'),('target',[1,0]),('lambda',-.01),('lambda',1.01),('lambda','NaN'),('lambda',1e100),('vector',[1,0]),('tenant_id','caller'),('model_version','wrong')]:
        request=query();request['dense'][field]=value
        error=sql(statement(request),ok=False);assert any(code in error for code in ['22023','55000']),error
    assert '42501' in sql('SET ROLE pgq_writer; '+statement(),ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.admit_mmr('recommendation_docs','{}')",ok=False)
    assert '0A000' in sql('BEGIN; ALTER TABLE recommendation_docs ENABLE ROW LEVEL SECURITY; '+statement(),ok=False)
    assert '55000' in sql('BEGIN; DELETE FROM recommendation_docs WHERE id=3; '+statement(),ok=False)
    assert '55000' in sql("BEGIN; DELETE FROM recommendation_docs WHERE id=3; INSERT INTO recommendation_docs(id,body) VALUES(3,'new incarnation'); "+statement(),ok=False)
    checks.append('MMR strict target/model/lambda envelopes, private admission, permissions, RLS and incarnation negatives')

    contract=dict(kind='dense',model_id='recommendation',model_version='r1',tokenizer='fixture',dimensions=2,
        distance='dot',normalization='none',storage_precision='float32',vector_field='v',fingerprint_field='fp',
        incarnation_field='inc',model_id_field='model',model_version_field='version')
    for key_type,keys in [('text',["target ' / one",'candidate / two','candidate / three','candidate / four']),
                          ('uuid',['00000000-0000-0000-0000-00000000000'+str(n) for n in range(1,5)])]:
        name='mmr_'+key_type
        sql('CREATE TABLE '+name+'(id '+key_type+' PRIMARY KEY,body text NOT NULL,v jsonb,fp text,inc uuid,model text,version text)')
        sql('INSERT INTO '+name+'(id,body) VALUES'+','.join('('+literal(k)+",'mmr anchor')" for k in keys))
        sql("SELECT qdrant.create_index('"+name+"','"+name+"','id',"+literal(json.dumps(dict(text=dict(fields=['body']),representations=dict(dense=contract))))+')');ready(name)
        for row in json.loads(sql("SELECT qdrant.encoding_inputs('"+name+"','dense')")):
            sql("UPDATE "+name+" SET v='[1,0]',fp='"+row['source_fingerprint']+"',inc='"+row['incarnation']+"',model='recommendation',version='r1' WHERE id="+literal(row['key']['value']))
        ready(name);request=query(1);request['dense']['target']=keys[0]
        hits=json.loads(sql("SELECT jsonb_agg(jsonb_build_object('key',source_key,'score',score)) FROM qdrant.search('"+name+"','','explore',3,"+literal(json.dumps(request))+",'{\"candidate_limit\":3}')"))
        assert {h['key'] for h in hits}==set(keys[1:]) and all(h['score']==1 for h in hits),(key_type,hits)
        sql("SELECT qdrant.drop_index('"+name+"')")
    checks.append('MMR resolves actual canonical UUID and quoted text targets and excludes them before candidates')

    page="SELECT qdrant.search_page('recommendation_docs','','explore',2,"+literal(json.dumps(query()))+",'{\"candidate_limit\":7}'"
    first=json.loads(sql(page+')'));assert [h['source_key'] for h in first['hits']]==['1','6'],first
    continuation=page+','+literal(json.dumps(first['next_cursor']))+')'
    assert len(json.loads(sql(continuation))['hits'])==2
    before=sql("SELECT jsonb_agg(jsonb_build_object('key',point_id,'rev',revision) ORDER BY point_id) FROM qdrant_internal.source_state WHERE index_name='recommendation_docs' AND tagged_key->>'value'<>'3'")
    sql("UPDATE recommendation_docs SET v='[0.8,0.2]' WHERE id=3");ready('recommendation_docs')
    assert '55000' in sql(continuation,ok=False)
    assert before==sql("SELECT jsonb_agg(jsonb_build_object('key',point_id,'rev',revision) ORDER BY point_id) FROM qdrant_internal.source_state WHERE index_name='recommendation_docs' AND tagged_key->>'value'<>'3'")
    sql("UPDATE recommendation_docs SET v='[0.9,0.1]' WHERE id=3");ready('recommendation_docs');replay()
    checks.append('MMR paging preserves native selection rank and refuses excluded-target-only mutation')

    if spawn is not None:
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'));pid=owner['engine_pid']
        for change in [False,True]:
            os.kill(pid,signal.SIGSTOP)
            try:
                application='pgq_mmr_change' if change else 'pgq_mmr_cancel'
                waiting=spawn(statement(),application);end=time.monotonic()+10
                while time.monotonic()<end:
                    busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    if (busy.get('active') or {}).get('operation')=='source_search':break
                    time.sleep(.02)
                assert (busy.get('active') or {}).get('operation')=='source_search',busy
                if change:sql("UPDATE recommendation_docs SET v='[0.5,0.5]' WHERE id=3")
                else:
                    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='"+application+"'")=='t'
                    out,err=waiting.communicate(timeout=10);assert waiting.returncode and '57014' in err,(out,err)
                    retained=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    assert retained['engine_instance']==owner['engine_instance'] and retained['engine_pid']==pid
                    assert (retained.get('active') or {}).get('operation')=='source_search',retained
            finally:os.kill(pid,signal.SIGCONT)
            if change:
                out,err=waiting.communicate(timeout=10);assert waiting.returncode and '55000' in err,(out,err)
                sql("UPDATE recommendation_docs SET v='[0.9,0.1]' WHERE id=3")
            end=time.monotonic()+10
            while time.monotonic()<end:
                completed=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if not completed.get('active'):break
                time.sleep(.02)
            assert not completed.get('active'),completed
            ready('recommendation_docs');replay()
        checks.append('actual MMR cancellation retains native ownership and concurrent target mutation refuses the complete in-flight response')
    return replay

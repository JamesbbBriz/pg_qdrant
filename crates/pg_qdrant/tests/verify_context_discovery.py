"""Actual source-key Discover/Context execution and seed freshness."""
import json
import math
import os
import signal
import time


def run(sql,ready,ticket_from,checks,spawn=None):
    # Reuse the committed source/model fixture, retaining its Q04 regressions.
    vectors={'1':[1,0],'2':[0,1],'3':[.9,.1],'4':[.1,.9],'5':[.5,.5],'6':[-1,0],'7':[0,0]}
    def query(strategy='discover',pairs=None):
        input=dict(model_id='recommendation',model_version='r1',strategy=strategy,
                   context=pairs or [dict(positive='1',negative='2')])
        if strategy=='discover': input['target']='3'
        return dict(dense=input)
    def statement(request=None,options=None,top_k=10,expression='jsonb_agg(jsonb_build_object(\'key\',source_key,\'score\',score) ORDER BY rank)'):
        return "SELECT "+expression+" FROM qdrant.search('recommendation_docs','','explore',"+str(top_k)+",'"+json.dumps(request or query())+"','"+json.dumps(options or {})+"')"
    def golden(request):
        input=request['dense']; exclude=set()
        if 'target' in input: exclude.add(input['target'])
        for pair in input['context']: exclude.update(pair.values())
        scores={}
        dot=lambda a,b:sum(x*y for x,y in zip(a,b))
        for key,v in vectors.items():
            if key in exclude: continue
            differences=[dot(v,vectors[pair['positive']])-dot(v,vectors[pair['negative']]) for pair in input['context']]
            if input['strategy']=='discover':
                target=dot(v,vectors[input['target']])
                scores[key]=sum(1 if d>0 else -1 if d<0 else 0 for d in differences)+.5*(target/(1+abs(target))+1)
            else:
                losses=[min(d-2**-23,0) for d in differences]
                scores[key]=sum(loss/(1+abs(loss)) for loss in losses)
        return scores
    def replay():
        for strategy in ['discover','context']:
            request=query(strategy); expected=golden(request)
            hits=json.loads(sql(statement(request)))
            assert {h['key'] for h in hits}==set(expected),(strategy,hits)
            for hit in hits:
                assert math.isclose(hit['score'],expected[hit['key']],abs_tol=5e-6),(strategy,hit,expected)
            assert all(a['score']>=b['score'] for a,b in zip(hits,hits[1:]))
    ready('recommendation_docs'); replay()
    for strategy in ['discover','context']:
        assert sql(statement(query(strategy),dict(matching=dict(key_exact='4')),1,'source_key'))=='4'
        assert sql(statement(query(strategy),dict(matching=dict(key_exact='1')),1,'count(*)'))=='0'
        plan=json.loads(sql("SELECT qdrant.explain_search('recommendation_docs','','explore',10,'"+json.dumps(query(strategy))+"')"))
        assert plan['effective_plan']=='explore_dense_'+strategy and plan['discovery_contract']['strategy']==strategy
        assert plan['seed_digest'] and all('role' in v for v in plan['discovery_contract']['seed_versions'])
    for strategy in ['discover','context']:
        request=query(strategy,[dict(positive='1',negative='2'),dict(positive='5',negative='6')])
        expected=golden(request)
        hits=json.loads(sql(statement(request)))
        assert {h['key'] for h in hits}==set(expected)
        assert all(math.isclose(h['score'],expected[h['key']],abs_tol=5e-6) for h in hits),(strategy,hits,expected)
    checks.append('native dense Discover/Context independent Dot goldens, multiple context pairs and before-cap source constraints/all-seed exclusion')

    invalids=[]
    for field,value in [('target',None),('context',[]),('vector',[1,0]),('tenant_id','caller'),('model_version','wrong')]:
        request=query();request['dense'][field]=value;invalids.append(request)
    request=query('context');request['dense']['target']='3';invalids.append(request)
    request=query();request['dense']['context']=[dict(positive='1',negative='1')];invalids.append(request)
    request=query();request['dense']['context'][0]['unknown']='1';invalids.append(request)
    request=query();request['dense']['target']='unknown';invalids.append(request)
    request=query();request['dense']['context']=[dict(positive='1',negative='2')]*16;invalids.append(request)
    for request in invalids:
        error=sql(statement(request),ok=False)
        assert any(code in error for code in ['22023','55000']),error
    assert '42501' in sql('SET ROLE pgq_writer; '+statement(),ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.admit_discovery('recommendation_docs','{}')",ok=False)
    assert '0A000' in sql('BEGIN; ALTER TABLE recommendation_docs ENABLE ROW LEVEL SECURITY; '+statement(),ok=False)
    assert '55000' in sql('BEGIN; DELETE FROM recommendation_docs WHERE id=3; '+statement(),ok=False)
    assert '55000' in sql("BEGIN; DELETE FROM recommendation_docs WHERE id=3; INSERT INTO recommendation_docs(id,body) VALUES(3,'new incarnation'); "+statement(),ok=False)
    checks.append('discovery model/envelope, private admission, role/RLS and current target incarnation negatives')

    contract=dict(kind='dense',model_id='recommendation',model_version='r1',tokenizer='fixture',dimensions=2,
        distance='dot',normalization='none',storage_precision='float32',vector_field='v',fingerprint_field='fp',
        incarnation_field='inc',model_id_field='model',model_version_field='version')
    literal=lambda value:"'"+value.replace("'","''")+"'"
    for key_type,keys in [('text',["target ' / one",'positive / two','negative / three','candidate / four']),
                          ('uuid',['00000000-0000-0000-0000-00000000000'+str(n) for n in range(1,5)])]:
        name='context_'+key_type
        sql('CREATE TABLE '+name+'(id '+key_type+' PRIMARY KEY,body text NOT NULL,v jsonb,fp text,inc uuid,model text,version text)')
        sql('INSERT INTO '+name+'(id,body) VALUES'+','.join('('+literal(k)+",'context anchor')" for k in keys))
        settings=dict(text=dict(fields=['body']),representations=dict(dense=contract))
        sql("SELECT qdrant.create_index('"+name+"','"+name+"','id','"+json.dumps(settings)+"')");ready(name)
        for row in json.loads(sql("SELECT qdrant.encoding_inputs('"+name+"','dense')")):
            sql("UPDATE "+name+" SET v='[1,0]',fp='"+row['source_fingerprint']+"',inc='"+row['incarnation']+"',model='recommendation',version='r1' WHERE id="+literal(row['key']['value']))
        ready(name)
        typed=dict(dense=dict(model_id='recommendation',model_version='r1',strategy='discover',target=keys[0],context=[dict(positive=keys[1],negative=keys[2])]))
        hits=json.loads(sql("SELECT jsonb_agg(jsonb_build_object('key',source_key,'score',score)) FROM qdrant.search('"+name+"','','explore',1,"+literal(json.dumps(typed))+")"))
        assert hits==[dict(key=keys[3],score=.75)],(key_type,hits)
        sql("SELECT qdrant.drop_index('"+name+"')")
    checks.append('actual discovery resolves canonical UUID and quoted text source keys through current authorized models')

    wide=dict(contract,dimensions=1024)
    sql('CREATE TABLE context_wide(id bigint PRIMARY KEY,body text NOT NULL,v jsonb,fp text,inc uuid,model text,version text)')
    sql("INSERT INTO context_wide(id,body) SELECT n,'context anchor '||n FROM generate_series(1,32)n")
    sql("SELECT qdrant.create_index('context_wide','context_wide','id','"+json.dumps(dict(text=dict(fields=['body']),representations=dict(dense=wide)))+"')");ready('context_wide')
    for row in json.loads(sql("SELECT qdrant.encoding_inputs('context_wide','dense')")):
        sql("UPDATE context_wide SET v='"+json.dumps([1]+[0]*1023)+"',fp='"+row['source_fingerprint']+"',inc='"+row['incarnation']+"',model='recommendation',version='r1' WHERE id="+row['key']['value'])
    ready('context_wide')
    oversized=query('context',[dict(positive=str(n),negative=str(n+1)) for n in range(1,33,2)])
    assert '54000' in sql("SELECT * FROM qdrant.search('context_wide','','explore',10,'"+json.dumps(oversized)+"')",ok=False)
    sql("SELECT qdrant.drop_index('context_wide')")
    checks.append('actual 32 distinct context source vectors enforce the resolved 64 KiB bound')

    page="SELECT qdrant.search_page('recommendation_docs','','explore',1,'"+json.dumps(query())+"','{\"candidate_limit\":4}'"
    first=json.loads(sql(page+')')); assert first['next_cursor']
    continuation=page+",'"+json.dumps(first['next_cursor'])+"')"
    assert len(json.loads(sql(continuation))['hits'])==1
    before=sql("SELECT jsonb_agg(jsonb_build_object('key',point_id,'rev',revision) ORDER BY point_id) FROM qdrant_internal.source_state WHERE index_name='recommendation_docs' AND tagged_key->>'value'<>'3'")
    sql("UPDATE recommendation_docs SET v='[0.8,0.2]' WHERE id=3");ready('recommendation_docs')
    error=sql(continuation,ok=False); assert '55000' in error,error
    assert before==sql("SELECT jsonb_agg(jsonb_build_object('key',point_id,'rev',revision) ORDER BY point_id) FROM qdrant_internal.source_state WHERE index_name='recommendation_docs' AND tagged_key->>'value'<>'3'")
    sql("UPDATE recommendation_docs SET v='[0.9,0.1]' WHERE id=3");ready('recommendation_docs');replay()
    checks.append('excluded discovery target mutation invalidates cached paging even when every captured candidate identity is unchanged')

    if spawn is not None:
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'));pid=owner['engine_pid']
        for change in [False,True]:
            os.kill(pid,signal.SIGSTOP)
            try:
                application='pgq_context_change' if change else 'pgq_context_cancel'
                waiting=spawn(statement(query('context')),application)
                end=time.monotonic()+10
                while time.monotonic()<end:
                    busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    if (busy.get('active') or {}).get('operation')=='source_search': break
                    time.sleep(.02)
                assert (busy.get('active') or {}).get('operation')=='source_search',busy
                if change: sql("UPDATE recommendation_docs SET v='[0.5,0.5]' WHERE id=1")
                else:
                    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='"+application+"'")=='t'
                    out,err=waiting.communicate(timeout=10)
                    assert waiting.returncode and '57014' in err,(out,err)
                    retained=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    assert retained['engine_instance']==owner['engine_instance'] and retained['engine_pid']==pid
                    assert (retained.get('active') or {}).get('operation')=='source_search',retained
            finally: os.kill(pid,signal.SIGCONT)
            if change:
                out,err=waiting.communicate(timeout=10);assert waiting.returncode and '55000' in err,(out,err)
                sql("UPDATE recommendation_docs SET v='[1,0]' WHERE id=1")
            end=time.monotonic()+10
            while time.monotonic()<end:
                completed=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if not completed.get('active'):break
                time.sleep(.02)
            assert not completed.get('active'),completed
            ready('recommendation_docs');replay()
        checks.append('actual context SQL cancellation retains the native owner; concurrent excluded context-seed mutation refuses complete in-flight results')
    return replay

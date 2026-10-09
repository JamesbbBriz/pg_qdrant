"""Actual source-key native feedback scoring, ownership and seed freshness."""
import json
import math
import os
import signal
import time


def run(sql,ready,ticket_from,checks,spawn=None):
    vectors={'1':[1,0],'2':[0,1],'3':[.9,.1],'4':[.1,.9],'5':[.5,.5],'6':[-1,0],'7':[0,0]}
    def query(scores=(2,0,-1),coefficients=None):
        return dict(dense=dict(model_id='recommendation',model_version='r1',strategy='feedback',target='3',
            feedback=[dict(key=k,score=score) for k,score in zip(['1','2','5'],scores)],
            coefficients=coefficients or dict(a=1,b=2,c=.25)))
    literal=lambda value:"'"+value.replace("'","''")+"'"
    def statement(request=None,options=None,top_k=10,expression="jsonb_agg(jsonb_build_object('key',source_key,'score',score) ORDER BY rank)"):
        return 'SELECT '+expression+" FROM qdrant.search('recommendation_docs','','explore',"+str(top_k)+','+literal(json.dumps(request or query()))+','+literal(json.dumps(options or {}))+')'
    def golden(request):
        input=request['dense'];c=input['coefficients'];exclude={input['target']}|{item['key'] for item in input['feedback']}
        dot=lambda a,b:sum(x*y for x,y in zip(a,b))
        result={}
        for key,v in vectors.items():
            if key in exclude:continue
            score=c['a']*dot(v,vectors[input['target']])
            for p in input['feedback']:
                for n in input['feedback']:
                    confidence=p['score']-n['score']
                    if confidence>0:score+=confidence**c['b']*c['c']*(dot(v,vectors[p['key']])-dot(v,vectors[n['key']]))
            result[key]=score
        return result
    def replay():
        for request in [query(),query((1,1,1)),query((1,)),query(coefficients=dict(a=-1,b=0,c=-.25)),
                        query((16,-16,0),dict(a=16,b=4,c=16))]:
            expected=golden(request);hits=json.loads(sql(statement(request)))
            assert {h['key'] for h in hits}==set(expected),(hits,expected)
            assert all(math.isclose(h['score'],expected[h['key']],rel_tol=1e-5,abs_tol=1e-5) for h in hits),(hits,expected)
            assert all(a['score']>=b['score'] for a,b in zip(hits,hits[1:]))
    ready('recommendation_docs');replay()
    registry=json.loads(sql("SELECT qdrant.capabilities('recommendation_docs')"))
    explore=next(mode for mode in registry['query_modes'] if mode['mode']=='explore')
    assert {'Q04','Q05','Q06'}<=set(explore['capability_ids']) and explore['admission_ready']
    assert not explore['release_supported'] and not explore['native_query_executed']
    assert sql(statement(options=dict(matching=dict(key_exact='4')),top_k=1,expression='source_key'))=='4'
    assert sql(statement(options=dict(matching=dict(key_exact='1')),top_k=1,expression='count(*)'))=='0'
    plan=json.loads(sql("SELECT qdrant.explain_search('recommendation_docs','','explore',10,"+literal(json.dumps(query()))+')'))
    assert plan['effective_plan']=='explore_dense_feedback' and plan['feedback_contract']['coefficients']==query()['dense']['coefficients']
    assert plan['seed_digest'] and [v['role'] for v in plan['feedback_contract']['seed_versions']]==['target','feedback','feedback','feedback']
    checks.append('native dense feedback independent multipair/equal/single-rating and bounded coefficient goldens with before-cap filters/all-example exclusion')

    invalid=[]
    for field,value in [('target',None),('feedback',[]),('coefficients',dict(a=1,b=2)),('vector',[1,0]),('tenant_id','caller'),('model_version','wrong')]:
        request=query();request['dense'][field]=value;invalid.append(request)
    for field,value in [('a',16.01),('b',-1),('b',4.01),('c',-16.01),('a','NaN'),('b',1e100)]:
        request=query();request['dense']['coefficients'][field]=value;invalid.append(request)
    for value in [16.01,-16.01,'NaN',1e100]:
        request=query();request['dense']['feedback'][0]['score']=value;invalid.append(request)
    request=query();request['dense']['feedback'][0]['tenant_id']='caller';invalid.append(request)
    request=query();request['dense']['feedback'][0]['key']='3';invalid.append(request)
    request=query();request['dense']['feedback'][0]['key']='unknown';invalid.append(request)
    request=query();request['dense']['feedback']*=11;invalid.append(request)
    for request in invalid:
        error=sql(statement(request),ok=False);assert any(code in error for code in ['22023','55000']),error
    assert '42501' in sql('SET ROLE pgq_writer; '+statement(),ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.admit_feedback('recommendation_docs','{}')",ok=False)
    assert '0A000' in sql('BEGIN; ALTER TABLE recommendation_docs ENABLE ROW LEVEL SECURITY; '+statement(),ok=False)
    assert '55000' in sql('BEGIN; DELETE FROM recommendation_docs WHERE id=3; '+statement(),ok=False)
    assert '55000' in sql("BEGIN; DELETE FROM recommendation_docs WHERE id=3; INSERT INTO recommendation_docs(id,body) VALUES(3,'new incarnation'); "+statement(),ok=False)
    checks.append('feedback strict model/source/role/RLS/incarnation admission and score/coefficient/envelope numeric negatives')

    contract=dict(kind='dense',model_id='recommendation',model_version='r1',tokenizer='fixture',dimensions=2,
        distance='dot',normalization='none',storage_precision='float32',vector_field='v',fingerprint_field='fp',
        incarnation_field='inc',model_id_field='model',model_version_field='version')
    for key_type,keys in [('text',["target ' / one",'positive / two','negative / three','candidate / four']),
                          ('uuid',['00000000-0000-0000-0000-00000000000'+str(n) for n in range(1,5)])]:
        name='feedback_'+key_type
        sql('CREATE TABLE '+name+'(id '+key_type+' PRIMARY KEY,body text NOT NULL,v jsonb,fp text,inc uuid,model text,version text)')
        sql('INSERT INTO '+name+'(id,body) VALUES'+','.join('('+literal(k)+",'feedback anchor')" for k in keys))
        sql("SELECT qdrant.create_index('"+name+"','"+name+"','id',"+literal(json.dumps(dict(text=dict(fields=['body']),representations=dict(dense=contract))))+')');ready(name)
        for row in json.loads(sql("SELECT qdrant.encoding_inputs('"+name+"','dense')")):
            sql("UPDATE "+name+" SET v='[1,0]',fp='"+row['source_fingerprint']+"',inc='"+row['incarnation']+"',model='recommendation',version='r1' WHERE id="+literal(row['key']['value']))
        ready(name)
        request=query((2,0));request['dense']['target']=keys[0]
        for item,key in zip(request['dense']['feedback'],keys[1:3]):item['key']=key
        hits=json.loads(sql("SELECT jsonb_agg(jsonb_build_object('key',source_key,'score',score)) FROM qdrant.search('"+name+"','','explore',1,"+literal(json.dumps(request))+')'))
        assert hits==[dict(key=keys[3],score=1)],(key_type,hits)
        sql("SELECT qdrant.drop_index('"+name+"')")
    checks.append('feedback resolves actual canonical UUID and quoted text source keys through current authorized models')

    name='feedback_wide';wide=dict(contract,dimensions=1024)
    sql('CREATE TABLE '+name+'(id bigint PRIMARY KEY,body text NOT NULL,v jsonb,fp text,inc uuid,model text,version text)')
    sql("INSERT INTO "+name+"(id,body) SELECT n,'feedback anchor '||n FROM generate_series(1,32)n")
    sql("SELECT qdrant.create_index('"+name+"','"+name+"','id',"+literal(json.dumps(dict(text=dict(fields=['body']),representations=dict(dense=wide))))+')');ready(name)
    for row in json.loads(sql("SELECT qdrant.encoding_inputs('"+name+"','dense')")):
        sql("UPDATE "+name+" SET v='"+json.dumps([1]+[0]*1023)+"',fp='"+row['source_fingerprint']+"',inc='"+row['incarnation']+"',model='recommendation',version='r1' WHERE id="+row['key']['value'])
    ready(name);request=query();request['dense']['target']='1'
    request['dense']['feedback']=[dict(key=str(n),score=n/2) for n in range(2,33)]
    assert '54000' in sql("SELECT * FROM qdrant.search('"+name+"','','explore',10,"+literal(json.dumps(request))+')',ok=False)
    sql("SELECT qdrant.drop_index('"+name+"')")
    checks.append('actual 32 distinct resolved feedback vectors enforce the 64 KiB admission bound')

    page="SELECT qdrant.search_page('recommendation_docs','','explore',1,"+literal(json.dumps(query()))+",'{\"candidate_limit\":3}'"
    first=json.loads(sql(page+')'));assert first['next_cursor']
    continuation=page+','+literal(json.dumps(first['next_cursor']))+')'
    assert len(json.loads(sql(continuation))['hits'])==1
    before=sql("SELECT jsonb_agg(jsonb_build_object('key',point_id,'rev',revision) ORDER BY point_id) FROM qdrant_internal.source_state WHERE index_name='recommendation_docs' AND tagged_key->>'value'<>'3'")
    sql("UPDATE recommendation_docs SET v='[0.8,0.2]' WHERE id=3");ready('recommendation_docs')
    assert '55000' in sql(continuation,ok=False)
    assert before==sql("SELECT jsonb_agg(jsonb_build_object('key',point_id,'rev',revision) ORDER BY point_id) FROM qdrant_internal.source_state WHERE index_name='recommendation_docs' AND tagged_key->>'value'<>'3'")
    sql("UPDATE recommendation_docs SET v='[0.9,0.1]' WHERE id=3");ready('recommendation_docs');replay()
    checks.append('excluded feedback target mutation invalidates paging while every captured candidate remains unchanged')

    if spawn is not None:
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'));pid=owner['engine_pid']
        for change in [False,True]:
            os.kill(pid,signal.SIGSTOP)
            try:
                application='pgq_feedback_change' if change else 'pgq_feedback_cancel'
                waiting=spawn(statement(),application);end=time.monotonic()+10
                while time.monotonic()<end:
                    busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    if (busy.get('active') or {}).get('operation')=='source_search':break
                    time.sleep(.02)
                assert (busy.get('active') or {}).get('operation')=='source_search',busy
                if change:sql("UPDATE recommendation_docs SET v='[0.5,0.5]' WHERE id=1")
                else:
                    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='"+application+"'")=='t'
                    out,err=waiting.communicate(timeout=10);assert waiting.returncode and '57014' in err,(out,err)
                    retained=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    assert retained['engine_instance']==owner['engine_instance'] and retained['engine_pid']==pid
                    assert (retained.get('active') or {}).get('operation')=='source_search',retained
            finally:os.kill(pid,signal.SIGCONT)
            if change:
                out,err=waiting.communicate(timeout=10);assert waiting.returncode and '55000' in err,(out,err)
                sql("UPDATE recommendation_docs SET v='[1,0]' WHERE id=1")
            end=time.monotonic()+10
            while time.monotonic()<end:
                if not json.loads(sql('SELECT qdrant_internal.p0_ping()')).get('active'):break
                time.sleep(.02)
            assert not json.loads(sql('SELECT qdrant_internal.p0_ping()')).get('active')
            ready('recommendation_docs');replay()
        checks.append('actual feedback SQL cancellation retains native ownership and concurrent excluded feedback-seed mutation refuses the complete in-flight response')
    return replay

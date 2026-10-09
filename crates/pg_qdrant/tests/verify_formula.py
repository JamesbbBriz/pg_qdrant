"""Native score expressions over actual bounded source candidates, not synthetic hits."""
import json
import math
import os
import signal
import time


def run(sql,ready,ticket_from,checks,spawn=None):
    literal=lambda value:"'"+value.replace("'","''")+"'"
    dense=dict(dense=dict(model_id='recommendation',model_version='r1',vector=[1,0]))
    score=dict(op='score');neg=dict(op='negate',arg=score)
    def statement(expression=None,cap=7,top=7,matching=None,request=None):
        options=dict(candidate_limit=cap)
        if expression is not None:options['formula']=expression
        if matching is not None:options['matching']=matching
        return "SELECT coalesce(jsonb_agg(jsonb_build_object('key',source_key,'score',score,'rank',rank,'plan',provenance->>'plan') ORDER BY rank),'[]') FROM qdrant.search('recommendation_docs','anchor','semantic',"+str(top)+','+literal(json.dumps(request or dense))+','+literal(json.dumps(options))+')'
    def replay():
        baseline=json.loads(sql(statement(cap=3,top=3)))
        scored=json.loads(sql(statement(score,cap=3,top=3)))
        negated=json.loads(sql(statement(neg,cap=3,top=3)))
        assert {h['key'] for h in baseline}=={h['key'] for h in scored}=={h['key'] for h in negated}=={'1','3','5'}
        assert [h['key'] for h in negated]==['5','3','1'],negated
        expected={h['key']:h['score'] for h in baseline}
        for hit in negated:
            assert math.isclose(hit['score'],-expected[hit['key']],abs_tol=1e-5),hit
            assert hit['plan']=='semantic_formula'
        assert len(json.loads(sql(statement(neg,cap=1,top=1,matching=dict(key_exact='6')))))==1
        assert json.loads(sql(statement(neg,cap=1,top=1,matching=dict(key_exact='6'))))[0]['key']=='6'
        expr=dict(op='sqrt',arg=dict(op='abs',arg=dict(op='negate',arg=dict(op='add',args=[score,dict(op='constant',value=2)]))))
        original={h['key']:h['score'] for h in json.loads(sql(statement()))}
        for h in json.loads(sql(statement(expr))):
            assert math.isclose(h['score'],math.sqrt(abs(original[h['key']]+2)),abs_tol=1e-5),h
    ready('recommendation_docs');replay()
    plan=json.loads(sql("SELECT qdrant.explain_search('recommendation_docs','anchor','semantic',3,"+literal(json.dumps(dense))+','+literal(json.dumps(dict(candidate_limit=3,formula=neg)))+')'))
    assert plan['formula']==neg and plan['effective_plan']=='semantic_formula'
    assert plan['formula_contract']['max_node_evaluations']==192 and not plan['formula_contract']['payload_fields_available']
    checks.append('native Formula independent score arithmetic, candidate-domain ranking reversal and before-cap mandatory source predicates')

    token_input=dict(model_id='fixture-tokens',model_version='r1',vector=[[1,0],[0,1]])
    dense_input=dict(model_id='fixture-dense',model_version='r1',vector=[1,0])
    sparse_input=dict(model_id='fixture-learned',model_version='r1',vector=dict(indices=[7],values=[1]),
        vocabulary='fixture-vocabulary-r1',idf_revision='not-applied')
    for mode,queries,fusion in [('text',{},None),('sparse',dict(learned=sparse_input),None),
            ('hybrid',dict(dense=dense_input),'rrf'),('hybrid',dict(dense=dense_input),'dbsf'),
            ('maxsim',dict(tokens=token_input),None),('precision',dict(tokens=token_input),None),
            ('precision',dict(tokens=token_input,dense=dense_input),'rrf'),
            ('precision',dict(tokens=token_input,dense=dense_input),'dbsf')]:
        options=dict(candidate_limit=10)
        if fusion:options['fusion']=fusion
        def combined(expression):
            if expression is None:options.pop('formula',None)
            else:options['formula']=expression
            return "SELECT coalesce(jsonb_agg(jsonb_build_object('key',source_key,'score',score) ORDER BY rank),'[]') FROM qdrant.search('token_docs','alpha','"+mode+"',10,"+literal(json.dumps(queries))+','+literal(json.dumps(options))+')'
        baseline=json.loads(sql(combined(None)));transformed=json.loads(sql(combined(neg)))
        expected={h['key']:-h['score'] for h in baseline}
        assert {h['key'] for h in transformed}==set(expected),(mode,fusion,baseline,transformed)
        assert all(math.isclose(h['score'],expected[h['key']],abs_tol=1e-5) for h in transformed),(mode,fusion,expected,transformed)
        assert [h['score'] for h in transformed]==sorted([h['score'] for h in transformed],reverse=True)
    checks.append('actual Formula over BM25, learned sparse, native RRF/DBSF, token MaxSim and nested hybrid-to-MaxSim candidate stages preserves candidate sets and transforms preceding scores')

    invalid=[None,[],dict(op='field',name='revision'),dict(op='score',name='$score[1]'),
        dict(op='constant',value=1,tenant_id='caller'),dict(op='constant',value=1000000.01),
        dict(op='constant',value=1e100),dict(op='constant',value='NaN'),
        dict(op='add',args=[score]),dict(op='multiply',args=[score]*9),
        dict(op='divide',left=score,right=score,by_zero_default=1000000.01)]
    deep=score
    for _ in range(8):deep=dict(op='abs',arg=deep)
    invalid.append(deep)
    too_many=dict(op='add',args=[dict(op='add',args=[score]*8)]*8);invalid.append(too_many)
    for expression in invalid:
        options=dict(candidate_limit=7,formula=expression)
        query="SELECT * FROM qdrant.search('recommendation_docs','anchor','semantic',7,"+literal(json.dumps(dense))+','+literal(json.dumps(options))+')'
        assert '22023' in sql(query,ok=False),expression
    assert '42501' in sql('SET ROLE pgq_writer; '+statement(score),ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.admit_score_formula('{\"op\":\"score\"}')",ok=False)
    assert '0A000' in sql('BEGIN; ALTER TABLE recommendation_docs ENABLE ROW LEVEL SECURITY; '+statement(score),ok=False)
    explore=dict(dense=dict(model_id='recommendation',model_version='r1',strategy='mmr',target='3',**{'lambda':.25}))
    assert '22023' in sql("SELECT * FROM qdrant.search('recommendation_docs','','explore',3,"+literal(json.dumps(explore))+','+literal(json.dumps(dict(candidate_limit=3,formula=score)))+')',ok=False)
    sixty_four=dict(op='add',args=[dict(op='add',args=[score]*8)]*7)
    assert len(json.loads(sql(statement(sixty_four))))==7
    assert '22023' in sql(statement(dict(op='negate',arg=sixty_four)),ok=False)
    checks.append('Formula strict fields/numeric/depth/node/arity and source permission/RLS/explore-combination refusals including exact 64-node boundary')

    owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
    division=dict(op='divide',left=dict(op='constant',value=1),right=dict(op='constant',value=0))
    for expression in [division,dict(op='sqrt',arg=dict(op='constant',value=-1)),
            dict(op='multiply',args=[dict(op='constant',value=1000000)]*8)]:
        error=sql(statement(expression),ok=False)
        assert 'XX000' in error,error
        replay()
        current=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
        assert current['engine_pid']==owner['engine_pid'] and current['engine_instance']==owner['engine_instance'],current
    division['by_zero_default']=-7
    assert all(h['score']==-7 for h in json.loads(sql(statement(division))))
    checks.append('actual native divide-by-zero, negative sqrt and float32 output overflow reject complete responses; explicit finite zero default and same-owner subsequent recovery')

    page="SELECT qdrant.search_page('recommendation_docs','anchor','semantic',2,"+literal(json.dumps(dense))+','+literal(json.dumps(dict(candidate_limit=7,formula=neg)))
    first=json.loads(sql(page+')')); assert len(first['hits'])==2 and first['next_cursor']
    second=json.loads(sql(page+','+literal(json.dumps(first['next_cursor']))+')'));assert len(second['hits'])==2
    changed=page.replace(literal(json.dumps(dict(candidate_limit=7,formula=neg))),literal(json.dumps(dict(candidate_limit=7,formula=score))))
    error=sql(changed+','+literal(json.dumps(first['next_cursor']))+')',ok=False)
    assert '22023' in error and 'Cursor query or offset differs' in error,error
    checks.append('Formula pages preserve computed ordering and bind the complete expression to the cursor request hash')

    if spawn is not None:
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'));pid=owner['engine_pid']
        os.kill(pid,signal.SIGSTOP)
        try:
            waiting=spawn(statement(neg),'pgq_formula_cancel');end=time.monotonic()+10
            while time.monotonic()<end:
                busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if (busy.get('active') or {}).get('operation')=='source_search':break
                time.sleep(.02)
            assert (busy.get('active') or {}).get('operation')=='source_search',busy
            assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='pgq_formula_cancel'")=='t'
            out,err=waiting.communicate(timeout=10);assert waiting.returncode and '57014' in err,(out,err)
            retained=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
            assert retained['engine_instance']==owner['engine_instance'] and retained['engine_pid']==pid
            assert (retained.get('active') or {}).get('operation')=='source_search',retained
        finally:os.kill(pid,signal.SIGCONT)
        end=time.monotonic()+10
        while time.monotonic()<end:
            if not json.loads(sql('SELECT qdrant_internal.p0_ping()')).get('active'):break
            time.sleep(.02)
        assert not json.loads(sql('SELECT qdrant_internal.p0_ping()')).get('active')
        replay();checks.append('actual Formula cancellation retains native owner until native completion')
    return replay

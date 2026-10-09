"""Installed three-branch native fusion against independent bounded score math."""
import json
import math


def run(sql,ready,checks):
    literal=lambda value:"'"+value.replace("'","''")+"'"
    dense=dict(model_id='fixture-dense',model_version='r1',vector=[1,0])
    learned=dict(model_id='fixture-learned',model_version='r1',vector=dict(indices=[7],values=[1]),vocabulary='fixture-vocabulary-r1',idf_revision='not-applied')
    tokens=dict(model_id='fixture-tokens',model_version='r1',vector=[[1,0],[0,1]])
    def search(q,mode,queries,options,top=3,ok=True,prefix=''):
        return sql(prefix+"SELECT coalesce(jsonb_agg(jsonb_build_object('key',source_key,'score',score,'plan',provenance->>'plan') ORDER BY rank),'[]') FROM qdrant.search('token_docs',"+literal(q)+','+literal(mode)+','+str(top)+','+literal(json.dumps(queries))+','+literal(json.dumps(options))+')',ok=ok)
    def replay():
        ready('token_docs')
        for q in ['alpha','nolexicalmatch']:
            for predicate in [None,dict(field='key',range=dict(gte=2,lte=4)),dict(field='key',eq=2)]:
                options=dict(candidate_limit=3)
                if predicate is not None:options['filter']=predicate
                branches=[json.loads(search(q,mode,query,options)) for mode,query in [('text',{}),('semantic',dict(dense=dense)),('sparse',dict(learned=learned))]]
                for fusion in ['rrf','dbsf']:
                    expected={}
                    for branch in branches:
                        values=[h['score'] for h in branch]
                        mean=sum(values)/len(values) if values else 0
                        variance=sum((v-mean)**2 for v in values)/(len(values)-1) if len(values)>1 else 0
                        for rank,hit in enumerate(branch):
                            value=1/(rank+2) if fusion=='rrf' else (.5+(hit['score']-mean)/(6*math.sqrt(variance)) if variance else .5)
                            expected[hit['key']]=expected.get(hit['key'],0)+value
                    actual=json.loads(search(q,'hybrid',dict(dense=dense,learned=learned),dict(options,fusion=fusion)))
                    assert len(actual)==min(3,len(expected)),(actual,expected)
                    for hit in actual:
                        assert abs(hit['score']-expected[hit['key']])<.00001,(fusion,hit,expected)
                        assert hit['plan']=='hybrid_bm25_dense_learned_sparse_'+fusion
                    scores=[h['score'] for h in actual]
                    assert scores==sorted(scores,reverse=True),scores
                    if len(expected)>3:assert min(scores)>=sorted(expected.values(),reverse=True)[2]-.00001
                    reranked=json.loads(search(q,'precision',dict(tokens=tokens,dense=dense,learned=learned),dict(options,fusion=fusion)))
                    assert {h['key'] for h in reranked}=={h['key'] for h in actual},(reranked,actual)
                    reference=json.loads(search(q,'maxsim',dict(tokens=tokens),dict(candidate_limit=10),top=10))
                    token_scores={h['key']:h['score'] for h in reference}
                    assert all(abs(h['score']-token_scores[h['key']])<.00001 for h in reranked),reranked
                    assert all(h['plan']=='precision_bm25_dense_learned_sparse_'+fusion+'_maxsim' for h in reranked)
        plan=json.loads(sql("SELECT qdrant.explain_search('token_docs','alpha','hybrid',3,"+literal(json.dumps(dict(dense=dense,learned=learned)))+",'{\"candidate_limit\":3}')"))
        assert plan['fusion_contract']['branches']==['bm25','dense','learned'] and plan['fusion_contract']['rrf_k']==2,plan
        for mode,request in [('hybrid',dict(dense=dense,learned=learned)),('precision',dict(tokens=tokens,dense=dense,learned=learned))]:
            expected=json.loads(search('alpha',mode,request,dict(candidate_limit=3)))
            cursor=None;captured=[]
            while True:
                page=json.loads(sql("SELECT qdrant.search_page('token_docs','alpha',"+literal(mode)+',1,'+literal(json.dumps(request))+",'{\"candidate_limit\":3}',"+('NULL' if cursor is None else literal(json.dumps(cursor)))+')'))
                captured += [dict(key=h['source_key'],score=h['score']) for h in page['hits']]
                cursor=page['next_cursor']
                if cursor is None:break
            assert captured==[dict(key=h['key'],score=h['score']) for h in expected],(captured,expected)
            transformed=json.loads(search('alpha',mode,request,dict(candidate_limit=3,formula=dict(op='negate',arg=dict(op='score')))))
            by_key={h['key']:h['score'] for h in expected}
            assert {h['key'] for h in transformed}==set(by_key)
            assert all(abs(h['score']+by_key[h['key']])<.00001 for h in transformed),transformed
    replay()
    for bad in [dict(dense=dense,tokens=tokens),dict(dense=dense,unknown=learned),dict(dense=dense,learned=dict(learned,vocabulary='other'))]:
        assert '22023' in search('alpha','hybrid',bad,{},ok=False),bad
    assert '42501' in search('alpha','hybrid',dict(dense=dense,learned=learned),{},ok=False,prefix='SET ROLE pgq_writer; ')
    checks.append('native three-branch BM25/dense/learned sparse RRF/DBSF match independent bounded score math with empty/singleton/filtered branches; candidate-domain MaxSim retains original token scores')
    checks.append('three-branch explain, model/vocabulary/slot admission and source permission fail closed; no raw cross-domain score addition')
    return replay

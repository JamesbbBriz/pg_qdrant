"""Declared scalar filters applied before native recall and checked against SQL."""
import json
import os
import signal
import time


def run(sql,ready,ticket_from,checks,spawn=None):
    literal=lambda value:"'"+value.replace("'","''")+"'"
    # Restore scalar attributes after the source-key reincarnation regressions.
    sql("UPDATE recommendation_docs SET category=CASE WHEN id=7 THEN NULL WHEN id%2=0 THEN 'Allow' ELSE 'Denied' END,quantity=CASE id WHEN 1 THEN 9223372036854775807 WHEN 2 THEN 9223372036854775806 WHEN 3 THEN 9007199254740993 WHEN 7 THEN NULL ELSE id END,price=CASE WHEN id=7 THEN NULL ELSE id::float8/10 END,available=CASE WHEN id=7 THEN NULL ELSE id=2 END")
    ready('recommendation_docs')
    dense=dict(dense=dict(model_id='recommendation',model_version='r1',vector=[1,0]))
    def statement(expression,mode='semantic',queries=None,options=None,top=1,index='recommendation_docs',ok=True,prefix=''):
        settings=dict(candidate_limit=top,filter=expression,**(options or {}))
        request=dense if queries is None else queries
        return sql(prefix+"SELECT coalesce(jsonb_agg(source_key ORDER BY rank),'[]') FROM qdrant.search("+
            literal(index)+','+literal('alpha' if index=='token_docs' else 'anchor')+','+literal(mode)+','+str(top)+','+literal(json.dumps(request))+','+literal(json.dumps(settings))+')',ok=ok)
    def keys(*args,**kw): return json.loads(statement(*args,**kw))
    def replay():
        ready('recommendation_docs');ready('token_docs')
        assert keys(dict(field='category',eq='Allow'))==['4']
        assert keys(dict(field='quantity',eq=9223372036854775807))==['1']
        assert keys(dict(field='quantity',eq=9223372036854775806))==['2']
        assert keys(dict(field='quantity',eq=9007199254740993))==['3']
        assert set(keys(dict(field='quantity',range=dict(lte=9007199254740992)),top=7))=={'4','5','6'}
        assert set(keys(dict(field='quantity',range=dict(gt=9007199254740992)),top=7))=={'1','2','3'}
        assert keys(dict(field='quantity',is_null=True))==['7']
        assert keys(dict(field='available',eq=True))==['2']
        assert keys(dict(field='price',eq=.4))==['4']
        assert keys(dict(field='category',prefix='all'))==[]
        expression=dict(all=[dict(field='category',prefix='All'),dict(field='price',range=dict(gte=.3,lt=.5))])
        for fusion in ['rrf','dbsf']:
            assert keys(expression,'hybrid',options=dict(fusion=fusion))==['4']
        assert keys(expression,'text',{})==['4']
        expression=dict(any=[dict(field='quantity',eq=4),dict(all=[dict(field='available',eq=True),{'not':dict(field='quantity',eq=9223372036854775806)}])])
        assert keys(expression)==['4']
        assert keys(dict(field='quantity',eq=4),options=dict(formula=dict(op='negate',arg=dict(op='score'))))==['4']
        for strategy in ['sum_scores','best_score','discover','context','feedback','mmr']:
            request=dict(model_id='recommendation',model_version='r1',strategy=strategy)
            if strategy in ['sum_scores','best_score']:request.update(positive=['1'],negative=['2'])
            elif strategy in ['discover','context']:
                request['context']=[dict(positive='1',negative='2')]
                if strategy=='discover':request['target']='3'
            elif strategy=='feedback':request.update(target='3',feedback=[dict(key='1',score=2),dict(key='2',score=0),dict(key='5',score=-1)],coefficients=dict(a=1,b=2,c=.25))
            else:request.update(target='3',**{'lambda':.25})
            assert keys(dict(field='quantity',eq=4),'explore',dict(dense=request))==['4'],strategy
            assert '22023' in statement(dict(field='quantity',eq=4),'explore',dict(dense=request),options=dict(formula=dict(op='score')),ok=False),strategy
        token=dict(model_id='fixture-tokens',model_version='r1',vector=[[1,0],[0,1]])
        token_dense=dict(model_id='fixture-dense',model_version='r1',vector=[1,0])
        learned=dict(model_id='fixture-learned',model_version='r1',vector=dict(indices=[7],values=[1]),vocabulary='fixture-vocabulary-r1',idf_revision='not-applied')
        for mode,request,fusion in [('sparse',dict(learned=learned),None),('maxsim',dict(tokens=token),None),
                ('precision',dict(tokens=token),None),('precision',dict(tokens=token,dense=token_dense),'rrf'),
                ('precision',dict(tokens=token,learned=learned),'dbsf'),
                ('hybrid',dict(learned=learned),'rrf'),
                ('hybrid',dict(learned=learned),'dbsf')]:
            options={} if fusion is None else dict(fusion=fusion)
            # The unrestricted token winner is key 3, outside this filter.
            assert keys(dict(field='key',eq=2),mode,request,options,index='token_docs')==['2'],(mode,fusion)
        for fusion in ['rrf','dbsf']:
            assert keys(dict(field='key',eq=2),'hybrid',dict(dense=token_dense,learned=learned),dict(fusion=fusion),index='token_docs')==['2']
            assert keys(dict(field='key',eq=2),'precision',dict(tokens=token,dense=token_dense,learned=learned),dict(fusion=fusion),index='token_docs')==['2']
    replay()
    expression=dict(field='quantity',eq=4)
    settings=literal(json.dumps(dict(filter=expression)))
    request=literal(json.dumps(dense))
    explained=json.loads(sql("SELECT qdrant.explain_search('recommendation_docs','anchor','semantic',1,"+request+','+settings+')'))
    assert explained['payload_filter']==expression,explained
    actual=json.loads(sql("SELECT provenance->'payload_filter' FROM qdrant.search('recommendation_docs','anchor','semantic',1,"+request+','+settings+')'))
    assert actual==expression,actual
    checks.append('actual before-cap scalar filters across BM25/dense/sparse/RRF/DBSF/MaxSim/precision, Formula and every source-example strategy fill admissible candidates')
    checks.append('native int64 equality distinguishes adjacent extrema, exact-bound integer ranges exclude 2^53+1 correctly, and null/finite float/bool/case-sensitive whole-keyword prefix semantics match SQL')
    bad=[None,[],{},dict(field='quantity',eq=None),dict(field='quantity',eq=1.5),dict(field='quantity',eq=18446744073709551615),
         dict(field='quantity',range=dict(lte=9007199254740993)),dict(field='category',range=dict(gte=1)),dict(field='price',range={}),
         dict(field='available',prefix='t'),dict(field='category',prefix=''),dict(field='fingerprint',eq='private'),
         dict(field='attributes.quantity',eq=4),dict(field='category',eq='Allow',tenant_id='forged'),dict(field='quantity',is_null=False),
         dict(all=[]),dict(any=[dict(field='quantity',eq=4)]*17)]
    deep=dict(field='quantity',eq=4)
    for _ in range(5):deep={'not':deep}
    bad += [deep,dict(all=[dict(any=[dict(field='quantity',eq=4)]*16)]*3),dict(field='category',eq='x'*9000)]
    for expression in bad:
        assert '22023' in statement(expression,ok=False),expression
    for expression in [dict(field='category',eq='Allow'),dict(field='quantity',is_null=True)]:
        assert '42501' in statement(expression,prefix='SET ROLE pgq_writer; ',ok=False)
    for function in ["admit_payload_filter('{}','{}')","payload_matches('{}','{}','{}')"]:
        assert '42501' in sql('SET ROLE pgq_writer; SELECT qdrant_internal.'+function,ok=False)
    error=sql("BEGIN; ALTER TABLE recommendation_docs ENABLE ROW LEVEL SECURITY; "+
        "SELECT * FROM qdrant.search('recommendation_docs','anchor','semantic',1,"+literal(json.dumps(dense))+','+literal(json.dumps(dict(filter=dict(field='quantity',eq=4))))+')',ok=False)
    assert '0A000' in error,error
    sql('REVOKE SELECT ON recommendation_docs FROM builder,pgq_recommendation_reader')
    sql('GRANT SELECT(id,body,v,fp,inc,model,version,category,price,available) ON recommendation_docs TO builder,pgq_recommendation_reader')
    assert '42501' in statement(dict(field='category',eq='Allow'),prefix='SET ROLE pgq_recommendation_reader; ',ok=False)
    sql('GRANT SELECT ON recommendation_docs TO builder,pgq_recommendation_reader')
    checks.append('filter admission rejects arbitrary paths/types/casts/null equality/forged fields/AST budgets and enforces source scalar SELECT, owner/private access and RLS refusal')
    def page(cursor=None,expression=None,ok=True):
        options=dict(filter=expression or dict(field='category',eq='Allow'))
        return sql("SELECT qdrant.search_page('recommendation_docs','anchor','semantic',1,"+literal(json.dumps(dense))+','+literal(json.dumps(options))+','+('NULL' if cursor is None else literal(json.dumps(cursor)))+')',ok=ok)
    before=json.loads(page());assert before['captured_count']==3 and len(before['hits'])==1,before
    assert '22023' in page(before['next_cursor'],dict(field='category',eq='Denied'),ok=False)
    ticket=ticket_from(sql("BEGIN; UPDATE recommendation_docs SET quantity=9,category='Denied' WHERE id=4; SELECT qdrant.track_changes('recommendation_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',30000)"))['durable']
    assert keys(dict(field='quantity',eq=4))==[]
    assert keys(dict(field='quantity',eq=9))==['4']
    assert '55000' in page(before['next_cursor'],ok=False)
    sql("UPDATE recommendation_docs SET quantity=4,category='Allow' WHERE id=4")
    ready('recommendation_docs');replay()
    checks.append('payload-only committed updates preserve model readiness, change filtered native results after exact durable ACK and invalidate immutable filter-bound pages')
    if spawn is not None:
        request="SELECT coalesce(jsonb_agg(source_key),'[]') FROM qdrant.search('recommendation_docs','anchor','semantic',1,"+literal(json.dumps(dense))+','+literal(json.dumps(dict(candidate_limit=1,filter=dict(field='quantity',eq=4))))+')'
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'));pid=owner['engine_pid']
        for changed in [False,True]:
            os.kill(pid,signal.SIGSTOP)
            application='pgq_filter_change' if changed else 'pgq_filter_cancel'
            try:
                waiting=spawn(request,application)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    if (busy.get('active') or {}).get('operation')=='source_search':break
                    time.sleep(.02)
                assert (busy.get('active') or {}).get('operation')=='source_search',busy
                if changed:sql('UPDATE recommendation_docs SET quantity=9 WHERE id=4')
                else:
                    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='"+application+"'")=='t'
                    out,err=waiting.communicate(timeout=10)
                    assert waiting.returncode and '57014' in err,(out,err)
                    retained=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    assert retained['engine_pid']==pid and retained['engine_instance']==owner['engine_instance'] and retained.get('active'),retained
            finally:os.kill(pid,signal.SIGCONT)
            if changed:
                out,err=waiting.communicate(timeout=10)
                assert (waiting.returncode and '55000' in err) or (not waiting.returncode and json.loads(out)==[]),(out,err)
                sql('UPDATE recommendation_docs SET quantity=4 WHERE id=4')
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                completed=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if not completed.get('active'):break
                time.sleep(.02)
            assert not completed.get('active'),completed
            replay()
        checks.append('filtered SQL cancellation retains the sole native operation until response and an in-flight payload update cannot return a stale matching hit')
    return replay

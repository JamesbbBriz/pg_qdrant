"""Exact native full-domain point counts/facets with current-source proof."""
import json
import os
import signal
import time


def run(sql,ready,ticket_from,checks,spawn=None):
    literal=lambda value:"'"+value.replace("'","''")+"'"
    def count(options=None,index='recommendation_docs',ok=True,prefix=''):
        return sql(prefix+'SELECT qdrant.count('+literal(index)+','+literal(json.dumps(options or {}))+')',ok=ok)
    def facet(field,limit=100,options=None,ok=True,prefix=''):
        return sql(prefix+"SELECT qdrant.facet('recommendation_docs',"+literal(field)+','+str(limit)+','+literal(json.dumps(options or {}))+')',ok=ok)
    def replay():
        ready('recommendation_docs')
        result=json.loads(count())
        assert result['points']==7 and result['live_points']==7 and result['unit']=='points'
        assert result['exact'] and result['documents'] is None and 'proof' not in result,result
        # This source has a model column named v. It must never shadow the
        # explicit native JSON element used by retrieval or statistics proofs.
        records=json.loads(sql("SELECT qdrant.retrieve('recommendation_docs','[\"1\",\"2\",\"3\",\"4\",\"5\",\"6\",\"7\"]')"))
        assert all(hit['status']=='found' for hit in records['items']),records
        for field,column in [('category','category'),('quantity','quantity'),('available','available')]:
            result=json.loads(facet(field))
            expected=json.loads(sql('SELECT coalesce(jsonb_agg(jsonb_build_object(\'value\',v,\'count\',n)),\'[]\') FROM (SELECT '+column+' AS v,count(*) n FROM recommendation_docs WHERE '+column+' IS NOT NULL GROUP BY '+column+') x'))
            actual=result['facet']
            assert actual['values_complete'] and actual['nulls']=='omitted',actual
            assert sorted(json.dumps(h,sort_keys=True) for h in actual['hits'])==sorted(json.dumps(h,sort_keys=True) for h in expected),(actual,expected)
            assert [h['count'] for h in actual['hits']]==sorted((h['count'] for h in expected),reverse=True)
        result=json.loads(facet('quantity',1))
        assert len(result['facet']['hits'])==1 and not result['facet']['values_complete'],result
        assert result['facet']['hits'][0]['value']==4,result
        for expression,predicate in [(dict(field='category',eq='Allow'),"category='Allow'"),(dict(field='quantity',eq=9223372036854775807),'quantity=9223372036854775807'),(dict(field='quantity',is_null=True),'quantity IS NULL'),(dict(field='category',eq='missing'),"category='missing'")]:
            expected=int(sql('SELECT count(*) FROM recommendation_docs WHERE '+predicate))
            result=json.loads(count(dict(filter=expression)))
            assert result['points']==expected,result
            grouped=json.loads(facet('category',options=dict(filter=expression)))
            assert grouped['points']==expected and sum(h['count'] for h in grouped['facet']['hits'])<=expected,grouped
        assert json.loads(count(dict(matching=dict(key_exact='2'))))['points']==1
        assert json.loads(count(dict(matching=dict(phrase='not a present phrase'))))['points']==0
    replay()
    sql('CREATE TABLE statistics_bound(id bigint PRIMARY KEY,body text NOT NULL,category text)')
    sql("SELECT qdrant.create_index('statistics_bound','statistics_bound','id','{\"text\":{\"fields\":[\"body\"]},\"payload\":{\"category\":{\"field\":\"category\",\"kind\":\"keyword\"}}}')")
    ready('statistics_bound')
    empty=json.loads(count(index='statistics_bound'))
    assert empty['points']==0 and empty['live_points']==0 and empty['exact'],empty
    empty=json.loads(sql("SELECT qdrant.facet('statistics_bound','category')"))
    assert empty['facet']['hits']==[] and empty['facet']['values_complete'],empty
    sql("INSERT INTO statistics_bound SELECT n,'bounded statistics '||n,n::text FROM generate_series(1,1001)n")
    ready('statistics_bound')
    assert '54000' in count(index='statistics_bound',ok=False)
    assert '54000' in sql("SELECT qdrant.facet('statistics_bound','category')",ok=False)
    sql('DELETE FROM statistics_bound WHERE id=1001');ready('statistics_bound')
    boundary=json.loads(count(index='statistics_bound'))
    assert boundary['points']==1000 and boundary['live_points']==1000 and boundary['max_live_points']==1000,boundary
    boundary=json.loads(sql("SELECT qdrant.facet('statistics_bound','category',100)"))
    assert len(boundary['facet']['hits'])==100 and not boundary['facet']['values_complete'],boundary
    assert all(hit['count']==1 for hit in boundary['facet']['hits']),boundary
    sql("SELECT qdrant.drop_index('statistics_bound')")
    for kind,key in [('uuid','00000000-0000-0000-0000-000000000001'),('text',"quoted 'key / 汉字")]:
        name='statistics_'+kind
        sql('CREATE TABLE '+name+'(id '+kind+' PRIMARY KEY,body text NOT NULL)')
        sql('INSERT INTO '+name+' VALUES('+literal(key)+",'first incarnation')")
        sql('SELECT qdrant.create_index('+literal(name)+','+literal(name)+",'id','{\"text\":{\"fields\":[\"body\"]}}')")
        ready(name)
        assert json.loads(count(index=name))['points']==1
        sql('BEGIN; DELETE FROM '+name+'; INSERT INTO '+name+' VALUES('+literal(key)+",'new incarnation'); COMMIT")
        ready(name)
        assert json.loads(count(index=name))['points']==1
        sql('TRUNCATE '+name);ready(name)
        assert json.loads(count(index=name))['points']==0
        sql('SELECT qdrant.drop_index('+literal(name)+')')
    checks.append('statistics execute empty, exact 1000-point and rejected 1001-point domains, complete native proof batches, bigint/uuid/Unicode text identities, deletion/reuse and TRUNCATE')
    for expression in [dict(q='anchor'),dict(candidate_limit=1),dict(filter=dict(field='unknown',eq=1))]:
        assert '22023' in count(expression,ok=False)
    assert '22023' in facet('attributes.category',ok=False)
    assert '0A000' in facet('price',ok=False)
    assert '22023' in facet('category',0,ok=False)
    assert '42501' in count(ok=False,prefix='SET ROLE pgq_writer; ')
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.p1_statistics('{}',5000)",ok=False)
    assert '0A000' in count(ok=False,prefix='BEGIN ISOLATION LEVEL REPEATABLE READ; ')
    assert '0A000' in count(ok=False,prefix='BEGIN; ALTER TABLE recommendation_docs ENABLE ROW LEVEL SECURITY; ')
    sql('REVOKE SELECT ON recommendation_docs FROM builder,pgq_recommendation_reader')
    sql('GRANT SELECT(id,body,v,fp,inc,model,version,quantity,price,available) ON recommendation_docs TO builder,pgq_recommendation_reader')
    assert '42501' in count(ok=False,prefix='SET ROLE pgq_recommendation_reader; ')
    sql('GRANT SELECT ON recommendation_docs TO builder,pgq_recommendation_reader')
    assert '55000' in count(ok=False,prefix="BEGIN; UPDATE recommendation_docs SET category='changed' WHERE id=1; ")
    ticket=ticket_from(sql("BEGIN; UPDATE recommendation_docs SET category='changed' WHERE id=1; SELECT qdrant.track_changes('recommendation_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',30000)"))['durable']
    assert json.loads(count(dict(filter=dict(field='category',eq='changed'))))['points']==1
    sql("UPDATE recommendation_docs SET category='Denied' WHERE id=1");ready('recommendation_docs')
    if spawn is not None:
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'));pid=owner['engine_pid']
        for changed in [False,True]:
            os.kill(pid,signal.SIGSTOP)
            application='pgq_statistics_change' if changed else 'pgq_statistics_cancel'
            try:
                waiting=spawn("SELECT qdrant.facet('recommendation_docs','category',10,'{\"filter\":{\"field\":\"category\",\"eq\":\"Allow\"}}')",application)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    if (busy.get('active') or {}).get('operation')=='source_statistics':break
                    time.sleep(.02)
                assert (busy.get('active') or {}).get('operation')=='source_statistics',busy
                if changed:
                    # An excluded row entering the matching domain must invalidate
                    # the complete result, even if all earlier matched rows agree.
                    sql("UPDATE recommendation_docs SET category='Allow' WHERE id=1")
                else:
                    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='"+application+"'")=='t'
                    out,err=waiting.communicate(timeout=10)
                    assert waiting.returncode and '57014' in err,(out,err)
                    retained=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    assert retained['engine_pid']==pid and retained['engine_instance']==owner['engine_instance'] and retained.get('active'),retained
            finally:os.kill(pid,signal.SIGCONT)
            if changed:
                out,err=waiting.communicate(timeout=10)
                assert waiting.returncode and '55000' in err,(out,err)
                sql("UPDATE recommendation_docs SET category='Denied' WHERE id=1")
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                completed=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if not completed.get('active'):break
                time.sleep(.02)
            assert not completed.get('active'),completed
            replay()
        checks.append('count/facet cancellation retains native ownership; an excluded source row entering a filtered domain refuses the entire stale statistic')
    checks.append('native exact point count and typed full-domain facets agree with independent SQL groups, preserve int64 extrema, omit nulls and report distinct-value truncation honestly')
    checks.append('statistics reject unknown aliases/options, float facets, stale/uncommitted source, denied source columns, RLS and unsupported snapshots; proof metadata stays private')
    return replay

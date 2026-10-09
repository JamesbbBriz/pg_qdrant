"""Native random sampled-domain matrices with independent source-vector scores."""
import json
import math
import os
import signal
import time


def run(sql,ready,ticket_from,checks,spawn=None):
    literal=lambda value:"'"+value.replace("'","''")+"'"
    def call(sample=7,neighbors=6,options=None,representation='dense',ok=True,prefix=''):
        return sql(prefix+"SELECT qdrant.search_matrix('recommendation_docs',"+literal(representation)+','+str(sample)+','+str(neighbors)+','+literal(json.dumps(options or {}))+')',ok=ok)
    def replay():
        ready('recommendation_docs')
        vectors=json.loads(sql("SELECT jsonb_object_agg(id::text,v) FROM recommendation_docs"))
        for options,expected in [({},set(vectors)),(dict(filter=dict(field='category',eq='Allow')),{'2','4','6'}),
            (dict(matching=dict(all='anchor distant')),{'4','6'}),
            (dict(matching=dict(key_exact='2')),set()),(dict(matching=dict(any='absent')),set())]:
            result=json.loads(call(options=options))
            assert result['neighbor_domain']=='sampled set only' and not result['exact'],result
            assert result['filtered_count_exact'] and result['model_id']=='recommendation',result
            assert 'proof' not in result and 'sample_ids' not in result,result
            actual={r['value'] for r in result['samples']}
            assert actual==expected,(actual,expected)
            assert len(result['rows'])==len(expected),result
            for row in result['rows']:
                key=row['source_key']['value']
                assert key in expected and 'id' not in row,row
                assert {h['source_key']['value'] for h in row['neighbors']}==expected-{key},row
                for hit in row['neighbors']:
                    other=hit['source_key']['value']
                    score=sum(a*b for a,b in zip(vectors[key],vectors[other]))
                    assert math.isfinite(hit['score']) and abs(hit['score']-score)<1e-6,(hit,score)
        result=json.loads(call(sample=3,neighbors=1))
        sampled={r['value'] for r in result['samples']}
        assert len(sampled)==3 and len(result['rows'])==3,result
        for row in result['rows']:
            assert len(row['neighbors'])==1 and row['neighbors'][0]['source_key']['value'] in sampled-{row['source_key']['value']},row
        assert json.loads(call(sample=1))['rows']==[]
    replay()
    # Work admission uses the real declared width and whole live source domain,
    # even when the model output has not yet arrived.
    budget_contract=json.loads(sql("SELECT contract FROM qdrant_internal.representation_catalog WHERE index_name='recommendation_docs' AND name='dense'"))
    budget_contract['dimensions']=4096
    sql('CREATE TABLE matrix_budget(LIKE recommendation_docs INCLUDING ALL)')
    sql("INSERT INTO matrix_budget(id,body) SELECT n,'matrix budget' FROM generate_series(1,100)n")
    sql("SELECT qdrant.create_index('matrix_budget','matrix_budget','id',"+literal(json.dumps(dict(text=dict(fields=['body']),representations=dict(dense=budget_contract))))+')')
    ready('matrix_budget')
    assert '54000' in sql("SELECT qdrant.search_matrix('matrix_budget','dense',64,32)",ok=False)
    assert '55000' in sql("SELECT qdrant.search_matrix('matrix_budget','dense',1,1)",ok=False)
    sql("SELECT qdrant.drop_index('matrix_budget')")
    for sample,neighbors in [(0,1),(65,1),(1,0),(1,33)]:
        assert '22023' in call(sample,neighbors,ok=False)
    for rep in ['unknown','bm25']:
        assert '22023' in call(representation=rep,ok=False)
    for options in [dict(exact=True),dict(fallback=True),dict(filter=dict(field='unknown',eq='x'))]:
        assert '22023' in call(options=options,ok=False)
    assert '0A000' in call(ok=False,prefix='BEGIN ISOLATION LEVEL REPEATABLE READ; ')
    assert '42501' in call(ok=False,prefix='SET ROLE pgq_writer; ')
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.admit_matrix('recommendation_docs','{}')",ok=False)
    sql('REVOKE SELECT ON recommendation_docs FROM builder,pgq_recommendation_reader')
    sql('GRANT SELECT(id,body,category,quantity,price,available) ON recommendation_docs TO pgq_recommendation_reader')
    assert '42501' in call(ok=False,prefix='SET ROLE pgq_recommendation_reader; ')
    sql('GRANT SELECT ON recommendation_docs TO builder,pgq_recommendation_reader')
    assert '0A000' in call(ok=False,prefix='BEGIN; ALTER TABLE recommendation_docs ENABLE ROW LEVEL SECURITY; ')
    assert '55000' in call(ok=False,prefix="BEGIN; UPDATE recommendation_docs SET v=NULL WHERE id=7; ")
    sql('UPDATE recommendation_docs SET v=NULL WHERE id=7');ready('recommendation_docs')
    assert '55000' in call(ok=False)
    row=json.loads(sql("SELECT qdrant.encoding_inputs('recommendation_docs','dense')"))[0]
    sql("UPDATE recommendation_docs SET v='[0,0]',fp="+literal(row['source_fingerprint'])+',inc='+literal(row['incarnation'])+",model='recommendation',version='r1' WHERE id=7")
    ready('recommendation_docs');replay()
    if spawn is not None:
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'));pid=owner['engine_pid']
        for changed in [False,True]:
            os.kill(pid,signal.SIGSTOP)
            application='pgq_matrix_change' if changed else 'pgq_matrix_cancel'
            try:
                waiting=spawn("SELECT qdrant.search_matrix('recommendation_docs','dense',7,6,'{\"filter\":{\"field\":\"category\",\"eq\":\"Allow\"}}')",application)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    if (busy.get('active') or {}).get('operation')=='source_statistics':break
                    time.sleep(.02)
                assert (busy.get('active') or {}).get('operation')=='source_statistics',busy
                if changed:sql("UPDATE recommendation_docs SET v='[0.2,0.3]' WHERE id=1")
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
                sql("UPDATE recommendation_docs SET v='[1,0]' WHERE id=1")
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                completed=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if not completed.get('active'):break
                time.sleep(.02)
            assert not completed.get('active'),completed
            replay()
        checks.append('matrix cancellation retains native ownership; concurrent excluded model output mutation refuses the complete in-flight response')
    checks.append('native random sampled-set dense matrix scores agree with independent source dot products, including filtered/empty/singleton domains and bounded partial sampling')
    checks.append('matrix rejects invalid sample/neighbor and actual 4096-wide scalar budgets, missing/stale models, denied model columns, RLS, unsupported snapshots and private entry points')
    return replay

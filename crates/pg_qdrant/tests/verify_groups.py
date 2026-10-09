"""Native grouping with independent dense goldens and authorized source joins."""
import json
import math
import os
import signal
import time


def run(sql,ready,ticket_from,checks,spawn=None):
    literal=lambda value:"'"+value.replace("'","''")+"'"
    dense=dict(dense=dict(model_id='recommendation',model_version='r1',vector=[1,0]))
    def statement(field='category',limit=10,size=3,mode='text',vectors=None,options=None,index='recommendation_docs',q='anchor'):
        return "SELECT qdrant.search_groups("+literal(index)+','+literal(q)+','+literal(field)+','+str(limit)+','+str(size)+','+literal(mode)+','+literal(json.dumps(vectors or {}))+','+literal(json.dumps(options or {}))+')'
    def call(ok=True,prefix='',**kwargs):return sql(prefix+statement(**kwargs),ok=ok)
    def replay():
        ready('recommendation_docs')
        facts=json.loads(sql("SELECT jsonb_object_agg(id::text,jsonb_build_object('category',category,'quantity',quantity,'v',v)) FROM recommendation_docs"))
        for field in ['category','quantity']:
            for options,allowed in [({},set(facts)),(dict(filter=dict(field='category',eq='Allow')),{'2','4','6'}),
                (dict(matching=dict(all='anchor distant')),{'4','6'}),(dict(matching=dict(key_exact='7')),{'7'}),
                (dict(matching=dict(any='absent')),set())]:
                result=json.loads(call(field=field,limit=10,size=3,mode='semantic',vectors=dense,options=options))
                assert result['exact_scores'] and not result['groups_complete'] and result['filtered_count_exact'],result
                assert 'proof' not in result and result['request_budget']==dict(collect=5,fill=5),result
                expected={}
                for key in allowed:
                    value=facts[key][field]
                    if value is not None:expected.setdefault(value,[]).append(facts[key]['v'][0])
                assert {g['value'] for g in result['groups']}==set(expected),result
                for group in result['groups']:
                    scores=sorted(expected[group['value']],reverse=True)[:3]
                    assert [h['rank'] for h in group['hits']]==list(range(1,len(scores)+1)),group
                    assert len(group['hits'])==len(scores),group
                    for hit,score in zip(group['hits'],scores):
                        key=hit['source_key']['value']
                        assert key in allowed and facts[key][field]==group['value'] and 'id' not in hit,hit
                        assert math.isfinite(hit['score']) and abs(hit['score']-score)<1e-6,hit
                best=[g['hits'][0]['score'] for g in result['groups']]
                assert best==sorted(best,reverse=True),result
        # A real SQL JOIN reads authorized source facts; grouped keys are typed.
        joined=sql("WITH result AS ("+statement(mode='semantic',vectors=dense)+") SELECT count(*) FROM result, LATERAL jsonb_array_elements(search_groups->'groups') g, LATERAL jsonb_array_elements(g->'hits') h JOIN recommendation_docs d ON d.id=(h #>> '{source_key,value}')::bigint WHERE d.category=g->>'value'")
        assert joined=='6',joined
    replay()
    # Native group discovery must refill beyond a dominated ordinary top-k.
    sql('CREATE TABLE groups_refill(id bigint PRIMARY KEY,body text NOT NULL,category text)')
    sql("INSERT INTO groups_refill SELECT n,CASE WHEN n=60 THEN 'anchor '||repeat('filler ',100) ELSE 'anchor' END,CASE WHEN n=60 THEN 'rare' ELSE 'dominant' END FROM generate_series(1,60)n")
    sql("SELECT qdrant.create_index('groups_refill','groups_refill','id','{\"text\":{\"fields\":[\"body\"]},\"payload\":{\"category\":{\"field\":\"category\",\"kind\":\"keyword\"}}}')")
    ready('groups_refill')
    assert sql("SELECT count(*) FROM qdrant.search('groups_refill','anchor','text',2) h JOIN groups_refill d ON d.id=h.source_key::bigint WHERE d.category='dominant'")=='2'
    result=json.loads(call(index='groups_refill',limit=2,size=1))
    assert {g['value'] for g in result['groups']}=={'dominant','rare'},result
    assert next(g for g in result['groups'] if g['value']=='rare')['hits'][0]['source_key']['value']=='60',result
    sql("SELECT qdrant.drop_index('groups_refill')")
    checks.append('native grouping discovers the rare group beyond a dominated ordinary top-k instead of grouping truncated recall results')
    # Admission includes every native discovery/fill pass over the whole source.
    budget_contract=json.loads(sql("SELECT contract FROM qdrant_internal.representation_catalog WHERE index_name='recommendation_docs' AND name='dense'"))
    budget_contract['dimensions']=4096
    sql('CREATE TABLE groups_budget(LIKE recommendation_docs INCLUDING ALL)')
    sql("INSERT INTO groups_budget(id,body,category) SELECT n,'group budget','one' FROM generate_series(1,500)n")
    configuration=dict(text=dict(fields=['body']),representations=dict(dense=budget_contract),payload=dict(category=dict(field='category',kind='keyword')))
    sql("SELECT qdrant.create_index('groups_budget','groups_budget','id',"+literal(json.dumps(configuration))+')')
    ready('groups_budget')
    assert '54000' in call(index='groups_budget',mode='semantic',vectors=dense,ok=False)
    dropped=json.loads(sql("SELECT qdrant.drop_index('groups_budget')"))
    assert json.loads(sql("SELECT qdrant.await_task('"+dropped['task_id']+"',30000)"))['state']=='succeeded'
    checks.append('actual500-row4096-dimension grouped search refuses work exceeding the20-million scalar bound before model readiness; cleanup completes')
    for field,code in [('unknown','22023'),('attributes.category','22023'),('price','0A000'),('available','0A000')]:
        assert code in call(field=field,ok=False)
    for limit,size in [(0,1),(33,1),(1,0),(1,17),(32,16)]:assert '22023' in call(limit=limit,size=size,ok=False)
    for mode in ['hybrid','sparse','precision','explore']:assert '22023' in call(mode=mode,ok=False)
    for options in [dict(candidate_limit=1),dict(formula=dict(op='score')),dict(fallback=True)]:assert '22023' in call(options=options,ok=False)
    assert '22023' in call(q='',ok=False)
    assert '0A000' in call(prefix='BEGIN ISOLATION LEVEL REPEATABLE READ; ',ok=False)
    assert '0A000' in call(prefix='BEGIN; ALTER TABLE recommendation_docs ENABLE ROW LEVEL SECURITY; ',ok=False)
    assert '42501' in call(prefix='SET ROLE pgq_writer; ',ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.admit_groups('recommendation_docs','{}')",ok=False)
    sql('REVOKE SELECT ON recommendation_docs FROM builder,pgq_recommendation_reader')
    sql('GRANT SELECT(id,body,category,quantity,price,available) ON recommendation_docs TO pgq_recommendation_reader')
    assert '42501' in call(prefix='SET ROLE pgq_recommendation_reader; ',ok=False)
    assert '42501' in call(prefix='SET ROLE pgq_recommendation_reader; ',mode='semantic',vectors=dense,ok=False)
    sql('GRANT SELECT ON recommendation_docs TO builder,pgq_recommendation_reader')
    assert '55000' in call(prefix="BEGIN; UPDATE recommendation_docs SET category='changed' WHERE id=1; ",ok=False)
    sql('UPDATE recommendation_docs SET v=NULL WHERE id=7');ready('recommendation_docs')
    assert '55000' in call(mode='semantic',vectors=dense,ok=False)
    assert json.loads(call())['groups']
    row=json.loads(sql("SELECT qdrant.encoding_inputs('recommendation_docs','dense')"))[0]
    sql("UPDATE recommendation_docs SET v='[0,0]',fp="+literal(row['source_fingerprint'])+',inc='+literal(row['incarnation'])+",model='recommendation',version='r1' WHERE id=7")
    ready('recommendation_docs');replay()
    if spawn is not None:
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'));pid=owner['engine_pid']
        for change in ['cancel','payload','model']:
            os.kill(pid,signal.SIGSTOP)
            application='pgq_groups_'+change
            try:
                waiting=spawn(statement(mode='semantic',vectors=dense,options=dict(filter=dict(field='category',eq='Allow'))),application)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    if (busy.get('active') or {}).get('operation')=='source_statistics':break
                    time.sleep(.02)
                assert (busy.get('active') or {}).get('operation')=='source_statistics',busy
                if change=='cancel':
                    assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='"+application+"'")=='t'
                    out,err=waiting.communicate(timeout=10)
                    assert waiting.returncode and '57014' in err,(out,err)
                    retained=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                    assert retained['engine_pid']==pid and retained['engine_instance']==owner['engine_instance'] and retained.get('active'),retained
                elif change=='payload':sql("UPDATE recommendation_docs SET category='Allow' WHERE id=1")
                else:sql("UPDATE recommendation_docs SET v='[0.2,0.3]' WHERE id=1")
            finally:os.kill(pid,signal.SIGCONT)
            if change!='cancel':
                out,err=waiting.communicate(timeout=10)
                assert waiting.returncode and '55000' in err,(out,err)
                sql("UPDATE recommendation_docs SET category='Denied',v='[1,0]' WHERE id=1")
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                completed=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if not completed.get('active'):break
                time.sleep(.02)
            assert not completed.get('active'),completed
            replay()
        checks.append('group cancellation retains native ownership; excluded source payload and model mutations refuse the whole in-flight grouped result')
    checks.append('native keyword/int64 grouping agrees with independent source dense scores, typed authorized SQL joins, null omission, filters, source-model readiness and documented request budgets')
    checks.append('grouping refuses invalid fields/modes/budgets/options, uncommitted source, missing table SELECT despite column grants, RLS, unsupported snapshots and private entry points')
    return replay

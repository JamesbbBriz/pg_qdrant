"""Pinned native source-example recommendations and seed-sensitive paging."""
import json
import math
import os
import signal
import time


def run(sql, ready, ticket_from, checks, spawn=None):
    contract=dict(kind='dense',model_id='recommendation',model_version='r1',tokenizer='fixture',dimensions=2,
        distance='dot',normalization='none',storage_precision='float32',vector_field='v',fingerprint_field='fp',
        incarnation_field='inc',model_id_field='model',model_version_field='version')
    sql('CREATE TABLE recommendation_docs(id bigint PRIMARY KEY,body text NOT NULL,v jsonb,fp text,inc uuid,model text,version text)')
    bodies=['positive anchor','negative anchor','anchor preferred crate','anchor distant crate','anchor compact blue','anchor distant red','anchor neutral room']
    vectors=[[1,0],[0,1],[.9,.1],[.1,.9],[.5,.5],[-1,0],[0,0]]
    for key,body in enumerate(bodies,1):
        sql("INSERT INTO recommendation_docs(id,body) VALUES("+str(key)+",'"+body+"')")
    sql("SELECT qdrant.create_index('recommendation_docs','recommendation_docs','id','"+json.dumps(dict(text=dict(fields=['body']),representations=dict(dense=contract)))+"')")
    ready('recommendation_docs')
    query=lambda strategy:dict(dense=dict(model_id='recommendation',model_version='r1',positive=['1'],negative=['2'],strategy=strategy))
    def call(operation='count(*)',strategy='sum_scores',options=None,tail='',ok=True,top_k=10):
        return sql("SELECT "+operation+" FROM qdrant.search('recommendation_docs','','explore',"+str(top_k)+",'"+json.dumps(query(strategy))+"','"+json.dumps(options or {})+"')"+tail,ok=ok)
    assert '55000' in call(ok=False)
    updates=[]
    for row in json.loads(sql("SELECT qdrant.encoding_inputs('recommendation_docs','dense')")):
        key=int(row['key']['value'])
        updates.append("UPDATE recommendation_docs SET v='"+json.dumps(vectors[key-1])+"',fp='"+row['source_fingerprint']+"',inc='"+row['incarnation']+"',model='recommendation',version='r1' WHERE id="+str(key))
    ticket=ticket_from(sql("BEGIN;"+';'.join(updates)+"; SELECT qdrant.track_changes('recommendation_docs'); COMMIT"))
    receipt=json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))
    assert receipt['durable'] and receipt['applied'] and receipt['pending_events']==0,receipt
    ready('recommendation_docs')
    def replay():
        for strategy in ['sum_scores','best_score']:
            hits=json.loads(call("jsonb_agg(jsonb_build_object('key',source_key,'score',score,'provenance',provenance) ORDER BY rank)",strategy))
            expected={'3':.8,'4':-.8,'5':0,'6':-1,'7':0} if strategy=='sum_scores' else {'3':.5*(1+.9/1.9),'4':-.5*(1+.9/1.9),'5':-2/3,'6':-.5,'7':-.5}
            assert hits[0]['key']=='3' and len(hits)==5,hits
            assert {h['key'] for h in hits}==set(expected)
            for hit in hits:
                assert math.isfinite(hit['score']) and abs(hit['score']-expected[hit['key']])<1e-6,hit
                assert len(hit['provenance']['seed_digest'])==64
            for matching,key in [({'all':'anchor distant'},'4'),({'any':'red'},'6'),({'exclude':'preferred'},'5'),
                ({'phrase':'distant crate'},'4'),({'token_prefix':'comp'},'5'),({'key_prefix':'4'},'4'),({'key_exact':'4'},'4')]:
                if 'all' in matching and strategy=='best_score': key='6'
                # Exclude alone has a zero tie for sum_scores; restrict the expected remaining candidate.
                if 'exclude' in matching: matching['key_exact']='5'
                actual=call('source_key',strategy,dict(matching=matching,candidate_limit=1),top_k=1)
                assert actual==key,(strategy,matching,actual,key)
            assert call('count(*)',strategy,dict(matching=dict(key_exact='1'),candidate_limit=1),top_k=1)=='0'
            multiple=query(strategy);multiple['dense']['positive']=['1','3']
            rows=json.loads(sql("SELECT jsonb_agg(jsonb_build_object('key',source_key,'score',score) ORDER BY rank) FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(multiple)+"')"))
            expected_multi={'4':-.62,'5':.5,'6':-1.9,'7':0} if strategy=='sum_scores' else {'4':-.5*(1+.9/1.9),'5':-2/3,'6':-.5,'7':-.5}
            assert {r['key'] for r in rows}==set(expected_multi),rows
            for row in rows:
                assert abs(row['score']-expected_multi[row['key']])<1e-6,(strategy,row)
            positive_only=query(strategy);positive_only['dense']['negative']=[]
            rows=json.loads(sql("SELECT jsonb_agg(jsonb_build_object('key',source_key,'score',score) ORDER BY rank) FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(positive_only)+"')"))
            assert rows[0]['key']=='3' and len(rows)==6 and all(r['key']!='1' for r in rows),rows
    replay()
    explain=json.loads(sql("SELECT qdrant.explain_search('recommendation_docs','','explore',10,'"+json.dumps(query('sum_scores'))+"')"))
    assert explain['effective_plan']=='explore_dense_sum_scores'
    assert explain['recommendation_contract']['scalar_work_limit']==20000000
    assert len(explain['recommendation_contract']['seed_versions'])==2
    assert not explain['recommendation_contract']['text_query_used_for_scoring']
    for field,value in [('positive',[]),('positive',['1','1']),('negative',['1']),('positive',['1']*33),('positive',[1]),('strategy',None),('strategy','average_vector'),('model_version','r2')]:
        invalid=query('sum_scores');invalid['dense'][field]=value
        assert '22023' in sql("SELECT * FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(invalid)+"')",ok=False),(field,value)
    invalid=query('sum_scores');invalid['dense']['positive']=['unknown']
    assert '55000' in sql("SELECT * FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(invalid)+"')",ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT * FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(query('sum_scores'))+"')",ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT qdrant_internal.admit_recommendation('recommendation_docs','{}')",ok=False)
    sql('CREATE ROLE pgq_recommendation_reader; GRANT builder TO pgq_recommendation_reader; GRANT SELECT ON recommendation_docs TO pgq_recommendation_reader')
    role_call="SET ROLE pgq_recommendation_reader; SELECT count(*) FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(query('sum_scores'))+"')"
    assert sql(role_call)=='5'
    sql('REVOKE SELECT ON recommendation_docs FROM builder,pgq_recommendation_reader')
    assert sql("SELECT has_table_privilege('pgq_recommendation_reader','recommendation_docs','SELECT')")=='f'
    assert '42501' in sql(role_call,ok=False)
    sql('GRANT SELECT ON recommendation_docs TO builder,pgq_recommendation_reader')
    assert sql(role_call)=='5'
    error=sql("BEGIN; ALTER TABLE recommendation_docs ENABLE ROW LEVEL SECURITY; SELECT * FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(query('sum_scores'))+"')",ok=False)
    assert '0A000' in error,error
    for options in [dict(fusion='rrf'),dict(plan='semantic'),dict(fallback=True)]:
        error=call(options=options,ok=False)
        assert any(code in error for code in ['22023','0A000']),error
    invalid=query('sum_scores'); invalid['dense']['vector']=[1,0]
    assert '22023' in sql("SELECT * FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(invalid)+"')",ok=False)
    # Current SQL identity, not a caller-provided native point ID, resolves seeds.
    for statement in ["DELETE FROM recommendation_docs WHERE id=1",
                      "DELETE FROM recommendation_docs WHERE id=1; INSERT INTO recommendation_docs(id,body) VALUES(1,'positive anchor')",
                      "UPDATE recommendation_docs SET id=100 WHERE id=1"]:
        error=sql("BEGIN; "+statement+"; SELECT * FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(query('sum_scores'))+"')",ok=False)
        assert '55000' in error,error
    for key_type,keys in [('text',['seed / one','candidate / two']),('uuid',['00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002'])]:
        name='recommendation_'+key_type
        sql('CREATE TABLE '+name+'(id '+key_type+' PRIMARY KEY,body text NOT NULL,v jsonb,fp text,inc uuid,model text,version text)')
        sql("INSERT INTO "+name+"(id,body) VALUES('"+keys[0]+"','anchor seed'),('"+keys[1]+"','anchor candidate')")
        sql("SELECT qdrant.create_index('"+name+"','"+name+"','id','"+json.dumps(dict(text=dict(fields=['body']),representations=dict(dense=contract)))+"')")
        ready(name)
        for row in json.loads(sql("SELECT qdrant.encoding_inputs('"+name+"','dense')")):
            sql("UPDATE "+name+" SET v='[1,0]',fp='"+row['source_fingerprint']+"',inc='"+row['incarnation']+"',model='recommendation',version='r1' WHERE id='"+row['key']['value']+"'")
        ready(name)
        typed=dict(dense=dict(model_id='recommendation',model_version='r1',positive=[keys[0]],negative=[],strategy='sum_scores'))
        assert sql("SELECT source_key FROM qdrant.search('"+name+"','','explore',1,'"+json.dumps(typed)+"')")==keys[1]
        sql("SELECT qdrant.drop_index('"+name+"')")
    checks.append('recommendation source seeds use bigint/uuid/text identities and reject uncommitted delete, key reuse and key mutation')
    # Large resolved examples are rejected before sending vectors to the helper.
    wide=dict(contract,dimensions=1024)
    sql('CREATE TABLE recommendation_wide(id bigint PRIMARY KEY,body text NOT NULL,v jsonb,fp text,inc uuid,model text,version text)')
    sql("INSERT INTO recommendation_wide(id,body) SELECT n,'anchor '||n FROM generate_series(1,32)n")
    sql("SELECT qdrant.create_index('recommendation_wide','recommendation_wide','id','"+json.dumps(dict(text=dict(fields=['body']),representations=dict(dense=wide)))+"')")
    ready('recommendation_wide')
    for row in json.loads(sql("SELECT qdrant.encoding_inputs('recommendation_wide','dense')")):
        sql("UPDATE recommendation_wide SET v='"+json.dumps([1]+[0]*1023)+"',fp='"+row['source_fingerprint']+"',inc='"+row['incarnation']+"',model='recommendation',version='r1' WHERE id="+row['key']['value'])
    ready('recommendation_wide')
    oversized=dict(dense=dict(model_id='recommendation',model_version='r1',positive=[str(n) for n in range(1,33)],negative=[],strategy='sum_scores'))
    assert '54000' in sql("SELECT * FROM qdrant.search('recommendation_wide','','explore',10,'"+json.dumps(oversized)+"')",ok=False)
    sql("SELECT qdrant.drop_index('recommendation_wide')")
    checks.append('resolved source recommendation vectors enforce the actual 64 KiB admission bound')
    page_request="SELECT qdrant.search_page('recommendation_docs','','explore',1,'"+json.dumps(query('sum_scores'))+"','{\"candidate_limit\":5}'"
    first=json.loads(sql(page_request+")"))
    assert first['hits'][0]['source_key']=='3' and first['next_cursor']
    continuation=page_request+",'"+json.dumps(first['next_cursor'])+"')"
    assert len(json.loads(sql(continuation))['hits'])==1
    before=json.loads(sql("SELECT jsonb_agg(jsonb_build_object('key',s.point_id,'revision',s.revision)) FROM qdrant_internal.source_state s WHERE index_name='recommendation_docs' AND tagged_key->>'value' NOT IN ('1','2')"))
    sql("UPDATE recommendation_docs SET v='[0.5,0.5]' WHERE id=1")
    ready('recommendation_docs')
    error=sql(continuation,ok=False)
    assert '55000' in error and 'Recommendation examples changed since snapshot capture' in error,error
    after=json.loads(sql("SELECT jsonb_agg(jsonb_build_object('key',s.point_id,'revision',s.revision)) FROM qdrant_internal.source_state s WHERE index_name='recommendation_docs' AND tagged_key->>'value' NOT IN ('1','2')"))
    assert before==after,'only the excluded source seed changed'
    sql("UPDATE recommendation_docs SET v='[1,0]' WHERE id=1")
    sql("UPDATE recommendation_docs SET body='seed source changed' WHERE id=1")
    assert '55000' in call(ok=False)
    sql("UPDATE recommendation_docs SET v=NULL,body='positive anchor' WHERE id=1")
    row=next(r for r in json.loads(sql("SELECT qdrant.encoding_inputs('recommendation_docs','dense')")) if r['key']['value']=='1')
    sql("UPDATE recommendation_docs SET v='[1,0]',fp='"+row['source_fingerprint']+"',inc='"+row['incarnation']+"' WHERE id=1")
    ready('recommendation_docs');replay()
    if spawn is not None:
        owner=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
        pid=owner['engine_pid']
        os.kill(pid,signal.SIGSTOP)
        try:
            waiting=spawn("SELECT * FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(query('sum_scores'))+"')",'pgq_recommendation_cancel')
            end=time.monotonic()+10
            while time.monotonic()<end:
                busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if (busy.get('active') or {}).get('operation')=='source_search': break
                time.sleep(.02)
            assert (busy.get('active') or {}).get('operation')=='source_search',busy
            assert sql("SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE application_name='pgq_recommendation_cancel'")=='t'
            out,err=waiting.communicate(timeout=10)
            assert waiting.returncode and '57014' in err,(out,err)
            retained=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
            assert retained['engine_instance']==owner['engine_instance'] and retained['engine_pid']==pid
            assert (retained.get('active') or {}).get('operation')=='source_search',retained
        finally:
            os.kill(pid,signal.SIGCONT)
        end=time.monotonic()+10
        while time.monotonic()<end:
            if not json.loads(sql('SELECT qdrant_internal.p0_ping()')).get('active'): break
            time.sleep(.02)
        assert not json.loads(sql('SELECT qdrant_internal.p0_ping()')).get('active')
        replay()
        # An excluded seed update must also invalidate an in-flight native query.
        os.kill(pid,signal.SIGSTOP)
        try:
            waiting=spawn("SELECT * FROM qdrant.search('recommendation_docs','','explore',10,'"+json.dumps(query('sum_scores'))+"')",'pgq_recommendation_seed_change')
            end=time.monotonic()+10
            while time.monotonic()<end:
                busy=json.loads(sql('SELECT qdrant_internal.p0_ping()'))
                if (busy.get('active') or {}).get('operation')=='source_search': break
                time.sleep(.02)
            assert (busy.get('active') or {}).get('operation')=='source_search',busy
            sql("UPDATE recommendation_docs SET v='[0.5,0.5]' WHERE id=1")
        finally:
            os.kill(pid,signal.SIGCONT)
        out,err=waiting.communicate(timeout=10)
        assert waiting.returncode and '55000' in err,(out,err)
        sql("UPDATE recommendation_docs SET v='[1,0]' WHERE id=1")
        ready('recommendation_docs');replay()
        checks.append('cancelled SQL recommendation retains native ownership; concurrent excluded seed mutation refuses in-flight results')
    checks.extend(['native dense source recommendation best_score/sum_scores independent goldens and pre-cap predicates/exclusion',
        'recommendation model/source permissions, invalid inputs, stale examples and seed-only cursor invalidation'])
    return replay

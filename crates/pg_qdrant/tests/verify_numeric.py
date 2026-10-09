"""Real native finite-score envelopes and transactional rejection of overflow."""
import json
import math


def run(sql, ready, ticket_from, checks):
    dense_slots, sparse_slots = {}, {}
    for metric in ['dot','cosine','euclid','manhattan']:
        dense_slots[metric] = dict(kind='dense',model_id='numeric-'+metric,model_version='r1',tokenizer='fixture',
            dimensions=2,distance=metric,normalization='none',storage_precision='float32',vector_field=metric+'_vector',
            fingerprint_field=metric+'_source',incarnation_field=metric+'_inc',model_id_field=metric+'_model',model_version_field=metric+'_version')
    for policy in ['none','external','engine']:
        sparse_slots[policy] = dict(kind='learned_sparse',model_id='numeric-'+policy,model_version='r1',tokenizer='fixture',
            dimensions=100,distance='dot',normalization='none',storage_precision='float32',vocabulary='numeric-r1',
            idf_policy=policy,idf_revision='qdrant-edge:0.8.0' if policy=='engine' else ('not-applied' if policy=='none' else 'numeric-r1'),
            vector_field=policy+'_vector',fingerprint_field=policy+'_source',incarnation_field=policy+'_inc',
            model_id_field=policy+'_model',model_version_field=policy+'_version')

    for table, slots in [('numeric_dense',dense_slots),('numeric_sparse',sparse_slots)]:
        fields = []
        for name in slots:
            fields += [name+'_vector jsonb',name+'_source text',name+'_inc uuid',name+'_model text',name+'_version text']
        sql('CREATE TABLE '+table+'(id bigint PRIMARY KEY,body text NOT NULL,'+','.join(fields)+')')
        sql("INSERT INTO "+table+"(id,body) VALUES(1,'anchor'),(2,'anchor'),(3,'anchor')")
        settings = json.dumps(dict(text=dict(fields=['body']),representations=slots))
        sql("SELECT qdrant.create_index('"+table+"','"+table+"','id','"+settings+"')")
        ready(table)
        for name,c in slots.items():
            rows = json.loads(sql("SELECT qdrant.encoding_inputs('"+table+"','"+name+"')"))
            assert all(row['numeric_score_budget']==dict(conversion='float32',max_squared_l2=1e16 if table=='numeric_dense' else 1e14,scope='complete vector') for row in rows)
            def output(row,vector,ok=True):
                assignments={c['vector_field']:json.dumps(vector),c['fingerprint_field']:row['source_fingerprint'],
                    c['incarnation_field']:row['incarnation'],c['model_id_field']:c['model_id'],c['model_version_field']:c['model_version']}
                return sql('BEGIN; UPDATE '+table+' SET '+','.join(k+"='"+v+"'" for k,v in assignments.items())+
                    ' WHERE id='+row['key']['value']+"; SELECT qdrant.track_changes('"+table+"'); COMMIT",ok=ok)
            before = sql("SELECT jsonb_build_object('revision',s.revision,'outbox',(SELECT count(*) FROM qdrant_internal.outbox WHERE index_name='"+table+"')) FROM qdrant_internal.source_state s WHERE s.index_name='"+table+"' AND s.tagged_key->>'value'='1'")
            high = [1e30,0] if table=='numeric_dense' else dict(indices=[7],values=[1e30])
            joint = [1e8,1e8] if table=='numeric_dense' else dict(indices=[7,8],values=[1e7,1e7])
            for invalid in [high,joint]:
                error=output(rows[0],invalid,ok=False)
                assert '22023' in error and 'score budget' in error, error
            after = sql("SELECT jsonb_build_object('revision',s.revision,'outbox',(SELECT count(*) FROM qdrant_internal.outbox WHERE index_name='"+table+"')) FROM qdrant_internal.source_state s WHERE s.index_name='"+table+"' AND s.tagged_key->>'value'='1'")
            assert before==after, (before,after)
            for row in rows:
                key=row['key']['value']
                vector=([1e8,0] if key=='1' else ([0,1e8] if key=='2' else [-1e8,0])) if table=='numeric_dense' else dict(indices=[7],values=[1e7 if key=='1' else (5e6 if key=='2' else -1e7)])
                ticket=ticket_from(output(row,vector))
                assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',60000)"))['durable']
        ready(table)

    def query(table,name,vector):
        c=(dense_slots if table=='numeric_dense' else sparse_slots)[name]
        v=dict(model_id=c['model_id'],model_version=c['model_version'],vector=vector)
        if c['kind']=='learned_sparse':
            v.update(vocabulary=c['vocabulary'],idf_revision=c['idf_revision'])
        return json.dumps({name:v})

    def hits(table,name,vector,mode,fusion=None,ok=True):
        options=dict(fusion=fusion) if fusion else {}
        value=sql("SELECT jsonb_agg(jsonb_build_object('key',source_key,'score',score) ORDER BY rank) FROM qdrant.search('"+table+"','anchor','"+mode+"',10,'"+query(table,name,vector)+"','"+json.dumps(options)+"')",ok=ok)
        return json.loads(value) if ok else value

    def goldens():
        plan=json.loads(sql("SELECT qdrant.explain_search('numeric_dense','anchor','semantic',10,'"+query('numeric_dense','dot',[1e8,0])+"')"))
        assert plan['numeric_score_budgets']['dense_squared_l2_max']==1e16
        assert plan['numeric_score_budgets']['sparse_squared_l2_max']==1e14
        assert plan['numeric_score_budgets']['nonfinite_result_policy']=='reject complete response'
        for table,names,mode,vector in [('numeric_dense',dense_slots,'semantic',[1e8,0]),
            ('numeric_sparse',sparse_slots,'sparse',dict(indices=[7],values=[1e7]))]:
            ready(table)
            for name in names:
                for current,fusion in [(mode,None),('hybrid','rrf'),('hybrid','dbsf')]:
                    found=hits(table,name,vector,current,fusion)
                    assert len(found)==3 and all(h['score'] is not None and math.isfinite(h['score']) for h in found), (name,current,fusion,found)
                high=[1e30,0] if table=='numeric_dense' else dict(indices=[7],values=[1e30])
                joint=[1e8,1e8] if table=='numeric_dense' else dict(indices=[7,8],values=[1e7,1e7])
                for invalid in [high,joint]:
                    assert '22023' in hits(table,name,invalid,mode,ok=False)
        found=hits('numeric_dense','dot',[1e8,0],'hybrid','dbsf')
        assert [h['key'] for h in found]==['1','2','3'], found
        assert all(abs(h['score']-target)<1e-5 for h,target in zip(found,[7/6,1,5/6])), found
    goldens()
    checks.append('finite but oversized dense/sparse source output and query inputs are rejected atomically before native scoring')
    checks.append('largest admitted dense four-distance and sparse three-IDF fixtures retain finite nearest/RRF/DBSF results and independent Dot DBSF goldens after replay')
    return goldens

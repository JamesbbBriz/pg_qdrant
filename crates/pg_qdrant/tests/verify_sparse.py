"""Declared vocabulary/IDF contracts and real native learned sparse SQL."""
import json
import math


def run(sql, ready, ticket_from, checks):
    fields=[]
    slots={}
    for name,policy,revision in [('learned','none','not-applied'),('external','external','fixture-idf-r1'),
                                 ('engine','engine','qdrant-edge:0.8.0')]:
        c=dict(kind='learned_sparse',model_id='fixture-sparse',model_version='r1',tokenizer='fixture-tokenizer-r1',
               dimensions=4294967296,distance='dot',normalization='none',storage_precision='float32',
               vocabulary='fixture-vocabulary-r1',idf_policy=policy,idf_revision=revision,
               vector_field=name+'_vector',fingerprint_field=name+'_source',incarnation_field=name+'_inc',
               model_id_field=name+'_model',model_version_field=name+'_version')
        slots[name]=c
        fields += [name+'_vector jsonb',name+'_source text',name+'_inc uuid',name+'_model text',name+'_version text']
    sql('CREATE TABLE sparse_docs(id bigint PRIMARY KEY,body text NOT NULL,'+','.join(fields)+')')
    sql("INSERT INTO sparse_docs(id,body) VALUES(1,'shared sparse'),(2,'shared source'),(3,'separate sparse')")
    for field,value in [('dimensions',4294967297),('distance','cosine'),('normalization','unit'),
                        ('idf_policy','unknown'),('idf_revision','wrong-edge-revision')]:
        bad=dict(slots['engine']); bad[field]=value
        settings=json.dumps({'text':{'fields':['body']},'representations':{'engine':bad}})
        assert '0A000' in sql("SELECT qdrant.create_index('bad_sparse','sparse_docs','id','"+settings+"')",ok=False)
    assert sql("SELECT count(*) FROM qdrant_internal.index_catalog WHERE index_name='bad_sparse'")=='0'
    bad=dict(slots['external']); bad['fingerprint_field']=bad['vector_field']
    duplicate=json.dumps({'text':{'fields':['body']},'representations':{'learned':slots['learned'],'external':bad}})
    assert '22023' in sql("SELECT qdrant.create_index('bad_sparse','sparse_docs','id','"+duplicate+"')",ok=False)
    settings=json.dumps({'text':{'fields':['body']},'representations':slots})
    sql("SELECT qdrant.create_index('sparse_docs','sparse_docs','id','"+settings+"')")
    ready('sparse_docs')

    def query(name='learned',vector=None,**extra):
        c=slots[name]
        return json.dumps({name:dict(model_id=c['model_id'],model_version=c['model_version'],
            vocabulary=c['vocabulary'],idf_revision=c['idf_revision'],
            vector=vector if vector is not None else {'indices':[7],'values':[1]},**extra)})

    def hits(name='learned',mode='sparse',q='shared',vector=None,fusion='rrf',ok=True):
        return sql("SELECT coalesce(jsonb_agg(jsonb_build_object('key',source_key,'score',score,'plan',provenance->>'plan') ORDER BY rank),'[]') "
                   "FROM qdrant.search('sparse_docs','"+q+"','"+mode+"',10,'"+
                   (query(name,vector) if mode!='text' else '{}')+"','"+
                   (json.dumps({'fusion':fusion}) if mode=='hybrid' else '{}')+"')",ok=ok)

    def output(row,name,vector,ok=True):
        c=slots[name]
        assert row['model']==c,row
        assignments={c['vector_field']:json.dumps(vector),c['fingerprint_field']:row['source_fingerprint'],
                     c['incarnation_field']:row['incarnation'],c['model_id_field']:c['model_id'],
                     c['model_version_field']:c['model_version']}
        statement=','.join(k+"='"+v+"'" for k,v in assignments.items())
        return sql('BEGIN; UPDATE sparse_docs SET '+statement+' WHERE id='+row['key']['value']+
                   "; SELECT qdrant.track_changes('sparse_docs'); COMMIT",ok=ok)

    def fill_missing():
        for name in slots:
            for row in json.loads(sql("SELECT qdrant.encoding_inputs('sparse_docs','"+name+"')")):
                key=row['key']['value']
                vector={'indices':[7,9],'values':[2,.5]} if key=='1' else (
                    {'indices':[7,10],'values':[1,.9]} if key=='2' else {'indices':[],'values':[]})
                ticket=ticket_from(output(row,name,vector))
                assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']

    assert '55000' in hits(ok=False)
    first=json.loads(sql("SELECT qdrant.encoding_inputs('sparse_docs','learned')"))[0]
    for wrong in [{'indices':[7,7],'values':[1,2]}, {'indices':[9,7],'values':[1,2]},
                  {'indices':[4294967296],'values':[1]}, {'indices':[-1],'values':[1]},
                  {'indices':[7.5],'values':[1]}, {'indices':[7],'values':[]},
                  {'indices':[7],'values':[0]}, {'indices':[7],'values':[1e100]},
                  {'indices':[7],'values':['bad']}, {'indices':[7],'values':[1],'extra':True}]:
        assert '22023' in output(first,'learned',wrong,ok=False),wrong
    fill_missing()
    maximum={'indices':list(range(2048)),'values':[1]*2048}
    assert [h['key'] for h in json.loads(hits(vector=maximum))]==['1','2']
    assert [round(h['score'],5) for h in json.loads(hits(vector=maximum))]==[2.5,1.9]
    assert hits(vector={'indices':[4294967295],'values':[1]})=='[]'
    assert '22023' in hits(vector={'indices':list(range(2049)),'values':[1]*2049},ok=False)

    def goldens():
        ready('sparse_docs')
        for name in slots:
            result=json.loads(hits(name))
            # Native live-corpus statistics count the three present slots,
            # including ready empty output, and exclude deleted old offsets.
            factor=math.log(1+(3-2+.5)/(2+.5)) if name=='engine' else 1
            assert [h['key'] for h in result]==['1','2'],result
            assert all(abs(h['score']-v*factor)<.00001 for h,v in zip(result,[2,1])),(name,result,factor)
            assert hits(name,vector={'indices':[],'values':[]})=='[]'
        for name in slots:
            branches=[json.loads(hits(mode='text')),json.loads(hits(name))]
            for fusion in ['rrf','dbsf']:
                expected={}
                for branch in branches:
                    scores=[h['score'] for h in branch]
                    mean=sum(scores)/len(scores) if scores else 0
                    variance=sum((v-mean)**2 for v in scores)/(len(scores)-1) if len(scores)>1 else 0
                    for rank,hit in enumerate(branch):
                        value=1/(rank+2) if fusion=='rrf' else (.5+(hit['score']-mean)/(6*math.sqrt(variance)) if variance else .5)
                        expected[hit['key']]=expected.get(hit['key'],0)+value
                results=json.loads(hits(name,mode='hybrid',fusion=fusion))
                assert {h['key'] for h in results}==set(expected),(results,expected)
                assert all(abs(h['score']-expected[h['key']])<.00001 for h in results),(fusion,results,expected)
                assert all(h['plan']=='hybrid_bm25_learned_sparse_'+fusion for h in results)

    goldens()
    assert '22023' in hits(mode='semantic',ok=False)
    wrong=json.loads(query())
    for field,value in [('vocabulary','different'),('idf_revision','different'),('model_version','r2')]:
        changed=json.loads(json.dumps(wrong)); changed['learned'][field]=value
        assert '22023' in sql("SELECT * FROM qdrant.search('sparse_docs','shared','sparse',10,'"+json.dumps(changed)+"')",ok=False)
    assert '42501' in sql("SET ROLE pgq_writer; SELECT * FROM qdrant.search('sparse_docs','shared','sparse',10,'"+query()+"')",ok=False)
    checks += ['real named learned sparse SQL with declared vocabulary and independent none/external/engine IDF score goldens',
               'sparse bounds, ordered unique IDs, float32 weights, empty vectors, model/vocabulary/IDF and permission rejection',
               'native BM25/learned-sparse RRF/DBSF match independent two-branch score goldens']

    old=json.loads(sql("SELECT jsonb_build_object('key',tagged_key,'incarnation',incarnation,'source_fingerprint',fingerprint,'model',"
                       "(SELECT contract FROM qdrant_internal.representation_catalog WHERE index_name='sparse_docs' AND name='learned')) "
                       "FROM qdrant_internal.source_state WHERE index_name='sparse_docs' AND tagged_key->>'value'='1'"))
    ticket=ticket_from(sql("BEGIN; UPDATE sparse_docs SET body='shared changed source' WHERE id=1; SELECT qdrant.track_changes('sparse_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    assert '55000' in hits(ok=False)
    assert '55000' in output(old,'learned',{'indices':[7],'values':[2]},ok=False)
    # Bypass public completeness admission to inspect the old slot before JOIN.
    native=sql("SELECT qdrant_internal.p1_search((SELECT jsonb_build_object('operation','source_search','index_id',i.index_id,"
               "'generation',i.generation,'storage_epoch',c.storage_epoch,'q','shared','top_k',10,'representation_query',"
               "jsonb_build_object('representation','learned','model_id','fixture-sparse','model_version','r1','vector',"
               "jsonb_build_object('indices',jsonb_build_array(7),'values',jsonb_build_array(1)))) "
               "FROM qdrant_internal.index_catalog i JOIN qdrant_internal.consumer_state c USING(index_name) WHERE i.index_name='sparse_docs'),5000)")
    assert len(json.loads(native))==1,native
    fill_missing()
    goldens()
    old_point=sql("SELECT point_id FROM qdrant_internal.source_state WHERE index_name='sparse_docs' AND tagged_key->>'value'='2'")
    old2=dict(old,key={'type':'bigint','value':'2'},
              incarnation=sql("SELECT incarnation FROM qdrant_internal.source_state WHERE index_name='sparse_docs' AND tagged_key->>'value'='2'"),
              source_fingerprint=sql("SELECT fingerprint FROM qdrant_internal.source_state WHERE index_name='sparse_docs' AND tagged_key->>'value'='2'"))
    ticket=ticket_from(sql("BEGIN; DELETE FROM sparse_docs WHERE id=2; INSERT INTO sparse_docs(id,body) VALUES(2,'shared source'); SELECT qdrant.track_changes('sparse_docs'); COMMIT"))
    assert json.loads(sql("SELECT qdrant.await_changes('"+ticket+"',10000)"))['durable']
    assert '55000' in hits(ok=False)
    assert '55000' in output(old2,'learned',{'indices':[7],'values':[1]},ok=False)
    fill_missing()
    assert sql("SELECT point_id<>"+old_point+" FROM qdrant_internal.source_state WHERE index_name='sparse_docs' AND tagged_key->>'value'='2'")=='t'
    goldens()
    task=json.loads(sql("SELECT qdrant.rebuild_index('sparse_docs')"))['task_id']
    assert json.loads(sql("SELECT qdrant.await_task('"+task+"',60000)"))['succeeded']
    goldens()
    checks += ['source edits remove stale native sparse slots, late outputs and reused-key completions rejected',
               'learned sparse IDF contracts and hybrid queries survive actual generation build/switch']
    return goldens

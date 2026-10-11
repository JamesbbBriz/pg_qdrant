#!/usr/bin/env python3
"""Measure installed native retrieval in a disposable PostgreSQL test database."""
import argparse
import base64
import hashlib
import json
import math
import os
import platform
import pathlib
import statistics
import subprocess
import time

from quality_metrics import metrics


def run(sql, ready, output, fixtures, installed_sha256, encoded_directory=None):
    report={'schema_version':1,'status':'running','full_benchmark':False,'release_supported':False,
        'metric_scope':'deterministic bounded subset, all selected query positive judgments retained; unjudged documents treated as non-relevant',
        'ranking':'installed native Edge 0.8.0 BM25 with live-generation IDF; default multilingual lowercase/no stemming/no stopwords/avg_len16',
        'latency_scope':'one psql process plus SQL/native query/source JOIN/result JSON per query, sequential, no warmup or repeats; not engine-only latency or production p95',
        'absent_ablations':['dense','learned_sparse','RRF','DBSF','MaxSim','standalone_Qdrant','pgvector','pg_search','VectorChord'],
        'datasets':[],'errors':[],
        'platform':{'system':platform.system(),'release':platform.release(),'machine':platform.machine()},
        'cgroup_limits':{name:(pathlib.Path('/sys/fs/cgroup')/name).read_text().strip() for name in ['memory.max','memory.swap.max','cpu.max','pids.max']},
        'unmeasured':['identifier_false_matches','authorized_filter_fill','document_diversity','representation_coverage','encoding_cost','native_RSS','disk','update_optimize_recovery_durations'],
        'build_info':json.loads(sql('SELECT qdrant.build_info()')),
        'postgresql':sql('SELECT version()'),
        'installed_sha256':installed_sha256}
    def persist():
        temporary=pathlib.Path(output).with_suffix('.tmp')
        temporary.write_text(json.dumps(report,indent=2),encoding='utf-8')
        os.replace(temporary,output)
    dense_contract = None
    if encoded_directory is not None:
        from quality_vectors import load_vectors
        model_bytes = (pathlib.Path(__file__).resolve().parents[1]/'ci/dense-quality-model.json').read_bytes()
        model = json.loads(model_bytes)
        tokenizer_sha = next(r['sha256'] for r in model['files'] if r['path']=='tokenizer.json')
        dense_contract = dict(kind='dense',model_id=model['model_id'],model_version=model['revision'],
            tokenizer='minilm-tokenizer-sha256:'+tokenizer_sha,dimensions=384,distance='dot',
            normalization='unit',storage_precision='float32',vector_field='embedding',
            fingerprint_field='embedding_source',incarnation_field='embedding_incarnation',
            model_id_field='embedding_model',model_version_field='embedding_version')
        report['ranking']='installed Edge BM25, exact normalized dense, native bounded RRF/DBSF; candidate_limit100; same fixed queries and complete positive judgments'
        report['absent_ablations']=[p for p in report['absent_ablations'] if p not in ['dense','RRF','DBSF']]
        report['unmeasured'].remove('encoding_cost')
        report['dense_contract']=dense_contract
    persist()
    for name in ['scifact','t2retrieval']:
        path=pathlib.Path(fixtures)/(name+'.json');raw=path.read_bytes()
        contracts=json.loads((pathlib.Path(__file__).resolve().parents[1]/'ci/quality-sources.json').read_text(encoding='utf-8'))
        expected=next(d['fixture_sha256'] for d in contracts['datasets'] if d['dataset']==name)
        if hashlib.sha256(raw).hexdigest()!=expected: raise ValueError('fixture identity mismatch: '+name)
        fixture=json.loads(raw)
        vectors = None
        if dense_contract is not None:
            vectors,encoding,encoding_sha=load_vectors(encoded_directory,name,fixture,expected,model_bytes)
            report['encoding']=encoding
            report['encoding_manifest_sha256']=encoding_sha
        dataset={'dataset':name,'fixture_sha256':hashlib.sha256(raw).hexdigest(),'source_manifest_sha256':fixture['source_manifest_sha256'],
            'upstream_license':fixture['upstream_license'],'selection':fixture['selection'],'original_corpus_points':fixture['original_corpus_points'],
            'selected_points':len(fixture['documents']),'source_bytes':fixture['source_bytes'],'queries':[],'status':'running'}
        report['datasets'].append(dataset);persist()
        table='quality_'+name
        def literal(value):return "'"+value.replace("'","''")+"'"
        try:
            assert 0<len(fixture['documents'])<=1000 and fixture['source_bytes']<=1024*1024
            docids={d['id'] for d in fixture['documents']};assert len(docids)==len(fixture['documents'])
            assert all(set(q['relevance'])<=docids for q in fixture['queries'])
            columns=',embedding jsonb,embedding_source text,embedding_incarnation uuid,embedding_model text,embedding_version text' if vectors is not None else ''
            sql('CREATE TABLE '+table+'(id text PRIMARY KEY,body text NOT NULL'+columns+')')
            # Complete UTF-8 bodies are encoded to bound SQL argv bytes without truncation.
            rows=[];bytes_used=0
            for d in fixture['documents']:
                encoded=base64.b64encode(d['body'].encode()).decode('ascii')
                row='('+literal(d['id'])+",convert_from(decode('"+encoded+"','base64'),'UTF8'))"
                size=len(row.encode())
                assert size<96000
                if rows and bytes_used+size>96000:
                    sql('INSERT INTO '+table+'(id,body) VALUES '+','.join(rows));rows=[];bytes_used=0
                rows.append(row);bytes_used+=size+1
            if rows:sql('INSERT INTO '+table+'(id,body) VALUES '+','.join(rows))
            started=time.monotonic()
            configuration={'text':{'fields':['body']}}
            if dense_contract is not None: configuration['representations']={'dense':dense_contract}
            sql('SELECT qdrant.create_index('+literal(table)+','+literal(table)+",'id',"+literal(json.dumps(configuration))+')')
            status=ready(table,timeout=300)
            dataset['registration_to_durable_seconds']=time.monotonic()-started
            dataset['index_status']=status
            if vectors is not None:
                seen=set();batches=0;vector_started=time.monotonic()
                while len(seen)<len(docids):
                    requests=json.loads(sql('SELECT qdrant.encoding_inputs('+literal(table)+",'dense',100)"))
                    if not requests:raise ValueError('missing pending encoding requests')
                    rows=[];bytes_used=0
                    def write_outputs():
                        nonlocal batches
                        command=('BEGIN; UPDATE ONLY '+table+' t SET embedding=v.embedding,embedding_source=v.source,'
                            'embedding_incarnation=v.incarnation,embedding_model='+literal(dense_contract['model_id'])+
                            ',embedding_version='+literal(dense_contract['model_version'])+' FROM (VALUES '+','.join(rows)+
                            ') v(id,embedding,source,incarnation) WHERE t.id=v.id; SELECT qdrant.track_changes('+literal(table)+'); COMMIT')
                        ticket=sql(command)
                        import uuid
                        uuid.UUID(ticket)
                        wait=json.loads(sql('SELECT qdrant.await_changes('+literal(ticket)+',30000)'))
                        if not wait['durable']:raise ValueError('model outputs did not become durable: '+json.dumps(wait))
                        batches+=1
                    for request in requests:
                        key=request['key']['value']
                        if (request['key']['type']!='text' or key in seen or key not in docids
                                or request['model']!=dense_contract or request['source_fingerprint']!=vectors['documents'][key]['text_sha256']
                                or hashlib.sha256(request['text'].encode()).hexdigest()!=vectors['documents'][key]['text_sha256']):
                            raise ValueError('trusted encoding request differs from measured input')
                        row='('+literal(key)+','+literal(json.dumps(vectors['documents'][key]['vector']))+'::jsonb,'+literal(request['source_fingerprint'])+','+literal(request['incarnation'])+'::uuid)'
                        size=len(row.encode())
                        if size>90000:raise ValueError('model output SQL budget exceeded')
                        if rows and bytes_used+size>90000:write_outputs();rows=[];bytes_used=0
                        rows.append(row);bytes_used+=size+1;seen.add(key)
                    if rows:write_outputs()
                status=ready(table,timeout=300)
                if status['representations']['dense']['rows']['ready']!=len(docids):
                    raise ValueError('incomplete durable representation coverage')
                dataset.update(model_output_to_durable_seconds=time.monotonic()-vector_started,
                    encoding_input_rows=len(seen),durable_model_batches=batches,index_status=status)
            dataset['capabilities_sha256']=hashlib.sha256(sql('SELECT qdrant.capabilities()').encode()).hexdigest()
            plans=['text'] if vectors is None else ['text','dense','rrf','dbsf']
            dataset['explain']={}
            for q in fixture['queries']:
                for plan in plans:
                    mode='semantic' if plan=='dense' else 'hybrid' if plan in ['rrf','dbsf'] else 'text'
                    query_vectors={} if plan=='text' else {'dense':{'model_id':dense_contract['model_id'],
                        'model_version':dense_contract['model_version'],'vector':vectors['queries'][q['id']]['vector']}}
                    options={'fusion':plan,'candidate_limit':100} if mode=='hybrid' else {}
                    arguments=','.join([literal(table),literal(q['text']),literal(mode),'10',literal(json.dumps(query_vectors)),literal(json.dumps(options))])
                    if plan not in dataset['explain']:
                        dataset['explain'][plan]=json.loads(sql('SELECT qdrant.explain_search('+arguments+')'))
                    started=time.monotonic()
                    response=sql("SELECT coalesce(jsonb_agg(jsonb_build_object('key',source_key,'rank',rank,'score',score) ORDER BY rank),'[]') FROM qdrant.search("+arguments+')')
                    elapsed=time.monotonic()-started
                    hits=json.loads(response);ranked=[h['key'] for h in hits]
                    assert set(ranked)<=docids and all(h['rank']==n+1 and math.isfinite(h['score']) for n,h in enumerate(hits)),hits
                    result={'query_id':q['id'],'plan':plan,'latency_seconds':elapsed,'ranking':hits,'positive_judgments':len(q['relevance']),**metrics(ranked,q['relevance'])}
                    dataset['queries'].append(result);persist()
            samples=dataset['queries'];latencies=sorted(q['latency_seconds'] for q in samples)
            dataset['aggregate']={m:statistics.mean(q[m] for q in samples) for m in ['recall_at_10','ndcg_at_10','mrr_at_10']}
            dataset['aggregate'].update(queries=len(samples),zero_results=sum(not q['ranking'] for q in samples),latency_p50_seconds=statistics.median(latencies),latency_p95_seconds=latencies[math.ceil(.95*len(latencies))-1])
            if vectors is not None:
                dataset['aggregate_by_plan']={}
                for plan in plans:
                    samples=[q for q in dataset['queries'] if q['plan']==plan]
                    latencies=sorted(q['latency_seconds'] for q in samples)
                    values={m:statistics.mean(q[m] for q in samples) for m in ['recall_at_10','ndcg_at_10','mrr_at_10']}
                    values.update(queries=len(samples),zero_results=sum(not q['ranking'] for q in samples),
                        latency_p50_seconds=statistics.median(latencies),latency_p95_seconds=latencies[math.ceil(.95*len(latencies))-1])
                    dataset['aggregate_by_plan'][plan]=values
                del dataset['aggregate']  # Different plans are separate measurements.
            dataset['status']='measured'
        except Exception as error:
            dataset['status']='failed';dataset['error']=str(error);report['errors'].append({'dataset':name,'error':str(error)})
        persist()
    report['status']='measured' if not report['errors'] else 'failed';persist()
    assert not report['errors'],report['errors']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=pathlib.Path, required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--encoded', type=pathlib.Path)
    args = parser.parse_args()
    # This entry point is intentionally restricted to the disposable test harness.
    data = pathlib.Path(os.environ['PG_QDRANT_DISPOSABLE_DATA']).resolve()
    if data.parent.parent != pathlib.Path('/tmp') or not data.parent.name.startswith('pgq-p1-product.'):
        raise ValueError('a disposable product-test cluster is required')
    def sql(query):
        result = subprocess.run(['psql', '-X', '-A', '-t', '-q', '-v', 'ON_ERROR_STOP=1',
                                 '-c', query], text=True, encoding='utf-8', capture_output=True, timeout=130)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()
    if pathlib.Path(sql('SHOW data_directory')).resolve() != data:
        raise ValueError('connected PostgreSQL is not the disposable cluster')
    def ready(index, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = json.loads(sql("SELECT qdrant.index_status('" + index + "')"))
            if status.get('engine_index_ready') and status['pending_events'] == 0:
                return status
            time.sleep(.02)
        raise RuntimeError('index did not become durable: ' + json.dumps(status))
    installed = {}
    for option, name in [('--bindir', 'pg_qdrant_p0_helper'), ('--pkglibdir', 'pg_qdrant.so'),
                         ('--sharedir', 'extension/pg_qdrant--0.0.1.sql')]:
        directory = subprocess.check_output(['pg_config', option], text=True).strip()
        installed[name] = hashlib.sha256((pathlib.Path(directory) / name).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sql('CREATE EXTENSION pg_qdrant')
    report = run(sql, ready, args.output, args.fixtures, installed, args.encoded)
    print(json.dumps({d['dataset']: d.get('aggregate_by_plan',d.get('aggregate')) for d in report['datasets']}, indent=2))


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Measure installed native BM25 in a disposable PostgreSQL test database."""
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


def run(sql, ready, output, fixtures, installed_sha256):
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
    persist()
    for name in ['scifact','t2retrieval']:
        path=pathlib.Path(fixtures)/(name+'.json');raw=path.read_bytes()
        contracts=json.loads((pathlib.Path(__file__).resolve().parents[1]/'ci/quality-sources.json').read_text(encoding='utf-8'))
        expected=next(d['fixture_sha256'] for d in contracts['datasets'] if d['dataset']==name)
        if hashlib.sha256(raw).hexdigest()!=expected: raise ValueError('fixture identity mismatch: '+name)
        fixture=json.loads(raw)
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
            sql('CREATE TABLE '+table+'(id text PRIMARY KEY,body text NOT NULL)')
            # Complete UTF-8 bodies are encoded to bound SQL argv bytes without truncation.
            rows=[];bytes_used=0
            for d in fixture['documents']:
                encoded=base64.b64encode(d['body'].encode()).decode('ascii')
                row='('+literal(d['id'])+",convert_from(decode('"+encoded+"','base64'),'UTF8'))"
                size=len(row.encode())
                assert size<96000
                if rows and bytes_used+size>96000:
                    sql('INSERT INTO '+table+' VALUES '+','.join(rows));rows=[];bytes_used=0
                rows.append(row);bytes_used+=size+1
            if rows:sql('INSERT INTO '+table+' VALUES '+','.join(rows))
            started=time.monotonic()
            sql('SELECT qdrant.create_index('+literal(table)+','+literal(table)+",'id','{\"text\":{\"fields\":[\"body\"]}}')")
            status=ready(table,timeout=300)
            dataset['registration_to_durable_seconds']=time.monotonic()-started
            dataset['index_status']=status
            dataset['capabilities_sha256']=hashlib.sha256(sql('SELECT qdrant.capabilities()').encode()).hexdigest()
            for q in fixture['queries']:
                started=time.monotonic()
                response=sql("SELECT coalesce(jsonb_agg(jsonb_build_object('key',source_key,'rank',rank,'score',score) ORDER BY rank),'[]') FROM qdrant.search("+literal(table)+','+literal(q['text'])+',top_k=>10)')
                elapsed=time.monotonic()-started
                hits=json.loads(response);ranked=[h['key'] for h in hits]
                assert set(ranked)<=docids and all(h['rank']==n+1 and math.isfinite(h['score']) for n,h in enumerate(hits)),hits
                result={'query_id':q['id'],'latency_seconds':elapsed,'ranking':hits,'positive_judgments':len(q['relevance']),**metrics(ranked,q['relevance'])}
                dataset['queries'].append(result);persist()
            samples=dataset['queries'];latencies=sorted(q['latency_seconds'] for q in samples)
            dataset['aggregate']={m:statistics.mean(q[m] for q in samples) for m in ['recall_at_10','ndcg_at_10','mrr_at_10']}
            dataset['aggregate'].update(queries=len(samples),zero_results=sum(not q['ranking'] for q in samples),latency_p50_seconds=statistics.median(latencies),latency_p95_seconds=latencies[math.ceil(.95*len(latencies))-1])
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
    report = run(sql, ready, args.output, args.fixtures, installed)
    print(json.dumps({d['dataset']: d['aggregate'] for d in report['datasets']}, indent=2))


if __name__ == '__main__':
    main()

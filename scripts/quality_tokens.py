"""Bounded, text-bound token encoding and independent candidate-domain scoring."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import time

from prepare_dense_quality import CONTRACT, TOKEN_CONTRACT, prepare
from quality_metrics import metrics

WIDTH = 48
CANDIDATES = 64
TOKEN_PLANS = ['maxsim_text', 'maxsim_rrf', 'maxsim_dbsf']
MATCHED_PLANS = ['matched_text', 'matched_rrf', 'matched_dbsf']


def candidate_diagnostics(candidates, ranking, baseline, matched, relevance, documents):
    """Partition observed relevant-hit losses without attributing truncation causes."""
    positives = {key for key, grade in relevance.items() if grade > 0}
    if (not positives or not positives <= set(documents)
            or len(candidates) > CANDIDATES or len(set(candidates)) != len(candidates)
            or not set(candidates) <= set(documents)):
        raise ValueError('invalid diagnostic candidate or judgment domain')
    for keys in (ranking, baseline, matched):
        if len(keys) > 10 or len(set(keys)) != len(keys) or not set(keys) <= set(documents):
            raise ValueError('invalid diagnostic ranking')
    if not set(ranking) <= set(candidates):
        raise ValueError('reranked hit escaped the diagnostic candidate domain')
    candidate_set, final = set(candidates), set(ranking)
    result = {'candidate_control_at_10': metrics(candidates[:10], relevance),
              'candidate_control_keys': candidates[:10],
              'relevant_not_recalled': sorted(positives-candidate_set),
              'relevant_recalled_below_top10': sorted((positives & candidate_set)-final),
              'relevant_token_lengths': {key: {field: documents[key][field]
                  for field in ('untruncated_tokens', 'encoded_tokens')} for key in sorted(positives)}}
    for name, keys in [('baseline100', baseline), ('matched64', matched),
                       ('recorded_candidate_control', candidates[:10])]:
        lost = (set(keys) & positives)-final
        result[name] = {'lost_outside_candidates': sorted(lost-candidate_set),
                        'lost_inside_candidates': sorted(lost & candidate_set),
                        'gained': sorted((final & positives)-set(keys)),
                        'metric_delta': {key: metrics(ranking, relevance)[key]-value
                            for key, value in metrics(keys, relevance).items()}}
    # The candidate list is a separate SQL observation; native fusion can choose
    # different equal-score candidates on another call. Keep the two controls.
    result['scope'] = 'observed candidate-domain partition; separate SQL calls, no tie-order or causal truncation guarantee'
    return result


def summarize_diagnostics(rows):
    """Aggregate every selected query, preserving empty length strata as empty."""
    result = {}
    for plan in TOKEN_PLANS:
        selected = [row['candidate_diagnostics'] for row in rows if row['plan'] == plan]
        if not selected:
            raise ValueError('missing reranking diagnostics')
        lengths = [any(v['untruncated_tokens'] > v['encoded_tokens']
                       for v in row['relevant_token_lengths'].values()) for row in selected]
        groups = {'all': selected,
                  'any_relevant_document_truncated': [row for row, flag in zip(selected, lengths) if flag],
                  'no_relevant_document_truncated': [row for row, flag in zip(selected, lengths) if not flag]}
        result[plan] = {}
        for group, samples in groups.items():
            summary = {'queries': len(samples), 'controls': {}}
            for control in ('baseline100', 'matched64', 'recorded_candidate_control'):
                summary['controls'][control] = {
                    field: sum(len(row[control][field]) for row in samples)
                    for field in ('lost_outside_candidates', 'lost_inside_candidates', 'gained')}
                summary['controls'][control]['mean_metric_delta'] = {
                    metric: sum(row[control]['metric_delta'][metric] for row in samples)/len(samples) if samples else None
                    for metric in ('recall_at_10', 'ndcg_at_10', 'mrr_at_10')}
            result[plan][group] = summary
    return result


def prefixed_ids(ids, prefix, maximum):
    """Insert the marker after CLS and retain SEP on right truncation."""
    if len(ids) < 2 or maximum < 3:
        raise ValueError('missing tokenizer special tokens')
    return [ids[0], prefix, *ids[1:maximum-2], ids[-1]] if len(ids)+1 > maximum else [ids[0], prefix, *ids[1:]]


def kept_positions(ids, attention, skiplist, is_query):
    if len(ids) != len(attention) or any(type(v) is not int or v not in (0, 1) for v in attention):
        raise ValueError('invalid token attention mask')
    positions = [i for i, (token, attend) in enumerate(zip(ids, attention))
                 if attend and (is_query or token not in skiplist)]
    if not positions:
        raise ValueError('no valid token vectors')
    return positions


def checked_matrix(matrix, maximum):
    if (not 1 <= len(matrix) <= maximum or any(len(row) != WIDTH for row in matrix)
            or any(type(v) not in (int, float) or not math.isfinite(v) for row in matrix for v in row)
            or any(abs(sum(v*v for v in row)-1) > .0001 for row in matrix)):
        raise ValueError('invalid token matrix shape, values or unit norm')
    return matrix


def verify_reranking(query, documents, candidates, hits):
    """Float64 dot/max/sum oracle, independent of native scoring and fusion."""
    import numpy as np
    if (len(candidates) > CANDIDATES or len(set(candidates)) != len(candidates)
            or not set(candidates) <= set(documents)):
        raise ValueError('invalid reranking candidate domain')
    if not candidates:
        return check_score_order(candidates, hits, {})
    q = np.asarray(query, dtype=np.float64)
    scores = {key: float((q @ np.asarray(documents[key], dtype=np.float64).T).max(axis=1).sum())
              for key in candidates}
    # A scalar implementation checks the vectorized oracle for one real pair.
    doc = documents[candidates[0]]
    scalar = sum(max(sum(a*b for a, b in zip(qrow, drow)) for drow in doc) for qrow in query)
    if abs(scalar-scores[candidates[0]]) > 1.e-9:
        raise ValueError('vectorized oracle differs from scalar dot/max/sum')
    return check_score_order(candidates, hits, scores)


def check_score_order(candidates, hits, scores):
    if (len(candidates) > CANDIDATES or len(set(candidates)) != len(candidates)
            or set(scores) != set(candidates) or not all(math.isfinite(v) for v in scores.values())
            or len(hits) != min(10, len(candidates))
            or len({h['key'] for h in hits}) != len(hits)
            or not {h['key'] for h in hits} <= set(candidates)):
        raise ValueError('invalid or escaped reranking candidate domain')
    tolerance = 5.e-5
    expected = sorted(scores.values(), reverse=True)[:10]
    errors = []
    for i, hit in enumerate(hits):
        error = abs(hit['score'] - scores[hit['key']])
        if (hit['rank'] != i+1 or not math.isfinite(hit['score']) or error > tolerance
                or abs(scores[hit['key']] - expected[i]) > tolerance):
            raise ValueError('native MaxSim differs from independent candidate-domain oracle')
        errors.append(error)
    return {'candidate_keys': candidates, 'candidate_scores_float64': scores,
            'maximum_score_error': max(errors, default=0), 'absolute_tolerance': tolerance,
            'scope': 'exact only within the recorded candidate set; ties within tolerance permit either order'}


def install_outputs(sql, ready, table, contract, vectors, literal):
    seen = set()
    started = time.monotonic()
    receipts = []
    while len(seen) < len(vectors['documents']):
        requests = json.loads(sql('SELECT qdrant.encoding_inputs('+literal(table)+",'tokens',100)"))
        if not requests:
            raise ValueError('missing pending token encoding inputs')
        for row in requests:
            key = row['key']['value']
            if (row['key']['type'] != 'text' or key in seen or key not in vectors['documents']
                    or row['model'] != contract or row['source_fingerprint'] != vectors['documents'][key]['text_sha256']
                    or hashlib.sha256(row['text'].encode()).hexdigest() != vectors['documents'][key]['text_sha256']):
                raise ValueError('trusted token encoding input differs')
            receipts.append(write_output(sql, table, contract, vectors['documents'][key]['vector'], row, literal))
            seen.add(key)
    status = ready(table, timeout=300)
    if status['representations']['tokens']['rows']['ready'] != len(seen):
        raise ValueError('incomplete durable token coverage')
    return {'rows': len(seen), 'durable_batches': len(receipts), 'seconds': time.monotonic()-started,
            'generation': receipts[-1]['generation']}


def durable(sql, command, table, literal):
    import uuid
    ticket = sql('BEGIN; '+command+'; SELECT qdrant.track_changes('+literal(table)+'); COMMIT')
    uuid.UUID(ticket)
    waited = json.loads(sql('SELECT qdrant.await_changes('+literal(ticket)+',30000)'))
    if (not all(waited.get(k) is True for k in ['committed', 'applied', 'durable'])
            or waited['pending_events'] != 0 or waited['timed_out'] or waited.get('error')):
        raise ValueError('output ticket did not reach committed/apply/flush/ACK: '+json.dumps(waited))
    return waited


def write_output(sql, table, contract, vector, row, literal):
    values = {contract['vector_field']: json.dumps(vector, separators=(',', ':'), allow_nan=False),
        contract['fingerprint_field']: row['source_fingerprint'], contract['incarnation_field']: row['incarnation'],
        contract['model_id_field']: contract['model_id'], contract['model_version_field']: contract['model_version']}
    command = 'UPDATE ONLY '+table+' SET '+','.join(k+'='+literal(v) for k, v in values.items())+' WHERE id='+literal(row['key']['value'])
    return durable(sql, command, table, literal)


def verify_freshness(sql, ready, table, fixture, token_contract, tokens, dense_contract, dense, literal, query_arguments):
    row = fixture['documents'][0]
    request_sql = 'SELECT qdrant.encoding_inputs('+literal(table)+",'tokens',100)"
    original = dict(key={'type': 'text', 'value': row['id']}, source_fingerprint=tokens['documents'][row['id']]['text_sha256'])
    # Retain the actual current incarnation; the source edits below preserve it.
    original['incarnation'] = sql('SELECT incarnation FROM qdrant_internal.source_state WHERE index_name='+literal(table)+
                                  " AND tagged_key->>'value'="+literal(row['id']))
    durable(sql, 'UPDATE ONLY '+table+' SET body='+literal(row['body']+' changed for freshness test')+' WHERE id='+literal(row['id']), table, literal)
    ready(table, timeout=300)
    failures = []
    for name, command in [('stale_query', 'SELECT count(*) FROM qdrant.search('+query_arguments+')'),
        ('late_model_output', 'UPDATE ONLY '+table+' SET '+token_contract['vector_field']+'='+
            literal(json.dumps(tokens['documents'][row['id']]['vector']))+' WHERE id='+literal(row['id']))]:
        try:
            sql(command)
        except RuntimeError as error:
            if '55000' not in str(error):
                raise
            failures.append(name)
        else:
            raise ValueError(name+' was unexpectedly accepted')
    pending = json.loads(sql(request_sql))
    if len(pending) != 1 or pending[0]['key']['value'] != row['id'] or pending[0]['source_fingerprint'] == original['source_fingerprint']:
        raise ValueError('source edit did not expose the new encoding contract')
    # Exact content restoration makes unchanged, matching outputs valid again.
    # Clear both slots first to exercise actual missing-output requests and writes.
    durable(sql, 'UPDATE ONLY '+table+' SET '+token_contract['vector_field']+'=NULL,'+
            dense_contract['vector_field']+'=NULL WHERE id='+literal(row['id']), table, literal)
    durable(sql, 'UPDATE ONLY '+table+' SET body='+literal(row['body'])+' WHERE id='+literal(row['id']), table, literal)
    for slot, contract, vectors in [('tokens', token_contract, tokens), ('dense', dense_contract, dense)]:
        requests = json.loads(sql('SELECT qdrant.encoding_inputs('+literal(table)+','+literal(slot)+',100)'))
        if (len(requests) != 1 or requests[0]['key']['value'] != row['id']
                or requests[0]['source_fingerprint'] != original['source_fingerprint']
                or requests[0]['incarnation'] != original['incarnation']):
            raise ValueError('restored source encoding identity differs')
        write_output(sql, table, contract, vectors['documents'][row['id']]['vector'], requests[0], literal)
    status = ready(table, timeout=300)
    if any(status['representations'][slot]['rows']['ready'] != len(fixture['documents']) for slot in ['tokens','dense']):
        raise ValueError('restored representations are incomplete')
    sql('SELECT count(*) FROM qdrant.search('+query_arguments+')')
    return {'negative_checks': failures, 'restored_after_exact_source_and_output': True,
            'durable': True, 'scope': 'one real source edit, rejected late model write, exact source restoration and both representations reinstalled'}


def load_tokens(directory, fixture, fixture_sha, contract_bytes):
    root = Path(directory)
    if root.is_symlink():
        raise ValueError('linked token output root')
    values = {}
    for name, maximum in [('manifest.json', 1024**2), ('scifact.json', 1024**2), ('scifact.f32', 32*1024**2)]:
        path = root / name
        if path.is_symlink() or not 0 < path.stat().st_size <= maximum:
            raise ValueError('linked or unbounded token output')
        values[name] = path.read_bytes()
    manifest = json.loads(values['manifest.json'])
    if (manifest['status'] != 'encoded' or manifest['schema_version'] != 1 or manifest['errors']
            or manifest['release_supported'] is not False
            or manifest['model_contract_sha256'] != hashlib.sha256(contract_bytes).hexdigest()
            or manifest['fixture_sha256'] != fixture_sha):
        raise ValueError('incompatible token encoding receipt')
    for name in ['scifact.json', 'scifact.f32']:
        row = manifest['outputs'][name]
        if row['bytes'] != len(values[name]) or row['sha256'] != hashlib.sha256(values[name]).hexdigest():
            raise ValueError('token output checksum differs')
    index = json.loads(values['scifact.json'])
    if index['fixture_sha256'] != fixture_sha:
        raise ValueError('token fixture identity differs')
    result = {'documents': {}, 'queries': {}}
    offset = 0
    for kind, textfield, maximum in [('documents', 'body', 128), ('queries', 'text', 48)]:
        expected = {r['id']: r[textfield] for r in fixture[kind]}
        if len(expected) != len(fixture[kind]) or set(index[kind]) != set(expected):
            raise ValueError('missing, duplicate or extra token identities')
        for key, text in expected.items():
            row = index[kind][key]
            tokens = row['tokens']
            if (type(tokens) is not int or not 0 < tokens <= maximum
                    or type(row['offset']) is not int or row['offset'] != offset
                    or type(row['encoded_tokens']) is not int
                    or type(row['untruncated_tokens']) is not int
                    or row['encoded_tokens'] != min(row['untruncated_tokens'], maximum)
                    or tokens > row['encoded_tokens']
                    or (kind == 'queries' and tokens != row['encoded_tokens'])
                    or row['text_sha256'] != hashlib.sha256(text.encode()).hexdigest()):
                raise ValueError('token text, range or truncation record differs')
            length = tokens * WIDTH * 4
            if offset + length > len(values['scifact.f32']):
                raise ValueError('token binary truncated')
            floats = struct.unpack_from('<' + 'f'*(tokens*WIDTH), values['scifact.f32'], offset)
            matrix = [list(floats[i:i+WIDTH]) for i in range(0, len(floats), WIDTH)]
            checked_matrix(matrix, maximum)
            result[kind][key] = dict(row, vector=matrix)
            offset += length
    if offset != len(values['scifact.f32']):
        raise ValueError('unrecorded trailing token bytes')
    return result, manifest, hashlib.sha256(values['manifest.json']).hexdigest()


def encode(root, fixtures, output):
    import importlib.metadata
    import numpy as np
    import onnxruntime as ort
    import resource
    from tokenizers import Tokenizer

    raw_contract = TOKEN_CONTRACT.read_bytes()
    contract = json.loads(raw_contract)
    prepare(root, contract)
    runtime = json.loads(CONTRACT.read_bytes())['runtime_wheels']
    for row in runtime:
        if row['name'] != 'pip' and importlib.metadata.version(row['name']) != row['version']:
            raise ValueError('encoding runtime differs from pinned wheels')
    directory = Path(output)
    directory.mkdir()
    report = {'schema_version': 1, 'status': 'encoding', 'release_supported': False, 'errors': [],
              'model_contract_sha256': hashlib.sha256(raw_contract).hexdigest(),
              'model_id': contract['model_id'], 'revision': contract['revision'],
              'dimensions': WIDTH, 'document_length': 128, 'query_length': 48, 'batch_size': 8,
              'runtime': {r['name']: r['version'] for r in runtime}, 'outputs': {}}
    def persist():
        temporary = directory / 'manifest.partial'
        temporary.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        os.replace(temporary, directory / 'manifest.json')
    started = time.monotonic()
    persist()
    try:
        model = Path(root) / 'colbert17m'
        config = json.loads((model / 'onnx_config.json').read_bytes())
        if (config['model_name'] != contract['model_id'] or config['embedding_dim'] != WIDTH
                or config['uses_token_type_ids'] or config['do_query_expansion']
                or config['query_length'] != 48 or config['document_length'] != 512
                or not config['do_lower_case'] or config['attend_to_expansion_tokens']):
            raise ValueError('publisher token model configuration differs')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        session = ort.InferenceSession(str(model / 'model.onnx'), options, providers=['CPUExecutionProvider'])
        if (session.get_providers() != ['CPUExecutionProvider']
                or {r.name for r in session.get_inputs()} != {'input_ids', 'attention_mask'}
                or [r.name for r in session.get_outputs()] != ['output']):
            raise ValueError('unexpected CPU token model graph')
        tokenizer = Tokenizer.from_file(str(model / 'tokenizer.json'))
        tokenizer.no_padding(); tokenizer.no_truncation()
        for field in ['query', 'document']:
            if tokenizer.token_to_id(config[field+'_prefix']) != config[field+'_prefix_id']:
                raise ValueError('publisher prefix token differs')
        skiplist = {tokenizer.token_to_id(word) for word in config['skiplist_words']}

        def infer(texts, is_query):
            maximum = 48 if is_query else 128
            prefix = config['query_prefix_id' if is_query else 'document_prefix_id']
            original = [tokenizer.encode(text.lower()).ids for text in texts]
            sequences = [prefixed_ids(ids, prefix, maximum) for ids in original]
            width = max(map(len, sequences))
            ids = np.full((len(texts), width), config['pad_token_id'], dtype=np.int64)
            attention = np.zeros_like(ids)
            for i, sequence in enumerate(sequences):
                ids[i, :len(sequence)] = sequence; attention[i, :len(sequence)] = 1
            hidden = session.run(['output'], {'input_ids': ids, 'attention_mask': attention})[0]
            if hidden.shape != (len(texts), width, WIDTH) or not np.isfinite(hidden).all():
                raise ValueError('unexpected token output shape or values')
            matrices = []
            for i in range(len(texts)):
                positions = kept_positions(ids[i].tolist(), attention[i].tolist(), skiplist, is_query)
                matrix = hidden[i, positions].astype('<f4')
                checked_matrix(matrix.tolist(), maximum)
                matrices.append(matrix)
            return matrices, [len(row)+1 for row in original], list(map(len, sequences))

        probes = ['search!', 'Retrieve scientific documents about treatment with stem cells.']
        differences = []
        for is_query in [True, False]:
            batch = infer(probes, is_query)[0]
            for i, text in enumerate(probes):
                solo = infer([text], is_query)[0][0]
                difference = float(np.max(np.abs(batch[i]-solo)))
                if batch[i].shape != solo.shape or difference > 1.e-5:
                    raise ValueError('token model depends on batch padding')
                differences.append(difference)
        report['native_padding_verification'] = {'probes': 4, 'maximum_absolute_difference': max(differences),
            'scope': 'pinned ONNX batch invariance and checked unit outputs; no PyTorch parity claim'}
        report['load_seconds'] = time.monotonic()-started
        source = json.loads((TOKEN_CONTRACT.parent / 'quality-sources.json').read_bytes())
        expected = next(d['fixture_sha256'] for d in source['datasets'] if d['dataset'] == 'scifact')
        raw = (Path(fixtures) / 'scifact.json').read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('token fixture identity mismatch')
        fixture = json.loads(raw)
        if len(fixture['documents']) != 500 or len(fixture['queries']) != 32:
            raise ValueError('fixed token input count differs')
        index = {'fixture_sha256': expected, 'documents': {}, 'queries': {}}
        report.update(fixture_sha256=expected, inputs=532, truncated_inputs=0, encoded_tokens=0,
                      stored_tokens=0, untruncated_tokens=0)
        encoding_started = time.monotonic()
        with (directory / 'scifact.f32').open('xb') as binary:
            for kind, field, maximum in [('documents', 'body', 128), ('queries', 'text', 48)]:
                for offset in range(0, len(fixture[kind]), 8):
                    rows = fixture[kind][offset:offset+8]
                    matrices, lengths, used = infer([row[field] for row in rows], kind == 'queries')
                    for row, matrix, length, count in zip(rows, matrices, lengths, used):
                        index[kind][row['id']] = {'text_sha256': hashlib.sha256(row[field].encode()).hexdigest(),
                            'offset': binary.tell(), 'tokens': len(matrix), 'untruncated_tokens': length, 'encoded_tokens': count}
                        binary.write(matrix.tobytes())
                        if binary.tell() > 32*1024**2:
                            raise ValueError('token binary budget exceeded')
                        report['truncated_inputs'] += int(length > maximum)
                        report['encoded_tokens'] += count; report['stored_tokens'] += len(matrix)
                        report['untruncated_tokens'] += length
        (directory / 'scifact.json').write_text(json.dumps(index, separators=(',', ':'))+'\n', encoding='utf-8')
        for name in ['scifact.json', 'scifact.f32']:
            data = (directory / name).read_bytes()
            report['outputs'][name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        report.update(status='encoded', encoding_seconds=time.monotonic()-encoding_started)
    except Exception as error:
        report['status'] = 'failed'; report['errors'].append(f'{type(error).__name__}: {error}')
        raise
    finally:
        report.update(total_seconds=time.monotonic()-started, process_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        persist()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ['root', 'fixtures', 'output']:
        parser.add_argument('--'+option, required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(encode(args.root, args.fixtures, args.output), indent=2))

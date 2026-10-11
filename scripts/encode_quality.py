#!/usr/bin/env python3
"""Encode fixed bounded quality fixtures locally with the pinned CPU ONNX model."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import time

from prepare_dense_quality import CONTRACT, prepare


def pool_tokens(hidden, attention):
    """Apply the declared masked mean and explicit float32 unit normalization."""
    import numpy as np
    if (hidden.ndim != 3 or hidden.shape != (*attention.shape, 384)
            or not np.isfinite(hidden).all()
            or not np.isin(attention, [0, 1]).all()
            or (attention.sum(axis=1) <= 0).any()):
        raise ValueError('invalid native model token output or attention mask')
    mask = attention.astype(np.float64)[..., None]
    mean = (hidden.astype(np.float64) * mask).sum(axis=1) / mask.sum(axis=1)
    norms = np.linalg.norm(mean, axis=1, keepdims=True)
    if not np.isfinite(norms).all() or (norms <= 0).any():
        raise ValueError('invalid pooled model output')
    vectors = (mean / norms).astype(np.float32)
    if not np.isfinite(vectors).all() or (np.abs((vectors.astype(np.float64) ** 2).sum(axis=1) - 1) > .0001).any():
        raise ValueError('invalid normalized float32 output')
    return vectors


def verify_pooling():
    """Independent scalar goldens require padding exclusion and token inclusion."""
    import numpy as np
    hidden = np.zeros((2, 3, 384), dtype=np.float32)
    hidden[0, 0, :2] = [2, 3]
    hidden[0, 1, :2] = [4, 5]
    hidden[0, 2] = 1.e6  # This padding must have no effect.
    hidden[1, :, :2] = [[0, 3], [0, 6], [0, 6]]
    vectors = pool_tokens(hidden, np.array([[1, 1, 0], [1, 1, 1]]))
    expected = np.zeros((2, 384), dtype=np.float32)
    expected[:, :2] = [[.6, .8], [0, 1]]
    if not np.allclose(vectors, expected, rtol=0, atol=1.e-7):
        raise ValueError('masked mean normalization differs from independent goldens')
    for mask in [np.zeros((2, 3)), np.full((2, 3), 2)]:
        try:
            pool_tokens(hidden, mask)
        except ValueError:
            continue
        raise ValueError('invalid attention mask was accepted')
    return {'scalar_goldens': 2, 'invalid_mask_refusals': 2}


def encode(root, fixtures, output):
    contract_bytes = CONTRACT.read_bytes()
    contract = json.loads(contract_bytes)
    prepare(root, contract)
    source = Path(__file__).resolve().parents[1]
    expected = json.loads((source / 'ci/quality-sources.json').read_bytes())
    model = Path(root) / 'minilm'
    config = json.loads((model / '1_Pooling/config.json').read_bytes())
    tokenizer_config = json.loads((model / 'sentence_bert_config.json').read_bytes())
    if (config != {'word_embedding_dimension': 384, 'pooling_mode_cls_token': False,
                  'pooling_mode_mean_tokens': True, 'pooling_mode_max_tokens': False,
                  'pooling_mode_mean_sqrt_len_tokens': False}
            or tokenizer_config != {'max_seq_length': 128, 'do_lower_case': False}):
        raise ValueError('publisher pooling or truncation contract differs')
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    for row in contract['runtime_wheels']:
        if row['name'].lower() != 'pip' and importlib.metadata.version(row['name']) != row['version']:
            raise ValueError('optional encoding runtime differs from pinned wheels')
    destination = Path(output)
    destination.mkdir()  # An existing output or incomplete prior run is preserved.
    report = {'schema_version': 1, 'status': 'encoding', 'release_supported': False,
              'model_contract_sha256': hashlib.sha256(contract_bytes).hexdigest(),
              'model_id': contract['model_id'], 'revision': contract['revision'],
              'pooling': 'attention-mask mean including special tokens', 'normalization': 'unit',
              'dimensions': 384, 'max_tokens': 128, 'batch_size': 8,
              'runtime': {r['name']: r['version'] for r in contract['runtime_wheels']},
              'datasets': [], 'errors': []}

    def persist():
        temporary = destination / 'manifest.partial'
        temporary.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, destination / 'manifest.json')

    persist()
    started = time.monotonic()
    try:
        report['pooling_verification'] = verify_pooling()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        session = ort.InferenceSession(str(model / 'onnx/model.onnx'), options, providers=['CPUExecutionProvider'])
        if session.get_providers() != ['CPUExecutionProvider']:
            raise ValueError('CPU-only execution required')
        names = {node.name for node in session.get_inputs()}
        if names != {'input_ids', 'attention_mask', 'token_type_ids'}:
            raise ValueError('unexpected model input graph')
        tokenizer = Tokenizer.from_file(str(model / 'tokenizer.json'))
        pad = tokenizer.token_to_id('<pad>')
        if pad is None:
            raise ValueError('publisher pad token missing')
        tokenizer.enable_padding(pad_id=pad, pad_token='<pad>')
        tokenizer.enable_truncation(max_length=128, direction='right')

        def infer(tokens):
            feed = {'input_ids': np.asarray([r.ids for r in tokens], dtype=np.int64),
                    'attention_mask': np.asarray([r.attention_mask for r in tokens], dtype=np.int64),
                    'token_type_ids': np.asarray([r.type_ids for r in tokens], dtype=np.int64)}
            return pool_tokens(session.run(None, feed)[0], feed['attention_mask'])

        probes = ['search', '检索数据库中的中文和 English documents using the same pinned model.']
        together = infer(tokenizer.encode_batch(probes))
        separate = np.concatenate([infer(tokenizer.encode_batch([text])) for text in probes])
        difference = float(np.max(np.abs(together - separate)))
        if not np.allclose(together, separate, rtol=0, atol=1.e-5):
            raise ValueError('native model embeddings depend on batch padding')
        report['pooling_verification'].update(native_padding_probes=len(probes),
            maximum_absolute_batch_difference=difference,
            scope='scalar pooling goldens and native batch padding invariance; no cross-framework parity claim')
        report['load_seconds'] = time.monotonic() - started
        for dataset in expected['datasets']:
            name = dataset['dataset']
            raw = (Path(fixtures) / (name + '.json')).read_bytes()
            if hashlib.sha256(raw).hexdigest() != dataset['fixture_sha256']:
                raise ValueError('quality fixture differs from fixed selection')
            fixture = json.loads(raw)
            rows = [('documents', row['id'], row['body']) for row in fixture['documents']]
            rows += [('queries', row['id'], row['text']) for row in fixture['queries']]
            if not 0 < len(rows) <= 1100:
                raise ValueError('bounded encoding input count exceeded')
            encoded = {'documents': {}, 'queries': {}, 'fixture_sha256': dataset['fixture_sha256']}
            measured = {'dataset': name, 'inputs': len(rows), 'truncated_inputs': 0,
                        'untruncated_tokens': 0, 'encoded_tokens': 0, 'fixture_sha256': dataset['fixture_sha256']}
            dataset_started = time.monotonic()
            for offset in range(0, len(rows), 8):
                batch = rows[offset:offset + 8]
                tokenizer.no_padding()
                tokenizer.no_truncation()
                lengths = [len(item.ids) for item in tokenizer.encode_batch([r[2] for r in batch])]
                tokenizer.enable_padding(pad_id=pad, pad_token='<pad>')
                tokenizer.enable_truncation(max_length=128, direction='right')
                tokens = tokenizer.encode_batch([r[2] for r in batch])
                vectors = infer(tokens)
                for (kind, key, body), vector, length, token in zip(batch, vectors, lengths, tokens):
                    if key in encoded[kind]:
                        raise ValueError('duplicate encoded source identity')
                    used = sum(token.attention_mask)
                    encoded[kind][key] = {'vector': vector.tolist(), 'text_sha256': hashlib.sha256(body.encode()).hexdigest(),
                                          'untruncated_tokens': length, 'encoded_tokens': used}
                    measured['truncated_inputs'] += int(length > 128)
                    measured['untruncated_tokens'] += length
                    measured['encoded_tokens'] += used
            data = (json.dumps(encoded, separators=(',', ':'), allow_nan=False) + '\n').encode()
            (destination / (name + '.json')).write_bytes(data)
            measured.update(encoding_seconds=time.monotonic() - dataset_started,
                            output_sha256=hashlib.sha256(data).hexdigest(), output_bytes=len(data))
            report['datasets'].append(measured)
            persist()
        report['status'] = 'encoded'
    except Exception as error:
        report['status'] = 'failed'
        report['errors'].append(f'{type(error).__name__}: {error}')
        raise
    finally:
        report['total_seconds'] = time.monotonic() - started
        report['process_peak_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        persist()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(encode(args.root, args.fixtures, args.output), indent=2))


if __name__ == '__main__':
    main()

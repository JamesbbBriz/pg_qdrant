"""Bind measured embeddings to exact fixture text and the optional model contract."""
import hashlib
import json
import math
from pathlib import Path


def validate_measurement(measured, fixtures, plans):
    """Require every frozen query exactly once for every declared plan."""
    if (measured.get('status') != 'measured' or measured.get('errors') != []
            or measured.get('full_benchmark') is not False
            or measured.get('release_supported') is not False):
        raise ValueError('measurement is incomplete or exceeds its declared scope')
    datasets = measured.get('datasets', [])
    if len(datasets) != len(fixtures) or {d['dataset'] for d in datasets} != set(fixtures):
        raise ValueError('measured dataset identities differ')
    for dataset in datasets:
        expected = fixtures[dataset['dataset']]
        queries = dataset.get('queries', [])
        if (dataset.get('status') != 'measured'
                or dataset.get('fixture_sha256') != expected['sha256']
                or len(queries) != len(expected['query_ids']) * len(plans)
                or {q['plan'] for q in queries} != set(plans)):
            raise ValueError('incomplete fixed query/plan measurement')
        for plan in plans:
            identities = [q['query_id'] for q in queries if q['plan'] == plan]
            if len(identities) != len(expected['query_ids']) or set(identities) != set(expected['query_ids']):
                raise ValueError('missing, duplicate or substituted fixed query measurement')
    return measured


def validate_vectors(encoded, fixture):
    for kind, field, text in [('documents', 'documents', 'body'), ('queries', 'queries', 'text')]:
        expected = {r['id']: r[text] for r in fixture[field]}
        if len(expected) != len(fixture[field]) or set(encoded[kind]) != set(expected):
            raise ValueError('encoded fixture identities differ')
        for key, body in expected.items():
            row = encoded[kind][key]
            vector = row['vector']
            if (row['text_sha256'] != hashlib.sha256(body.encode()).hexdigest()
                    or not isinstance(vector, list) or len(vector) != 384
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
                    or abs(sum(v * v for v in vector) - 1) > .0001
                    or type(row['encoded_tokens']) is not int or not 0 < row['encoded_tokens'] <= 128
                    or type(row['untruncated_tokens']) is not int
                    or row['encoded_tokens'] != min(row['untruncated_tokens'], 128)):
                raise ValueError('embedding text, shape, normalization or token record differs')
    return encoded


def load_vectors(directory, name, fixture, fixture_sha, contract_bytes):
    root = Path(directory)
    if name not in ('scifact', 't2retrieval') or root.is_symlink():
        raise ValueError('unknown dataset or linked encoding root')
    manifest_path = root / 'manifest.json'
    if manifest_path.is_symlink() or not 0 < manifest_path.stat().st_size <= 1024 * 1024:
        raise ValueError('linked or unbounded encoding receipt')
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    if (manifest['status'] != 'encoded' or manifest['schema_version'] != 1
            or manifest['model_contract_sha256'] != hashlib.sha256(contract_bytes).hexdigest()
            or manifest['release_supported'] is not False or manifest['errors']):
        raise ValueError('incomplete or incompatible encoding receipt')
    rows = [r for r in manifest['datasets'] if r['dataset'] == name]
    if len(rows) != 1 or rows[0]['fixture_sha256'] != fixture_sha:
        raise ValueError('encoding fixture receipt differs')
    path = root / (name + '.json')
    if path.is_symlink() or not 0 < path.stat().st_size <= 16 * 1024 * 1024:
        raise ValueError('encoding artifact is linked or outside budget')
    raw = path.read_bytes()
    if len(raw) != rows[0]['output_bytes'] or hashlib.sha256(raw).hexdigest() != rows[0]['output_sha256']:
        raise ValueError('encoding artifact checksum differs')
    value = json.loads(raw)
    if value['fixture_sha256'] != fixture_sha:
        raise ValueError('encoded fixture differs')
    return validate_vectors(value, fixture), manifest, hashlib.sha256(manifest_raw).hexdigest()

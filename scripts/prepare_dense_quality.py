#!/usr/bin/env python3
"""Prepare checksum-pinned optional model inputs; never load or run model code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request
from urllib.parse import urlsplit

CONTRACT = Path(__file__).resolve().parents[1] / 'ci/dense-quality-model.json'
MAX_ASSET_BYTES = 1024 ** 3


def asset_admission(root, rows):
    """Count old files and partials as well as the current declared inputs."""
    total = 0
    files = 0
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in folders + names:
            child = Path(directory) / name
            if child.is_symlink() or (hasattr(child, 'is_junction') and child.is_junction()):
                raise ValueError('linked optional model cache component refused')
        for name in names:
            child = Path(directory) / name
            if not child.is_file():
                raise ValueError('unsupported optional model cache entry')
            total += child.stat().st_size
            files += 1
            if total > MAX_ASSET_BYTES or files > 256:
                raise ValueError('optional model cache capacity exhausted; existing files retained')
    missing = sum(row['bytes'] for name, row in rows if not (root / name).exists())
    if total + missing > MAX_ASSET_BYTES:
        raise ValueError('optional model cache admission refused; existing files retained')


def entries(contract):
    revision = contract['revision']
    model = contract['model_id']
    if (contract['schema_version'] != 1 or contract['release_supported'] is not False
            or not re.fullmatch('[a-f0-9]{40}', revision)
            or model != 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
            or contract['dimensions'] != 384 or contract['normalization'] != 'unit'
            or contract['max_tokens'] != 128 or contract['pooling'] != 'attention-mask mean'
            or not 1 <= len(contract['files']) <= 16 or not 1 <= len(contract['runtime_wheels']) <= 48):
        raise ValueError('unsupported optional model contract')
    result = []
    for row in contract['files']:
        name = row['path']
        if row['url'] != f'https://huggingface.co/{model}/resolve/{revision}/{name}':
            raise ValueError('model URL must identify the fixed publisher revision')
        result.append(('minilm/' + name, row))
    for row in contract['runtime_wheels']:
        url = urlsplit(row['url'])
        if (url.scheme != 'https' or url.netloc != 'files.pythonhosted.org'
                or not url.path.startswith('/packages/') or url.query or url.fragment
                or any(p in ('', '.', '..') for p in url.path[1:].split('/'))
                or url.path.rsplit('/', 1)[-1] != row['file'] or not row['file'].endswith('.whl')):
            raise ValueError('publisher wheel URL required')
        result.append(('wheels/' + row['file'], row))
    seen = set()
    for name, row in result:
        if (name in seen or '\\' in name or ':' in name or name.startswith('/')
                or any(p in ('', '.', '..') for p in name.split('/'))
                or type(row['bytes']) is not int or not 0 < row['bytes'] <= 512 * 1024 * 1024
                or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
            raise ValueError('ambiguous or unbounded model asset')
        seen.add(name)
    if sum(row['bytes'] for _, row in result) > 1024 * 1024 * 1024:
        raise ValueError('optional model asset budget exceeded')
    return result


def validate(path, row):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes']:
        raise ValueError('missing, linked or incorrectly sized model asset: ' + path.name)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != row['sha256']:
        raise ValueError('model asset checksum mismatch: ' + path.name)


def prepare(root, contract, download=False):
    root = Path(root)
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise ValueError('linked model root refused')
    if download:
        root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    rows = entries(contract)
    asset_admission(root, rows)
    for name, row in rows:
        path = root / name
        if any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(root)):
            raise ValueError('linked model asset component refused')
        if not path.exists() and download:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + '.partial')
            # Exclusive temporary creation preserves interrupted or unexpected files.
            with temporary.open('xb') as target, urllib.request.urlopen(row['url'], timeout=60) as response:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > row['bytes']:
                        raise ValueError('publisher response exceeded declared size')
                    target.write(chunk)
            validate(temporary, row)
            if path.exists() or path.is_symlink():
                raise ValueError('model asset appeared during acquisition')
            os.link(temporary, path)  # Exclusive publication, including concurrent acquisition.
            temporary.unlink()
        validate(path, row)
    return {name: row['sha256'] for name, row in entries(contract)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--download', action='store_true', help='download only absent checksum-pinned assets')
    args = parser.parse_args()
    contract = json.loads(CONTRACT.read_bytes())
    files = prepare(args.root, contract, args.download)
    print(json.dumps({'status': 'inputs_verified', 'files': files, 'evaluation_executed': False}))


if __name__ == '__main__':
    main()

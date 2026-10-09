#!/usr/bin/env python3
"""Collect unchanged notice evidence; never declare distribution review complete."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tomllib

MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
NOTICE_NAME = re.compile(r'^(?:LICENSE|LICENCE|COPYING|COPYRIGHT|NOTICE|AUTHORS)(?:[._-].*)?$', re.I)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative(value):
    if (not isinstance(value,str) or not value or '\\' in value or ':' in value
            or value.startswith('/') or any(p in ('','.','..') for p in value.split('/'))):
        raise ValueError('ambiguous notice path')
    return value


class NoticeWriter:
    def __init__(self,destination):
        self.root=Path(destination).absolute()
        if self.root.parent.resolve(strict=True)!=self.root.parent:
            raise ValueError('notice destination parent must be canonical')
        self.root.mkdir(mode=0o700)
        self.total=0
        self.files=[]

    def copy(self,source,name,expected_sha=None):
        relative(name)
        source=Path(source)
        if source.is_symlink() or not source.is_file() or source.stat().st_size>MAX_FILE:
            raise ValueError('invalid or oversized notice input')
        data=source.read_bytes()
        digest=hashlib.sha256(data).hexdigest()
        if expected_sha is not None and digest!=expected_sha:
            raise ValueError('notice changed after original verification')
        if len(data)>MAX_FILE or self.total+len(data)>MAX_TOTAL:
            raise ValueError('notice byte budget exceeded')
        destination=self.root/name
        destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with destination.open('xb') as stream:
            stream.write(data)
        row={'path':name,'sha256':digest,'bytes':len(data)}
        self.total+=len(data)
        self.files.append(row)
        return row


def confined(root,name):
    relative(name)
    root=Path(root).resolve(strict=True)
    source=root/name
    if any((root/Path(*Path(name).parts[:n])).is_symlink() for n in range(1,len(Path(name).parts)+1)):
        raise ValueError('symlink notice input')
    if not source.resolve(strict=True).is_relative_to(root) or not source.is_file():
        raise ValueError('notice source escapes package')
    return source


def crate_member_matches(archive,member_name,path):
    """Bind copied text to the checksum-verified original archive, not its cache metadata."""
    with tarfile.open(archive,'r:gz') as source:
        found=[]
        for index,member in enumerate(source):
            if index>=20000:
                raise ValueError('crate member inspection budget exceeded')
            if member.name==member_name:
                found.append(member)
        if len(found)!=1 or not found[0].isfile() or not 0<=found[0].size<=MAX_FILE:
            raise ValueError('missing, duplicate or invalid original notice member')
        digest=hashlib.file_digest(source.extractfile(found[0]),'sha256').hexdigest()
        if digest!=sha(path):
            raise ValueError('notice differs from original crate member')
        return digest


def collect_rust(metadata,lock_bytes,writer,cargo_home,project_root):
    packages=tomllib.loads(lock_bytes.decode('utf-8'))['package']
    locked={(p['name'],p['version'],p.get('source')):p for p in packages}
    nodes={p['id']:p for p in metadata['resolve']['nodes']}
    workspace=set(metadata['workspace_members'])
    rows=[]
    for package in sorted(metadata['packages'],key=lambda p:(p['name'],p['version'],p['source'] or '')):
        if package['id'] not in nodes:
            continue
        name,version,source=package['name'],package['version'],package['source']
        if not re.fullmatch(r'[A-Za-z0-9_-]+',name) or not re.fullmatch(r'[A-Za-z0-9.+_-]+',version):
            raise ValueError('invalid package coordinate')
        entry=locked[(name,version,source)]
        root=Path(package['manifest_path']).parent.resolve(strict=True)
        is_workspace=package['id'] in workspace
        if source is None and not is_workspace:
            raise ValueError('unreviewed non-workspace path dependency')
        checksums=None
        crate_verified=False
        if is_workspace:
            if not root.is_relative_to(project_root.resolve(strict=True)):
                raise ValueError('workspace notice outside project')
        else:
            registry=(cargo_home/'registry/src').resolve(strict=True)
            if not root.is_relative_to(registry) or root.parent.parent!=registry:
                raise ValueError('unsupported source layout; registry crate required')
            checksum_path=root/'.cargo-checksum.json'
            if checksum_path.exists() or checksum_path.is_symlink():
                checksums=json.loads(confined(root,'.cargo-checksum.json').read_text(encoding='utf-8'))
                if checksums['package']!=entry['checksum']:
                    raise ValueError('crate checksum differs from Cargo.lock')
            archive=cargo_home/'registry/cache'/root.parent.name/(name+'-'+version+'.crate')
            if archive.exists():
                if archive.is_symlink() or sha(archive)!=entry['checksum']:
                    raise ValueError('cached crate archive differs from Cargo.lock')
                crate_verified=True
            if not crate_verified and checksums is None:
                raise ValueError('neither original crate archive nor file checksum evidence is available')
        candidates={p.name for p in root.iterdir() if p.is_file() and NOTICE_NAME.fullmatch(p.name)}
        declared=package.get('license_file')
        if declared:
            candidates.add(relative(declared))
        copied=[]
        identifier=hashlib.sha256((source or 'workspace').encode()).hexdigest()[:12]
        for candidate in sorted(candidates):
            path=confined(root,candidate)
            expected=checksums['files'].get(candidate) if checksums is not None else None
            if checksums is not None and expected!=sha(path):
                raise ValueError('notice differs from crate file checksum')
            if crate_verified:
                expected=crate_member_matches(archive,name+'-'+version+'/'+candidate,path)
            copied.append(writer.copy(path,'rust/'+name+'-'+version+'-'+identifier+'/'+candidate,expected))
        rows.append({'package':name+'@'+version,'source':source or 'workspace',
            'crate_checksum':entry.get('checksum'),'cached_crate_archive_verified':crate_verified,
            'license_expression':package.get('license'),'declared_license_file':declared,
            'features':sorted(nodes[package['id']]['features']),'notices':copied,
            'discovery':'top-level conventional names plus explicit license_file; bundled/nested materials require review'})
    return rows


def collect_native(inventory,writer,doc_root):
    rows=[]
    for line in sorted(inventory.splitlines()):
        if not line:
            continue
        package,version=line.split('\t')
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?',package):
            raise ValueError('invalid installed package name')
        name=package.split(':')[0]
        path=doc_root/name/'copyright'
        row={'package':package,'version':version,'copyright':None}
        if path.exists():
            # Debian documentation often deliberately shares a copyright file.
            # Follow only a resolved target confined to the installed doc tree.
            resolved=path.resolve(strict=True)
            if not resolved.is_relative_to(doc_root.resolve(strict=True)):
                raise ValueError('native copyright escapes installed documentation')
            row['copyright']=writer.copy(resolved,'native/'+package.replace(':','_')+'/copyright')
            row['installed_document']=resolved.relative_to(doc_root.resolve(strict=True)).as_posix()
        rows.append(row)
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/src'))
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.root.resolve(strict=True)
    lock=(root/'Cargo.lock').read_bytes()
    metadata=json.loads(subprocess.check_output(['cargo','metadata','--frozen','--format-version','1',
        '--no-default-features','--features','pg_qdrant/pg17,pg_qdrant/p0-managed-helper',
        '--filter-platform','x86_64-unknown-linux-gnu'],cwd=root,timeout=120))
    before=subprocess.check_output(['dpkg-query','-W','-f=${binary:Package}\t${Version}\n'],text=True)
    expected=(root/'artifacts/native-packages.tsv').read_text(encoding='utf-8')
    if sorted(before.splitlines())!=sorted(expected.splitlines()):
        raise ValueError('installed native inventory changed since build')
    writer=NoticeWriter(args.output)
    report={'schema_version':1,'kind':'unchanged_notice_evidence','status':'collecting',
        'release_supported':False,'license_review_complete':False,
        'cargo_lock_sha256':hashlib.sha256(lock).hexdigest(),
        'target':'x86_64-unknown-linux-gnu',
        'requested_features':['pg_qdrant/pg17','pg_qdrant/p0-managed-helper'],
        'limitations':['Resolved workspace graph is not binary linkage evidence.',
            'Conventional notice discovery does not establish complete bundled/native material coverage.',
            'Metadata expressions and collected texts do not establish distribution compliance.']}
    try:
        report['project_notices']=[writer.copy(confined(root,name),'project/'+name)
            for name in ['LICENSE','NOTICE']]
        report['rust']=collect_rust(metadata,lock,writer,Path(os.environ['CARGO_HOME']),root)
        report['native']=collect_native(before,writer,Path('/usr/share/doc'))
        if (root/'Cargo.lock').read_bytes()!=lock:
            raise ValueError('lock changed during collection')
        after=subprocess.check_output(['dpkg-query','-W','-f=${binary:Package}\t${Version}\n'],text=True)
        if sorted(after.splitlines())!=sorted(before.splitlines()):
            raise ValueError('installed native inventory changed during collection')
        report['status']='collected'
    except Exception as error:
        report.update(status='failed',error=f'{type(error).__name__}: {error}')
        raise
    finally:
        report.update(files=writer.files,total_bytes=writer.total)
        (writer.root/'manifest.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':report['status'],'rust_packages':len(report['rust']),
        'native_packages':len(report['native']),'notice_files':len(writer.files),
        'rust_without_notice':[p['package'] for p in report['rust'] if not p['notices']],
        'native_without_copyright':[p['package'] for p in report['native'] if not p['copyright']]}))


if __name__=='__main__':
    main()

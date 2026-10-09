import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location('upstream_collector',Path(__file__).resolve().parents[1]/'collect_notices.py')
MODULE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class UpstreamNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cargo=self.root/'cargo'
        self.source=self.cargo/'registry/src/test/example-1.0.0';self.source.mkdir(parents=True)
        self.bundle=self.root/'bundle';self.bundle.mkdir()
        self.text=b'Original upstream copyright\r\nPermission retained.\n'
        (self.bundle/'LICENSE').write_bytes(self.text)
        self.revision='a'*40;self.repository='https://github.com/example/project'
        self.vcs={'git':{'sha1':self.revision},'path_in_vcs':'component'}
        self.cargo_text=b'[package]\nname="example"\nversion="1.0.0"\nrepository="https://github.com/example/project/"\n'
        self.archive=self.cargo/'registry/cache/test/example-1.0.0.crate';self.archive.parent.mkdir(parents=True)
        self.write_archive()
        self.package={'name':'example','version':'1.0.0','id':'opaque','source':'registry+test',
            'license':'MIT','manifest_path':str(self.source/'Cargo.toml')}
        self.metadata={'packages':[self.package],'resolve':{'nodes':[{'id':'opaque'}]}}
        self.row={'package':'example@1.0.0','source':'registry+test','checksum':MODULE.sha(self.archive),
            'license':'MIT','repository':self.repository,'commit':self.revision,'path_in_vcs':'component',
            'crate_members':self.member_hashes(), 'notices':[{'path':'LICENSE','file':'LICENSE',
                'url':'https://raw.githubusercontent.com/example/project/'+self.revision+'/LICENSE',
                'sha256':hashlib.sha256(self.text).hexdigest(),'bytes':len(self.text),
                'git_blob_sha1':hashlib.sha1(b'blob '+str(len(self.text)).encode()+b'\0'+self.text).hexdigest()}]}
        self.manifest={'schema_version':1,'kind':'pinned_upstream_notice_evidence',
            'license_review_complete':False,'release_supported':False,'packages':[self.row]}
        self.writer=MODULE.NoticeWriter(self.root/'output')

    def values(self):
        return {'Cargo.toml':self.cargo_text,'.cargo_vcs_info.json':json.dumps(self.vcs).encode()}

    def member_hashes(self):
        return {k:hashlib.sha256(v).hexdigest() for k,v in self.values().items()}

    def write_archive(self):
        with tarfile.open(self.archive,'w:gz') as archive:
            for name,data in self.values().items():
                info=tarfile.TarInfo('example-1.0.0/'+name);info.size=len(data)
                archive.addfile(info,io.BytesIO(data))

    def collect(self):
        (self.bundle/'manifest.json').write_text(json.dumps(self.manifest))
        lock=('version=4\n[[package]]\nname="example"\nversion="1.0.0"\nsource="registry+test"\nchecksum="'+MODULE.sha(self.archive)+'"\n').encode()
        return MODULE.collect_upstream(self.metadata,lock,self.writer,self.cargo,self.bundle)

    def test_exact_original_text_and_vcs_association_without_clearance(self):
        result=self.collect()
        self.assertEqual(result[0]['commit'],self.revision)
        self.assertFalse(result[0]['license_review_complete'])
        self.assertEqual((self.writer.root/result[0]['notices'][0]['path']).read_bytes(),self.text)
        self.assertNotIn(str(self.root),json.dumps(result))

    def test_modified_text_or_git_blob_refused(self):
        self.row['notices'][0]['git_blob_sha1']='0'*40
        with self.assertRaisesRegex(ValueError,'text hash'):
            self.collect()
        self.assertEqual(self.writer.files,[])

    def test_changed_original_archive_contract_refused(self):
        self.row['checksum']='0'*64
        with self.assertRaisesRegex(ValueError,'resolved dependency'):
            self.collect()

    def test_changed_original_metadata_refused(self):
        self.row['crate_members']['.cargo_vcs_info.json']='0'*64
        with self.assertRaisesRegex(ValueError,'metadata hash'):
            self.collect()

    def test_dirty_or_mismatched_vcs_is_not_associated(self):
        for git in ({'sha1':'b'*40},{'sha1':self.revision,'dirty':True}):
            self.vcs['git']=git;self.write_archive()
            self.row['checksum']=MODULE.sha(self.archive);self.row['crate_members']=self.member_hashes()
            with self.subTest(git=git), self.assertRaisesRegex(ValueError,'VCS association'):
                self.collect()
        self.assertEqual(self.writer.files,[])

    def test_repository_or_component_mismatch_refused(self):
        self.row['path_in_vcs']='other'
        with self.assertRaisesRegex(ValueError,'VCS association'):
            self.collect()

    def test_non_ancestor_notice_refused(self):
        self.row['notices'][0]['path']='unrelated/LICENSE'
        self.row['notices'][0]['url']='https://raw.githubusercontent.com/example/project/'+self.revision+'/unrelated/LICENSE'
        with self.assertRaisesRegex(ValueError,'ancestor scope'):
            self.collect()

    def test_unpinned_url_and_unsafe_local_path_refused(self):
        notice=self.row['notices'][0];notice['url']=notice['url'].replace(self.revision,'main')
        with self.assertRaisesRegex(ValueError,'ancestor scope'):
            self.collect()
        notice['url']=notice['url'].replace('main',self.revision);notice['file']='../LICENSE'
        with self.assertRaisesRegex(ValueError,'ambiguous notice path'):
            self.collect()

    def test_symlink_text_refused_without_following(self):
        (self.bundle/'LICENSE').unlink();(self.bundle/'LICENSE').symlink_to(self.root/'outside')
        with self.assertRaisesRegex(ValueError,'symlink'):
            self.collect()

    def test_duplicate_package_refused(self):
        self.manifest['packages'].append(copy.deepcopy(self.row))
        with self.assertRaisesRegex(ValueError,'ambiguous or unresolved'):
            self.collect()

    def test_duplicate_original_metadata_member_refused(self):
        with tarfile.open(self.archive,'w:gz') as archive:
            for _ in range(2):
                info=tarfile.TarInfo('example-1.0.0/Cargo.toml');info.size=1
                archive.addfile(info,io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError,'original metadata member'):
            MODULE.original_metadata(self.archive,'example-1.0.0/Cargo.toml')

    def test_review_or_release_claim_refused(self):
        self.manifest['license_review_complete']=True
        with self.assertRaisesRegex(ValueError,'evidence contract'):
            self.collect()


if __name__=='__main__':
    unittest.main()

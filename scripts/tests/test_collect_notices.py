import hashlib
import importlib.util
import json
import io
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('collect_notices',Path(__file__).resolve().parents[1]/'collect_notices.py')
MODULE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NoticeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.project=self.root/'project';self.project.mkdir()
        self.cargo=self.root/'cargo'
        self.source=self.cargo/'registry/src/test-index/example-1.0.0'
        self.source.mkdir(parents=True)
        (self.source/'Cargo.toml').write_bytes(b'[package]\nname="example"\nversion="1.0.0"\n')
        self.original=b'Original copyright and license\r\nDo not change authors.\n'
        (self.source/'LICENSE-MIT').write_bytes(self.original)
        self.archive=self.cargo/'registry/cache/test-index/example-1.0.0.crate'
        self.archive.parent.mkdir(parents=True)
        with tarfile.open(self.archive,'w:gz') as archive:
            member=tarfile.TarInfo('example-1.0.0/LICENSE-MIT')
            member.size=len(self.original)
            archive.addfile(member,io.BytesIO(self.original))
        digest=MODULE.sha(self.archive)
        (self.source/'.cargo-checksum.json').write_text(json.dumps({'package':digest,
            'files':{'LICENSE-MIT':hashlib.sha256(self.original).hexdigest()}}),encoding='utf-8')
        self.lock=('version = 4\n[[package]]\nname="example"\nversion="1.0.0"\n'
            'source="registry+test"\nchecksum="'+digest+'"\n').encode()
        self.package={'id':'opaque-package-identity','name':'example','version':'1.0.0',
            'source':'registry+test','manifest_path':str(self.source/'Cargo.toml'),
            'license':'MIT','license_file':None}
        self.metadata={'packages':[self.package],'workspace_members':[],
            'resolve':{'nodes':[{'id':self.package['id'],'features':[]}]}}
        self.writer=MODULE.NoticeWriter(self.root/'output')

    def collect(self):
        return MODULE.collect_rust(self.metadata,self.lock,self.writer,self.cargo,self.project)

    def test_original_bytes_and_locked_crate_identity_preserved(self):
        rows=self.collect()
        self.assertTrue(rows[0]['cached_crate_archive_verified'])
        self.assertEqual((self.writer.root/rows[0]['notices'][0]['path']).read_bytes(),self.original)
        self.assertEqual(rows[0]['license_expression'],'MIT')
        self.assertNotIn(str(self.root),json.dumps(rows))

    def test_changed_notice_refused_before_any_copy(self):
        (self.source/'LICENSE-MIT').write_bytes(b'altered')
        with self.assertRaisesRegex(ValueError,'file checksum'):
            self.collect()
        self.assertEqual(self.writer.files,[])

    def test_changed_crate_archive_refused(self):
        self.archive.write_bytes(b'altered cached crate')
        with self.assertRaisesRegex(ValueError,'archive differs'):
            self.collect()

    def test_notice_change_between_original_verification_and_copy_is_rejected(self):
        verify=MODULE.crate_member_matches
        def change_after_verification(archive,member,path):
            expected=verify(archive,member,path)
            Path(path).write_bytes(b'changed after verification')
            return expected
        with patch.object(MODULE,'crate_member_matches',side_effect=change_after_verification):
            with self.assertRaisesRegex(ValueError,'changed after original verification'):
                self.collect()
        self.assertEqual(self.writer.files,[])

    def test_forged_extracted_checksum_cannot_replace_original_notice(self):
        (self.source/'LICENSE-MIT').write_bytes(b'forged original')
        path=self.source/'.cargo-checksum.json'
        checksums=json.loads(path.read_text(encoding='utf-8'))
        checksums['files']['LICENSE-MIT']=MODULE.sha(self.source/'LICENSE-MIT')
        path.write_text(json.dumps(checksums),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'original crate member'):
            self.collect()

    def test_absent_crate_archive_is_explicit_unverified_evidence(self):
        self.archive.unlink()
        self.assertFalse(self.collect()[0]['cached_crate_archive_verified'])

    def test_original_archive_verifies_notice_without_optional_cache_metadata(self):
        (self.source/'.cargo-checksum.json').unlink()
        row=self.collect()[0]
        self.assertTrue(row['cached_crate_archive_verified'])
        self.assertEqual((self.writer.root/row['notices'][0]['path']).read_bytes(),self.original)

    def test_without_cache_metadata_original_member_still_rejects_forged_notice(self):
        (self.source/'.cargo-checksum.json').unlink()
        (self.source/'LICENSE-MIT').write_bytes(b'forged notice')
        with self.assertRaisesRegex(ValueError,'original crate member'):
            self.collect()
        self.assertEqual(self.writer.files,[])

    def test_missing_original_and_cache_metadata_cannot_establish_notice_identity(self):
        self.archive.unlink()
        (self.source/'.cargo-checksum.json').unlink()
        with self.assertRaisesRegex(ValueError,'neither original'):
            self.collect()
        self.assertEqual(self.writer.files,[])

    def test_source_identity_must_match_lock_not_only_name_version(self):
        self.package['source']='registry+different'
        with self.assertRaises(KeyError):
            self.collect()

    def test_declared_notice_cannot_escape_crate(self):
        self.package['license_file']='../outside'
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            self.collect()

    def test_notice_writer_never_overwrites_or_exceeds_budget(self):
        first=self.writer.copy(self.source/'LICENSE-MIT','one.txt')
        with self.assertRaises(FileExistsError):
            self.writer.copy(self.source/'LICENSE-MIT','one.txt')
        self.assertEqual((self.writer.root/first['path']).read_bytes(),self.original)
        with patch.object(MODULE,'MAX_TOTAL',1),self.assertRaisesRegex(ValueError,'budget'):
            self.writer.copy(self.source/'LICENSE-MIT','two.txt')
        self.assertFalse((self.writer.root/'two.txt').exists())

    def test_native_shared_copyright_follows_only_confined_doc_target(self):
        docs=self.root/'docs';(docs/'common').mkdir(parents=True);(docs/'example').mkdir()
        copyright=docs/'common/copyright';copyright.write_bytes(self.original)
        (docs/'example/copyright').symlink_to(copyright)
        rows=MODULE.collect_native('example:amd64\t1.0.0\n',self.writer,docs)
        self.assertEqual(rows[0]['installed_document'],'common/copyright')
        self.assertEqual((self.writer.root/rows[0]['copyright']['path']).read_bytes(),self.original)

    def test_missing_notices_remain_explicit_without_license_fabrication(self):
        (self.source/'LICENSE-MIT').unlink()
        row=self.collect()[0]
        self.assertEqual(row['notices'],[])
        self.assertEqual(row['license_expression'],'MIT')
        docs=self.root/'docs';docs.mkdir()
        self.assertIsNone(MODULE.collect_native('missing\t1.0\n',self.writer,docs)[0]['copyright'])

    def test_native_copyright_outside_document_tree_is_rejected(self):
        docs=self.root/'docs';(docs/'example').mkdir(parents=True)
        outside=self.root/'outside';outside.write_bytes(self.original)
        (docs/'example/copyright').symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'escapes installed documentation'):
            MODULE.collect_native('example\t1.0\n',self.writer,docs)
        self.assertEqual(self.writer.files,[])
        self.assertEqual(outside.read_bytes(),self.original)


if __name__=='__main__':
    unittest.main()

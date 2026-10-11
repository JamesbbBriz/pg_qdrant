"""Optional measurements refuse changed text, forged vectors and unsafe assets."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.prepare_dense_quality import CONTRACT, entries, prepare, validate
from scripts.quality_vectors import load_vectors, validate_measurement, validate_vectors

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_quality


class DenseQuality(unittest.TestCase):
    def input_copies(self, source):
        payloads = {"fixtures/scifact.json": b"fixture", "model-assets/minilm/config.json": b"model",
                    "model-assets/wheels/runtime.whl": b"wheel"}
        for name, body in payloads.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        (source / "README.md").write_bytes(b"frozen source")
        return {"snapshot": {"files": [{"path": "README.md"}]},
                "fixtures": {"scifact.json": hashlib.sha256(b"fixture").hexdigest()},
                "model_inputs": {name.removeprefix("model-assets/"): {
                    "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
                    for name, body in payloads.items() if name.startswith("model-assets/")}}

    def test_successful_input_copy_retirement_keeps_source_and_original_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "snapshot"; source.mkdir()
            report = self.input_copies(source)
            original = root / "original-model"; original.write_bytes(b"model")
            receipt = local_quality.retire_measurement_inputs(source, report)
            self.assertEqual(receipt["retired_files"], 3)
            self.assertEqual(receipt["retired_bytes"], 17)
            self.assertFalse(list((source / "model-assets").rglob("*.whl")))
            self.assertFalse((source / "fixtures/scifact.json").exists())
            self.assertEqual((source / "README.md").read_bytes(), b"frozen source")
            self.assertEqual(original.read_bytes(), b"model")

    def test_changed_or_unrecorded_input_refuses_all_retirement(self):
        for kind in ("changed", "unknown", "missing", "source_overlap"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                source = Path(directory); report = self.input_copies(source)
                if kind == "changed": (source / "model-assets/minilm/config.json").write_bytes(b"wrong")
                if kind == "unknown": (source / "model-assets/unknown").write_bytes(b"keep")
                if kind == "missing": (source / "model-assets/minilm/config.json").unlink()
                if kind == "source_overlap": report["snapshot"]["files"].append({"path": "fixtures/scifact.json"})
                with self.assertRaises(ValueError): local_quality.retire_measurement_inputs(source, report)
                self.assertTrue((source / "fixtures/scifact.json").is_file())
                self.assertTrue((source / "model-assets/wheels/runtime.whl").is_file())

    def test_input_retirement_failure_pins_failed_source_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(local_quality, 'ROOT', Path(directory)),
                  patch.object(local_quality.secrets, 'token_hex', return_value='fixed-run'),
                  patch.object(local_quality.sys, 'argv', ['local_quality.py', '--image', 'selected-product']),
                  patch.object(local_quality.sys, 'stdout'), patch.object(local_quality.sys, 'stderr'),
                  patch.object(local_quality, 'require_clean_start'),
                  patch.object(local_quality, 'storage_preflight', return_value={}),
                  patch.object(local_quality, 'snapshot', return_value={'commit': 'fixed-commit'}),
                  patch.object(local_quality, 'measure', return_value=0),
                  patch.object(local_quality, 'finish_run', return_value={'status': 'passed'}),
                  patch.object(local_quality, 'complete_images', return_value={'errors': []}),
                  patch.object(local_quality, 'retire_measurement_inputs', side_effect=ValueError('changed input')),
                  patch.object(local_quality, 'finish_snapshot', return_value={}) as snapshots):
                self.assertEqual(local_quality.main(), 1)
                self.assertIs(snapshots.call_args.args[2], False)
            report = json.loads((Path(directory) / 'artifacts/local-ci/quality-fixed-run/run-report.json').read_bytes())
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['measurement_input_retention_error'], 'changed input')

    def test_launcher_records_snapshot_and_docker_failures(self):
        for stage in ['snapshot', 'docker']:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                with (patch.object(local_quality, 'ROOT', Path(directory)),
                      patch.object(local_quality.secrets, 'token_hex', return_value='fixed-run'),
                      patch.object(local_quality.sys, 'argv', ['local_quality.py', '--image', 'selected-product']),
                      patch.object(local_quality.sys, 'stdout'), patch.object(local_quality.sys, 'stderr'),
                      patch.object(local_quality, 'require_clean_start'),
                      patch.object(local_quality, 'storage_preflight', return_value={}),
                      patch.object(local_quality, 'finish_run', return_value={'status': 'passed'}) as cleanup,
                      patch.object(local_quality, 'complete_images', return_value={'errors': []}) as retention,
                      patch.object(local_quality, 'snapshot', **({'side_effect': RuntimeError('changed snapshot')}
                          if stage == 'snapshot' else {'return_value': {'commit': 'fixed-commit'}})),
                      patch.object(local_quality, 'output', side_effect=RuntimeError('Docker unavailable')) as docker):
                    self.assertEqual(local_quality.main(), 1)
                    cleanup.assert_called_once()
                    retention.assert_called_once()
                    self.assertEqual(retention.call_args.args[1], 'failed')
                    if stage == 'snapshot': docker.assert_not_called()
                report = json.loads((Path(directory) / 'artifacts/local-ci/quality-fixed-run/run-report.json').read_bytes())
                self.assertEqual(report['status'], 'failed')
                self.assertIs(report['release_supported'], False)
                self.assertIn('RuntimeError:', report['error'])
                self.assertNotIn('act_exit', report)
                if stage == 'docker': self.assertEqual(report['snapshot']['commit'], 'fixed-commit')

    def test_failed_diagnostic_export_prevents_image_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(local_quality, 'ROOT', Path(directory)),
                  patch.object(local_quality.secrets, 'token_hex', return_value='fixed-run'),
                  patch.object(local_quality.sys, 'argv', ['local_quality.py', '--image', 'selected-product']),
                  patch.object(local_quality.sys, 'stdout'), patch.object(local_quality.sys, 'stderr'),
                  patch.object(local_quality, 'require_clean_start'),
                  patch.object(local_quality, 'storage_preflight', return_value={}),
                  patch.object(local_quality, 'snapshot', return_value={}),
                  patch.object(local_quality, 'measure', return_value=0),
                  patch.object(local_quality, 'finish_run', return_value={'status': 'failed'}),
                  patch.object(local_quality, 'complete_images') as retention):
                self.assertEqual(local_quality.main(), 1)
                retention.assert_not_called()
            report = json.loads((Path(directory) / 'artifacts/local-ci/quality-fixed-run/run-report.json').read_bytes())
            self.assertEqual(report['status'], 'failed')

    def test_measurement_requires_exact_datasets_queries_and_plans(self):
        fixtures = {name: {'sha256': name * 8, 'query_ids': ['q1', 'q2']}
                    for name in ['scifact', 't2retrieval']}
        plans = ['text', 'dense', 'rrf', 'dbsf']
        measured = {'status': 'measured', 'errors': [], 'release_supported': False,
                    'full_benchmark': False, 'datasets': [
            {'dataset': name, 'status': 'measured', 'fixture_sha256': fixture['sha256'],
             'queries': [{'plan': plan, 'query_id': q} for plan in plans for q in fixture['query_ids']]}
            for name, fixture in fixtures.items()]}
        self.assertIs(validate_measurement(measured, fixtures, plans), measured)
        changes = [lambda r: r['datasets'][0]['queries'][0].update(query_id='substitute'),
                   lambda r: r['datasets'][0]['queries'][0].update(query_id='q2'),
                   lambda r: r['datasets'][0]['queries'][0].update(plan='other'),
                   lambda r: r['datasets'][0].update(fixture_sha256='wrong'),
                   lambda r: r['datasets'][0].update(status='running'),
                   lambda r: r['datasets'][0].update(dataset='t2retrieval'),
                   lambda r: r['datasets'].pop(),
                   lambda r: r.update(full_benchmark=True),
                   lambda r: r.update(release_supported=True),
                   lambda r: r.update(errors=['incomplete'])]
        for mutate in changes:
            changed = copy.deepcopy(measured); mutate(changed)
            with self.assertRaises(ValueError): validate_measurement(changed, fixtures, plans)

    def asset_contract(self):
        contract = json.loads(CONTRACT.read_bytes())
        model = contract['model_id']; revision = contract['revision']
        contract['files'] = [{'path': 'config.json', 'bytes': 5,
            'sha256': hashlib.sha256(b'model').hexdigest(),
            'url': f'https://huggingface.co/{model}/resolve/{revision}/config.json'}]
        contract['runtime_wheels'] = [{'file': 'runtime.whl', 'bytes': 5,
            'sha256': hashlib.sha256(b'wheel').hexdigest(),
            'url': 'https://files.pythonhosted.org/packages/ab/cd/runtime.whl'}]
        return contract

    def fixture(self):
        fixture = {'documents': [{'id': 'd1', 'body': '中文 test'}],
                   'queries': [{'id': 'q1', 'text': 'test'}]}
        value = {}
        for kind, field in [('documents', 'body'), ('queries', 'text')]:
            row = fixture[kind][0]
            value[kind] = {row['id']: {'text_sha256': hashlib.sha256(row[field].encode()).hexdigest(),
                'vector': [1.] + [0.] * 383, 'untruncated_tokens': 130, 'encoded_tokens': 128}}
        return fixture, value

    def test_real_manifest_entries_are_bounded_and_unique(self):
        rows = entries(json.loads(CONTRACT.read_bytes()))
        self.assertEqual(len(rows), 32)
        self.assertEqual(len({p for p, _ in rows}), 32)
        self.assertLess(sum(r['bytes'] for _, r in rows), 1024 ** 3)

    def test_manifest_refuses_unsafe_paths_hashes_sizes_and_duplicates(self):
        cases = [('path', '../config.json'), ('path', 'a\\config.json'),
                 ('path', 'a//config.json'), ('bytes', True), ('bytes', 0),
                 ('bytes', 513 * 1024 ** 2), ('sha256', 'A' * 64)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                contract = self.asset_contract(); contract['files'][0][field] = value
                with self.assertRaises(ValueError): entries(contract)
        contract = self.asset_contract(); contract['files'] *= 2
        with self.assertRaises(ValueError): entries(contract)

    def test_manifest_refuses_mutable_revision_wrong_model_and_contract(self):
        for key, value in [('revision', 'main'), ('model_id', 'other/model'),
                           ('dimensions', 768), ('normalization', 'none'), ('max_tokens', 512),
                           ('pooling', 'cls'), ('release_supported', True)]:
            contract = self.asset_contract(); contract[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): entries(contract)

    def test_wheel_origin_filename_and_components_must_match(self):
        for url in ['http://files.pythonhosted.org/packages/ab/runtime.whl',
                    'https://files.pythonhosted.org.evil/packages/ab/runtime.whl',
                    'https://files.pythonhosted.org/packages/../runtime.whl',
                    'https://files.pythonhosted.org/packages/ab/other.whl',
                    'https://files.pythonhosted.org/packages/ab/runtime.whl?mutable=1']:
            contract = self.asset_contract(); contract['runtime_wheels'][0]['url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError): entries(contract)

    def test_existing_assets_are_verified_without_network_or_overwrite(self):
        contract = self.asset_contract()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, data in [('minilm/config.json', b'model'), ('wheels/runtime.whl', b'wheel')]:
                path = root / name; path.parent.mkdir(); path.write_bytes(data)
            with patch('urllib.request.urlopen', side_effect=AssertionError('unexpected download')):
                self.assertEqual(len(prepare(root, contract)), 2)
                (root / 'minilm/config.json').write_bytes(b'wrong')
                with self.assertRaises(ValueError): prepare(root, contract, download=True)
            self.assertEqual((root / 'minilm/config.json').read_bytes(), b'wrong')

    def test_interrupted_partial_is_preserved_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'minilm/config.json.partial'
            path.parent.mkdir(); path.write_bytes(b'previous incomplete input')
            with self.assertRaises(FileExistsError): prepare(root, self.asset_contract(), download=True)
            self.assertEqual(path.read_bytes(), b'previous incomplete input')

    def test_old_assets_and_partials_count_toward_download_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / 'old-runtime.whl.partial'; old.write_bytes(b'keep old bytes')
            with patch('scripts.prepare_dense_quality.MAX_ASSET_BYTES', 20), patch(
                    'urllib.request.urlopen', side_effect=AssertionError('capacity refusal must precede download')):
                with self.assertRaisesRegex(ValueError, 'admission refused'):
                    prepare(root, self.asset_contract(), download=True)
            self.assertEqual(old.read_bytes(), b'keep old bytes')
            self.assertFalse((root / 'minilm').exists())

    def test_download_checks_bytes_and_publishes_exclusively(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('urllib.request.urlopen', side_effect=[io.BytesIO(b'model'), io.BytesIO(b'wheel')]):
                self.assertEqual(len(prepare(Path(directory), self.asset_contract(), download=True)), 2)
            self.assertFalse(list(Path(directory).rglob('*.partial')))

    def test_oversized_download_remains_failed_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('urllib.request.urlopen', return_value=io.BytesIO(b'too large')):
                with self.assertRaises(ValueError): prepare(Path(directory), self.asset_contract(), download=True)
            self.assertTrue((Path(directory) / 'minilm/config.json.partial').is_file())
            self.assertFalse((Path(directory) / 'minilm/config.json').exists())

    def test_linked_assets_and_components_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); outside = root / 'outside'; outside.mkdir()
            try: (root / 'minilm').symlink_to(outside, target_is_directory=True)
            except OSError as error: self.skipTest(str(error))
            with self.assertRaises(ValueError): prepare(root, self.asset_contract(), download=True)

    def test_complete_text_bound_normalized_vectors_are_accepted(self):
        fixture, value = self.fixture()
        self.assertIs(validate_vectors(value, fixture), value)

    def test_vectors_refuse_missing_extra_wrong_text_shape_nonfinite_and_token_counts(self):
        fixture, value = self.fixture()
        for field, replacement in [('text_sha256', '0' * 64), ('vector', [1.] * 384),
                ('vector', [1.] + [0.] * 382), ('vector', [True] + [0.] * 383),
                ('vector', [float('nan')] + [0.] * 383), ('encoded_tokens', True),
                ('encoded_tokens', 127), ('untruncated_tokens', 100)]:
            changed = copy.deepcopy(value); changed['documents']['d1'][field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError): validate_vectors(changed, fixture)
        for kind in ['documents', 'queries']:
            changed = copy.deepcopy(value); changed[kind].clear()
            with self.assertRaises(ValueError): validate_vectors(changed, fixture)
            changed = copy.deepcopy(value); changed[kind]['extra'] = copy.deepcopy(next(iter(changed[kind].values())))
            with self.assertRaises(ValueError): validate_vectors(changed, fixture)

    def test_receipt_binds_contract_fixture_output_and_completed_encoding(self):
        fixture, value = self.fixture(); fixture_sha = 'a' * 64; contract = b'fixed model contract'
        value['fixture_sha256'] = fixture_sha
        raw = json.dumps(value).encode()
        manifest = {'schema_version': 1, 'status': 'encoded', 'release_supported': False, 'errors': [],
            'model_contract_sha256': hashlib.sha256(contract).hexdigest(),
            'datasets': [{'dataset': 'scifact', 'fixture_sha256': fixture_sha,
                'output_bytes': len(raw), 'output_sha256': hashlib.sha256(raw).hexdigest()}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'scifact.json').write_bytes(raw)
            def publish(current): (root / 'manifest.json').write_text(json.dumps(current))
            publish(manifest)
            self.assertEqual(load_vectors(root, 'scifact', fixture, fixture_sha, contract)[0], value)
            for key, replacement in [('status', 'failed'), ('model_contract_sha256', '0' * 64),
                                     ('release_supported', True), ('errors', ['incomplete'])]:
                changed = copy.deepcopy(manifest); changed[key] = replacement; publish(changed)
                with self.subTest(key=key), self.assertRaises(ValueError): load_vectors(root, 'scifact', fixture, fixture_sha, contract)
            publish(manifest); (root / 'scifact.json').write_bytes(raw.replace(b'130', b'131'))
            with self.assertRaises(ValueError): load_vectors(root, 'scifact', fixture, fixture_sha, contract)


if __name__ == '__main__':
    unittest.main()

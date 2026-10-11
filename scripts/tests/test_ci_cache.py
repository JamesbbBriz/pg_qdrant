import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci_cache as cache
import ci_build
from benchmark_compiler import native_metrics
from ci_resources import docker


class BoundedCache(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = cache.Store("snapshots", 100, 2, self.root)

    def put(self, key, data=b"x" * 40):
        with self.store.write(key * 64, len(data)) as entry:
            (entry / "data").write_bytes(data)

    def test_lru_eviction_keeps_recently_reused_artifacts(self):
        self.put("1")
        self.put("2")
        self.store.verify("1" * 64)
        self.put("3")
        self.assertEqual(set(self.store.state["entries"]), {"1" * 64, "3" * 64})
        self.assertLessEqual(self.store.audit(), 100)

    def test_pins_fill_capacity_and_refuse_new_entry(self):
        self.put("1")
        self.put("2")
        self.store.pin("1" * 64)
        self.store.pin("2" * 64)
        with self.assertRaisesRegex(RuntimeError, "protected"):
            self.put("3")
        self.assertTrue((self.store.root / ("1" * 64) / "data").exists())

    def test_corruption_prevents_reuse_and_cleanup(self):
        self.put("1")
        (self.store.root / ("1" * 64) / "data").write_bytes(b"z" * 40)
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            self.store.verify("1" * 64)
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            self.store.reserve(100)

    def test_unknown_and_interrupted_data_are_preserved(self):
        (self.store.root / "unknown.pending").mkdir()
        (self.store.root / "unknown.pending" / "evidence").write_text("keep")
        with self.assertRaisesRegex(RuntimeError, "unrecorded"):
            self.store.reserve(1)
        self.assertEqual((self.store.root / "unknown.pending/evidence").read_text(), "keep")

    def test_oversize_and_failed_write_do_not_grow_cache(self):
        with self.assertRaisesRegex(RuntimeError, "budget"):
            self.put("1", b"x" * 101)
        with self.assertRaises(RuntimeError):
            with self.store.write("1" * 64, 20) as entry:
                (entry / "data").write_bytes(b"x" * 30)
        self.assertEqual(self.store.audit(), 0)

    def test_protected_input_is_not_evicted_between_exports(self):
        self.put("1")
        self.put("2")
        with self.store.write("3" * 64, 40, protect=("1" * 64,)) as entry:
            (entry / "data").write_bytes(b"x" * 40)
        self.assertIn("1" * 64, self.store.state["entries"])

    def test_foreign_owner_refuses_adoption(self):
        self.store.state["owner"] = "foreign"
        self.store.save()
        with self.assertRaisesRegex(ValueError, "foreign"):
            cache.Store("snapshots", 100, 2, self.root)

    def test_paths_cannot_escape_owned_root(self):
        for value in ("../outside", "/tmp/other", "C:/other", "a\\b", "a/./b"):
            with self.assertRaises(ValueError):
                cache.checked_child(self.root, value)

    def test_evidence_limit_refuses_without_deleting_failures(self):
        (self.root / "failure").write_bytes(b"x" * 20)
        with self.assertRaisesRegex(RuntimeError, "evidence budget"):
            cache.evidence_admission(self.root, maximum=20)
        self.assertTrue((self.root / "failure").exists())

    def test_snapshot_archive_reused_and_source_view_verified(self):
        source = self.root / "source"
        source.mkdir()
        (source / "Cargo.lock").write_bytes(b"locked")
        files = [{"path": "Cargo.lock", "sha256": hashlib.sha256(b"locked").hexdigest()}]
        key = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        report = {"source_sha256": key, "files": files}
        (source / "ci-input.json").write_text(json.dumps(report))
        with patch.dict(os.environ, PGQ_CACHE_ROOT=str(self.root / "cache")):
            first = cache.archive_snapshot(source, report)
            second = cache.archive_snapshot(source, report)
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            receipt = cache.finish_snapshot(source, report, True)
            self.assertTrue(receipt["materialized_sources_retired"])
        self.assertTrue((source / "ci-input.json").exists())
        self.assertFalse((source / "Cargo.lock").exists())

    def test_source_change_prevents_archive(self):
        source = self.root / "source"
        source.mkdir()
        (source / "Cargo.lock").write_bytes(b"modified")
        with patch.dict(os.environ, PGQ_CACHE_ROOT=str(self.root / "cache")):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                cache.archive_snapshot(source, {"source_sha256": "a" * 64, "files": [
                    {"path": "Cargo.lock", "sha256": hashlib.sha256(b"locked").hexdigest()}]})

    def test_archived_snapshot_restores_exact_source_and_rejects_changed_manifest(self):
        source = self.root / "source"
        source.mkdir()
        (source / "Cargo.lock").write_bytes(b"locked")
        files = [{"path": "Cargo.lock", "sha256": hashlib.sha256(b"locked").hexdigest()}]
        key = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        report = {"source_sha256": key, "files": files}
        manifest = self.root / "ci-input.json"
        manifest.write_text(json.dumps(report))
        with patch.dict(os.environ, PGQ_CACHE_ROOT=str(self.root / "cache")):
            cache.archive_snapshot(source, report)
            restored = self.root / "restored"
            cache.restore_snapshot(manifest, restored)
            self.assertEqual((restored / "Cargo.lock").read_bytes(), b"locked")
            with self.assertRaisesRegex(ValueError, "empty"):
                cache.restore_snapshot(manifest, restored)
            report["files"][0]["sha256"] = "0" * 64
            manifest.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "manifest hash"):
                cache.restore_snapshot(manifest, self.root / "other")

    def test_passed_dirty_snapshot_remains_pinned(self):
        source = self.root / "source"
        source.mkdir()
        (source / "Cargo.lock").write_bytes(b"locked")
        files = [{"path": "Cargo.lock", "sha256": hashlib.sha256(b"locked").hexdigest()}]
        key = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        report = {"source_sha256": key, "files": files, "untracked_inputs": ["Cargo.lock"]}
        (source / "ci-input.json").write_text(json.dumps(report))
        with patch.dict(os.environ, PGQ_CACHE_ROOT=str(self.root / "cache")):
            cache.finish_snapshot(source, report, True)
            store = cache.Store("snapshots", 2 * cache.GiB, 32)
            self.assertTrue(store.state["entries"][key]["pinned"])

    def test_native_builder_never_accepts_non_build_command(self):
        with self.assertRaises(ValueError):
            ci_build.prepare(["docker", "system", "prune", "--all"])

    def test_failed_builder_admission_preserves_receipt_without_maintenance(self):
        with patch.object(ci_build, "ROOT", self.root), patch.object(ci_build, "prepare", side_effect=RuntimeError("full")), \
                patch.object(ci_build, "maintain_builder") as maintain:
            with self.assertRaisesRegex(RuntimeError, "full"):
                ci_build.run(["docker", "build", "."], lambda command: 0)
        maintain.assert_not_called()
        receipt = json.loads(next((self.root / "artifacts/build-cache").glob("*.json")).read_text())
        self.assertEqual(receipt["error"], "full")
        self.assertEqual(receipt["exit_code"], 1)

    def test_failed_post_build_gc_preserves_receipt_and_refuses_success(self):
        with patch.object(ci_build, "ROOT", self.root), patch.object(ci_build, "prepare", return_value=(["build"], [])), \
                patch.object(ci_build, "maintain_builder", side_effect=RuntimeError("busy cache")):
            with self.assertRaisesRegex(RuntimeError, "busy cache"):
                ci_build.run(["docker", "build", "."], lambda command: 0)
        receipt = json.loads(next((self.root / "artifacts/build-cache").glob("*.json")).read_text())
        self.assertEqual(receipt["maintenance_error"], "busy cache")
        self.assertEqual(receipt["exit_code"], 1)

    def test_builder_capacity_uses_integer_bytes_and_refuses_display_units(self):
        with patch.object(ci_build, "docker", return_value='[{"size": 1048577}, {"size": 3}]'):
            self.assertEqual(ci_build.cache_usage(), 1048580)
        with patch.object(ci_build, "docker", return_value='[{"size": "1MB"}]'):
            with self.assertRaisesRegex(ValueError, "byte measurement"):
                ci_build.cache_usage()


class OciIdentity(unittest.TestCase):
    def test_docker_config_manifest_and_index_ids_anchor_exact_linux_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "blobs/sha256").mkdir(parents=True)
            def blob(value):
                data = json.dumps(value).encode()
                key = hashlib.sha256(data).hexdigest()
                (root / "blobs/sha256" / key).write_bytes(data)
                return {"digest": "sha256:" + key, "size": len(data)}
            config = blob({"architecture": "amd64", "os": "linux"})
            manifest = blob({"config": config, "layers": []})
            index = blob({"manifests": [manifest]})
            (root / "index.json").write_text(json.dumps({"manifests": [index]}))
            for identity in (config, manifest, index):
                self.assertEqual(ci_build.oci_manifest(root, identity["digest"]), manifest["digest"])
            with self.assertRaisesRegex(ValueError, "does not match"):
                ci_build.oci_manifest(root, "sha256:" + "0" * 64)
            (root / "blobs/sha256" / manifest["digest"][7:]).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                ci_build.oci_manifest(root, manifest["digest"])

    def test_native_measurement_distinguishes_cached_work_and_cargo_output(self):
        log = "#9 [verification 4/9] RUN --mount=type=cache,target=/src/target cargo test\n"
        built = native_metrics(log + "#9 1.2 \x1b[32m Compiling crate v1\x1b[0m\n"
                               "#9 309.5 \x1b[32m    Finished\x1b[0m installing pg_qdrant\n#9 DONE 351.4s\n")
        self.assertEqual(built["native_seconds"], 351.4)
        self.assertEqual(built["compile_install_seconds"], 309.5)
        self.assertEqual(built["compiled_crates"], ["crate v1"])
        cached = native_metrics(log + "#9 CACHED\n")
        self.assertTrue(cached["native_layer_cached"])
        self.assertEqual(cached["native_seconds"], 0.0)
        self.assertEqual(cached["compile_install_seconds"], 0.0)
        self.assertIsNone(native_metrics("failed before native work")["native_seconds"])


@unittest.skipUnless(os.environ.get("PGQ_RUN_ID"), "requires local act Docker runner")
class SavedOciInput(unittest.TestCase):
    def test_real_saved_image_verified_and_reused_with_capacity_refusal(self):
        with tempfile.TemporaryDirectory() as root:
            store = cache.Store("oci", 2 * cache.GiB, 2, Path(root))
            image = "pg-qdrant-act-runner:29.8.0-node24"
            identifier = json.loads(docker("image", "inspect", image))[0]["Id"]
            first, manifest, receipt = ci_build.save_oci(image, store)
            second, other, reused = ci_build.save_oci(image, store)
            self.assertEqual(first, second)
            self.assertEqual(manifest, other)
            self.assertEqual(receipt["image_id"], identifier)
            self.assertFalse(receipt["cache_hit"])
            self.assertTrue(reused["cache_hit"])
            blob = first / "blobs/sha256" / manifest[7:]
            self.assertEqual(cache.digest(blob), manifest[7:])
            smaller = cache.Store("oci", 1, 2, Path(root) / "small")
            with self.assertRaisesRegex(RuntimeError, "byte budget"):
                ci_build.save_oci(image, smaller)
            self.assertEqual(smaller.audit(), 0)

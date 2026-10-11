import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from export_native_artifacts import export


class NativeArtifacts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = self.root / "target"
        for name in ("debug/pg-qdrant-edge-probe", "debug/pg_qdrant_p0_helper",
                     "debug/build/qdrant-edge-1/out/native-sse.o", "debug/build/qdrant-edge-2/out/native-avx2.o",
                     "debug/build/pgrx-pg-sys-1/stderr", "debug/build/pgrx-pg-sys-1/out/pg17.rs",
                     "debug/deps/intermediate.rlib", "debug/deps/unit-test"):
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())

    def test_runtime_and_every_audit_directory_survive_without_intermediates(self):
        destination = self.root / "exported"
        report = export(self.target, destination, helper=True)
        paths = {item["path"] for item in report["files"]}
        self.assertEqual(len(paths), 6)
        self.assertIn("debug/build/qdrant-edge-2/out/native-avx2.o", paths)
        self.assertIn("debug/build/pgrx-pg-sys-1/out/pg17.rs", paths)
        self.assertFalse((destination / "debug/deps").exists())
        self.assertTrue((self.target / "debug/deps/intermediate.rlib").is_file())
        self.assertEqual(json.loads((destination / "manifest.json").read_text()), report)

    def test_missing_required_binary_or_capacity_refuses_before_writing(self):
        destination = self.root / "exported"
        with self.assertRaisesRegex(RuntimeError, "byte budget"):
            export(self.target, destination, helper=True, maximum=1)
        self.assertFalse(destination.exists())
        (self.target / "debug/pg_qdrant_p0_helper").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            export(self.target, destination, helper=True)
        self.assertFalse(destination.exists())

    @unittest.skipIf(sys.platform == "win32", "real symlink fixture requires the supported Linux act environment")
    def test_audit_link_does_not_export_external_content(self):
        outside = self.root / "external"
        outside.write_text("preserve")
        (self.target / "debug/build/qdrant-edge-1/out/link").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "links"):
            export(self.target, self.root / "exported")
        self.assertEqual(outside.read_text(), "preserve")
        self.assertFalse((self.root / "exported").exists())

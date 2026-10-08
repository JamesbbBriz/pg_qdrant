import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "verify_native.py"
SPEC = importlib.util.spec_from_file_location("verify_native", MODULE_PATH)
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class NativeGateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = self.root / "packages.json"
        self.expected = self.root / "expected.tsv"
        self.expected.write_text("alpha:amd64\t1\nbeta\t2\n")
        self.identities = {}
        rows = []
        for index in range(7):
            name = f"package-{index}"
            path = self.root / f"{name}.deb"
            data = (name + " archive bytes").encode()
            path.write_bytes(data)
            self.identities[path.name] = (name, "1.pgdg24.04+1", "amd64")
            rows.append({"package": name, "version": "1.pgdg24.04+1", "architecture": "amd64",
                         "deb_sha256": hashlib.sha256(data).hexdigest(), "deb_size": len(data)})
        self.manifest.write_text(json.dumps({"schema_version": 1, "packages": rows}))
        self.identity = patch.object(native, "archive_identity", side_effect=lambda p: self.identities[p.name])
        self.identity.start()
        self.addCleanup(self.identity.stop)

    def test_all_seven_exact_archives_pass(self):
        self.assertEqual(native.verify_archives(self.manifest, self.root)["verified_pgdg_archives"], 7)

    def test_missing_archive_fails(self):
        (self.root / "package-2.deb").unlink()
        with self.assertRaisesRegex(native.VerificationError, "missing verified archives"):
            native.verify_archives(self.manifest, self.root)

    def test_same_size_changed_bytes_fail(self):
        path = self.root / "package-2.deb"
        data = path.read_bytes()
        path.write_bytes(b"X" + data[1:])
        with self.assertRaisesRegex(native.VerificationError, "checksum changed"):
            native.verify_archives(self.manifest, self.root)

    def test_wrong_version_fails_before_hash(self):
        self.identities["package-2.deb"] = ("package-2", "2.pgdg24.04+1", "amd64")
        with self.assertRaisesRegex(native.VerificationError, "identity changed"):
            native.verify_archives(self.manifest, self.root)

    def test_unexpected_pgdg_archive_fails(self):
        (self.root / "unknown.deb").write_bytes(b"unknown")
        self.identities["unknown.deb"] = ("unknown", "1.pgdg24.04+1", "amd64")
        with self.assertRaisesRegex(native.VerificationError, "unexpected PGDG"):
            native.verify_archives(self.manifest, self.root)

    def test_duplicate_archive_fails(self):
        (self.root / "duplicate.deb").write_bytes((self.root / "package-2.deb").read_bytes())
        self.identities["duplicate.deb"] = self.identities["package-2.deb"]
        with self.assertRaisesRegex(native.VerificationError, "duplicate archive"):
            native.verify_archives(self.manifest, self.root)

    def test_symlink_archive_fails(self):
        path = self.root / "package-2.deb"
        path.unlink()
        path.symlink_to(self.root / "package-1.deb")
        with self.assertRaisesRegex(native.VerificationError, "regular file"):
            native.verify_archives(self.manifest, self.root)

    def test_inventory_comparison_ignores_only_order(self):
        self.assertEqual(native.verify_installed(self.expected, "beta\t2\nalpha:amd64\t1\n")["installed_package_entries"], 2)
        for text in ["beta\t2\n", "beta\t2\nalpha:amd64\t2\n", "beta\t2\nalpha:amd64\t1\nextra\t1\n"]:
            with self.subTest(text=text), self.assertRaises(native.VerificationError):
                native.verify_installed(self.expected, text)

    def test_duplicate_inventory_is_not_silently_overwritten(self):
        with self.assertRaisesRegex(native.VerificationError, "duplicate"):
            native.verify_installed(self.expected, "beta\t2\nbeta\t2\nalpha:amd64\t1\n")


if __name__ == "__main__":
    unittest.main()

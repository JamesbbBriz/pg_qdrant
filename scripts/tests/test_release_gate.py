"""Safety regressions for an offline release readiness checker.

These tests cannot prove the claims inside an attestation were truly executed.
"""
import hashlib
import pathlib
import tempfile
import unittest

from scripts.check_release_gate import (
    REQUIRED_GATES, REQUIRED_FEATURES, assess, verified_file,
)


class GateTest(unittest.TestCase):
    def test_empty_revision_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            report = assess(root, root / "missing.json", root / "dist", "0" * 40)
            self.assertFalse(report["ready"])
            self.assertTrue(any("P1-DURABILITY" in s for s in report["blockers"]))
            self.assertTrue(any("workflow is missing" in s for s in report["blockers"]))

    def test_manual_only_workflow_cannot_be_release_ready(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/p0.yml").write_text("on:\n  workflow_dispatch:\n")
            report = assess(root, root / "missing.json", root, "1" * 40)
            self.assertIn("automatic CI triggers remain disabled; restore push and pull_request",
                          report["blockers"])

    def test_integrity_checker_rejects_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            target = root / "good.txt"
            target.write_bytes(b"legitimate")
            digest = hashlib.sha256(b"legitimate").hexdigest()
            self.assertTrue(verified_file(root, {"path": "good.txt", "sha256": digest}))
            self.assertFalse(verified_file(root, {"path": "../good.txt", "sha256": digest}))
            (root / "linked.txt").symlink_to(target)
            self.assertFalse(verified_file(root, {"path": "linked.txt", "sha256": digest}))
            (root / "dirlink").symlink_to(root, target_is_directory=True)
            self.assertFalse(verified_file(root, {"path": "dirlink/good.txt", "sha256": digest}))
            self.assertFalse(verified_file(root, {"path": "good.txt", "sha256": "0" * 64}))

    def test_contract_counts_are_stable(self):
        self.assertEqual(len(REQUIRED_FEATURES), 54)
        self.assertEqual(len(REQUIRED_GATES), 44)
        self.assertEqual(len(set(REQUIRED_FEATURES)), 54)
        self.assertEqual(len(set(REQUIRED_GATES)), 44)


if __name__ == "__main__":
    unittest.main()

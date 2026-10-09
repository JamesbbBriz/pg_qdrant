"""Local receipts must preserve failure, dependency and snapshot boundaries."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.check_release_gate import local_ci_evidence
from scripts.verify import execute_steps, selected_steps


class LocalCI(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "step commands run in the Linux act runner")
    def test_failure_is_retained_while_independent_and_collect_steps_run(self):
        with tempfile.TemporaryDirectory() as directory:
            steps = [
                {"id": "image", "name": "controlled failure", "requires": [], "command": "exit 7"},
                {"id": "dependent", "name": "cannot run", "requires": ["image"], "command": "exit 0"},
                {"id": "collect", "name": "collect after failure", "requires": [], "command": "printf preserved"},
            ]
            report = {"steps": []}
            with contextlib.redirect_stdout(io.StringIO()):
                passed = execute_steps(steps, report, Path(directory), dict(os.environ))
            self.assertFalse(passed)
            self.assertEqual([item["status"] for item in report["steps"]], ["failed", "skipped", "passed"])
            self.assertEqual(report["steps"][0]["exit_code"], 7)
            self.assertEqual((Path(directory) / "collect.log").read_text(), "preserved")
            self.assertTrue((Path(directory) / "verify-report.json").is_file())

    def test_full_profile_includes_every_gate_in_order(self):
        registry = json.loads(Path("ci/steps.json").read_text())
        self.assertEqual(len(selected_steps(registry, "engine")), 30)
        self.assertEqual(len(selected_steps(registry, "ledger")), 4)
        self.assertEqual(len(selected_steps(registry, "static")), 3)
        full = selected_steps(registry, "full")
        self.assertEqual(len(full), 37)
        self.assertEqual(len({step["id"] for step in full}), 37)

    def test_release_receipt_rejects_subset_dirty_skipped_and_modified_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ci").mkdir()
            registry = {"profiles": {"engine": [{"id": "native"}]}}
            (root / "ci/steps.json").write_text(json.dumps(registry))
            (root / "native.log").write_bytes(b"actual test output")
            file = {"path": "ci/steps.json", "sha256": hashlib.sha256((root / "ci/steps.json").read_bytes()).hexdigest()}
            files = [file]
            snapshot = {"commit": "1" * 40, "dirty_diff_sha256": hashlib.sha256(b"").hexdigest(),
                        "untracked_inputs": [], "source_sha256": hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), "files": files}
            verify = {"status": "passed", "profile": "full", "run_id": "abcdef0123456789", "snapshot": snapshot,
                      "steps": [{"id": "native", "status": "passed", "exit_code": 0, "log": "native.log",
                                 "log_sha256": hashlib.sha256(b"actual test output").hexdigest()}]}
            run = {"status": "passed", "profile": "full", "run_id": verify["run_id"], "snapshot": snapshot,
                   "act_exit_code": 0, "act_version": "act version 0.2.89"}
            ci = {"runner": "act-local", "conclusion": "success", "checkout_sha": "1" * 40}

            def assess():
                for key, data in [("run_report", run), ("verify_report", verify)]:
                    path = root / (key + ".json")
                    path.write_text(json.dumps(data))
                    ci[key] = {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                return local_ci_evidence(root, ci, "1" * 40)

            self.assertTrue(assess())
            verify["profile"] = "static"
            self.assertFalse(assess())
            verify["profile"] = "full"
            snapshot["dirty_diff_sha256"] = "2" * 64
            self.assertFalse(assess())
            snapshot["dirty_diff_sha256"] = hashlib.sha256(b"").hexdigest()
            snapshot["untracked_inputs"] = ["extra.rs"]
            self.assertFalse(assess())
            snapshot["untracked_inputs"] = []
            verify["steps"][0]["status"] = "skipped"
            self.assertFalse(assess())
            verify["steps"][0]["status"] = "passed"
            (root / "native.log").write_bytes(b"changed")
            self.assertFalse(assess())

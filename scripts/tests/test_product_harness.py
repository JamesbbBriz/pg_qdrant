"""Exercise the real shell cleanup boundary with disposable command fixtures."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


@unittest.skipIf(os.name == "nt", "the shell harness runs in the Linux act runner")
class ProductHarness(unittest.TestCase):
    def test_success_removes_only_its_cluster_and_failures_retain_it(self):
        script = Path(__file__).resolve().parents[2] / "crates/pg_qdrant/tests/run-p1.sh"
        for test_exit, stop_exit in [(0, 0), (7, 0), (0, 1)]:
            with self.subTest(test_exit=test_exit, stop_exit=stop_exit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                commands = root / "commands"
                commands.mkdir()
                cluster = root / "owned-cluster"
                sibling = root / "unrelated"
                sibling.mkdir()
                (sibling / "keep").write_text("untouched")
                fixtures = {
                    "id": 'if [[ "$1" == -u ]]; then echo 10001; else echo fixture; fi',
                    "pg_config": 'printf "%s\\n" "$FIXTURE_COMMANDS"',
                    "mktemp": 'mkdir "$FIXTURE_CLUSTER"; printf "%s\\n" "$FIXTURE_CLUSTER"',
                    "initdb": 'mkdir "$2"; echo retained >"$2/sentinel"',
                    "pg_ctl": 'if [[ " $* " == *" stop "* ]]; then exit "$FIXTURE_STOP_EXIT"; fi',
                    "python3": 'if [[ "$1" == /src/crates/pg_qdrant/tests/verify_p1.py ]]; then '
                               'echo controlled-fixture; exit "$FIXTURE_TEST_EXIT"; fi\n'
                               + 'exec ' + shlex.quote(sys.executable) + ' "$@"',
                }
                for name, body in fixtures.items():
                    path = commands / name
                    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body + "\n")
                    path.chmod(0o700)
                artifacts = root / "artifacts"
                env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                           FIXTURE_COMMANDS=str(commands), FIXTURE_CLUSTER=str(cluster),
                           FIXTURE_TEST_EXIT=str(test_exit), FIXTURE_STOP_EXIT=str(stop_exit),
                           PG_QDRANT_ARTIFACT_DIR=str(artifacts), PG_QDRANT_P1_STORAGE_TEST="0")
                result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, test_exit or stop_exit)
                self.assertEqual((sibling / "keep").read_text(), "untouched")
                report = artifacts / "p1-failed-cluster.json"
                if test_exit or stop_exit:
                    self.assertEqual((cluster / "data/sentinel").read_text().strip(), "retained")
                    evidence = json.loads(report.read_text())
                    self.assertEqual(evidence["cluster"], str(cluster))
                    self.assertEqual(evidence["stop_exit_code"], stop_exit)
                    self.assertEqual(evidence["exit_code"], test_exit or stop_exit)
                    self.assertEqual(evidence["status"], "failed")
                    self.assertFalse(evidence["release_supported"])
                else:
                    self.assertFalse(cluster.exists())
                    self.assertFalse(report.exists())

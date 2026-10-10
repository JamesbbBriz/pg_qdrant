"""Ownership, evidence loss and real Docker failure/cancellation regression tests."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import ci_resources as resources


class ResourceGuards(unittest.TestCase):
    def test_invalid_ownership_never_inspects(self):
        with patch.object(resources, "docker") as docker:
            for label, identity in [(resources.LABELS[0], "../bad"), ("foreign", "a" * 16)]:
                with self.assertRaises(ValueError):
                    resources.inspect_owned("id", label, identity)
            docker.assert_not_called()

    def test_mismatched_label_never_returns_ownership(self):
        with patch.object(resources, "docker", return_value=json.dumps([{"Config": {"Labels": {resources.LABELS[0]: "b" * 16}}}])):
            with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                resources.inspect_owned("id", resources.LABELS[0], "a" * 16)

    def test_pending_export_prevents_resource_growth(self):
        with patch.object(resources, "docker", return_value="unretired-id"):
            with self.assertRaisesRegex(RuntimeError, "unretired"):
                resources.require_clean_start()
        with patch.object(resources, "docker", return_value=""):
            resources.require_clean_start()

    def test_runner_only_removes_exact_workspace_and_env(self):
        data = {"Name": "/act-job", "Mounts": [
            {"Type": "volume", "Name": name} for name in
            ("act-job", "act-job-env", "act-toolcache", "database", "act-other-job")
        ]}
        self.assertEqual(resources.runner_volumes(data), ["act-job", "act-job-env"])

    def test_enumeration_error_is_a_failed_receipt(self):
        with tempfile.TemporaryDirectory() as root, patch.object(resources, "docker", side_effect=RuntimeError("offline")):
            receipt = resources.retire_owned(resources.LABELS[0], "a" * 16, Path(root))
            self.assertEqual(receipt["status"], "failed")
            self.assertIn("offline", receipt["error"])
            self.assertEqual(json.loads((Path(root) / "ci-cleanup.json").read_text()), receipt)

    def test_lost_runner_export_preserves_all_originals(self):
        data = {"Id": "1" * 64, "State": {"Running": False}}
        with tempfile.TemporaryDirectory() as root, \
                patch.object(resources, "docker", return_value="1" * 64), \
                patch.object(resources, "inspect_owned", return_value=data), \
                patch.object(resources, "archive_path", side_effect=RuntimeError("disk full")), \
                patch.object(resources, "retire_owned") as retire:
            result = resources.finish_run("a" * 16, root, resources.LABELS[0], resources.LABELS[1], "/tmp/evidence/.")
            self.assertEqual(result["status"], "failed")
            self.assertIn("disk full", result["error"])
            retire.assert_not_called()

    def test_failed_child_export_preserves_runner(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.object(resources, "docker", return_value=""), \
                patch.object(resources, "retire_owned", return_value={"status": "failed"}) as retire:
            result = resources.finish_run("a" * 16, root, resources.LABELS[0], resources.LABELS[1], "/tmp/evidence/.")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(retire.call_count, 1)
            self.assertEqual(retire.call_args.args[0], resources.LABELS[0])

    def test_runner_volume_retry_uses_original_identity(self):
        for observed, expected in [("original", "passed"), ("replacement", "failed")]:
            with self.subTest(observed=observed), tempfile.TemporaryDirectory() as root:
                row = {"id": "1" * 64, "name": "/act-job", "removed": True,
                       "volumes_removed": [], "volumes_pending": [{"name": "act-job-env", "created_at": "original"}],
                       "error": "volume in use"}
                receipt = {"run_id": "a" * 16, "label": resources.LABELS[1], "containers": [row]}
                (Path(root) / "runner-cleanup.json").write_text(json.dumps(receipt))

                def docker(*args):
                    if args[:2] == ("volume", "inspect"):
                        return json.dumps([{"CreatedAt": observed}])
                    return ""

                with patch.object(resources, "docker", side_effect=docker) as calls:
                    result = resources.retire_owned(resources.LABELS[1], "a" * 16, root, runner=True)
                self.assertEqual(result["status"], expected)
                removed = any(call.args[:2] == ("volume", "rm") for call in calls.call_args_list)
                self.assertEqual(removed, expected == "passed")

    def test_retry_preserves_receipts_for_already_retired_containers(self):
        with tempfile.TemporaryDirectory() as root:
            row = {"id": "1" * 64, "removed": True, "files": {"container.log": "digest"}}
            old = {"run_id": "a" * 16, "label": resources.LABELS[0], "containers": [row]}
            (Path(root) / "ci-cleanup.json").write_text(json.dumps(old))
            with patch.object(resources, "docker", return_value=""):
                result = resources.retire_owned(resources.LABELS[0], "a" * 16, root)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["containers"], [row])


@unittest.skipUnless(os.environ.get("PGQ_RUN_ID"), "requires the local act Docker runner")
class RealDockerRetirement(unittest.TestCase):
    def setUp(self):
        self.run_id = secrets.token_hex(8)
        self.other_run = secrets.token_hex(8)
        self.ids = []
        self.image = resources.docker("image", "inspect", "pg-qdrant-act-runner:29.8.0-node24", "--format", "{{.Id}}")
        self.directory = Path("artifacts/resource-lifecycle") / self.run_id
        self.directory.mkdir(parents=True)

    def create(self, command, *, other=False):
        identity = self.other_run if other else self.run_id
        identifier = resources.docker("create", "--label", resources.LABELS[0] + "=" + identity,
            "--network=none", "--memory=128m", "--pids-limit=32", self.image, "sh", "-c",
            "mkdir -p /src/artifacts; echo diagnostic > /src/artifacts/result; " + command)
        self.ids.append(identifier)
        return identifier

    def tearDown(self):
        # Only exact IDs this test created; never sweep the daemon or unrelated labels.
        for identifier in self.ids:
            observed = subprocess.run(["docker", "inspect", identifier], capture_output=True)
            if observed.returncode == 0:
                data = json.loads(observed.stdout)[0]
                self.assertIn(data["Config"]["Labels"][resources.LABELS[0]], (self.run_id, self.other_run))
                subprocess.run(["docker", "rm", "-f", data["Id"]], check=True, capture_output=True)

    def test_success_and_failure_export_then_remove_without_touching_other_run(self):
        success = self.create("echo success; exit 0")
        failure = self.create("mkdir /tmp/cluster; echo checkpoint > /tmp/cluster/state; echo failure; exit 7")
        sentinel = self.create("exit 0", other=True)
        resources.docker("start", success, failure)
        self.assertEqual(resources.docker("wait", success), "0")
        self.assertEqual(resources.docker("wait", failure), "7")
        receipt = resources.retire_owned(resources.LABELS[0], self.run_id, self.directory)
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue(all(row["removed"] for row in receipt["containers"]))
        failed = next(row for row in receipt["containers"] if row["id"] == failure)
        self.assertIn("artifacts", failed)
        archive = self.directory / failure / failed["failure_tmp"]["path"]
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), failed["failure_tmp"]["sha256"])
        with tarfile.open(archive) as tar:
            member = next(member for member in tar if member.name.endswith("cluster/state"))
            self.assertEqual(tar.extractfile(member).read(), b"checkpoint\n")
        self.assertEqual(resources.docker("ps", "-aq", "--filter", "label=" + resources.LABELS[0] + "=" + self.run_id), "")
        self.assertEqual(resources.inspect_owned(sentinel, resources.LABELS[0], self.other_run)["Id"], sentinel)

    def test_bounded_export_failure_retains_original_then_retry_recovers(self):
        failure = self.create("echo checkpoint > /tmp/state; exit 7")
        resources.docker("start", failure)
        self.assertEqual(resources.docker("wait", failure), "7")
        receipt = resources.retire_owned(resources.LABELS[0], self.run_id, self.directory, max_bytes=1)
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["containers"][0]["removed"])
        self.assertEqual(resources.inspect_owned(failure, resources.LABELS[0], self.run_id)["State"]["ExitCode"], 7)
        retry = resources.retire_owned(resources.LABELS[0], self.run_id, self.directory)
        self.assertEqual(retry["status"], "passed")
        self.assertTrue(retry["containers"][0]["removed"])

    def test_running_interrupted_test_is_stopped_archived_and_removed(self):
        running = self.create("echo ready > /tmp/interrupted; exec sleep 300")
        resources.docker("start", running)
        resources.docker("exec", running, "sh", "-c", "test -f /tmp/interrupted")
        receipt = resources.retire_owned(resources.LABELS[0], self.run_id, self.directory)
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue(receipt["containers"][0]["stopped_for_cleanup"])
        self.assertTrue(receipt["containers"][0]["removed"])

    def test_referenced_runner_volume_is_retained_and_exact_retry_recovers(self):
        name = "pgq-lifecycle-volume-" + self.run_id
        resources.docker("volume", "create", "--label", resources.LABELS[0] + "=" + self.run_id, name)
        try:
            owner = resources.docker("create", "--name", name,
                "--label", resources.LABELS[0] + "=" + self.run_id,
                "--label", resources.LABELS[1] + "=" + self.run_id,
                "--mount", "type=volume,source=" + name + ",target=/workspace",
                "--network=none", self.image, "true")
            self.ids.append(owner)
            blocker = resources.docker("create", "--label", resources.LABELS[0] + "=" + self.other_run,
                "--mount", "type=volume,source=" + name + ",target=/workspace",
                "--network=none", self.image, "true")
            self.ids.append(blocker)
            first = resources.retire_owned(resources.LABELS[1], self.run_id, self.directory, runner=True)
            self.assertEqual(first["status"], "failed")
            self.assertTrue(first["containers"][0]["removed"])
            self.assertEqual(first["containers"][0]["volumes_pending"][0]["name"], name)
            self.assertEqual(resources.inspect_owned(blocker, resources.LABELS[0], self.other_run)["Id"], blocker)
            resources.docker("rm", blocker)
            retry = resources.retire_owned(resources.LABELS[1], self.run_id, self.directory, runner=True)
            self.assertEqual(retry["status"], "passed")
            self.assertEqual(retry["containers"][0]["volumes_pending"], [])
            self.assertEqual(retry["containers"][0]["volumes_removed"], [name])
        finally:
            self.tearDown()
            result = subprocess.run(["docker", "volume", "inspect", name], capture_output=True)
            if result.returncode == 0:
                data = json.loads(result.stdout)[0]
                self.assertEqual(data["Labels"][resources.LABELS[0]], self.run_id)
                resources.docker("volume", "rm", name)

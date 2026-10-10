import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci_images as images
import ci_resources as resources


class ImagePolicy(unittest.TestCase):
    def runs(self):
        return [{"run_id": str(n)*16, "completed": n, "status": "failed" if n in (2, 4) else "passed",
                 "images": [{"id": "sha256:"+str(n)*64, "tags": ["fixture-"+str(n)+":latest"]}]}
                for n in range(1, 6)]

    def test_two_successful_and_latest_failed_families_are_retained(self):
        self.assertEqual(images.retained_runs(self.runs()), {"3"*16, "4"*16, "5"*16})

    def test_pins_and_container_references_are_never_removed(self):
        state = {"runs": self.runs(), "pins": ["sha256:"+"1"*64]}
        def docker(*args, **kwargs):
            if args[:2] == ("image", "ls"):
                return "\n".join("sha256:"+str(n)*64 for n in range(1, 6))
            if args[:2] == ("image", "inspect"):
                return json.dumps([{"Id": args[2], "Config": {"Labels": {images.IMAGE_RUN: "2"*16}},
                                    "RepoTags": ["fixture-2:latest"]}])
            return "container-reference"
        with patch.object(images, "docker", side_effect=docker) as calls:
            result = images.retire_images(state, lambda: None)
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["retained"]), 2)
        self.assertFalse(any(call.args[:2] == ("image", "rm") for call in calls.call_args_list))

    def test_changed_ownership_or_foreign_tag_retains_image(self):
        for label, tags in (("foreign", ["fixture-1:latest"]), ("1"*16, ["foreign:latest"])):
            state = {"runs": self.runs(), "pins": ["sha256:"+"2"*64]}
            data = {"Id": "sha256:"+"1"*64, "Config": {"Labels": {images.IMAGE_RUN: label}}, "RepoTags": tags}
            def docker(*args, **kwargs):
                if args[:2] == ("image", "ls"):
                    return data["Id"]
                return json.dumps([data])
            with patch.object(images, "docker", side_effect=docker) as calls:
                result = images.retire_images(state, lambda: None)
            self.assertEqual(len(result["errors"]), 1)
            self.assertFalse(any(call.args[:2] == ("image", "rm") for call in calls.call_args_list))

    def test_unknown_run_cannot_enter_completed_ledger(self):
        with self.assertRaises(ValueError):
            images.complete_images("foreign", "passed")

    def test_retry_recognizes_completed_daemon_deletion(self):
        state = {"runs": self.runs(), "pins": ["sha256:"+"2"*64]}
        with patch.object(images, "docker", return_value="") as calls:
            result = images.retire_images(state, lambda: None)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["removed"], ["sha256:"+"1"*64])
        self.assertTrue(state["runs"][0]["images"][0]["removed"])
        self.assertFalse(any(call.args[:2] == ("image", "rm") for call in calls.call_args_list))

    def test_temporary_alias_must_still_reference_exact_source(self):
        with patch.object(images, "docker", return_value='[{"Id":"different"}]') as calls:
            with self.assertRaises(RuntimeError):
                images.release_alias("pgq-quality-base-"+"1"*16, "expected")
        self.assertEqual(calls.call_count, 1)
        with self.assertRaises(ValueError):
            images.release_alias("business:latest", "expected")


@unittest.skipUnless(os.environ.get("PGQ_RUN_ID"), "requires the local act Docker runner")
class RealImagePolicy(unittest.TestCase):
    def test_old_family_removed_with_pinned_and_referenced_sentinels_preserved(self):
        nonce = secrets.token_hex(8)
        owned = []
        sentinel = None
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            base_tag = "pg-qdrant-act-runner:29.8.0-node24"
            base = resources.docker("image", "inspect", base_tag, "--format", "{{.Id}}")
            state = {"runs": [], "pins": []}
            try:
                for n in range(5):
                    run_id = secrets.token_hex(8)
                    tag = "pgq-retention-fixture-"+nonce+"-"+str(n)
                    (path / "Dockerfile").write_text("FROM "+base_tag+"\nLABEL "+images.IMAGE_RUN+"="+run_id+"\n")
                    built = subprocess.run(["docker", "build", "--pull=false", "--network=none", "-t", tag, str(path)],
                                           capture_output=True, timeout=60)
                    self.assertEqual(built.returncode, 0, built.stderr.decode(errors="replace"))
                    self.assertEqual(resources.docker("image", "inspect", base_tag, "--format", "{{.Id}}"), base)
                    identifier = resources.docker("image", "inspect", tag, "--format", "{{.Id}}")
                    owned.append(identifier)
                    state["runs"].append({"run_id": run_id, "completed": n, "status": "passed",
                        "images": [{"id": identifier, "tags": [tag+":latest"]}]})
                state["pins"] = [owned[0]]
                sentinel = resources.docker("create", "--network=none", "--label", resources.LABELS[0]+"="+nonce,
                    owned[1], "true")
                result = images.retire_images(state, lambda: images.save_state(path/"ledger.json", state))
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["removed"], [owned[2]])
                self.assertEqual({r["reason"] for r in result["retained"]}, {"pinned", "container reference"})
                for identifier in (owned[0], owned[1], owned[3], owned[4]):
                    self.assertEqual(resources.docker("image", "inspect", identifier, "--format", "{{.Id}}"), identifier)
                self.assertTrue(images.read_state(path/"ledger.json")["runs"][2]["images"][0]["removed"])
            finally:
                if sentinel:
                    data = resources.inspect_owned(sentinel, resources.LABELS[0], nonce)
                    resources.docker("rm", data["Id"])
                for identifier in owned:
                    observed = subprocess.run(["docker", "image", "inspect", identifier], capture_output=True, timeout=30)
                    if observed.returncode == 0:
                        data = json.loads(observed.stdout)[0]
                        self.assertIn(data["Config"]["Labels"][images.IMAGE_RUN], [r["run_id"] for r in state["runs"]])
                        resources.docker("image", "rm", "--no-prune", identifier, timeout=90)

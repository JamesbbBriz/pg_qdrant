"""Pure negative/configuration tests; never invoke Docker or native allocation."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "run_oom_container.py"
SPEC = importlib.util.spec_from_file_location("oom_container", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def inspected():
    return {"Id": "a" * 64, "Image": "sha256:" + "b" * 64, "Mounts": [],
        "Config": {"User": "10001:10001", "Labels": {"io.pg_qdrant.p0.oom": "c" * 32}},
        "HostConfig": {"Memory": MODULE.MEMORY, "MemorySwap": MODULE.MEMORY,
            "PidsLimit": 128, "NanoCpus": 2_000_000_000, "NetworkMode": "none", "IpcMode": "private",
            "CgroupnsMode": "private", "PidMode": "", "Privileged": False, "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"], "Ulimits": [{"Name": "core", "Soft": 0, "Hard": 0}]}}


def observation():
    return {"status": "observed", "profile": "managed_helper",
        "identities": {"target": {"pid": 17, "start_ticks": 101}},
        "counter_delta": {"max": 9, "oom": 1, "oom_kill": 1, "oom_group_kill": 0},
        "target_exit": {"engine_pid": 17, "signal": 9, "supervisor_kill_requested": False, "kill_attempts": 0},
        "caller_client_kill_requested": False, "companion_client_kill_requested": False,
        "companion_survived": True, "supervisor_preserved": True, "postmaster_preserved": True,
        "committed_marker_retained": True, "companion_overlapped_allocation": True,
        "replacement_edge_smoke_passed": True}


class OomEvidenceTests(unittest.TestCase):
    def test_positive_drivers_refuse_optimized_python_before_external_actions(self):
        inner = SOURCE.parents[1] / "crates/pg_qdrant/tests/verify_oom.py"
        for script in [SOURCE, inner]:
            for arguments, environment in [(["-O"], dict(os.environ)),
                    ([], dict(os.environ, PYTHONOPTIMIZE="1"))]:
                with self.subTest(script=script.name, arguments=arguments):
                    result = subprocess.run([sys.executable, *arguments, str(script)], env=environment,
                        capture_output=True, text=True, timeout=3)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("RuntimeError: optimized Python is unsupported for guarded P0 experiments", result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_owned_observer_commands_bound_time_and_each_output_stream(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), self.assertRaisesRegex(RuntimeError, "output budget"):
                MODULE.command([sys.executable, "-c", f"import sys; sys.{stream}.write('x'*{MODULE.LIMIT + 4096})"])
        with self.assertRaises(subprocess.TimeoutExpired):
            MODULE.command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
        result = MODULE.command([sys.executable, "-c", "import sys; print(sys.stdin.read()); print('err',file=sys.stderr)"], data="owned input")
        self.assertEqual(result.stdout, "owned input\n")
        self.assertEqual(result.stderr, "err\n")

    def test_actual_inspection_requires_every_resource_and_isolation_bound(self):
        baseline = inspected()
        MODULE.inspect_container(baseline, baseline["Image"], "c" * 32)
        for field, unsafe in [("Memory", 5*1024**3), ("MemorySwap", -1), ("PidsLimit", 0),
            ("NanoCpus", 0), ("NetworkMode", "host"), ("IpcMode", "host"), ("PidMode", "host"),
            ("CgroupnsMode", "host"), ("Privileged", True), ("CapDrop", []), ("SecurityOpt", []),
            ("Binds", ["/:/host"]), ("OomKillDisable", True)]:
            wrong = copy.deepcopy(baseline)
            wrong["HostConfig"][field] = unsafe
            with self.subTest(field=field), self.assertRaises(AssertionError):
                MODULE.inspect_container(wrong, baseline["Image"], "c" * 32)

    def test_counters_and_sigkill_alone_never_pass_strict_target_attribution(self):
        result = MODULE.classify(observation(), kernel_victim_verified=False, manual_termination=False)
        self.assertEqual(result["victim_attribution"], "correlated_only")
        self.assertEqual(result["status"], "inconclusive")
        self.assertFalse(result["target_isolation_gate_passed"])

    def test_supervisor_kill_and_watchdog_are_excluded_even_with_kernel_records(self):
        for field, value in [("supervisor_kill_requested", True), ("kill_attempts", 1)]:
            wrong = observation()
            wrong["target_exit"][field] = value
            self.assertFalse(MODULE.classify(wrong, kernel_victim_verified=True,
                manual_termination=False)["strict_target_attribution_passed"])
        self.assertFalse(MODULE.classify(observation(), kernel_victim_verified=True,
            manual_termination=True)["strict_target_attribution_passed"])

    def test_missing_postgres_outcome_or_multiple_victims_cannot_pass(self):
        for field in ["postmaster_preserved", "committed_marker_retained", "companion_overlapped_allocation",
                      "companion_survived", "replacement_edge_smoke_passed"]:
            wrong = observation()
            wrong.pop(field)
            self.assertFalse(MODULE.classify(wrong, kernel_victim_verified=True,
                manual_termination=False)["strict_target_attribution_passed"])
        for key, value in [("oom_kill", 2), ("oom_group_kill", 1), ("max", 0)]:
            wrong = observation()
            wrong["counter_delta"][key] = value
            self.assertFalse(MODULE.classify(wrong, kernel_victim_verified=True,
                manual_termination=False)["strict_target_attribution_passed"])

    def test_kernel_identity_requires_matching_memcg_and_victim_pair(self):
        container = "a"*64
        def record(message):
            return json.dumps({"__REALTIME_TIMESTAMP": "1000000", "MESSAGE": message})
        selector = record(f"oom-kill:constraint=CONSTRAINT_MEMCG,oom_memcg=/docker/{container},task=helper,pid=321,uid=10001")
        victim = record("Memory cgroup out of memory: Killed process 321 (helper) total-vm:100")
        mapping = {"host_pid": 321, "host_cgroup": f"/docker/{container}"}
        self.assertTrue(MODULE.kernel_victim_records(selector+"\n"+victim, mapping, container)[1])
        parent = record(f"oom-kill:constraint=CONSTRAINT_MEMCG,oom_memcg=/docker,task_memcg=/docker/{container},task=helper,pid=321,uid=10001")
        for wrong in [selector, victim, selector.replace(container, "b"*64)+"\n"+victim,
                      selector.replace("pid=321", "pid=322")+"\n"+victim,
                      parent+"\n"+victim,
                      selector.replace("oom_memcg=", "task_memcg=")+"\n"+victim,
                      selector.replace("task=helper", "oom_memcg=/docker,task=helper")+"\n"+victim,
                      selector.replace("CONSTRAINT_MEMCG", "CONSTRAINT_NONE")+"\n"+victim]:
            self.assertFalse(MODULE.kernel_victim_records(wrong, mapping, container)[1])

    def test_direct_recovery_is_a_valid_comparison_but_not_failure_isolation(self):
        direct = observation()
        direct.update(profile="direct_worker", companion_survived=False, supervisor_preserved=False)
        result = MODULE.classify(direct, kernel_victim_verified=True, manual_termination=False)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["strict_target_attribution_passed"])
        self.assertFalse(result["target_isolation_gate_passed"])
        self.assertFalse(result["production_memory_isolation_verified"])


if __name__ == "__main__":
    unittest.main()

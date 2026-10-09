"""Pure negative/configuration tests; never invoke Docker or native allocation."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

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
    def test_inner_ready_publication_is_complete_and_never_replaces(self):
        inner = SOURCE.parents[1] / "crates/pg_qdrant/tests/verify_oom.py"
        spec = importlib.util.spec_from_file_location("oom_inner_publication", inner)
        module = importlib.util.module_from_spec(spec)
        environment = {"PG_QDRANT_PSQL": "unused", "PG_QDRANT_ARTIFACT_DIR": "/unused",
            "PG_QDRANT_DISPOSABLE_DATA": "/unused", "PG_QDRANT_P0_OOM_RUN_ID": "0" * 32}
        with patch.dict(os.environ, environment):
            spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ready.json"
            fsync = os.fsync
            def before_publication(fd):
                self.assertFalse(path.exists())
                self.assertEqual(json.loads(path.with_name("ready.json.pending").read_bytes()),
                    {"nonce": "complete"})
                fsync(fd)
            with patch.object(module.os, "fsync", side_effect=before_publication):
                module.write_owned(path, {"nonce": "complete"})
            with self.assertRaises(FileExistsError):
                module.write_owned(path, {"nonce": "replacement"})
            self.assertEqual(json.loads(path.read_bytes()), {"nonce": "complete"})
            self.assertFalse(path.with_name("ready.json.pending").exists())

    def test_barrier_is_invisible_until_complete_stdin_is_published(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "go.json"
            child = subprocess.Popen([sys.executable, "-c", MODULE.BARRIER_WRITER, str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                child.stdin.write(b'{"nonce":')
                child.stdin.flush()
                deadline = time.monotonic() + 3
                while not path.with_name("go.json.pending").exists():
                    self.assertIsNone(child.poll())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                self.assertFalse(path.exists())
                output, error = child.communicate(b'"complete"}\n', timeout=3)
                self.assertEqual(child.returncode, 0, error)
                self.assertEqual(output, b"")
                self.assertEqual(json.loads(path.read_bytes()), {"nonce": "complete"})
                self.assertFalse(path.with_name("go.json.pending").exists())
                if os.name == "posix":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=3)

    def test_barrier_refuses_incomplete_oversized_or_reused_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "go.json"
            for data in (b"", b'{"nonce":', b" " * 16385):
                result = subprocess.run([sys.executable, "-c", MODULE.BARRIER_WRITER, str(path)],
                    input=data, capture_output=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(path.exists())
                self.assertFalse(path.with_name("go.json.pending").exists())
            path.write_bytes(b'{"nonce":"original"}')
            result = subprocess.run([sys.executable, "-c", MODULE.BARRIER_WRITER, str(path)],
                input=b'{"nonce":"replacement"}', capture_output=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_bytes(), b'{"nonce":"original"}')
            self.assertFalse(path.with_name("go.json.pending").exists())

    def test_mapping_wrapper_compiles_with_future_import_and_limits_observer_capabilities(self):
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if args[1] == 'run':
                compile(args[args.index('-c') + 1], '<observer-wrapper>', 'exec')
                return subprocess.CompletedProcess(args, 0, stdout='{}', stderr='')
            return subprocess.CompletedProcess(args, 1, stdout='', stderr='')
        with patch.object(MODULE, 'command', side_effect=command):
            MODULE.observe('local-image', 'mapping', {'kernel_task_identity': True})
            MODULE.observe('local-image', 'kernel', {})
        runs = [args for args in calls if args[1] == 'run']
        self.assertIn('--cap-add=BPF', runs[0])
        self.assertIn('--cap-add=PERFMON', runs[0])
        for args in runs:
            self.assertNotIn('--privileged', args)
            self.assertNotIn('--cap-add=SYS_PTRACE', args)
            self.assertIn('--read-only', args)
        self.assertNotIn('--cap-add=BPF', runs[1])
        self.assertNotIn('--cap-add=PERFMON', runs[1])

    def test_kernel_victim_uses_prebound_initial_namespace_pid(self):
        container = 'a' * 64
        mapping = {'host_pid': 77, 'container_pid': 9, 'start_ticks': 123,
                   'namespace_inode': 1234, 'clock_ticks': 100,
                   'host_cgroup': '/docker/' + container,
                   'kernel_identity': {'inner_pid': 9, 'kernel_pid': 88, 'kernel_tgid': 88,
                                       'namespace_inode': 1234, 'start_boottime_ns': 1230000000}}
        def records(pid):
            return '\n'.join(json.dumps({'MESSAGE': message}) for message in [
                f'oom-kill:constraint=CONSTRAINT_MEMCG,oom_memcg=/docker/{container},pid={pid}',
                f'Memory cgroup out of memory: Killed process {pid} (native)'])
        self.assertTrue(MODULE.kernel_victim_records(records(88), mapping, container)[1])
        self.assertFalse(MODULE.kernel_victim_records(records(77), mapping, container)[1])

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

    def test_collateral_first_log_still_requires_exact_kernel_victim(self):
        direct = observation()
        direct.update(profile='direct_worker', companion_survived=False, supervisor_preserved=False,
            postgresql_crash_log={'caller_logged': True, 'reinitializing': True})
        direct['target_exit'].update(signal=None, source='requires_exact_kernel_victim_record')
        self.assertEqual(MODULE.classify(direct, kernel_victim_verified=True,
            manual_termination=False)['status'], 'passed')
        self.assertFalse(MODULE.classify(direct, kernel_victim_verified=False,
            manual_termination=False)['strict_target_attribution_passed'])
        for field in ['caller_logged', 'reinitializing']:
            wrong = copy.deepcopy(direct)
            wrong['postgresql_crash_log'][field] = False
            self.assertFalse(MODULE.classify(wrong, kernel_victim_verified=True,
                manual_termination=False)['strict_target_attribution_passed'])
        direct['profile'] = 'managed_helper'
        self.assertFalse(MODULE.classify(direct, kernel_victim_verified=True,
            manual_termination=False)['strict_target_attribution_passed'])


if __name__ == "__main__":
    unittest.main()

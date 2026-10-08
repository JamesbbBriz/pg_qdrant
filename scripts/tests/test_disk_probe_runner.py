"""Exercise wrapper decisions with real, owned child processes; no disk filling."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import signal
import sys
import unittest


SPEC = importlib.util.spec_from_file_location(
    "run_disk_probe", Path(__file__).resolve().parents[1] / "run_disk_probe.py")
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class DiskProbeRunnerTest(unittest.TestCase):
    def run_child(self, source: str, timeout: float = 2) -> dict:
        return RUNNER.run_probe([sys.executable, "-c", source], timeout=timeout)

    def test_real_success_and_stage_evidence(self) -> None:
        report = self.run_child(
            "import sys; "
            "print('pg_qdrant_p0_stage=completed', file=sys.stderr, flush=True); "
            "print('{\"kind\":\"edge_enospc_probe\",\"status\":\"passed\"}')")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["execution"]["returncode"], 0)
        self.assertEqual(report["execution"]["last_stage"], "completed")

    def test_body_cannot_hide_nonzero_process_exit(self) -> None:
        report = self.run_child(
            "print('{\"kind\":\"edge_enospc_probe\",\"status\":\"passed\"}', flush=True); "
            "raise SystemExit(17)")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["native_report_status"], "passed")
        self.assertEqual(report["execution"]["exit_code"], 17)

    def test_selected_fixture_profile_must_match_the_native_report(self) -> None:
        source = "print('{\"kind\":\"edge_enospc_probe\",\"status\":\"passed\",\"profile\":\"vector_keyword\"}')"
        command = [sys.executable, "-c", source]
        matched = RUNNER.run_probe(command, required_profile="vector_keyword")
        self.assertEqual(matched["status"], "passed")
        wrong = RUNNER.run_probe(command, required_profile="full_text")
        self.assertEqual(wrong["status"], "failed")
        self.assertEqual(wrong["native_report_status"], "passed")

    def test_native_signal_without_json_retains_failure_phase(self) -> None:
        report = self.run_child(
            "import os, signal, sys; "
            "print('pg_qdrant_p0_stage=fixture_create', file=sys.stderr, flush=True); "
            "os.kill(os.getpid(), signal.SIGTERM)")
        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["native_report_status"])
        self.assertEqual(report["execution"]["returncode"], -signal.SIGTERM)
        self.assertEqual(report["execution"]["signal_name"], "SIGTERM")
        self.assertEqual(report["execution"]["last_stage"], "fixture_create")

    def test_not_run_and_wrong_experiment_are_not_passes(self) -> None:
        for body in [
            '{"kind":"edge_enospc_probe","status":"not_run"}',
            '{"kind":"edge_corruption_probe","status":"passed"}',
        ]:
            with self.subTest(body=body):
                self.assertEqual(self.run_child(f"print({body!r})")["status"], "failed")
        body = ('{"kind":"edge_enospc_probe","status":"not_run",'
                '"reason":"explicit guard rejection","reason_code":"unsafe_environment"}')
        guard = self.run_child(f"print({body!r}, flush=True); raise SystemExit(2)")
        self.assertEqual(guard["status"], "failed")
        self.assertEqual(guard["native_report_reason"], "explicit guard rejection")
        self.assertEqual(guard["reason_code"], "unsafe_environment")

    def test_timeout_preserves_output_without_guessing_exit_signal(self) -> None:
        report = self.run_child(
            "import sys, time; "
            "print('pg_qdrant_p0_stage=fill', file=sys.stderr, flush=True); "
            "time.sleep(10)", timeout=0.2)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["execution"]["timed_out"])
        self.assertIsNone(report["execution"]["returncode"])
        self.assertIsNone(report["execution"]["signal_name"])
        self.assertEqual(report["execution"]["last_stage"], "fill")


if __name__ == "__main__":
    unittest.main()

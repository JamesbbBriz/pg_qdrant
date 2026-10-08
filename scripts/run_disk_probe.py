#!/usr/bin/env python3
"""Run the explicitly provisioned tmpfs experiment and require a real pass."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess


OUTPUT_TAIL_LIMIT = 8000
STAGE_PREFIX = "pg_qdrant_p0_stage="


def output_tail(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "")[-OUTPUT_TAIL_LIMIT:]


def run_probe(command: list[str], timeout: float = 90) -> dict:
    """Keep process failure evidence even when native code cannot return JSON."""
    returncode = None
    timed_out = False
    launch_error = None
    try:
        result = subprocess.run(command, text=True, encoding="utf-8", errors="replace",
                                capture_output=True, timeout=timeout)
        returncode = result.returncode
        stdout, stderr = result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        # subprocess.run kills and reaps its child, but TimeoutExpired does not
        # expose its final return code. Do not invent a native exit signal.
        timed_out = True
        stdout, stderr = output_tail(error.stdout), output_tail(error.stderr)
    except OSError as error:
        launch_error = {"errno": error.errno, "message": str(error)}
        stdout, stderr = "", ""

    stages = [line[len(STAGE_PREFIX):] for line in output_tail(stderr).splitlines()
              if line.startswith(STAGE_PREFIX)]
    signal_number = -returncode if returncode is not None and returncode < 0 else None
    signal_name = None
    if signal_number:
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            # Python does not enumerate every platform's real-time signals.
            signal_name = f"signal_{signal_number}"
    execution = {
        "returncode": returncode,
        "exit_code": returncode if returncode is not None and returncode >= 0 else None,
        "signal_number": signal_number,
        "signal_name": signal_name,
        "timed_out": timed_out,
        "timeout_seconds": timeout,
        "launch_error": launch_error,
        "stderr_tail": output_tail(stderr),
        "stages": stages,
        "last_stage": stages[-1] if stages else None,
    }
    parsed_native_report = False
    try:
        report = json.loads(stdout)
        parsed_native_report = isinstance(report, dict)
    except json.JSONDecodeError:
        report = {"status": "failed", "reason": "Probe did not return a JSON report"}
        execution["stdout_tail"] = output_tail(stdout)
    if not isinstance(report, dict):
        report = {"status": "failed", "reason": "Probe report must be a JSON object"}
        execution["stdout_tail"] = output_tail(stdout)

    # A body claiming success cannot override a crash, nonzero exit, timeout,
    # wrong experiment, or not_run result from the guarded native probe.
    passed = (returncode == 0 and not timed_out
              and report.get("kind") == "edge_enospc_probe"
              and report.get("status") == "passed")
    if not passed:
        native_status = report.get("status") if parsed_native_report else None
        native_reason = report.get("reason") if parsed_native_report else None
        report["status"] = "failed"
        report["native_report_status"] = native_status
        report["native_report_reason"] = native_reason
        if timed_out:
            report["reason"] = "Probe exceeded its execution limit; child killed and reaped"
        elif launch_error:
            report["reason"] = "Probe process could not be started"
        elif returncode != 0:
            report["reason"] = "Probe terminated without a successful process exit"
        else:
            report.setdefault("reason", "A successful ENOSPC report is required")
    report["execution"] = execution
    return report


def main() -> None:
    artifacts = Path(os.environ.get("PG_QDRANT_ARTIFACT_DIR", "artifacts"))
    artifacts.mkdir(parents=True, exist_ok=True)
    report = run_probe(["target/debug/pg-qdrant-edge-probe", "--disk-full-probe"])
    (artifacts / "edge-enospc.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    if report["status"] != "passed":
        raise SystemExit("The provisioned ENOSPC experiment did not pass.")


if __name__ == "__main__":
    main()

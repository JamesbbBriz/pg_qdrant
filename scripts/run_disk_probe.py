#!/usr/bin/env python3
"""Run the explicitly provisioned tmpfs experiment and require a real pass."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


def main() -> None:
    artifacts = Path(os.environ.get("PG_QDRANT_ARTIFACT_DIR", "artifacts"))
    artifacts.mkdir(parents=True, exist_ok=True)
    command = ["target/debug/pg-qdrant-edge-probe", "--disk-full-probe"]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=90)
    except subprocess.TimeoutExpired:
        report = {"status": "failed", "reason": "Probe exceeded the 90-second execution limit"}
        (artifacts / "edge-enospc.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
        raise SystemExit("The provisioned ENOSPC experiment timed out.") from None
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {"status": "failed", "reason": "Probe did not return a JSON report"}
    if not isinstance(report, dict):
        report = {"status": "failed", "reason": "Probe report must be a JSON object"}
    (artifacts / "edge-enospc.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    if result.returncode != 0 or report.get("status") != "passed":
        if result.stderr:
            print(result.stderr[-8000:])
        raise SystemExit("The provisioned ENOSPC experiment did not pass.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Execute the shared local CI registry on Linux; export failures as failures."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def selected_steps(registry, profile):
    if profile in registry.get("diagnostics", {}):
        return registry["diagnostics"][profile]
    profiles = list(registry["profiles"]) if profile == "full" else [profile]
    return [step for name in profiles for step in registry["profiles"][name]]


def execute_steps(steps, report, artifacts, env):
    outcomes = {}
    for step in steps:
        row = {"id": step["id"], "name": step["name"], "command": step["command"]}
        if any(outcomes.get(dep) != "passed" for dep in step["requires"]):
            row.update(status="skipped", reason="required image gate failed")
        else:
            started = time.monotonic()
            print(f"\n>>> {step['id']}: {step['name']}", flush=True)
            log_path = artifacts / (step["id"] + ".log")
            with log_path.open("wb") as log:
                process = subprocess.Popen(["bash", "-euo", "pipefail", "-c", step["command"]],
                    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                for chunk in iter(lambda: process.stdout.read1(65536), b""):
                    log.write(chunk)
                    log.flush()
                    print(chunk.decode(errors="replace"), end="", flush=True)
                code = process.wait()
                process.stdout.close()
            row.update(status="passed" if code == 0 else "failed", exit_code=code,
                       duration_seconds=round(time.monotonic() - started, 3), log=log_path.name,
                       log_sha256=hashlib.sha256(log_path.read_bytes()).hexdigest())
        outcomes[step["id"]] = row["status"]
        report["steps"].append(row)
        (artifacts / "verify-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return all(value == "passed" for value in outcomes.values())


def cleanup(run_id, report, remove):
    """Record owned containers; the host retires them after verified export."""
    result = subprocess.run(["docker", "ps", "-aq", "--filter",
                             "label=io.pg_qdrant.ci=" + run_id], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("unable to enumerate owned test containers")
    report["containers"] = []
    for container_id in result.stdout.split():
        data = json.loads(subprocess.check_output(["docker", "inspect", container_id]))[0]
        if data["Config"].get("Labels", {}).get("io.pg_qdrant.ci") != run_id:
            raise RuntimeError("container ownership changed")
        report["containers"].append({"id": data["Id"], "name": data["Name"],
                                     "state": data["State"], "removed": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["full", "engine", "ledger", "static", "lifecycle_failure"], default="full")
    args = parser.parse_args()
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    run_id = os.environ.get("PGQ_RUN_ID", "")
    if not re.fullmatch(r"[a-f0-9]{16}", run_id):
        raise ValueError("PGQ_RUN_ID must be a fresh 16-character lowercase hex run identity")
    env = dict(os.environ)
    snapshot = json.loads((ROOT / "ci-input.json").read_text())
    env["PG_QDRANT_SOURCE_SHA"] = snapshot["commit"]
    report = {"schema_version": 1, "run_id": run_id, "profile": args.profile,
              "snapshot": snapshot, "python": platform.python_version(), "steps": [],
              "status": "running", "release_supported": False}
    started = time.monotonic()
    passed = False
    try:
        for item in snapshot["files"]:
            if hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
                raise RuntimeError("source snapshot changed: " + item["path"])
        report["docker"] = subprocess.check_output(["docker", "version", "--format", "{{json .}}"], text=True).strip()
        registry = json.loads((ROOT / "ci/steps.json").read_text())
        passed = execute_steps(selected_steps(registry, args.profile), report, artifacts, env)
    except Exception as error:
        report["error"] = str(error)
    finally:
        try:
            cleanup(run_id, report, passed)
        except Exception as error:
            passed = False
            report["cleanup_error"] = str(error)
        report.update(status="passed" if passed else "failed",
                      duration_seconds=round(time.monotonic() - started, 3))
        (artifacts / "verify-report.json").write_text(json.dumps(report, indent=2) + "\n")
        summary = {key: value for key, value in report.items() if key != "snapshot"}
        summary["source_sha256"] = snapshot["source_sha256"]
        print(json.dumps(summary, indent=2), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

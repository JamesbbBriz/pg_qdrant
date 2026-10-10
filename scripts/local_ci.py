#!/usr/bin/env python3
"""Run local act against an immutable input copy and export complete evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

from ci_images import complete_images
from ci_resources import MANAGED, require_clean_start, finish_run, local_run_lock, storage_preflight

ROOT = Path(__file__).resolve().parents[1]
RUNNER = "pg-qdrant-act-runner:29.8.0-node24"


def output(args, **kwargs):
    return subprocess.check_output(args, cwd=ROOT, **kwargs)


def snapshot(destination):
    commit = output(["git", "rev-parse", "HEAD"], text=True).strip()
    before = output(["git", "status", "--porcelain=v1", "-z"])
    dirty_diff = output(["git", "diff", "HEAD", "--binary"])
    untracked = output(["git", "ls-files", "-z", "--others", "--exclude-standard"])
    paths = output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]).split(b"\0")
    files = []
    for raw in sorted(set(paths)):
        if not raw:
            continue
        relative = raw.decode("utf-8")
        source = ROOT / relative
        if not source.exists():
            continue  # tracked deletion is recorded in the diff hash
        if source.is_symlink():
            raise ValueError("snapshot symlinks are unsupported: " + relative)
        data = source.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest()})
    if (output(["git", "rev-parse", "HEAD"], text=True).strip() != commit
            or output(["git", "status", "--porcelain=v1", "-z"]) != before
            or output(["git", "diff", "HEAD", "--binary"]) != dirty_diff
            or any(hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest() != item["sha256"] for item in files)):
        raise RuntimeError("working tree changed while capturing CI input; retry with stable inputs")
    manifest = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    report = {"commit": commit,
              "ref": output(["git", "branch", "--show-current"], text=True).strip(),
              "dirty_diff_sha256": hashlib.sha256(dirty_diff).hexdigest(),
              "untracked_inputs": [path.decode("utf-8") for path in untracked.split(b"\0") if path],
              "source_sha256": hashlib.sha256(manifest).hexdigest(), "files": files,
              "cargo_lock_sha256": hashlib.sha256((destination / "Cargo.lock").read_bytes()).hexdigest()}
    (destination / "ci-input.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def act_binary(value):
    if value:
        return value
    found = shutil.which("act")
    if found:
        return found
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        matches = list((Path(os.environ["LOCALAPPDATA"]) / "Microsoft/WinGet/Packages").glob("nektos.act_*/act.exe"))
        if len(matches) == 1:
            return str(matches[0])
    raise RuntimeError("act is required; install act or pass --act PATH")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["full", "engine", "ledger", "static", "lifecycle_failure"], default="full")
    parser.add_argument("--act", default=os.environ.get("ACT_BINARY"))
    args = parser.parse_args()
    run_id = secrets.token_hex(8)
    directory = ROOT / "artifacts/local-ci" / run_id
    source = directory / "snapshot"
    source.mkdir(parents=True)
    report = {"run_id": run_id, "profile": args.profile, "status": "running", "release_supported": False}
    network = "pgq-ci-" + run_id
    network_created = False
    started = time.monotonic()
    code = 1
    try:
        require_clean_start()
        report["snapshot"] = snapshot(source)
        act = act_binary(args.act)
        report["act_version"] = output([act, "--version"], text=True).strip()
        subprocess.run(["docker", "build", "-f", "ci/Dockerfile.runner", "-t", RUNNER, "."], cwd=source, check=True)
        image = output(["docker", "image", "inspect", RUNNER, "--format", "{{.Id}}"], text=True).strip()
        report["runner_image_id"] = image
        report["storage_preflight"] = storage_preflight(directory, image, run_id, "io.pg_qdrant.ci",
            heavy=args.profile not in ("static", "lifecycle_failure"))
        subprocess.run(["docker", "network", "create", "--label", "io.pg_qdrant.ci.runner=" + run_id, network], check=True)
        network_created = True
        workflow = directory / "run.yml"
        workflow.write_text((source / "ci/verify.yml").read_text()
                            .replace("name: Local verification", "name: Local verification " + run_id)
                            .replace("  verify:", "  verify_" + run_id + ":"))
        empty_env = directory / "empty-env"
        empty_env.write_text("")
        command = [act, "workflow_dispatch", "-C", str(source), "-W", str(workflow),
                   "-j", "verify_" + run_id, "-P", "ubuntu-24.04=" + image,
                   "--pull=false", "--reuse", "--network", network,
                   "--container-daemon-socket", "/var/run/docker.sock",
                   "--env-file", str(empty_env), "--secret-file", str(empty_env),
                   "--container-options", "--label " + MANAGED + "=1 --label io.pg_qdrant.ci.runner=" + run_id
                   + ' --mount "type=bind,source=' + source.as_posix() + ',target=/pgq-input,readonly"',
                   "--env", "PGQ_RUN_ID=" + run_id, "--env", "PGQ_PROFILE=" + args.profile]
        report["command"] = command
        with (directory / "act.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                for chunk in iter(lambda: process.stdout.read1(65536), b""):
                    log.write(chunk)
                    log.flush()
                    print(chunk.decode(errors="replace"), end="", flush=True)
                code = process.wait()
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=30)
        report["act_exit_code"] = code
    except Exception as error:
        report["error"] = str(error)
    finally:
        try:
            ids = output(["docker", "ps", "-aq", "--filter", "label=io.pg_qdrant.ci.runner=" + run_id], text=True).split()
            if len(ids) != 1:
                raise RuntimeError("expected exactly one owned act runner for evidence export")
            data = json.loads(output(["docker", "inspect", ids[0]]))[0]
            if data["Config"].get("Labels", {}).get("io.pg_qdrant.ci.runner") != run_id:
                raise RuntimeError("act runner ownership changed")
            evidence = directory / "evidence"
            evidence.mkdir()
            subprocess.run(["docker", "cp", data["Id"] + ":/tmp/pgq-ci-evidence/.", str(evidence)], check=True, timeout=90)
            verification = json.loads((evidence / "verify-report.json").read_text())
            if verification["run_id"] != run_id or verification["snapshot"] != report["snapshot"]:
                raise RuntimeError("exported evidence belongs to a different input snapshot")
            for step in verification["steps"]:
                if "log" in step and hashlib.sha256((evidence / step["log"]).read_bytes()).hexdigest() != step["log_sha256"]:
                    raise RuntimeError("exported step log changed")
            if verification["status"] != "passed":
                code = 1
        except Exception as error:
            code = 1
            report["export_error"] = str(error)
        report["resource_cleanup"] = finish_run(run_id, directory, "io.pg_qdrant.ci",
            "io.pg_qdrant.ci.runner", "/tmp/pgq-ci-evidence/.")
        if report["resource_cleanup"]["status"] != "passed":
            code = 1
        else:
            try:
                report["image_retention"] = complete_images(run_id, "passed" if code == 0 else "failed")
                if report["image_retention"]["errors"]:
                    code = 1
            except Exception as error:
                code = 1
                report["image_retention_error"] = str(error)
        if network_created:
            result = subprocess.run(["docker", "network", "rm", network], capture_output=True, text=True)
            if result.returncode:
                code = 1
                report["network_cleanup_error"] = result.stderr
        report.update(status="passed" if code == 0 else "failed", duration_seconds=round(time.monotonic() - started, 3))
        (directory / "run-report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({key: value for key, value in report.items() if key != "snapshot"}, indent=2), flush=True)
        print("Evidence: " + str(directory), flush=True)
    return code


if __name__ == "__main__":
    with local_run_lock():
        raise SystemExit(main())

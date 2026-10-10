#!/usr/bin/env python3
"""Run bounded quality measurement locally with act and frozen input hashes."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys

from local_ci import ROOT, RUNNER, act_binary, snapshot
from ci_images import complete_images, pin_image, release_alias
from ci_resources import MANAGED, require_clean_start, finish_run, local_run_lock, storage_preflight


def output(args):
    return subprocess.check_output(args, text=True, encoding="utf-8").strip()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="matching installed development product image")
    parser.add_argument("--fixtures", type=Path, default=ROOT / "artifacts/quality-fixtures")
    parser.add_argument("--act")
    args = parser.parse_args()
    run_id = secrets.token_hex(8)
    directory = ROOT / "artifacts/local-ci" / ("quality-" + run_id)
    directory.mkdir(parents=True)
    require_clean_start()
    capacity = storage_preflight(directory, RUNNER, run_id,
        "io.pg_qdrant.ci.quality", heavy=True)
    source = directory / "snapshot"
    source.mkdir(parents=True)
    report = {"run_id": run_id, "kind": "bounded_quality_measurement", "release_supported": False, "storage_preflight": capacity,
              "snapshot": snapshot(source), "status": "running"}
    image = output(["docker", "image", "inspect", args.image, "--format", "{{.Id}}"])
    runner = output(["docker", "image", "inspect", RUNNER, "--format", "{{.Id}}"])
    pin_image(image)
    report.update(product_image=image, runner_image=runner, fixtures={})
    expected = json.loads((source / "ci/quality-sources.json").read_text(encoding="utf-8"))
    (source / "fixtures").mkdir()
    for dataset in expected["datasets"]:
        name = dataset["dataset"] + ".json"
        data = (args.fixtures / name).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != dataset["fixture_sha256"]:
            raise ValueError("fixture identity mismatch: " + name)
        (source / "fixtures" / name).write_bytes(data)
        report["fixtures"][name] = digest
    base = "pgq-quality-base-" + run_id
    subprocess.run(["docker", "tag", image, base], check=True)
    if output(["docker", "image", "inspect", base, "--format", "{{.Id}}"]) != image:
        raise ValueError("base image identity changed")
    empty = directory / "empty-env"
    empty.write_bytes(b"")
    manifest = directory / "run-report.json"
    manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    command = [act_binary(args.act), "workflow_dispatch", "-C", str(source), "-W", str(source / "ci/quality.yml"),
               "-j", "quality", "-P", "ubuntu-24.04=" + runner, "--pull=false", "--reuse", "--network", "none",
               "--container-daemon-socket", "/var/run/docker.sock", "--env-file", str(empty), "--secret-file", str(empty),
               "--env", "PGQ_RUN_ID=" + run_id, "--container-options",
               '--label ' + MANAGED + '=1 --label io.pg_qdrant.ci.quality-runner=' + run_id + ' --mount "type=bind,source=' + source.as_posix() + ',target=/pgq-input,readonly"']
    code = 1
    try:
        code = measure(command, directory, report, manifest, source, run_id)
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        report["resource_cleanup"] = finish_run(run_id, directory, "io.pg_qdrant.ci.quality",
            "io.pg_qdrant.ci.quality-runner", "/tmp/quality-evidence/.")
        if report["resource_cleanup"]["status"] != "passed":
            code = 1
        else:
            try:
                release_alias(base, image)
                report["image_retention"] = complete_images(run_id, "passed" if code == 0 else "failed")
                if report["image_retention"]["errors"]:
                    code = 1
            except Exception as error:
                code = 1
                report["image_retention_error"] = str(error)
        if code != 0:
            report["status"] = "failed"
        manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return code


def measure(command, directory, report, manifest, source, run_id):
    print(directory, flush=True)
    with (directory / "act.log").open("wb") as log:
        try:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1200)
        except subprocess.TimeoutExpired:
            report.update(status="failed", act_exit=124, reason="act deadline; diagnostic retirement required")
            manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
            raise
    report["act_exit"] = result.returncode
    ids = output(["docker", "ps", "-aq", "--filter", "label=io.pg_qdrant.ci.quality-runner=" + run_id]).split()
    if len(ids) != 1:
        raise ValueError("quality runner identity is ambiguous")
    state = json.loads(output(["docker", "inspect", ids[0]]))[0]
    if state["Config"]["Labels"]["io.pg_qdrant.ci.quality-runner"] != run_id:
        raise ValueError("unexpected quality runner")
    subprocess.run(["docker", "cp", ids[0] + ":/tmp/quality-evidence", str(directory / "evidence")], check=True, timeout=90)
    native_name = "pgq-quality-" + run_id
    observed = subprocess.run(["docker", "inspect", native_name], text=True, capture_output=True)
    if observed.returncode == 0:
        native = json.loads(observed.stdout)[0]
        if native["Config"]["Labels"]["io.pg_qdrant.ci.quality"] != run_id:
            raise ValueError("unexpected product container")
        report["container_state"] = native["State"]
    report["act_log_sha256"] = hashlib.sha256((directory / "act.log").read_bytes()).hexdigest()
    for item in report["snapshot"]["files"]:
        if hashlib.sha256((source / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("frozen source input changed: " + item["path"])
    for name, digest in report["fixtures"].items():
        if hashlib.sha256((source / "fixtures" / name).read_bytes()).hexdigest() != digest:
            raise ValueError("frozen fixture changed: " + name)
    report["evidence_sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in (directory / "evidence").iterdir() if path.is_file()}
    if result.returncode == 0:
        measured = json.loads((directory / "evidence/quality-results.json").read_text(encoding="utf-8"))
        if measured["status"] != "measured" or measured["errors"] or len(measured["datasets"]) != 2:
            raise ValueError("measurement is incomplete")
        if any(d["status"] != "measured" or len(d["queries"]) != 32 for d in measured["datasets"]):
            raise ValueError("expected 64 measured queries")
    report["status"] = "measured" if result.returncode == 0 else "failed"
    manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Evidence:", directory)
    return result.returncode


if __name__ == "__main__":
    with local_run_lock():
        raise SystemExit(main())

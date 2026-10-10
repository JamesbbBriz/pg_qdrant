#!/usr/bin/env python3
"""Build and install a local preview through act, with frozen provenance."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys

from local_ci import ROOT, RUNNER, act_binary, snapshot
from ci_resources import MANAGED, require_clean_start, finish_run


def output(arguments):
    return subprocess.check_output(arguments, text=True, encoding="utf-8").strip()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    product = parser.add_mutually_exclusive_group(required=True)
    product.add_argument("--image", help="existing normal managed-helper image")
    product.add_argument("--build", action="store_true", help="compile the clean frozen source through local act")
    parser.add_argument("--source-snapshot", type=Path, help="existing image's clean compiled ci-input.json")
    parser.add_argument("--act")
    args = parser.parse_args()
    require_clean_start()
    if bool(args.source_snapshot) != bool(args.image):
        raise ValueError("--source-snapshot is required only with --image")
    run_id = secrets.token_hex(8)
    directory = ROOT / "artifacts/local-ci" / ("preview-" + run_id)
    source = directory / "snapshot"
    source.mkdir(parents=True)
    captured = snapshot(source)
    original = args.source_snapshot.read_bytes() if args.image else (source / "ci-input.json").read_bytes()
    compiled = json.loads(original)
    if (compiled["dirty_diff_sha256"] != hashlib.sha256(b"").hexdigest() or compiled["untracked_inputs"]):
        raise ValueError("compiled source snapshot must be clean")
    image_data = None
    if args.image:
        image_data = json.loads(output(["docker", "image", "inspect", args.image]))[0]
        variables = dict(item.split("=", 1) for item in image_data["Config"]["Env"])
        if (variables.get("PG_QDRANT_SOURCE_SHA") != compiled["commit"]
                or variables.get("PG_QDRANT_MANAGED_HELPER") != "1"):
            raise ValueError("image and clean source snapshot do not match")
    report = {"run_id": run_id, "kind": "local_installation_preview", "status": "running",
              "release_supported": False, "snapshot": captured,
              "product_image": image_data["Id"] if image_data else None, "build_product": args.build,
              "compiled_source_commit": compiled["commit"], "compiled_source_manifest_sha256": compiled["source_sha256"],
              "compiled_snapshot_file_sha256": hashlib.sha256(original).hexdigest()}
    (source / "source-input.json").write_bytes(original)
    runner = output(["docker", "image", "inspect", RUNNER, "--format", "{{.Id}}"])
    report["runner_image"] = runner
    base = "pgq-preview-base-" + run_id
    if image_data:
        subprocess.run(["docker", "tag", image_data["Id"], base], check=True)
        if output(["docker", "image", "inspect", base, "--format", "{{.Id}}"]) != image_data["Id"]:
            raise ValueError("base image changed")
    empty = directory / "empty-env"
    empty.write_bytes(b"")
    manifest = directory / "run-report.json"
    manifest.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
    command = [act_binary(args.act), "workflow_dispatch", "-C", str(source), "-W", str(source / "ci/preview.yml"),
               "-j", "preview", "-P", "ubuntu-24.04=" + runner, "--pull=false", "--reuse", "--network", "none",
               "--container-daemon-socket", "/var/run/docker.sock", "--env-file", str(empty), "--secret-file", str(empty),
               "--env", "PGQ_RUN_ID=" + run_id, "--env", "PGQ_BUILD_PRODUCT=" + ("1" if args.build else "0"),
               "--env", "PGQ_SOURCE_COMMIT=" + compiled["commit"], "--container-options",
               '--label ' + MANAGED + '=1 --label io.pg_qdrant.ci.preview-runner=' + run_id + ' --mount "type=bind,source=' + source.as_posix() + ',target=/pgq-input,readonly"']
    print(directory, flush=True)
    code = 1
    try:
        with (directory / "act.log").open("wb") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1200)
        report["act_exit"] = code = result.returncode
        if args.build:
            built = subprocess.run(["docker", "image", "inspect", base, "--format", "{{.Id}}"],
                                   text=True, capture_output=True)
            if built.returncode == 0:
                report["product_image"] = built.stdout.strip()
        ids = output(["docker", "ps", "-aq", "--filter", "label=io.pg_qdrant.ci.preview-runner=" + run_id]).split()
        if len(ids) != 1:
            raise ValueError("preview runner identity is ambiguous")
        state = json.loads(output(["docker", "inspect", ids[0]]))[0]
        if state["Config"]["Labels"]["io.pg_qdrant.ci.preview-runner"] != run_id:
            raise ValueError("unexpected preview runner")
        subprocess.run(["docker", "cp", ids[0] + ":/tmp/preview-evidence", str(directory / "evidence")], check=True, timeout=90)
        for kind in ("runtime", "archive"):
            observed = subprocess.run(["docker", "inspect", "pgq-preview-" + kind + "-" + run_id],
                                      text=True, capture_output=True)
            if observed.returncode:
                continue
            native = json.loads(observed.stdout)[0]
            if native["Config"]["Labels"]["io.pg_qdrant.ci.preview"] != run_id:
                raise ValueError("unexpected preview container")
            report[kind + "_container_state"] = native["State"]
            if kind == "runtime" and code == 0 and native["State"]["ExitCode"] != 0:
                raise ValueError("runtime container did not complete")
        if code == 0:
            installed = json.loads((directory / "evidence/preview-result.json").read_text(encoding="utf-8"))
            if installed["status"] != "passed" or len(installed["checks"]) != 4:
                raise ValueError("installed preview is incomplete")
            if installed["source_commit"] != compiled["commit"]:
                raise ValueError("installed source revision mismatch")
            evidence = directory / "evidence"
            report["evidence_sha256"] = {path.relative_to(evidence).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in evidence.rglob("*") if path.is_file()}
        for item in report["snapshot"]["files"]:
            if hashlib.sha256((source / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("frozen packaging input changed")
        if (source / "source-input.json").read_bytes() != original:
            raise ValueError("compiled source manifest changed")
    except Exception as error:
        code = 1
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        report["resource_cleanup"] = finish_run(run_id, directory, "io.pg_qdrant.ci.preview",
            "io.pg_qdrant.ci.preview-runner", "/tmp/preview-evidence/.")
        if report["resource_cleanup"]["status"] != "passed":
            code = 1
        report["status"] = "passed" if code == 0 else "failed"
        if (directory / "act.log").is_file():
            report["act_log_sha256"] = hashlib.sha256((directory / "act.log").read_bytes()).hexdigest()
        manifest.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
        print("Evidence:", directory)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

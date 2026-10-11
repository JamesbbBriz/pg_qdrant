#!/usr/bin/env python3
"""Run bounded quality measurement locally with act and frozen input hashes."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

from local_ci import ROOT, RUNNER, act_binary, snapshot
from ci_build import act_cache_options
from ci_cache import checked_child, digest as cache_digest, evidence_admission, finish_snapshot, inventory
from quality_vectors import validate_measurement
from ci_images import complete_images, pin_image, release_alias
from ci_resources import MANAGED, require_clean_start, finish_run, local_run_lock, storage_preflight, guarded_run


def output(args):
    return subprocess.check_output(args, text=True, encoding="utf-8").strip()


def retire_measurement_inputs(source, report):
    """Retire verified successful input copies; keep original assets and evidence."""
    expected = {"fixtures/" + name: digest for name, digest in report["fixtures"].items()}
    expected.update({"model-assets/" + name: row["sha256"]
                     for name, row in report["model_inputs"].items()})
    tracked = {item["path"] for item in report["snapshot"]["files"]}
    if tracked & set(expected):
        raise ValueError("measurement input overlaps frozen source")
    actual = {}
    for folder in ("fixtures", "model-assets"):
        base = checked_child(source, folder)
        actual.update({folder + "/" + name: size for name, size in inventory(base).items()})
    if set(actual) != set(expected):
        raise ValueError("unrecorded or missing measurement input; retain for review")
    for name, digest in expected.items():
        if cache_digest(checked_child(source, name)) != digest:
            raise ValueError("measurement input changed; retain for review")
    # Validate the complete set before removing any file. Source and output
    # manifests remain; authoritative fixtures/model assets live outside this view.
    for name in expected:
        checked_child(source, name).unlink()
    return {"retired_files": len(expected), "retired_bytes": sum(actual.values()),
            "sha256": expected}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="matching installed development product image")
    parser.add_argument("--fixtures", type=Path, default=ROOT / "artifacts/quality-fixtures")
    parser.add_argument("--model-inputs", type=Path, help="optional checksum-pinned offline dense model and Linux runtime wheels")
    parser.add_argument("--tokens", action="store_true", help="also measure the pinned English token model on SciFact")
    parser.add_argument("--act")
    args = parser.parse_args()
    if args.tokens and not args.model_inputs:
        parser.error('--tokens requires --model-inputs')
    evidence_admission(ROOT / "artifacts/local-ci")
    run_id = secrets.token_hex(8)
    directory = ROOT / "artifacts/local-ci" / ("quality-" + run_id)
    directory.mkdir(parents=True)
    source = directory / "snapshot"
    source.mkdir(parents=True)
    report = {"run_id": run_id, "kind": "bounded_quality_measurement", "release_supported": False,
              "status": "running"}
    code = 1
    try:
        require_clean_start()
        report['storage_preflight'] = storage_preflight(directory, RUNNER, run_id,
            'io.pg_qdrant.ci.quality', heavy=True)
        report['snapshot'] = snapshot(source)
        code = measure(args, run_id, directory, source, report)
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        print(report['error'], file=sys.stderr)
        code = 1
    finally:
        report['resource_cleanup'] = finish_run(run_id, directory, 'io.pg_qdrant.ci.quality',
            'io.pg_qdrant.ci.quality-runner', '/tmp/quality-evidence/.')
        if report['resource_cleanup']['status'] != 'passed':
            code = 1
        else:
            try:
                if report.get('base_alias'):
                    release_alias(report['base_alias'], report['product_image'], temporary_pin=report['temporary_image_pin'])
                report['image_retention'] = complete_images(run_id, 'passed' if code == 0 else 'failed')
                if report['image_retention']['errors']:
                    code = 1
            except Exception as error:
                code = 1
                report['image_retention_error'] = str(error)
        if code != 0:
            report['status'] = 'failed'
        if report.get("snapshot"):
            if code == 0:
                try:
                    report["measurement_input_retention"] = retire_measurement_inputs(source, report)
                except Exception as error:
                    code = 1
                    report.update(status="failed", measurement_input_retention_error=str(error))
            try:
                report["snapshot_retention"] = finish_snapshot(source, report["snapshot"], code == 0)
            except Exception as error:
                code = 1
                report.update(status="failed", snapshot_retention_error=str(error))
        (directory / 'run-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print('Evidence:', directory, flush=True)
    return code


def measure(args, run_id, directory, source, report):
    image = output(["docker", "image", "inspect", args.image, "--format", "{{.Id}}"])
    image_data = json.loads(output(["docker", "image", "inspect", image]))[0]
    variables = dict(row.split("=", 1) for row in image_data["Config"]["Env"])
    if variables.get("PG_QDRANT_MANAGED_HELPER") != "1":
        raise ValueError("a managed-helper product image is required")
    runner = output(["docker", "image", "inspect", RUNNER, "--format", "{{.Id}}"])
    report["temporary_image_pin"] = pin_image(image)
    report.update(product_image=image, compiled_source_commit=variables["PG_QDRANT_SOURCE_SHA"],
                  runner_image=runner, fixtures={}, model_inputs={})
    expected = json.loads((source / "ci/quality-sources.json").read_text(encoding="utf-8"))
    (source / "fixtures").mkdir()
    fixed_queries = {}
    for dataset in expected["datasets"]:
        name = dataset["dataset"] + ".json"
        data = (args.fixtures / name).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != dataset["fixture_sha256"]:
            raise ValueError("fixture identity mismatch: " + name)
        (source / "fixtures" / name).write_bytes(data)
        report["fixtures"][name] = digest
        fixture = json.loads(data)
        identities = [row['id'] for row in fixture['queries']]
        if len(identities) != 32 or len(set(identities)) != 32:
            raise ValueError("invalid fixed query identities: " + name)
        fixed_queries[dataset['dataset']] = {'sha256': digest, 'query_ids': identities}
    if args.model_inputs:
        from prepare_dense_quality import entries, prepare, validate
        contract = json.loads((source / "ci/dense-quality-model.json").read_bytes())
        prepare(args.model_inputs, contract)
        for name, row in entries(contract):
            target = source / "model-assets" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(args.model_inputs / name, target)
            validate(target, row)  # Validate the copied bytes, including an acquisition race.
            report["model_inputs"][name] = {"sha256": row["sha256"], "bytes": row["bytes"]}
        report["kind"] = "bounded_dense_and_fusion_measurement"
        if args.tokens:
            contract = json.loads((source / "ci/token-quality-model.json").read_bytes())
            prepare(args.model_inputs, contract)
            for name, row in entries(contract):
                target = source / "model-assets" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(args.model_inputs / name, target)
                validate(target, row)
                report["model_inputs"][name] = {"sha256": row["sha256"], "bytes": row["bytes"]}
            report["kind"] = "bounded_dense_fusion_and_token_measurement"
    base = "pgq-quality-base-" + run_id
    subprocess.run(["docker", "tag", image, base], check=True)
    report["base_alias"] = base
    if output(["docker", "image", "inspect", base, "--format", "{{.Id}}"]) != image:
        raise ValueError("base image identity changed")
    empty = directory / "empty-env"
    empty.write_bytes(b"")
    manifest = directory / "run-report.json"
    manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    cache_mount, cache_environment = act_cache_options()
    workflow = "ci/dense-quality.yml" if args.model_inputs else "ci/quality.yml"
    command = [act_binary(args.act), "workflow_dispatch", "-C", str(source), "-W", str(source / workflow),
               "-j", "quality", "-P", "ubuntu-24.04=" + runner, "--pull=false", "--reuse", "--network", "none",
               "--container-daemon-socket", "/var/run/docker.sock", "--env-file", str(empty), "--secret-file", str(empty),
               "--env", "PGQ_RUN_ID=" + run_id, "--container-options",
               '--label ' + MANAGED + '=1 --label io.pg_qdrant.ci.quality-runner=' + run_id + ' --mount "type=bind,source=' + source.as_posix() + ',target=/pgq-input,readonly"' + cache_mount, *cache_environment]
    if args.tokens:
        command += ['--env', 'PGQ_QUALITY_TOKENS=1']
    print(directory, flush=True)
    with (directory / "act.log").open("wb") as log:
        try:
            result = guarded_run(command, directory, log, timeout=1200)
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
    for name, row in report["model_inputs"].items():
        validate(source / "model-assets" / name, row)
    report["evidence_sha256"] = {path.relative_to(directory / "evidence").as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in (directory / "evidence").rglob("*") if path.is_file()}
    if result.returncode == 0:
        measured = json.loads((directory / "evidence/quality-results.json").read_text(encoding="utf-8"))
        plans = ["text", "dense", "rrf", "dbsf"] if args.model_inputs else ["text"]
        if args.tokens:
            from quality_tokens import MATCHED_PLANS, TOKEN_PLANS
            plans = {'scifact': plans + MATCHED_PLANS + TOKEN_PLANS, 't2retrieval': plans}
        validate_measurement(measured, fixed_queries, plans)
    report["status"] = "measured" if result.returncode == 0 else "failed"
    manifest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return result.returncode


if __name__ == "__main__":
    with local_run_lock():
        raise SystemExit(main())

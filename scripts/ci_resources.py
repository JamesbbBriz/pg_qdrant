"""Retire exact run-owned Docker resources after exporting diagnostic evidence."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
import tarfile
import tempfile
import time

MANAGED = "io.pg_qdrant.ci.lifecycle"
LABELS = ("io.pg_qdrant.ci", "io.pg_qdrant.ci.runner",
          "io.pg_qdrant.ci.preview", "io.pg_qdrant.ci.preview-runner",
          "io.pg_qdrant.ci.quality", "io.pg_qdrant.ci.quality-runner")


@contextmanager
def local_run_lock(path=None):
    """One launcher per OS user, across checkouts; the OS releases a killed owner."""
    path = Path(path) if path else Path(tempfile.gettempdir()) / "pg-qdrant-local-ci.lock"
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("another local CI launcher owns the run lock") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def check_free_space(host_free, docker_free, *, heavy):
    """Admission floors, not a prediction or a runtime disk quota."""
    required = {"host": (10 if heavy else 2) * 1024**3,
                "docker": (30 if heavy else 2) * 1024**3}
    measured = {"host": host_free, "docker": docker_free}
    if any(type(value) is not int or value < 0 for value in measured.values()):
        raise ValueError("invalid storage measurement")
    if any(measured[key] < required[key] for key in required):
        raise RuntimeError("insufficient free space for local CI: " + json.dumps(
            {"free_bytes": measured, "required_bytes": required}))
    return {"free_bytes": measured, "required_bytes": required}


def storage_preflight(directory, image, run_id, child_label, *, heavy):
    """Measure the evidence filesystem and a disposable Docker writable layer."""
    if child_label not in LABELS or not re.fullmatch(r"[a-f0-9]{16}", run_id):
        raise ValueError("invalid run ownership")
    free = docker("run", "--rm", "--network=none", "--read-only", "--memory=64m",
        "--pids-limit=16", "--label", MANAGED + "=1", "--label", child_label + "=" + run_id,
        "--entrypoint=python3", image, "-c", "import shutil; print(shutil.disk_usage('/').free)")
    return check_free_space(shutil.disk_usage(directory).free, int(free), heavy=heavy)


def nested_run_labels():
    """Nested fault/observer containers remain discoverable after runner termination."""
    run_id = os.environ.get("PGQ_RUN_ID")
    if run_id is None:
        return []
    if not re.fullmatch(r"[a-f0-9]{16}", run_id):
        raise ValueError("invalid parent run identity")
    return ["--label", MANAGED + "=1", "--label", LABELS[0] + "=" + run_id]


def docker(*args, timeout=30):
    return subprocess.check_output(["docker", *args], text=True, timeout=timeout).strip()


def inspect_owned(identifier, label, run_id):
    if label not in LABELS or not re.fullmatch(r"[a-f0-9]{16}", run_id):
        raise ValueError("invalid run ownership")
    data = json.loads(docker("inspect", identifier))[0]
    if data["Config"].get("Labels", {}).get(label) != run_id:
        raise RuntimeError("container ownership changed")
    return data


def require_clean_start():
    """A failed export must be resolved before another managed run is started."""
    ids = docker("ps", "-aq", "--filter", f"label={MANAGED}=1").split()
    if ids:
        raise RuntimeError("unretired managed CI resources; finish the previous run's "
                           "evidence export and cleanup before starting another run")


def archive_path(identifier, source, destination, max_bytes=512 * 1024 * 1024, timeout=90):
    """Copy a bounded tar stream without extracting untrusted member paths."""
    destination = Path(destination)
    partial = destination.with_suffix(destination.suffix + ".partial")
    with partial.open("wb") as stream, destination.with_suffix(".stderr").open("wb") as errors:
        process = subprocess.Popen(["docker", "cp", identifier + ":" + source, "-"],
                                   stdout=stream, stderr=errors)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline or partial.stat().st_size > max_bytes:
                    raise RuntimeError("diagnostic export exceeded byte or time budget")
                time.sleep(0.05)
            if process.returncode:
                errors.flush()
                error_text = destination.with_suffix(".stderr").read_text(errors="replace")
                if "Could not find the file" in error_text:
                    raise FileNotFoundError("diagnostic path absent: " + source)
                raise RuntimeError("diagnostic export failed: " + source)
            if partial.stat().st_size > max_bytes:
                raise RuntimeError("diagnostic export exceeded byte budget")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    # Read every member body to catch truncated data, not only a valid first header.
    with tarfile.open(partial) as archive:
        for member in archive:
            if member.isfile():
                count = 0
                with archive.extractfile(member) as body:
                    for chunk in iter(lambda: body.read(65536), b""):
                        count += len(chunk)
                if count != member.size:
                    raise RuntimeError("truncated diagnostic archive")
    partial.replace(destination)
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": destination.name, "bytes": destination.stat().st_size, "sha256": digest}


def runner_volumes(data):
    """Only act's exact per-container workspace/env volumes; never toolcache/data."""
    name = data["Name"].lstrip("/")
    return [mount["Name"] for mount in data.get("Mounts", [])
            if mount["Type"] == "volume" and mount["Name"] in (name, name + "-env")]


def retire_owned(label, run_id, directory, *, runner=False, max_bytes=512 * 1024 * 1024):
    """Export failures before removal; errors retain the container and fail cleanup."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {"run_id": run_id, "label": label, "status": "running", "containers": []}
    receipt_path = directory / (label.rsplit(".", 1)[-1] + "-cleanup.json")

    previous = json.loads(receipt_path.read_text()) if receipt_path.is_file() else None

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    def remove_volumes(row):
        for volume in list(row.get("volumes_pending", [])):
            if volume["name"] not in (row["name"].lstrip("/"), row["name"].lstrip("/") + "-env"):
                raise RuntimeError("unexpected runner volume identity")
            data = json.loads(docker("volume", "inspect", volume["name"]))[0]
            if data["CreatedAt"] != volume["created_at"]:
                raise RuntimeError("runner volume was replaced")
            docker("volume", "rm", volume["name"])
            row["volumes_removed"].append(volume["name"])
            row["volumes_pending"].remove(volume)
            save()

    try:
        if label not in LABELS or not re.fullmatch(r"[a-f0-9]{16}", run_id):
            raise ValueError("invalid run ownership")
        if previous and (previous["run_id"] != run_id or previous["label"] != label):
            raise RuntimeError("cleanup receipt ownership changed")
        if previous:
            for row in previous["containers"]:
                if row.get("removed"):
                    receipt["containers"].append(row)
                    if runner and row.get("volumes_pending"):
                        row.pop("error", None)
                        try:
                            remove_volumes(row)
                        except Exception as error:
                            row["error"] = f"{type(error).__name__}: {error}"
        save()
        identifiers = docker("ps", "-aq", "--filter", f"label={label}={run_id}").split()
        for identifier in identifiers:
            row = {"id": identifier, "removed": False}
            receipt["containers"].append(row)
            try:
                data = inspect_owned(identifier, label, run_id)
                identifier = data["Id"]
                row.update(id=identifier, name=data["Name"], image=data["Image"], state=data["State"])
                if data["State"].get("Running"):
                    docker("stop", "--time", "5", identifier)
                    data = inspect_owned(identifier, label, run_id)
                    row["stopped_for_cleanup"] = True
                target = directory / identifier
                target.mkdir(exist_ok=True)
                # Config.Env and command lines can contain secrets; retain only ownership/state.
                (target / "state.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
                with (target / "container.log").open("wb") as log:
                    subprocess.run(["docker", "logs", "--tail", "10000", identifier], stdout=log, stderr=subprocess.STDOUT,
                                   check=True, timeout=30)
                if not runner:
                    source = "/src/artifacts/."
                    if label == "io.pg_qdrant.ci.preview":
                        source = "/package-output/." if "preview-archive-" in data["Name"] else "/preview-evidence/."
                    try:
                        row["artifacts"] = archive_path(identifier, source, target / "artifacts.tar", max_bytes)
                    except FileNotFoundError:
                        row["artifacts_absent"] = source  # Early startup may not create its output directory.
                # Failed test clusters live under /tmp. Preserve them independently of step collectors.
                if not runner and (data["State"].get("ExitCode") != 0 or row.get("stopped_for_cleanup")):
                    row["failure_tmp"] = archive_path(identifier, "/tmp/.", target / "failure-tmp.tar", max_bytes)
                with (target / "filesystem-changes.log").open("wb") as changes:
                    subprocess.run(["docker", "diff", identifier], stdout=changes, check=True, timeout=30)
                row["files"] = {}
                for path in target.iterdir():
                    if path.is_file() and path.suffix != ".partial":
                        with path.open("rb") as stream:
                            row["files"][path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
                inspect_owned(identifier, label, run_id)  # Recheck exact ID immediately before mutation.
                row["volumes_removed"] = []
                row["volumes_pending"] = []
                if runner:
                    for volume in runner_volumes(data):
                        state = json.loads(docker("volume", "inspect", volume))[0]
                        row["volumes_pending"].append({"name": volume, "created_at": state["CreatedAt"]})
                save()  # Persist evidence and ownership receipt before removing the original.
                docker("rm", identifier)
                row["removed"] = True
                save()
                if runner:
                    remove_volumes(row)  # Docker refuses a still-referenced volume.
            except Exception as error:
                row["error"] = f"{type(error).__name__}: {error}"
            save()
        receipt["status"] = "failed" if any("error" in row for row in receipt["containers"]) else "passed"
    except Exception as error:
        receipt.update(status="failed", error=f"{type(error).__name__}: {error}")
    save()
    return receipt


def finish_run(run_id, directory, child_label, runner_label, evidence_path):
    """Always export the runner before touching child resources, including on timeout."""
    directory = Path(directory) / "resources"
    directory.mkdir(parents=True, exist_ok=True)
    result = {"status": "failed", "run_id": run_id}
    try:
        ids = docker("ps", "-aq", "--filter", f"label={runner_label}={run_id}").split()
        if len(ids) > 1:
            raise RuntimeError("runner identity is ambiguous")
        if ids:
            data = inspect_owned(ids[0], runner_label, run_id)
            if data["State"].get("Running"):
                docker("stop", "--time", "5", data["Id"])
            try:
                result["runner_evidence"] = archive_path(data["Id"], evidence_path, directory / "runner-evidence.tar")
            except Exception:
                # Early interruption may precede the always-collector. Preserve the workspace instead.
                result["runner_workspace"] = archive_path(data["Id"], "/tmp/.", directory / "runner-workspace.tar")
        result["tests"] = retire_owned(child_label, run_id, directory)
        if result["tests"]["status"] != "passed":
            raise RuntimeError("test diagnostics or retirement incomplete; runner retained")
        result["runner"] = retire_owned(runner_label, run_id, directory, runner=True)
        if result["runner"]["status"] != "passed":
            raise RuntimeError("runner retirement incomplete")
        result["status"] = "passed"
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    (directory / "cleanup-report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Retry exact-run diagnostic export and cleanup")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--kind", choices=("verify", "preview", "quality"), default="verify")
    args = parser.parse_args()
    kind_labels = {"verify": (LABELS[0], LABELS[1], "/tmp/pgq-ci-evidence/."),
                   "preview": (LABELS[2], LABELS[3], "/tmp/preview-evidence/."),
                   "quality": (LABELS[4], LABELS[5], "/tmp/quality-evidence/.")}
    child, runner, evidence = kind_labels[args.kind]
    with local_run_lock():
        result = finish_run(args.run_id, args.directory, child, runner, evidence)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)

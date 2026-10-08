#!/usr/bin/env python3
"""Run the explicitly provisioned tmpfs experiment and require a real pass."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import subprocess


OUTPUT_TAIL_LIMIT = 8000
STAGE_PREFIX = "pg_qdrant_p0_stage="
OBSERVATION_PREFIX = "pg_qdrant_p0_observation="
VECTOR_KEYWORD_MAX_BYTES = 64 * 1024 * 1024
FULL_TEXT_BYTES = 384 * 1024 * 1024
REFUSED_FULL_TEXT_BYTES = 128 * 1024 * 1024
FULL_TEXT_CAPACITY_REFUSAL = "full-text tmpfs capacity must be exactly 384 MiB"
CAPACITY_REFUSAL_KIND = "edge_full_text_128_capacity_refusal_probe"


def decode_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value or ""


def output_tail(value: str | bytes | None) -> str:
    return decode_output(value)[-OUTPUT_TAIL_LIMIT:]


def run_probe(command: list[str], timeout: float = 90, required_profile: str | None = None,
              required_kind: str = "edge_enospc_probe") -> dict:
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
        stdout, stderr = decode_output(error.stdout), decode_output(error.stderr)
    except OSError as error:
        launch_error = {"errno": error.errno, "message": str(error)}
        stdout, stderr = "", ""

    stages = [line[len(STAGE_PREFIX):] for line in stderr.splitlines()
              if line.startswith(STAGE_PREFIX)]
    stages_truncated = len(stages) > 64
    stages = stages[-64:]
    observations = []
    observation_errors = []
    for line in (stderr or "").splitlines():
        if not line.startswith(OBSERVATION_PREFIX):
            continue
        if len(observations) >= 32 or len(line) > OUTPUT_TAIL_LIMIT:
            observation_errors.append("native observation exceeded the bounded report budget")
            break
        try:
            observation = json.loads(line[len(OBSERVATION_PREFIX):])
            if not isinstance(observation, dict) or not isinstance(observation.get("stage"), str):
                raise ValueError("observation must name its stage")
            observations.append(observation)
        except (ValueError, json.JSONDecodeError) as error:
            observation_errors.append(str(error))
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
        "stderr_tail_truncated": len(stderr) > OUTPUT_TAIL_LIMIT,
        "stages": stages,
        "stages_truncated": stages_truncated,
        "last_stage": stages[-1] if stages else None,
        "observations": observations,
        "observation_errors": observation_errors,
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
              and report.get("kind") == required_kind
              and (required_profile is None or report.get("profile") == required_profile)
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


def observation_capacity_allowed(profile: str, capacity: int) -> bool:
    if profile == "vector_keyword":
        return 0 < capacity <= VECTOR_KEYWORD_MAX_BYTES
    if profile == "full_text":
        return 0 < capacity <= FULL_TEXT_BYTES
    if profile == "full_text_128_capacity_refusal":
        return capacity == REFUSED_FULL_TEXT_BYTES
    return False


def observe_provisioned_tmpfs(profile: str = "vector_keyword") -> dict:
    """Read bounded metadata after exit; never mount, fill, repair or remove it."""
    root = Path("/pgq-p0-faults")
    if os.environ.get("PG_QDRANT_ENOSPC_DIR") != str(root):
        return {"status": "not_requested"}
    try:
        metadata = root.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()):
            raise ValueError("expected the exact dedicated owned 0700 directory")
        command = subprocess.run(
            ["/usr/bin/stat", "--file-system", "--format=%T %S %b %f %a", "--", str(root)],
            check=True, text=True, capture_output=True, timeout=2)
        fields = command.stdout.split()
        if len(fields) != 5 or fields[0] != "tmpfs":
            raise ValueError("post-exit observation requires tmpfs")
        unit, blocks, free, available = map(int, fields[1:])
        if not observation_capacity_allowed(profile, unit * blocks):
            raise ValueError("post-exit observation exceeds the fixed capacity budget")
        files = logical = allocated = directories = 0
        stack = [(root, 0)]
        largest = []
        while stack:
            path, depth = stack.pop()
            if depth > 16:
                raise ValueError("post-exit metadata depth budget exceeded")
            with os.scandir(path) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if info.st_dev != metadata.st_dev:
                        raise ValueError("post-exit metadata crossed a filesystem")
                    if stat.S_ISDIR(info.st_mode):
                        directories += 1
                        if directories > 512:
                            raise ValueError("post-exit directory budget exceeded")
                        stack.append((Path(entry.path), depth + 1))
                    elif stat.S_ISREG(info.st_mode):
                        files += 1
                        logical += info.st_size
                        allocation = info.st_blocks * 512
                        allocated += allocation
                        if files > 512 or logical > 512 * 1024 * 1024:
                            raise ValueError("post-exit file budget exceeded")
                        largest.append({"relative_file": str(Path(entry.path).relative_to(root)),
                                        "logical_bytes": info.st_size, "allocated_bytes": allocation})
                    else:
                        raise ValueError("post-exit metadata rejects symlinks and special files")
        largest.sort(key=lambda value: value["allocated_bytes"], reverse=True)
        return {"status": "observed", "stage": "after_child_exit",
                "filesystem": {"type": "tmpfs", "capacity_bytes": unit * blocks,
                               "free_bytes": unit * free, "available_bytes": unit * available},
                "mount_identity": {"device": metadata.st_dev, "inode": metadata.st_ino},
                "storage": {"files": files, "directories": directories,
                            "logical_bytes": logical, "allocated_bytes": allocated,
                            "largest_allocated_files": largest[:8]},
                "scope": "post-exit metadata only; no native fault attribution or content inspection"}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"status": "observation_failed", "reason": str(error)}


def validate_capacity_refusal(report: dict, before: dict, after: dict) -> dict:
    """Accept only the separately named no-write guard characterization."""
    entries = report.get("entry_refusals", {})
    entries = entries if isinstance(entries, dict) else {}
    fault = entries.get("enospc", {})
    fault = fault if isinstance(fault, dict) else {}
    expected_native = (
        report.get("status") == "passed"
        and report.get("kind") == CAPACITY_REFUSAL_KIND
        and report.get("profile") == "full_text"
        and report.get("observed_capacity_bytes") == REFUSED_FULL_TEXT_BYTES
        and report.get("required_capacity_bytes") == FULL_TEXT_BYTES
        and report.get("expected_refusal") == FULL_TEXT_CAPACITY_REFUSAL
        and all(report.get(key) is False for key in ["disk_writes_attempted", "engine_open_attempted",
                                                    "filler_created", "enospc_verified", "recovery_verified"])
        and entries.get("enospc_exit_code") == 2
        and fault.get("status") == "not_run"
        and fault.get("kind") == "edge_enospc_probe"
        and fault.get("profile") == "full_text"
        and fault.get("reason_code") == "unsafe_environment"
        and fault.get("reason") == FULL_TEXT_CAPACITY_REFUSAL
        and fault.get("disk_writes_attempted") is False
        and entries.get("clean_error") == FULL_TEXT_CAPACITY_REFUSAL
        and report.get("execution", {}).get("returncode") == 0
        and report.get("execution", {}).get("stages") == [
            "capacity_refusal_guard", "guard", "capacity_refusal_completed"]
    )

    def empty_observation(value: dict) -> bool:
        storage = value.get("storage", {})
        filesystem = value.get("filesystem", {})
        return (value.get("status") == "observed"
                and filesystem.get("type") == "tmpfs"
                and filesystem.get("capacity_bytes") == REFUSED_FULL_TEXT_BYTES
                and all(storage.get(key) == 0 for key in ["files", "directories", "logical_bytes", "allocated_bytes"])
                and isinstance(value.get("mount_identity", {}).get("device"), int)
                and isinstance(value.get("mount_identity", {}).get("inode"), int))

    unchanged_empty_mount = (empty_observation(before) and empty_observation(after)
                            and before["filesystem"] == after["filesystem"]
                            and before["mount_identity"] == after["mount_identity"])
    if not expected_native or not unchanged_empty_mount:
        report["status"] = "failed"
        report["reason"] = "Exact native capacity refusals and unchanged empty 128 MiB mount observations are required"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["vector-keyword", "full-text"], default="vector-keyword")
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--clean", action="store_true", help="full-text clean reopen control; no filler")
    controls.add_argument("--characterize-128-capacity-refusal", action="store_true",
                          help="full-text guard characterization only; no engine or disk writes")
    args = parser.parse_args()
    full_text = args.profile == "full-text"
    refusal = args.characterize_128_capacity_refusal
    if (args.clean or refusal) and not full_text:
        parser.error("full-text controls require --profile full-text")
    artifacts = Path(os.environ.get("PG_QDRANT_ARTIFACT_DIR", "artifacts"))
    artifacts.mkdir(parents=True, exist_ok=True)
    flag = ("--full-text-128-capacity-refusal-probe" if refusal else
            "--full-text-tmpfs-fixture-probe" if args.clean else
            "--full-text-disk-full-probe" if full_text else "--disk-full-probe")
    profile = "full_text" if full_text else "vector_keyword"
    observation_profile = "full_text_128_capacity_refusal" if refusal else profile
    kind = CAPACITY_REFUSAL_KIND if refusal else "edge_disk_fixture_probe" if args.clean else "edge_enospc_probe"
    before = observe_provisioned_tmpfs(observation_profile) if refusal else None
    report = run_probe(["target/debug/pg-qdrant-edge-probe", flag],
                       required_profile=profile, required_kind=kind)
    report["requested_experiment"] = {"kind": kind, "profile": profile, "clean_control": args.clean,
                                      "capacity_refusal_characterization": refusal}
    after = observe_provisioned_tmpfs(observation_profile)
    report["post_exit_observation"] = after
    if refusal:
        report["pre_execution_observation"] = before
        validate_capacity_refusal(report, before, after)
    if args.clean and report["status"] == "passed" and not (
            report.get("filler_created") is False and report.get("provisioned_tmpfs") is True):
        report["status"] = "failed"
        report["reason"] = "A clean control must prove no filler and the provisioned tmpfs"
    filename = ("edge-full-text-128-capacity-refusal.json" if refusal else
                "edge-full-text-clean.json" if args.clean else
                "edge-full-text-enospc.json" if full_text else "edge-enospc.json")
    (artifacts / filename).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    if report["status"] != "passed":
        raise SystemExit("The provisioned disk experiment did not pass.")


if __name__ == "__main__":
    main()

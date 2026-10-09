#!/usr/bin/env python3
"""Run one explicitly selected, bounded OOM comparison in a fresh CI container.

Run positive OOM only in its guarded disposable container, including local act
CI. Image builds are separate; this command requires an already built local
image and never requests extra privileges or host mounts.
"""
from __future__ import annotations

if not __debug__:
    raise RuntimeError("optimized Python is unsupported for guarded P0 experiments")

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import subprocess
import time

MEMORY = 768 * 1024 * 1024
LIMIT = 2 * 1024 * 1024


def command(args, *, timeout=10, data=None, check=True):
    process = subprocess.Popen(args, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    pending = memoryview(data.encode() if data is not None else b"")
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin is not None:
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                for key, _ in selector.select(min(remaining, 0.2)):
                    if key.data == "stdin":
                        try:
                            pending = pending[os.write(key.fd, pending[:4096]):]
                        except BrokenPipeError:
                            pending = memoryview(b"")
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        else:
                            output[key.data].extend(chunk)
                            if len(output[key.data]) > LIMIT:
                                raise RuntimeError("outer observation exceeded output budget")
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=1)
        for stream in [process.stdin, process.stdout, process.stderr]:
            if stream is not None:
                stream.close()
    completed = subprocess.CompletedProcess(args, returncode,
        output["stdout"].decode(errors="replace"), output["stderr"].decode(errors="replace"))
    if check and completed.returncode:
        raise RuntimeError(f"{args[0]} failed ({completed.returncode}): {completed.stderr[-8192:]}")
    return completed


def inspect_container(value, image_id, nonce):
    config, host = value["Config"], value["HostConfig"]
    assert value["Image"] == image_id
    assert config["User"] == "10001:10001"
    assert config.get("Labels", {}).get("io.pg_qdrant.p0.oom") == nonce
    assert (config.get("Healthcheck") or {}).get("Test", ["NONE"])[0] == "NONE"
    assert not config.get("Volumes") and not value.get("Mounts")
    assert host["Memory"] == MEMORY and host["MemorySwap"] == MEMORY
    assert host["PidsLimit"] == 128 and host["NanoCpus"] == 2_000_000_000
    assert host["NetworkMode"] == "none" and host["IpcMode"] == "private"
    assert host["CgroupnsMode"] == "private" and host["PidMode"] in ("", "private")
    assert not host["Privileged"] and not host.get("CapAdd")
    assert host["CapDrop"] == ["ALL"]
    assert any(option in ("no-new-privileges", "no-new-privileges=true") for option in host["SecurityOpt"])
    assert "no-new-privileges=false" not in host["SecurityOpt"]
    assert not host.get("Binds") and not host.get("Devices") and not host.get("DeviceRequests")
    assert not host.get("OomKillDisable") and not host.get("AutoRemove")
    assert any(item["Name"] == "core" and item["Soft"] == 0 and item["Hard"] == 0
               for item in host["Ulimits"])
    return {"image_id": image_id, "container_id": value["Id"], "user": config["User"],
            "memory_limit_bytes": host["Memory"], "memory_swap_total_bytes": host["MemorySwap"],
            "pids_limit": host["PidsLimit"], "nano_cpus": host["NanoCpus"],
            "network": host["NetworkMode"], "pid_mode": host["PidMode"],
            "ipc_mode": host["IpcMode"], "cgroup_namespace": host["CgroupnsMode"],
            "cap_drop": host["CapDrop"], "no_new_privileges": True, "host_mounts": False}


def host_mapping(container_id, target):
    rows = command(["docker", "top", container_id, "-eo", "pid"]).stdout.splitlines()[1:]
    assert len(rows) <= 128
    found = []
    for row in rows:
        pid = int(row.strip())
        proc = Path(f"/proc/{pid}")
        try:
            status = (proc / "status").read_text()
            nspid = next(line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:"))
            if len(nspid) < 2 or int(nspid[-1]) != target["pid"]:
                continue
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            cgroup = (proc / "cgroup").read_text().strip()
            assert int(fields[19]) == target["start_ticks"] and fields[0] != "Z"
            assert cgroup.startswith("0::/") and container_id in cgroup
            found.append({"host_pid": pid, "container_pid": target["pid"],
                          "start_ticks": int(fields[19]), "host_cgroup": cgroup[3:]})
        except (FileNotFoundError, ProcessLookupError):
            continue
    assert len(found) == 1, "unique native host PID/start/cgroup mapping unavailable"
    return found[0]


def kernel_victim_records(lines, mapping, container_id):
    # Require both a memcg constraint/victim selector and an actual killed-victim
    # line. A generic SIGKILL or global OOM line is not direct memcg attribution.
    pid = mapping["host_pid"]
    expected_cgroup = mapping["host_cgroup"]
    assert expected_cgroup.startswith("/") and container_id in expected_cgroup
    candidates = []
    selectors, victims = [], []
    for line in lines.splitlines():
        item = json.loads(line)
        message = item.get("MESSAGE", "")
        if not isinstance(message, str):
            continue
        # Linux dump_oom_victim/mem_cgroup_print_oom_context separate oom_memcg from the
        # victim's task_memcg. A parent-limit OOM must not pass merely because
        # task_memcg contains this container's ID.
        pairs = []
        if message.startswith("oom-kill:"):
            pairs = [part.split("=", 1) for part in message[len("oom-kill:"):].split(",") if "=" in part]
        fields = dict(pairs)
        selector = (len(fields) == len(pairs)
                    and fields.get("constraint") == "CONSTRAINT_MEMCG"
                    and fields.get("oom_memcg") == expected_cgroup
                    and fields.get("pid") == str(pid))
        victim = re.search(rf"Memory cgroup out of memory: Killed process {pid}\b", message)
        if selector or victim:
            narrowed = {"timestamp_us": item.get("__REALTIME_TIMESTAMP"), "message": message[:16384]}
            candidates.append(narrowed)
            (selectors if selector else victims).append(narrowed)
    return candidates, len(selectors) == 1 and len(victims) == 1


def classify(sql, *, kernel_victim_verified, manual_termination):
    delta = sql.get("counter_delta", {})
    kernel_event = all(delta.get(key, 0) > 0 for key in ("max", "oom", "oom_kill"))
    one_kill = delta.get("oom_kill") == 1 and delta.get("oom_group_kill", 0) == 0
    exit_record = sql.get("target_exit", {})
    uninterrupted = (not manual_termination and exit_record.get("supervisor_kill_requested") is False
                     and exit_record.get("kill_attempts") == 0
                     and not sql.get("caller_client_kill_requested", True)
                     and not sql.get("companion_client_kill_requested", True))
    profile = sql.get("profile")
    helper = profile == "managed_helper"
    postgres_outcome = (profile in ("managed_helper", "direct_worker")
        and sql.get("companion_survived") is helper and sql.get("supervisor_preserved") is helper
        and sql.get("postmaster_preserved") is True and sql.get("committed_marker_retained") is True
        and sql.get("companion_overlapped_allocation") is True)
    target = sql.get("identities", {}).get("target", {})
    expected_target = (isinstance(target.get("pid"), int) and target["pid"] > 1
        and exit_record.get("engine_pid") == target["pid"])
    exact_signal = exit_record.get("signal") == 9 or (
        profile == 'direct_worker' and kernel_victim_verified
        and exit_record.get('signal') is None
        and exit_record.get('source') == 'requires_exact_kernel_victim_record'
        and sql.get('postgresql_crash_log', {}).get('caller_logged') is True
        and sql.get('postgresql_crash_log', {}).get('reinitializing') is True)
    observations = (sql.get("status") == "observed" and kernel_event and one_kill and postgres_outcome and expected_target
                    and exact_signal and uninterrupted
                    and sql.get("replacement_edge_smoke_passed") is True)
    attribution = "kernel_record" if observations and kernel_victim_verified else (
        "correlated_only" if observations else "none")
    strict = observations and kernel_victim_verified
    return {"status": "passed" if strict else "inconclusive" if observations else "failed",
            "kernel_oom_observed": kernel_event, "victim_attribution": attribution,
            "strict_target_attribution_passed": strict,
            "target_isolation_gate_passed": strict and helper, "production_memory_isolation_verified": False,
            "unflushed_edge_recovery_verified": False, "postgres_edge_atomicity_verified": False}


def execute(args):
    nonce = secrets.token_hex(16)
    name = "pgq-p0-oom-" + args.profile.replace("_", "-") + "-" + nonce
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 1, "kind": "p0_kernel_oom", "profile": args.profile,
              "nonce": nonce, "status": "not_run", "stage": "host_preflight",
              "external_termination_requested": False, "kernel_oom_observed": False,
              "victim_attribution": "none", "target_isolation_gate_passed": False,
              "strict_target_attribution_passed": False,
              "production_memory_isolation_verified": False}
    container_id = None
    mapping = None
    ready = None
    creation_requested = False
    started_wall = time.time()
    try:
        values = {line.split(':', 1)[0]: line.split(':', 1)[1].strip()
                  for line in Path('/proc/meminfo').read_text().splitlines()}
        available = int(values["MemAvailable"].split()[0]) * 1024
        report["host_mem_available_before_bytes"] = available
        assert available >= 3 * 1024**3, "insufficient available host memory for this isolated experiment"
        image = json.loads(command(["docker", "image", "inspect", args.image]).stdout)[0]
        image_id = image["Id"]
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)
        create = ["docker", "create", "--name", name, "--memory=768m", "--memory-swap=768m",
                  "--cpus=2", "--pids-limit=128", "--cgroupns=private", "--ipc=private", "--network=none",
                  "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=10001:10001", "--ulimit=core=0",
                  "--label", "io.pg_qdrant.p0.oom=" + nonce,
                  "--env", "PG_QDRANT_P0_OOM_RUN_ID=" + nonce,
                  "--env", "PG_QDRANT_MANAGED_HELPER=" + ("1" if args.profile == "managed_helper" else "0"),
                  image_id, "bash", "crates/pg_qdrant/tests/run-oom.sh"]
        creation_requested = True
        container_id = command(create).stdout.strip()
        assert re.fullmatch(r"[a-f0-9]{64}", container_id)
        inspected = json.loads(command(["docker", "inspect", container_id]).stdout)[0]
        report["container"] = inspect_container(inspected, image_id, nonce)
        report["stage"] = "container_native_guard"
        command(["docker", "start", container_id])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = json.loads(command(["docker", "inspect", container_id]).stdout)[0]
            if not current["State"]["Running"]:
                break
            if ready is None:
                result = command(["docker", "exec", container_id, "cat", "/src/artifacts/oom-ready.json"], check=False)
                if result.returncode == 0:
                    ready = json.loads(result.stdout)
                    assert ready["nonce"] == nonce and ready["profile"] == args.profile
                    mapping = host_mapping(container_id, ready["target"])
                    report["target_mapping"] = mapping
                    report["stage"] = "allocation_and_recovery"
                    go = {"nonce": nonce, "target": ready["target"], "container_inspection_verified": True,
                          "host_pid": mapping["host_pid"]}
                    writer = "import os,sys; p=sys.argv[1]; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600); f=os.fdopen(fd,'w'); f.write(sys.stdin.read(16384)); f.flush(); os.fsync(f.fileno()); f.close()"
                    command(["docker", "exec", "--user", "10001:10001", "-i", container_id,
                             "python3", "-c", writer, f"/tmp/pgq-p0-oom-{nonce}/go.json"], data=json.dumps(go)+"\n")
            time.sleep(0.2)
        else:
            report["external_termination_requested"] = True
            report["reason_code"] = "harness_timeout"
            command(["docker", "kill", container_id], check=False)
            raise TimeoutError("fresh OOM container exceeded its 90-second watchdog")
        report["container_exit"] = current["State"]
        report["stage"] = "collect_and_attribute"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        verified = False
        try:
            if creation_requested and container_id is None:
                # A timed-out Docker create can leave a stopped container even
                # if its ID was not returned. Recover only the fresh nonce/name.
                recovered = command(["docker", "inspect", name], check=False)
                if recovered.returncode == 0:
                    value = json.loads(recovered.stdout)[0]
                    assert value["Name"] == "/" + name
                    assert value["Config"].get("Labels", {}).get("io.pg_qdrant.p0.oom") == nonce
                    assert re.fullmatch(r"[a-f0-9]{64}", value["Id"])
                    container_id = value["Id"]
            if container_id:
                current = json.loads(command(["docker", "inspect", container_id]).stdout)[0]
                if current["State"]["Running"]:
                    report["external_termination_requested"] = True
                    command(["docker", "kill", container_id], check=False)
                # Only this invocation's fresh, inspected container is collected/removed.
                copied = command(["docker", "cp", container_id + ":/src/artifacts/.", str(artifacts)], check=False)
                report["artifact_copy_returncode"] = copied.returncode
                logs = command(["docker", "logs", "--tail", "1000", container_id], check=False)
                (artifacts / "container.log").write_text(logs.stdout + logs.stderr)
                report["container_exit"] = json.loads(command(["docker", "inspect", container_id]).stdout)[0]["State"]
                command(["docker", "rm", container_id], check=False)
            if mapping:
                try:
                    journal = command(["journalctl", "-k", "--no-pager", "-n", "2000", "-o", "json",
                                       "--since", "@" + str(int(started_wall))], check=False)
                    report["kernel_journal_returncode"] = journal.returncode
                    if journal.returncode == 0:
                        records, verified = kernel_victim_records(journal.stdout, mapping, container_id)
                        report["kernel_victim_records"] = records
                    else:
                        report["kernel_journal_unavailable"] = journal.stderr[-4096:]
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                    report["kernel_journal_unavailable"] = f"{type(error).__name__}: {error}"
            sql_path = artifacts / "oom-sql.json"
            if sql_path.exists():
                assert sql_path.stat().st_size <= LIMIT
                sql = json.loads(sql_path.read_text())
                assert sql["nonce"] == nonce and sql["profile"] == args.profile
                assert ready is not None and sql["identities"]["target"] == ready["target"]
                # Earlier outer failures cannot be overwritten by a coincident event.
                summary = classify(sql, kernel_victim_verified=verified,
                    manual_termination=(report["external_termination_requested"] or "error" in report
                        or report.get("container_exit", {}).get("ExitCode") != 0))
                report.update(summary)
            else:
                report.update(status="failed", missing_sql_observation=True)
        except Exception as error:
            report.update(status="failed", target_isolation_gate_passed=False,
                strict_target_attribution_passed=False, collection_error=f"{type(error).__name__}: {error}")
            if container_id:
                # A collection failure never leaves an intended positive verdict.
                # Attempt bounded cleanup of only the newly created owned container.
                report["external_termination_requested"] = True
                try:
                    command(["docker", "kill", container_id], check=False)
                    command(["docker", "rm", container_id], check=False)
                except Exception as cleanup_error:
                    report["cleanup_error"] = str(cleanup_error)
        finally:
            report["elapsed_seconds"] = time.time() - started_wall
            (artifacts / "oom-result.json").write_text(json.dumps(report, indent=2)+"\n")
            print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Already built local private-fault image")
    parser.add_argument("--profile", choices=["direct_worker", "managed_helper"], required=True)
    parser.add_argument("--artifacts", type=Path, required=True, help="New, non-existing evidence directory")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]*", args.image):
        parser.error("invalid local image reference")
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())

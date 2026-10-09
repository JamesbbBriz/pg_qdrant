#!/usr/bin/env python3
"""Real helper pipe/ownership/EOF probes, independent of PostgreSQL startup."""

import argparse
import json
import os
from pathlib import Path
import resource
import select
import signal
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument("binary", type=Path)
parser.add_argument("--faults", action="store_true")
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
binary = args.binary.resolve()
report = {"schema_version": 1, "kind": "standalone_helper_protocol", "status": "running", "checks": []}
processes = []
orphan_helpers = []


def frame(process, timeout=5, verify_pid=True):
    deadline = time.monotonic() + timeout
    received = bytearray()
    while time.monotonic() < deadline:
        if not select.select([process.stdout], [], [], max(0, deadline-time.monotonic()))[0]:
            break
        data = os.read(process.stdout.fileno(), 8192)
        assert data, "helper closed stdout before its response"
        received.extend(data)
        assert len(received) <= 1024 * 1024
        if b"\n" in received:
            assert received.count(b"\n") == 1 and received.endswith(b"\n")
            result = json.loads(received)
            assert result["protocol_version"] == 1
            assert not verify_pid or result["engine_pid"] == process.pid
            return result
    raise AssertionError("helper response deadline exceeded")


def start(owner, ready=True, inherited_address_space=None):
    def prepare():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if inherited_address_space is not None:
            resource.setrlimit(resource.RLIMIT_AS, (inherited_address_space, inherited_address_space))
    process = subprocess.Popen([str(binary), str(owner)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               cwd=owner.parent, preexec_fn=prepare)
    processes.append(process)
    if ready:
        result = frame(process)
        assert result["request_id"] == 0 and result["result"]["ready"] is True
        assert result["result"]["fault_injection"] is args.faults
        limits = result["result"]["resource_limits"]
        assert limits['address_space_limit_enforced'] and not limits['rss_limit_enforced']
        assert limits['address_space_soft_bytes'] == limits['address_space_hard_bytes']
        assert limits['glibc_arena_max'] == 2
        assert 512*1024**2 <= limits['address_space_hard_bytes'] <= 8*1024**3
        assert resource.prlimit(process.pid, resource.RLIMIT_AS) == (limits['address_space_soft_bytes'], limits['address_space_hard_bytes'])
    return process


def send(process, request_id, operation, **parameters):
    request = {"protocol_version": 1, "request_id": request_id,
               "operation": {"operation": operation, **parameters}}
    process.stdin.write(json.dumps(request).encode() + b"\n")
    process.stdin.flush()


def record(name, **details):
    report["checks"].append({"name": name, "status": "passed", **details})


def process_identity(pid):
    proc = Path(f"/proc/{pid}")
    uid = proc.stat().st_uid
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "start_ticks": int(fields[19]),
            "state": fields[0], "parent_pid": int(fields[1]), "uid": uid}


def stopped(expected):
    try:
        current = process_identity(expected["pid"])
        return current["start_ticks"] != expected["start_ticks"] or current["state"] == "Z"
    except (FileNotFoundError, ProcessLookupError):
        return True


try:
    with tempfile.TemporaryDirectory(prefix="pgq-helper-test-") as directory:
        owner = Path(directory) / "engine.owner"
        process = start(owner)
        second = start(owner, ready=False)
        assert second.wait(timeout=3) != 0
        record("unique_native_owner_and_ready_identity")

        send(process, 1, "delay", delay_ms=5)
        reply = frame(process)
        assert reply["request_id"] == 1 and reply["result"]["completed_delay_ms"] == 5
        send(process, 2, "engine")
        reply = frame(process, timeout=120)
        assert reply["request_id"] == 2 and reply["result"]["status"] == "passed"
        record("real_edge_through_helper_pipe", checks=len(reply["result"]["checks"]))

        send(process, 3, 'address_space_probe')
        mapping = frame(process)
        if args.faults:
            assert mapping['result']['mapping_refused'] and mapping['result']['errno'] == 12
            assert mapping['result']['requested_mapping_bytes'] > mapping['result']['address_space_hard_bytes']
            assert mapping['result']['physical_pages_touched'] == 0 and not mapping['result']['kernel_oom']
            record('actual_kernel_address_space_refusal_preserves_helper', result=mapping['result'])
        else:
            assert mapping['error']['code'] == 'invalid_parameter'
            record('address_space_probe_refused_without_private_feature')

        if args.faults:
            send(process, 4, "panic")
            assert frame(process)["error"]["code"] == "engine_panic"
            assert process.poll() is None
            record("caught_native_panic_preserves_helper")
        else:
            send(process, 4, "abort")
            assert frame(process)["error"]["code"] == "invalid_parameter"
            send(process, 5, "oom")
            assert frame(process)["error"]["code"] == "invalid_parameter"
            assert process.poll() is None
            record("fault_operation_refused_without_private_feature")

        send(process, 6, "delay", delay_ms=10000)
        time.sleep(0.1)
        begin = time.monotonic()
        process.stdin.close()
        assert process.wait(timeout=3) == 0
        record("parent_pipe_eof_stops_outstanding_work", exit_seconds=time.monotonic()-begin)

        inherited = start(owner, inherited_address_space=1024**3)
        assert resource.prlimit(inherited.pid, resource.RLIMIT_AS) == (1024**3,1024**3)
        send(inherited, 1, 'delay', delay_ms=1)
        assert frame(inherited)['result']['completed_delay_ms'] == 1
        inherited.stdin.close()
        assert inherited.wait(timeout=3) == 0
        rejected = start(owner, ready=False, inherited_address_space=256*1024**2)
        assert rejected.wait(timeout=3) == 70
        assert not rejected.stdout.read()
        record('tighter_inherited_address_space_preserved_and_unsupported_budget_refused')

        replacement = start(owner)
        send(replacement, 1, "delay", delay_ms=1)
        assert frame(replacement)["request_id"] == 1
        send(replacement, 1, "delay", delay_ms=1)
        assert replacement.wait(timeout=3) != 0
        record("replacement_ownership_and_replayed_request_refusal")

        oversized = start(owner)
        oversized.communicate(input=b"x" * (16 * 1024 + 1), timeout=3)
        assert oversized.returncode != 0
        record("oversized_incomplete_frame_refused")

        # This controller alone owns the helper's stdin writer. Its SIGKILL
        # exercises real descriptor closure, without starting PostgreSQL as root.
        controller_code = """
import json, subprocess, sys, time
child = subprocess.Popen([sys.argv[1], sys.argv[2]], stdin=subprocess.PIPE,
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
ready = child.stdout.readline()
child.stdin.write(json.dumps({'protocol_version': 1, 'request_id': 1,
    'operation': {'operation': 'delay', 'delay_ms': 10000}}).encode() + b'\\n')
child.stdin.flush()
sys.stdout.buffer.write(ready)
sys.stdout.buffer.flush()
time.sleep(30)
"""
        controller = subprocess.Popen([sys.executable, "-c", controller_code, str(binary), str(owner)],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=owner.parent)
        processes.append(controller)
        ready = frame(controller, verify_pid=False)
        assert ready["request_id"] == 0 and ready["result"]["ready"] is True
        assert ready["result"]["fault_injection"] is args.faults
        child_pid = ready["engine_pid"]
        assert child_pid != controller.pid
        # A missing proc entry is only death evidence after the same live
        # identity was visible here. Reject incompatible observation namespaces.
        try:
            child_identity = process_identity(child_pid)
        except (FileNotFoundError, ProcessLookupError) as error:
            report["process_observation_gate"] = {
                "status": "failed", "reason": "live_helper_identity_unavailable", "engine_pid": child_pid}
            raise AssertionError("live helper identity is not observable in this proc namespace") from error
        assert child_identity["uid"] == os.geteuid(), "observed helper must be an owned process"
        assert child_identity["state"] != "Z"
        assert child_identity["parent_pid"] == controller.pid
        orphan_helpers.append(child_identity)
        time.sleep(0.1)
        assert controller.poll() is None and not stopped(child_identity)
        controller.kill()
        assert controller.wait(timeout=3) == -signal.SIGKILL
        deadline = time.monotonic() + 3
        while not stopped(child_identity) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stopped(child_identity), "helper outlived the killed controller"
        record("supervisor_sigkill_closes_pipe_and_stops_helper", observed_live_identity=child_identity)

        if args.faults:
            aborted = start(owner)
            send(aborted, 1, "abort")
            assert aborted.wait(timeout=3) == -signal.SIGABRT
            record("private_native_abort_terminates_only_child")
    report["status"] = "passed"
except Exception as error:
    report["status"] = "failed"
    report["error"] = str(error)
    raise
finally:
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
    for expected in orphan_helpers:
        if not stopped(expected):
            try:
                os.kill(expected["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))

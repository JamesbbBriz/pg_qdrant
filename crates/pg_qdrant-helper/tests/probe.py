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


def start(owner, ready=True):
    process = subprocess.Popen([str(binary), str(owner)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               cwd=owner.parent, preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
    processes.append(process)
    if ready:
        result = frame(process)
        assert result["request_id"] == 0 and result["result"]["ready"] is True
        assert result["result"]["fault_injection"] is args.faults
    return process


def send(process, request_id, operation, **parameters):
    request = {"protocol_version": 1, "request_id": request_id,
               "operation": {"operation": operation, **parameters}}
    process.stdin.write(json.dumps(request).encode() + b"\n")
    process.stdin.flush()


def record(name, **details):
    report["checks"].append({"name": name, "status": "passed", **details})


def stopped(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].strip().split()[0] == "Z"
    except FileNotFoundError:
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

        if args.faults:
            send(process, 3, "panic")
            assert frame(process)["error"]["code"] == "engine_panic"
            assert process.poll() is None
            record("caught_native_panic_preserves_helper")
        else:
            send(process, 3, "abort")
            assert frame(process)["error"]["code"] == "invalid_parameter"
            assert process.poll() is None
            record("fault_operation_refused_without_private_feature")

        send(process, 4, "delay", delay_ms=10000)
        time.sleep(0.1)
        begin = time.monotonic()
        process.stdin.close()
        assert process.wait(timeout=3) == 0
        record("parent_pipe_eof_stops_outstanding_work", exit_seconds=time.monotonic()-begin)

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
        orphan_helpers.append(child_pid)
        time.sleep(0.1)
        controller.kill()
        assert controller.wait(timeout=3) == -signal.SIGKILL
        deadline = time.monotonic() + 3
        while not stopped(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stopped(child_pid), "helper outlived the killed controller"
        record("supervisor_sigkill_closes_pipe_and_stops_helper")

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
    for pid in orphan_helpers:
        if not stopped(pid):
            os.kill(pid, signal.SIGKILL)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))

#!/usr/bin/env python3
"""Read daemon-host PID identity and kernel records; never allocate or signal."""
from __future__ import annotations

if not __debug__:
    raise RuntimeError("optimized Python is unsupported for OOM observations")

import argparse
import ctypes
import json
from pathlib import Path
import re

KERNEL_BYTES = 2 * 1024 * 1024


def read_kernel():
    """SYSLOG_ACTION_READ_ALL does not consume, clear, or resize the ring."""
    library = ctypes.CDLL(None, use_errno=True)
    library.klogctl.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    library.klogctl.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(KERNEL_BYTES)
    size = library.klogctl(3, buffer, KERNEL_BYTES)
    if size < 0:
        number = ctypes.get_errno()
        raise OSError(number, "read-only kernel log unavailable")
    assert size < KERNEL_BYTES, "kernel log reached observation budget"
    return parse_kernel(buffer.raw[:size])


def parse_kernel(data):
    assert isinstance(data, bytes) and len(data) < KERNEL_BYTES
    assert not data or data.endswith(b"\n"), "incomplete kernel record"
    records = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        match = re.fullmatch(r"<(\d{1,3})>\[\s*(\d+)\.(\d{6})\] (.*)", line)
        assert match is not None, "kernel record format unavailable"
        priority, seconds, fraction, message = match.groups()
        assert int(priority) <= 191, "invalid syslog priority"
        if int(priority) >= 8:
            continue  # Userspace facilities cannot provide kernel attribution.
        records.append({"msg": message, "time": seconds + "." + fraction,
                        "time_us": int(seconds) * 1000000 + int(fraction)})
    return records


def read_mapping(pids, target, container_id, proc_root=Path("/proc")):
    assert re.fullmatch(r"[a-f0-9]{64}", container_id)
    assert 0 < len(pids) <= 128 and len(set(pids)) == len(pids)
    found = []
    for pid in pids:
        assert isinstance(pid, int) and pid > 0
        proc = proc_root / str(pid)
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


def narrow_kernel(records, mapping, container_id):
    assert re.fullmatch(r"[a-f0-9]{64}", container_id)
    assert isinstance(mapping["host_pid"], int) and mapping["host_pid"] > 0
    assert container_id in mapping["host_cgroup"]
    cursor = mapping["kernel_log_cursor_us"]
    assert isinstance(cursor, int) and cursor >= 0
    narrowed = []
    for item in records:
        if item["time_us"] <= cursor:
            continue
        message = item.get("msg", "")
        if not isinstance(message, str):
            continue
        if (message.startswith("oom-kill:") and container_id in message) or re.search(
                rf"Memory cgroup out of memory: Killed process {mapping['host_pid']}\b", message):
            assert len(message.encode()) <= 16384 and len(narrowed) < 32
            narrowed.append({"MESSAGE": message, "__MONOTONIC_TIMESTAMP": item.get("time")})
    return narrowed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["mapping", "kernel"])
    parser.add_argument("payload")
    args = parser.parse_args()
    request = json.loads(args.payload)
    if args.operation == "mapping":
        mapping = read_mapping(request["pids"], request["target"], request["container_id"])
        # Snapshot the kernel's own clock before releasing the native barrier.
        # Wall time and process start ticks are not interchangeable with it.
        mapping["kernel_log_cursor_us"] = max((r["time_us"] for r in read_kernel()), default=0)
        print(json.dumps(mapping))
    else:
        records = narrow_kernel(read_kernel(), request["mapping"], request["container_id"])
        print("\n".join(json.dumps(item) for item in records), end="\n" if records else "")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Read daemon-host PID identity and kernel records; never allocate or signal."""
from __future__ import annotations

if not __debug__:
    raise RuntimeError("optimized Python is unsupported for OOM observations")

import argparse
import json
from pathlib import Path
import re
import subprocess


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
    narrowed = []
    for item in records:
        message = item.get("msg", "")
        if not isinstance(message, str):
            continue
        if (message.startswith("oom-kill:") and container_id in message) or re.search(
                rf"Memory cgroup out of memory: Killed process {mapping['host_pid']}\b", message):
            assert len(message.encode()) <= 16384 and len(narrowed) < 32
            narrowed.append({"MESSAGE": message, "__MONOTONIC_TIMESTAMP": item.get("time")})
    return narrowed


def parse_dmesg(data):
    if not data.strip():
        return []  # No kernel records cannot satisfy strict victim attribution.
    result = json.loads(data)
    assert isinstance(result, dict) and isinstance(result.get("dmesg"), list)
    return result["dmesg"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["mapping", "kernel"])
    parser.add_argument("payload")
    args = parser.parse_args()
    request = json.loads(args.payload)
    if args.operation == "mapping":
        print(json.dumps(read_mapping(request["pids"], request["target"], request["container_id"])))
    else:
        # Read-only SYSLOG_ACTION_READ_ALL, never clear or change kernel logging.
        result = subprocess.run(["dmesg", "--syslog", "--json", "--since", "@" + str(int(request["since"]))],
                                capture_output=True, timeout=5, check=True)
        assert len(result.stdout) <= 2 * 1024 * 1024
        records = narrow_kernel(parse_dmesg(result.stdout), request["mapping"], request["container_id"])
        print("\n".join(json.dumps(item) for item in records), end="\n" if records else "")


if __name__ == "__main__":
    main()

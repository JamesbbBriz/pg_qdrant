"""Bounded read-only counters for the exact native OOM target's main thread."""

import os
from pathlib import Path


MEMORY_KEYS = frozenset(("anon", "file", "kernel", "file_dirty", "file_writeback",
                         "workingset_refault_file", "pgscan_direct", "pgsteal_direct", "pgmajfault"))
CPU_KEYS = frozenset(("usage_usec", "nr_throttled", "throttled_usec"))


def allocation_snapshot(target, bounded_text, cgroup=Path("/sys/fs/cgroup")):
    """Missing counters stay absent; unexpected observation failures propagate."""
    def selected(name, keys):
        result = {}
        for line in bounded_text(cgroup / name).splitlines():
            key, value = line.split()
            if key in keys:
                if key in result:
                    raise ValueError("duplicate observation counter")
                result[key] = int(value)
        return result

    pressure = {}
    for line in bounded_text(cgroup / "memory.pressure").splitlines():
        kind, *fields = line.split()
        totals = [field.removeprefix("total=") for field in fields if field.startswith("total=")]
        if kind not in ("some", "full") or kind in pressure or len(totals) != 1:
            raise ValueError("ambiguous memory pressure observation")
        pressure[kind] = int(totals[0])
    result = {"memory_stat": selected("memory.stat", MEMORY_KEYS),
              "cpu_stat": selected("cpu.stat", CPU_KEYS), "pressure_us": pressure}
    pid = int(target["pid"])
    if pid <= 0:
        raise ValueError("positive target PID required")
    try:
        # comm can contain spaces and parentheses. The main-thread stat avoids
        # attributing native helper activity to unrelated auxiliary threads.
        raw = bounded_text(Path(f"/proc/{pid}/task/{pid}/stat"))
        prefix, suffix = raw.rsplit(")", 1)
        if int(prefix.split(" (", 1)[0]) != pid:
            raise ValueError("unexpected task PID")
        fields = suffix.split()
        if int(fields[19]) != target["start_ticks"]:
            result["target_identity_changed"] = True
        else:
            result["target"] = {"start_ticks": int(fields[19]),
                "user_ticks": int(fields[11]), "system_ticks": int(fields[12]),
                "minor_faults": int(fields[7]), "major_faults": int(fields[9]),
                "clock_ticks": os.sysconf("SC_CLK_TCK")}
    except (FileNotFoundError, ProcessLookupError):
        result["target_gone"] = True
    return result

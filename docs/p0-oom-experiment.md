# P0 directed OOM comparison

Status: both directed OOM comparisons passed their stated assertions again in
[CI8](evidence/p0-current-ci.json), head `57c58fc`, following the retained
[CI7 result](evidence/p0-source-capacity-oom-ci.json). Exact kernel
victim/cgroup records support the result; source and negative tests alone do not.
Direct-worker containment failed as expected; the native helper preserved its
PostgreSQL supervisor and companion session. This remains separate from ordinary
SIGKILL/abort and the 125-second stop test, and is not a production memory quota.

The native operation is compiled only with `p0-fault-injection`. That feature
forwards to the existing Edge adapter and enables an exact optional
`libc =0.2.189` dependency already present in Cargo.lock. Normal SQL builds do not
expose the fault function, and normal helper builds refuse its `oom` protocol
operation. The private SQL call accepts no native allocation size, path or allocation-time
budget: `qdrant_internal.p0_fault('oom', 30000)`. Its second argument is the
existing SQL request wait deadline; it does not change the native guard's fixed
allocation limits. The existing schema and superuser checks
remain in force. This is a synthetic process-allocation fault, not an Edge index
write or an unflushed-index recovery test.

## Execution bounds

Positive execution belongs only to the separately reviewed CI container runner,
`scripts/run_oom_container.py`. It requires an already built local private-fault
image and creates one fresh container for one comparison. Build jobs must finish
before either comparison starts. Run the direct-worker and managed-helper
comparisons sequentially, with no retry to obtain a preferred victim.
Both positive Python drivers refuse optimized Python at module entry, including
`-O` and `PYTHONOPTIMIZE`, before any Docker or PostgreSQL action; their assertions
must remain active.

The runner requires at least 3 GiB host `MemAvailable`. Each container has an
exact 768 MiB memory limit, the same combined memory/swap limit (no container
swap), 128 PIDs, two CPUs, private PID/IPC/cgroup namespaces, no network, no host
mounts, all capabilities dropped, `no-new-privileges`, UID/GID 10001, and disabled
core dumps. It inspects the actual image and container settings before start.
It never requests a privileged container, a host cgroup write, a host OOM score
change, a protected negative score, or a kernel-log permission bypass.

The native guard reads the real cgroup-v2 settings and local event counters. It
requires a read-only leaf cgroup, `memory.oom.group=0`, no prior OOM kills, and
memory usage below half of the limit both before arming and immediately before
allocation. An owned, private, single-use nonce marker binds the profile,
cgroup, exact PID/start identity of each participant, dedicated PostgreSQL data
directory, and previously committed marker. The supervisor must be a child of
the bound postmaster. The helper must be distinct from the PG participants.

The private guard requires an unlimited data allocation limit. A finite helper
address-space limit is admitted only when measured `/proc/self/statm` mappings
leave at least twice the entire 768 MiB allocation cap available. The check runs
before arming and again at the allocation barrier; its measured bounds appear in
the native records. A finite direct-worker address-space limit remains refused.
This permits the development helper's 8 GiB virtual limit while retaining the
strict distinction between allocator refusal and a kernel-attributed OOM kill.

Only the target process temporarily raises its own `oom_score_adj` to 1000.
The prior nonnegative value is restored and checked if execution returns. This
is a victim preference, not a guarantee. The outer runner maps the target to a
host PID/start/cgroup identity before releasing a bounded barrier. Native code
then retains at most 768 MiB in 1 MiB chunks and touches every native page. The
10-second loop limit is cooperative; a blocked allocation or page fault is not
native cancellation. The independent outer container watchdog remains active,
and any watchdog, supervisor kill request, cleanup intervention, returned
allocation error, or reached allocation cap prevents a positive verdict.

## Evidence and verdicts

The inner observer records before/after `memory.events.local`, memory samples,
native stages, the exact target exit, companion SQL outcome, postmaster and
supervisor identities, committed marker retention, replacement identity, and a
real Edge smoke on the replacement. It also checks that replacement work did
not cause another OOM kill. A pre-observed 30-second `PgSleep` and the exact live
companion identity at the native allocation boundary establish overlap; that
boundary must be observed within 25 seconds of companion launch.

The direct-worker comparison expects PostgreSQL collateral session termination
and recovery. Passing that comparison cannot pass an isolation gate. The helper
comparison requires the original supervisor, postmaster, and companion session
to survive, the failed helper to be replaced, and the committed marker to remain.

Local OOM event deltas plus target SIGKILL are classified as `correlated_only`.
They never pass strict target attribution. The outer runner must additionally
read matching kernel records naming the exact mapped victim and its container
memory cgroup. The parsed `oom_memcg` must equal the observed host cgroup path;
a parent-limit OOM whose `task_memcg` contains the container ID does not qualify.
If kernel records are inaccessible or insufficient, the result is
inconclusive and the command fails; there is no escalation or substitute signal.
The helper's exit record must show zero supervisor kill attempts, and the outer
runner must show no manual termination. `production_memory_isolation_verified`,
`unflushed_edge_recovery_verified`, and `postgres_edge_atomicity_verified` remain
false regardless of the result.

The owned container's SQL/native reports, PostgreSQL and container logs, inspected
bounds, narrowed kernel victim records, and final verdict are collected even
after failure where the Docker daemon remains available. Only this invocation's
fresh container is eligible for cleanup. Output collection and subprocess waits
are bounded. A missing or malformed observation is a failed gate.

## Verification and CI integration

Safe local checks do not call the native operation or change OOM scores:

```sh
cargo test --locked -p pg-qdrant-edge-probe --features p0-fault-injection --lib oom_probe::tests
python3 -m unittest discover -s scripts/tests -p test_oom_container.py -v
```

The Rust tests cover invalid nonces, every required resource setting, and
malformed counter evidence. Python tests cover inspected bounds, attribution
classification, direct-worker collateral semantics, supervisor/watchdog
intervention, optimized-interpreter refusal, and real bounded observer subprocesses. Ordinary CI must also
continue running the normal helper protocol suite, which refuses `oom` without
the private feature. PostgreSQL profile compilation is required separately.

The P0 workflow now wires two sequential positive OOM steps after successful
private image builds. Independent failure conditions keep a failed direct
comparison from hiding the helper comparison; the experiments do not run
concurrently. A final always-run artifact upload retains both directories.
The [recorded CI7 execution](evidence/p0-source-capacity-oom-ci.json) passed
both comparisons. The regenerated
[normal dependency graph](dependency-graph.json),
[four-profile feature comparison](dependency-profiles.json) and
[dependency baseline](dependency-baseline.json) identify the integrated normal
and private selections; `scripts/dependency_report.py` checks those records.

Primary behavior references:

- [Linux cgroup-v2 memory interface](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html): local counters, memory limit, OOM grouping, and swap controls.
- [Linux proc interface](https://www.kernel.org/doc/html/latest/filesystems/proc.html): process identity and `oom_score_adj` behavior.
- [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/): memory/swap and CPU limits.
- Linux v6.8 primary source: [victim summary and kill record](https://github.com/torvalds/linux/blob/v6.8/mm/oom_kill.c) and [limiting versus task memory-cgroup context](https://github.com/torvalds/linux/blob/v6.8/mm/memcontrol.c). The limiter and victim cgroups can differ in a hierarchy; the parser requires the limiter to match exactly.

These interfaces describe the experiment's controls. They are not evidence that
the selected container runtime, kernel, or PostgreSQL profile passed it.

## Local validation

The [integrated local record](evidence/p0-integrated-local.json) binds the actual
source inputs: all four extension profiles and both helper binary profiles pass
compilation; three native guard tests and eight Python OOM decision tests pass.
No positive OOM allocation or new PostgreSQL SQL execution occurred locally.
A CI result must independently prove its target victim and PostgreSQL outcome.

## Recorded positive outcome

The verified archive contains the actual inner SQL reports, ready identities,
native allocation records, replacement engine reports and PostgreSQL logs.
They agree with the outer kernel selector/killed-process records for each exact
host PID and limiting memory cgroup. Each run has one OOM kill, no group kill and
no external, supervisor or client kill intervention.

| Observation | Direct worker | Managed helper |
| --- | --- | --- |
| `memory.events.local` delta: max / oom / oom_kill | 219 / 1 / 1 | 224 / 1 / 1 |
| Faulting SQL client exit | 2, PostgreSQL recovery | 1, managed helper disconnected |
| Companion SQL client exit | 2, terminated | 0, completed |
| PostgreSQL supervisor | Replaced during PG recovery | Same process survives |
| Committed PostgreSQL marker | Retained | Retained |
| Replacement Edge smoke | 16 groups passed | 16 groups passed |
| Counters after replacement work | No additional OOM kill | No additional OOM kill |

Inner reports retain `correlated_only`; matching external kernel records establish
the outer `kernel_record` attribution. The helper's roughly 30-second recorded
recovery interval includes waiting for the already running companion `pg_sleep`;
it is not a restart-latency measurement. The deliberate positive OOM preference
and tiny fixture do not establish general memory isolation or index durability.

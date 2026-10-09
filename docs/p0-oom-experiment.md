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

## Local act observation

Local act runs the same two directed comparisons. The act runner cannot read
sibling container PIDs from its private PID namespace. With `--observer-image`,
the outer harness resolves the observer image ID and starts a separate bounded
read-only observer for native PID/start/cgroup mapping and kernel records. This
observer has host PID/cgroup visibility, SYSLOG only, no network, 256 MiB with
no swap, 16 PIDs and a read-only root filesystem. It reads `/proc` and uses
`dmesg --syslog --json` without clearing the kernel buffer or changing logging.
Only matching OOM messages are retained; monotonic timestamps are labeled as
such. The observer does not allocate fault memory, signal test processes or
change cgroups. Test containers retain every isolation guard below. Missing
records still fail strict attribution. Historical hosted evidence above does
not establish that this local observer has passed a current complete run.

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
then reserves at most 768 MiB in one private anonymous mapping and writes one
byte in every native page. Linux initializes anonymous pages; no second bulk
initialization is needed. The 10-second loop limit is cooperative; a blocked
allocation or page fault is not
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

For local container-daemon observation, a separate read-only observer binds the
native PID, start ticks and container cgroup before releasing allocation. It
reads the kernel ring through `klogctl(SYSLOG_ACTION_READ_ALL)` into a fixed
2 MiB buffer; it never consumes, clears or resizes the ring. A full buffer,
incomplete record, unavailable interface or unrecognized format fails closed.
The observer records the kernel log's monotonic cursor before the barrier and
considers only later records. It does not convert wall time or process start
ticks into kernel log timestamps. Projection remains bounded to 32 matching
records of at most 16 KiB each. Exact victim PID and limiting-cgroup checks still
apply; cgroup counters and SIGKILL alone cannot pass attribution.

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

The local act registry wires two sequential positive OOM steps after successful
private image builds. Independent failure conditions keep a failed direct
comparison from hiding the helper comparison; the experiments do not run
concurrently. Local evidence export retains both directories. Hosted workflows
are disabled.
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

## Historical bounded debug initialization

The private native allocator initializes each successfully reserved 1 MiB buffer
with a bulk byte write before publishing its length. The pointer covers the
reserved `u8` allocation and all bytes are initialized before `set_len`;
subsequent volatile writes still touch every native page. This avoids debug
`Vec::resize` iterator overhead. The 768 MiB aggregate cap, 10-second cooperative
deadline, allocation-error refusal, process checks and exact victim attribution
remain unchanged.

Scoped local act `oominit-617f038029ebf56c` rebuilt both private native profiles
from matching installed build inputs plus the hashed allocator change. Four
native guard tests passed. Each actual profile observed one kernel OOM kill;
the direct worker exit affected companion SQL, while the helper supervisor and
companion survived and source replay reached a fresh native owner. Neither
strict outer gate passed: the daemon-visible PID did not establish the kernel
victim PID. These remain correlated observations, not strict attribution,
production isolation or full-revision CI evidence. The failed receipts are
retained and the checker continues to fail closed.

## Initial-namespace PID observation

Nested container runtimes can expose a daemon PID which differs from the PID in
the kernel's OOM record. The local OOM steps use `--kernel-task-observer` to bind
the initial-namespace task PID before releasing the allocation barrier. A
separate read-only observer loads a bounded BPF task iterator. It accepts exactly
one leader matching the native guard's innermost PID, PID namespace inode and
start tick; it then rechecks the daemon-visible process start and cgroup. The
kernel selector and killed-process records must still match this bound PID and
the exact limiting cgroup after the pre-barrier monotonic cursor.

The iterator follows the running kernel's
[BTF layout](https://docs.kernel.org/bpf/btf.html) and uses the documented
[iterator lifecycle](https://docs.kernel.org/bpf/bpf_iterators.html): load, link,
read, close. It reads at most 32 MiB of BTF, emits a single 32-byte identity and
refuses missing, ambiguous or unsupported identities. The current implementation
requires x86_64 Linux, task-iterator BTF and `CAP_BPF`/`CAP_PERFMON`; unavailable
interfaces fail the gate. It creates no pinned objects or tracepoint hooks and
does not signal tasks or write kernel memory. All descriptors close on success
and failure. Kernel-log collection remains a separate `CAP_SYSLOG` observation.

These capabilities belong only to the separate observation process. The native
fault container retains its private namespaces, non-root user, dropped
capabilities, fixed memory/CPU/PID bounds and allocation deadline. No ptrace
capability or privileged container is required.

Scoped local act `oomidentity-68a41dacbe39c74b` passed strict managed-helper
attribution: one OOM kill, both exact kernel records, surviving PostgreSQL and
companion, and source replay reaching a durable ticket under a fresh owner.
Its direct-worker comparison failed: memory reclaim reached the native
cooperative deadline without an OOM kill. That failed result remains retained;
this slice does not establish a full-revision CI pass, production memory
isolation or completion of P1 recovery acceptance.

Scoped local act `oomidentity-781315b5b943e90d` repeated the strict helper pass.
Separate live iterator checks rejected an incorrect PID, namespace inode and
start tick, refused loading without the two BPF capabilities, and retained the
same open-descriptor count after every success and failure. Its direct-worker
comparison again failed without an OOM kill; both failed comparisons remain
failures in their receipts.

## Private mapping and atomic observation publication

The fault allocator owns one private anonymous Linux mapping, checks its fixed
length and each page offset, and releases the exact mapping on returned failure.
It does not request a fixed address, locked pages or eager population. See the
[Linux mmap interface](https://man7.org/linux/man-pages/man2/mmap.2.html).
The 768 MiB cap, 10-second cooperative deadline, identity checks, score restore,
outer watchdog and strict kernel victim/cgroup attribution remain unchanged.

Scoped local act `oommap-9e588b001fba09e5` rebuilt both fault profiles on immutable
prior full-run images with the hashed mapping change. Five native guard tests
passed. Both directed comparisons passed strict kernel attribution without
external termination: direct-worker collateral interruption and helper session
survival/replacement were observed. This scope predates the publication fix
below and does not establish full-current-revision CI or production isolation.

The earlier full act `f40ec9039a2f4fbc` direct-worker failure occurred before
allocation: native evidence reports `allocation_started=false` and JSON EOF at
the observation barrier. The writer had created the public path before writing
its body. This was not evidence of reaching the allocation deadline. The failed
receipt remains failed and retained. The release and ready publishers now write
and sync a private exclusive pending inode, then publish it with an exclusive
hard link. Existing paths are never replaced; incomplete input is never visible
under the release name. Native JSON and identity validation still fail closed.

Scoped local act `oomatomic-d639d8adc9019247` repeated both strict comparisons
with atomic release and ready publication. Five frozen executable/observer
inputs and the complete act log were hash-verified. Direct kernel task 5277 and
helper task 7916 matched their exact limiting cgroups; no external termination
occurred. The helper retained PostgreSQL and companion sessions and replayed
source data under a replacement owner. All 84 Python tests passed in the
separate three-gate static scope `5ca30ddc729b7039`, including staged-input and
invalid/reused publication checks. A full current-revision run remains required.

## Bounded external allocation observations

The SQL fault driver records selected cgroup memory, reclaim, pressure and CPU
throttling counters every 50 ms during its existing 30-second observation window,
with an explicit maximum of 601 samples. It also reads the exact native target's
main-thread CPU ticks and page faults from the Linux
[task stat interface](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html).
The start tick must match before any thread counters are attributed; a reused
PID or disappeared task is recorded separately. Missing optional cgroup counters
remain absent, and unexpected read or parsing failures fail the observation.
Samples already collected remain in the failure report.

These read-only observations help distinguish reclaim pressure, CPU throttling
and page-fault work if the cooperative deadline recurs. They do not prove a
cause or change the native allocator, its 768 MiB cap, 10-second deadline,
64 KiB native-log limit or the outer 2 MiB SQL-report limit. Exact kernel
victim attribution and PostgreSQL/source recovery remain required independently.

Scoped local act `oomsnapshot-b91541b37b6c5a8c` passed both strict kernel-victim
comparisons with these observers and eight parser/refusal tests. The native
direct/helper binaries were retained unchanged from clean `123f611`; their
extension hashes were checked against their original native build reports.
Seven frozen executable/observer inputs and the full act log were hash-verified;
the log SHA-256 is
`6ec7425a3798eb63f468ac98843e9092145c0eb504b32e14678972d26e7d9098`.
Direct-worker collateral interruption, PostgreSQL recovery and marker retention
passed; the helper preserved the companion and replayed deletion/key reuse to a
durable ticket with obsolete native incarnation removal. These scoped passes do
not replace the earlier failed full regression or establish the deadline's cause.

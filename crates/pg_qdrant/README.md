# PostgreSQL P0 integration prototype

This crate is a feasibility experiment for PostgreSQL 17 on Linux x86_64. It
directly links the sibling `pg-qdrant-edge-probe` library, which uses the exact
published `qdrant-edge =0.8.0` crate. It does not implement an index catalog,
source-table synchronization, a product search function, or a durable indexing
service. The 54 product requirements remain in the capability registry as
planned and not release-supported.

The [latest recorded CI](../../docs/evidence/p0-capacity-and-sql-ci.json), run
37732857051 at `d843706`, built all four profiles and passed SQL 13/15/13/19
(direct normal/private, helper normal/private), helper pipes 7/8, 20 standalone
engine tests, two child tests and nine Python tests. All three candidate-vector
format groups ran in every SQL profile. The workflow failed independently:
both full-text 128 MiB fault and no-filler clean controls received SIGBUS during
reopen after exhausting their mounts. The [P0 report](../../docs/p0-report.md)
retains prior outcomes and the precise scope of this regression.

New tagged identity/source-recheck diagnostics and the guarded OOM comparison
have all four local PG17 compile profiles passing. The source-recheck normal
schema was also linked in a separately recorded earlier source snapshot. Their
new SQL/positive fault assertions remain pending; the prior CI does not certify
later additions. Proposed vector, source and public search contracts remain
unfrozen.

## Build inputs

- Rust 1.96.0 and pgrx/cargo-pgrx 0.19.3.
- An explicit `pg17` feature and the pgrx `cshim` feature.
- PostgreSQL 17 development headers and its matching `pg_config`.
- Clang/libclang, a C/C++ toolchain, and the native dependencies documented by the
  root build container.
- The root workspace lockfile; release and reproduction builds use `--locked`.

```bash
cargo check --locked -p pg_qdrant --no-default-features --features pg17
cargo pgrx install --manifest-path crates/pg_qdrant/Cargo.toml \
  --pg-config "$(command -v pg_config)" --cargo=--locked \
  --no-default-features --features pg17
```

The installation command writes into the selected PostgreSQL installation.
Run the SQL tests as a non-root operating-system user after installation:

```bash
PG_QDRANT_ARTIFACT_DIR=artifacts/p0-sql \
  bash crates/pg_qdrant/tests/run-p0.sh
```

The runner initializes and removes its own temporary cluster. It never targets
an existing database. `PGRX_PG_CONFIG_PATH` can select the PostgreSQL installation.
The report contains the tests that actually ran and their outcome; the presence
of this runner is not itself evidence that the tests have passed.

## Diagnostic SQL

```sql
CREATE EXTENSION pg_qdrant;
SELECT qdrant.build_info();
SELECT qdrant.capabilities();

-- P0 diagnostics require a PostgreSQL superuser.
SELECT qdrant_internal.p0_start_worker();
SELECT qdrant_internal.p0_ping();
SELECT qdrant_internal.p0_engine_probe(120000);
SELECT qdrant_internal.p0_delay(500, 1000);
```

`p0_engine_probe` runs actual BM25, vector, filter, fusion, reranking, grouping,
flush and reopen probes in a disposable shard. The sibling engine crate defines
the exact checks. It does not search a PostgreSQL table and does not establish
source consistency, authorization over indexed data, or benchmark quality.

All SQL functions are declared `VOLATILE`, `PARALLEL UNSAFE`, and security
invoker. The internal schema and its functions revoke public privileges, and
every P0 entry point independently checks the calling PostgreSQL superuser
status. `capabilities(index_name)` returns SQLSTATE `0A000` because this prototype
has no index catalog. Unknown/invalid transport parameters use `22023`, timeouts
use `57014`, queue saturation uses `53400`, and an unavailable worker uses
`55000`. Errors include a diagnostic detail rather than a successful placeholder.

The [format diagnostic](../../docs/p0-input-formats.md) validates dense, sparse
and rectangular token arrays. The separate
[identity/recheck diagnostic](../../docs/p0-source-recheck.md) accepts tagged
bigint/uuid/text keys and checks explicit fixture versions against source
visibility. Both remain nullable, volatile, parallel-unsafe, superuser-only
internal functions. The latter's `PgRelation` input conversion locks the source
before the function-body role check, and its conditional namespace lock is a
broad P0-only mechanism. No source SELECT/RLS authorization or durable identity
allocation follows from these diagnostic checks.

## Process, ownership and cancellation experiment

The first call starts a dynamic PostgreSQL background worker for the current
database. The database OID crosses the process boundary as a scalar `Datum`;
strings and PostgreSQL memory pointers do not. A private Unix socket in a
PostgreSQL-owned directory transports versioned, owned JSON messages. An OS
file lock selects one owner endpoint even when two SQL sessions start it at the
same time. No engine runtime or thread is initialized in the postmaster.

In the default direct-worker profile, the owner main thread handles PostgreSQL
signals, the socket, and queue accounting. At most one dedicated Rust thread executes the engine probe or delay
operation. Engine threads receive no SPI handle, PostgreSQL pointer, `Datum`, or
memory context. The engine probe owns its disposable shard until completion.

| Bound | P0 value |
| --- | --- |
| Active engine jobs | 1 |
| Queued jobs | 8 |
| Accepted connections | 16 |
| Request bytes | 16 KiB |
| Response bytes | 1 MiB |
| Requested timeout | 1–120,000 ms |
| Incomplete request / slow response interval | 5 seconds |

Socket connect/read/write are nonblocking. SQL waits call PostgreSQL's interrupt
checks and latch wait, allowing both `statement_timeout` and
`pg_cancel_backend` to interrupt the calling backend. A queued request is
discarded after its caller disconnects or its deadline expires.

An Edge call already running has no public cancellation token in this pinned
probe path. Stopping the caller's wait **does not stop that native call**. The
owner retains its active job until actual completion, does not start another
engine job in its place, and continues answering `p0_ping`. A stuck native call
therefore exhausts the one active execution slot; the status reports that fact.
This is a P0 result to measure, not a production cancellation guarantee.

Transport bounds do not cap Edge RSS, mmap, file size, native threads, or
optimization resources. `work_mem` is not an engine memory limit. Those limits,
source durability, graceful shutdown, index generations and replay remain
separate product acceptance gates.

## Fault experiments

Fault functions do not exist in a normal build. A separate, explicitly compiled
`p0-fault-injection` feature adds a superuser-only `p0_fault(text, integer)` with
two fixed operations: a caught engine-thread panic and process abort. Use only
the disposable-cluster runner:

```bash
cargo pgrx install --manifest-path crates/pg_qdrant/Cargo.toml \
  --pg-config "$(command -v pg_config)" --cargo=--locked \
  --no-default-features --features 'pg17 p0-fault-injection'
PG_QDRANT_ARTIFACT_DIR=artifacts/p0-faults \
  bash crates/pg_qdrant/tests/run-faults.sh
```

The fault runner checks a caught Rust panic, worker `SIGKILL`, native process
abort, effects on another SQL session, and recovery of a committed PostgreSQL
marker. It does not establish actual kernel OOM, full-text disk-failure
recovery, or persistent product-index corruption recovery. The separate narrow
engine ENOSPC pass does not close these PostgreSQL durability gates. It does
not allocate until the host is exhausted or fill the host filesystem. OOM and disk-full tests require isolated resource domains.

PostgreSQL background workers attach to shared memory. Their process boundary
must not be advertised as unconditional crash isolation. If the measured worker
failure terminates an unrelated session, the default architecture needs a
packaged helper-process evaluation before a reliability claim or release.

## Optional managed-helper comparison

`p0-managed-helper` selects a separate P0 process experiment. The same PostgreSQL
worker owns the SQL socket, queue, signals and permission boundary. It executes
the installed `pg_qdrant_p0_helper` binary beside PostgreSQL's own executable;
SQL cannot select an executable path. The helper has no PostgreSQL dependency
and initializes Edge after `exec`, outside PostgreSQL shared memory.

The supervisor and helper exchange bounded owned messages over stdin/stdout.
The ready handshake verifies the process ID, protocol, package version and fault
feature; each subsequent response must match that process and request ID. The
helper holds an independent engine-owner file lock until process termination.
When the supervisor's stdin writer closes, a dedicated bounded reader exits the
entire helper, including outstanding native work. A replacement must acquire
the same engine-owner lock.

The supervisor reaps a failed helper before attempting a replacement, retains an
outstanding native request after caller cancellation, and permits three restart
attempts with increasing backoff. A separate 125-second operation limit can kill
the helper; this is a P0 process-stop policy without a durable-index shutdown
claim. `p0_ping` reports separate worker/engine PIDs, readiness, starts, last
failure and restart exhaustion. One bounded last-exit record retains the old
engine PID, exit code/signal and supervisor stop reason across a successful
replacement. Requests do not fall back to the direct profile.

```bash
cargo build --locked -p pg-qdrant-helper
install -m 755 target/debug/pg_qdrant_p0_helper \
  "$(pg_config --bindir)/pg_qdrant_p0_helper"
cargo pgrx install --manifest-path crates/pg_qdrant/Cargo.toml \
  --pg-config "$(command -v pg_config)" --cargo=--locked \
  --no-default-features --features 'pg17 p0-managed-helper'
PG_QDRANT_MANAGED_HELPER=1 PG_QDRANT_ARTIFACT_DIR=artifacts/p0-helper \
  bash crates/pg_qdrant/tests/run-p0.sh
```

For the private fault comparison, build **both** binaries with
`p0-fault-injection`, include `p0-managed-helper` in the extension features, and
run `run-faults.sh` with `PG_QDRANT_MANAGED_HELPER=1`. A build mismatch fails the
ready handshake. The shared test suite preserves the direct-worker expectation
of collateral session termination and separately requires helper crashes to
preserve the supervisor and companion SQL session. The helper profile also
tests that supervisor `SIGTERM` stops an outstanding helper and allows a fresh
owner, and that three restart attempts end in an observable unavailable state.
A separate CI assertion freezes the active helper with `SIGSTOP`, cancels the
SQL caller, and waits for the unchanged 125-second process-stop limit to replace
that helper while preserving the PostgreSQL supervisor and a companion query.
The standalone helper suite checks pipe cleanup after killing its own pure
controller. CI6 separately exercised forced PostgreSQL-supervisor SIGKILL:
PostgreSQL recovery terminated companion SQL, retained postmaster identity and
the committed marker, stopped the old helper before replacement, and reacquired
the same fence inode. That is expected supervisor collateral, not failure of
the separate native-helper containment assertion.

These results establish only the recorded P0 behavior. Source outbox durability,
positive kernel OOM, full-text recovery at the supported experiment capacity,
production generations, permissions, upgrade safety and topology acceptance
remain separate gates. The [OOM harness](../../docs/p0-oom-experiment.md) uses
fresh inspected fixed-limit containers and exact kernel victim attribution;
its private selector is not part of the ordinary fault script or normal build.

The sibling engine suite also verifies read-only loading/refresh using a
test-supplied manifest and update-only preview/no-write branches. In fixed Edge
0.8.0, update-only Store, Delete and empty bootstrap trigger unimplemented
panics and remain unavailable. Those experiments do not provide a replacement
for the ordinary EdgeShard mutation path, a snapshot restore mechanism, or a
PostgreSQL committed-change ACK contract.

The [durability ADR](../../docs/adr/0003-edge-durability-and-recovery.md) also
records that Edge update return is not durable ACK and the fixed loader does
not replay logical WAL operations. Load can mutate/repair storage; a future
source-index owner must validate or reconstruct an unclean generation before
serving it. The current diagnostics do not implement that protocol.

## Versioned source evidence

- [pgrx 0.19.3 published manifest](https://docs.rs/crate/pgrx/0.19.3/source/Cargo.toml)
  declares Rust 1.96, PG17 support and the `cshim` feature.
- [pgrx 0.19.3 worker implementation](https://docs.rs/crate/pgrx/0.19.3/source/src/bgworkers.rs)
  defines `BackgroundWorkerBuilder::load_dynamic`, `set_argument`,
  `enable_spi_access`, `BackgroundWorker::connect_worker_to_spi_by_oid`, and
  `BackgroundWorker::wait_latch` used here.
- [PostgreSQL 17 background workers](https://www.postgresql.org/docs/17/bgworker.html)
  documents worker registration, shared memory, scalar argument restrictions,
  one database connection per worker, shutdown and restart behavior.

Source review, compilation, SQL integration, fault execution and release support
are distinct evidence levels. The stage reports in the root documentation state
which have actually been obtained for the current code.

# PostgreSQL P0 integration prototype

This crate is a feasibility experiment for PostgreSQL 17 on Linux x86_64. It
directly links the sibling `pg-qdrant-edge-probe` library, which uses the exact
published `qdrant-edge =0.8.0` crate. It does not implement an index catalog,
source-table synchronization, a product search function, or a durable indexing
service. The 54 product requirements remain in the capability registry as
planned and not release-supported.

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

## Process, ownership and cancellation experiment

The first call starts a dynamic PostgreSQL background worker for the current
database. The database OID crosses the process boundary as a scalar `Datum`;
strings and PostgreSQL memory pointers do not. A private Unix socket in a
PostgreSQL-owned directory transports versioned, owned JSON messages. An OS
file lock selects one owner endpoint even when two SQL sessions start it at the
same time. No engine runtime or thread is initialized in the postmaster.

The owner main thread handles PostgreSQL signals, the socket, and queue
accounting. At most one dedicated Rust thread executes the engine probe or delay
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
marker. It records incomplete OOM, disk-full, and persistent-index corruption
gates explicitly. It does not allocate until the host is exhausted or fill the
host filesystem. OOM and disk-full tests require isolated resource domains.

PostgreSQL background workers attach to shared memory. Their process boundary
must not be advertised as unconditional crash isolation. If the measured worker
failure terminates an unrelated session, the default architecture needs a
packaged helper-process evaluation before a reliability claim or release.

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

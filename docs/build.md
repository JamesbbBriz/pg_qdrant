# Build and feasibility verification

The current code is a P0 investigation harness. It does not implement indexing an
ordinary source table, automatic source change capture, or the proposed product
search functions. Successful probes do not constitute a production release.

## Exact inputs

| Input | Selected value | Evidence boundary |
| --- | --- | --- |
| Rust | 1.96.0 | Combined engine/PG17 build and scoped SQL probes verified at the recorded CI revision |
| Qdrant Edge | 0.8.0 | Published archive checksum matches Cargo.lock; no declared crate features |
| pgrx / cargo-pgrx | 0.19.3 / 0.19.3 | Matching library and build tool; no substitution with a network client |
| PostgreSQL | 17, headers/package 17.11 | Other PostgreSQL majors are outside the tested target |
| pgrx features | `pg17`, `cshim` | Exactly one PostgreSQL major; unsafe fault injection is off by default |
| Charabia | 0.9.9 in current lockfile | Upstream-owned transitive dependency; not a separate analyzer implementation |
| Rust CPU target | Default x86_64 target, no `target-cpu=native` | Upstream C SIMD kernels include Haswell code; broader CPUs remain a separate gate |
| Container base | Digest-pinned Ubuntu 24.04 | Exact digest and selected native inputs in Dockerfile.p0 |

`Cargo.lock` records the entire resolution, including platform-specific packages.
`dependency-graph.json` records the selected Linux PG17 resolution, enabled
features, package checksums, dependency edges and package-declared licenses. It
omits local build paths. The graph is not a substitute for compilation or a
complete third-party license audit.

The container pins its base image, PostgreSQL package, Rust, cargo-pgrx and Rust
dependency graph. Other native APT packages are inventoried at build time; they
are not yet fully pinned to an immutable package snapshot. A bit-for-bit
reproducible native package set is therefore not claimed.

## Standalone engine

Install the exact Rust toolchain and a C compiler/linker. The engine build uses
native C SIMD source supplied by its published crate. No Qdrant Server, ONNX
runtime, model download, pgvector installation, or embedding service is required.

```sh
cargo check --locked -p pg-qdrant-edge-probe
cargo run --locked -p pg-qdrant-edge-probe
cargo test --locked -p pg-qdrant-edge-probe -- --test-threads=1
```

The JSON output names each check and its scope. Synthetic vectors exercise the
engine API; they are not outputs of a claimed production model. The persistence
test kills only a dedicated child process after explicit flush, then checks its
reopened data. This does not establish atomicity with PostgreSQL commits.

The current local locked suite passes 20 engine tests. It includes read-only
loading and refresh with an explicitly test-supplied manifest, snapshot-manifest
inspection, and update-only preview/no-write replay. Fixed Edge 0.8.0's
update-only Store, Delete and empty bootstrap paths trigger unimplemented panics
and are reported unavailable; the ordinary `EdgeShard` mutation path is separate.
The bounded public-lifecycle test requires Linux `taskset`, verifies singleton
CPU affinity before engine pools start, and uses only owned temporary fixtures.
These tests do not establish snapshot archive production/restoration or SQL
source/authorization behavior. The [probe inventory](../crates/edge-probe/README.md)
records the exact coverage.

## PostgreSQL prototype

The build requires PostgreSQL 17 server headers and `pg_config`, libclang for
bindgen, and a C toolchain for pgrx's explicit `cshim`. libclang 11 or later is an
upstream pgrx requirement; the container obtains the PG17 package's LLVM toolchain.
The build tool also needs its native OpenSSL/pkg-config dependencies.

```sh
cargo install cargo-pgrx --version 0.19.3 --locked
export PGRX_PG_CONFIG_PATH=/usr/lib/postgresql/17/bin/pg_config
cargo build --locked -p pg_qdrant --no-default-features --features pg17
cargo pgrx install --manifest-path crates/pg_qdrant/Cargo.toml --no-default-features --features pg17 \
  --pg-config "$PGRX_PG_CONFIG_PATH" --cargo=--locked
```

Use a PostgreSQL installation whose extension directories are writable by the
installer, or cargo-pgrx's documented `--sudo` installation option. Run PostgreSQL
under a non-root operating-system user. The test harness creates its own
disposable cluster; it refuses to use a root process or an unrelated data directory.

```sh
bash crates/pg_qdrant/tests/run-p0.sh
```

The private SQL functions are listed in the prototype README. They require a
PostgreSQL superuser and cannot register or search application tables. Each
database's worker has an exclusive owner lock, a bounded Unix-socket protocol,
at most one active engine task, queue admission limits, caller deadlines and
interruptible backend waits. Engine threads receive only owned Rust data.

Caller cancellation and native engine cancellation are different. The pinned
public Edge request API does not expose an external cancellation token. An
already running engine call retains its owner until completion after a caller
disconnects. The probe reports this limitation rather than reopening the same
shard or claiming that the engine stopped.

## Container and CI

```sh
docker build --file Dockerfile.p0 --tag pg-qdrant-p0 .
docker run --rm --memory=5g --cpus=2 pg-qdrant-p0
```

The container compiles and installs as root, then runs the isolated PostgreSQL
tests as an ordinary user. CI uses these same commands and exports the test
reports. The [first recorded CI run](evidence/p0-postgresql-ci.json) passed 10
normal-profile checks and 12 private-fault checks at its recorded source tree.
Those results include a negative finding about direct-worker crash isolation.
New code and additional profiles must earn their own run evidence before their
verification status advances.

The [latest CI](evidence/p0-full-text-disk-ci.json), run 37729282903 at head
`77f9ecc`, built and installed the normal extension and passed 15 engine tests,
two child-bookkeeping tests, six Python runner tests and narrow 32 MiB ENOSPC.
The separate full-text 128 MiB experiment received SIGBUS at `recovery_reopen`;
all four SQL profiles were skipped. Historical [four-profile helper evidence](evidence/p0-helper-ci.json)
and the [partially passing later regression](evidence/p0-regression-ci.json)
remain tied to their own revisions. Current local Python tests pass nine cases;
they do not substitute for a current container/SQL regression.

For intentionally destructive *disposable-cluster* experiments only:

```sh
docker build --file Dockerfile.p0 --build-arg P0_FAULTS=1 --tag pg-qdrant-p0-faults .
docker run --rm --memory=5g --cpus=2 pg-qdrant-p0-faults \
  bash crates/pg_qdrant/tests/run-faults.sh
```

Fault entry points are compiled out of a normal build. A successful fault test
may demonstrate collateral PostgreSQL session termination; that finding is
evidence against the tested isolation boundary, not a production pass.

### Managed-helper comparison

The optional helper uses the same pinned Edge library in a separately exec'ed
process. The package installs its executable beside PostgreSQL; SQL arguments
cannot choose the executable. The PostgreSQL supervisor retains queue admission
and protocol handling. The direct-worker profile remains independently testable.

```sh
docker build --file Dockerfile.p0 --build-arg P0_HELPER=1 \
  --tag pg-qdrant-p0-helper-normal .
docker run --rm --memory=5g --cpus=2 pg-qdrant-p0-helper-normal

docker build --file Dockerfile.p0 --build-arg P0_HELPER=1 --build-arg P0_FAULTS=1 \
  --tag pg-qdrant-p0-helper-faults .
docker run --rm --memory=5g --cpus=2 pg-qdrant-p0-helper-faults \
  bash crates/pg_qdrant/tests/run-faults.sh
```

CI defines both normal and fault-enabled helper profiles, including their standalone
pipe tests and actual SQL calls. The selected image sets
`PG_QDRANT_MANAGED_HELPER=1`; the suite checks this against the extension build
and helper handshake. Missing or mismatched helpers fail explicitly and do not
silently select the direct-worker profile. See the [helper protocol tests](../crates/pg_qdrant-helper/README.md)
and [PostgreSQL prototype](../crates/pg_qdrant/README.md) for ownership, restart,
parent-death and cancellation boundaries. Presence of these CI steps alone does
not claim that they passed. Independently qualified steps now depend on their
image outcome and cancellation state, so a disk or pipe failure need not skip
unrelated SQL probes; any failed experiment still fails the overall job.

### Bounded disk and corruption experiments

The default standalone suite corrupts only owned copies of an eight-point
fixture and verifies that invalid metadata cannot open as healthy data. It then
reopens an intact copy and the original, checking actual filtered retrieval.
This is rejection and recovery-source validation, not arbitrary bit-rot repair.

The disk-full test requires an explicitly provisioned, empty, isolated tmpfs:

```sh
docker run --rm --memory=5g --cpus=2 \
  --tmpfs /pgq-p0-faults:rw,noexec,nosuid,nodev,size=32m,uid=10001,gid=10001,mode=0700 \
  --env PG_QDRANT_ENOSPC_DIR=/pgq-p0-faults \
  pg-qdrant-p0 python3 scripts/run_disk_probe.py
```

The probe checks the mount type, ownership, permissions, size and data-directory
separation before writing. It requires a real `ENOSPC` response from an Edge
configuration save, verifies that persisted configuration is unchanged and
separately records whether in-memory settings changed, frees its filler and
checks retry/reopen. The wrapper requires a successful native exit and an ENOSPC
report with `status: "passed"`; an unconfigured `not_run` is not a passing
positive experiment. Its artifact includes the child return code or signal,
timeout state, bounded output and last recorded execution phase, including when
native code cannot return JSON. The wrapper's own real-child regression tests
run before the container build:

```sh
python3 -m unittest discover -s scripts/tests -p 'test_*.py'
```

The narrow fixture retains eight records, four named representations and keyword
indexes while excluding the mutable text indexes. Its 32 MiB ENOSPC
configuration-save/retry/reopen checks passed in [CI4](evidence/p0-regression-ci.json)
and [CI5](evidence/p0-full-text-disk-ci.json). The older full-fixture 32 MiB
attempt in [CI2](evidence/p0-helper-ci.json) produced no JSON and did not retain
its exit code; its cause remains unknown. The intervening
[errno-wrapper failure](evidence/p0-disk-ci.json) is a separate preserved result.

The full-text combination has its own 128 MiB bound and remains a failing gate.
Run the fault experiment and its clean control in **separate disposable
containers**, each with an initially empty mount:

```sh
docker run --rm --memory=5g --cpus=2 --network=none \
  --tmpfs /pgq-p0-faults:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700 \
  --env PG_QDRANT_ENOSPC_DIR=/pgq-p0-faults \
  pg-qdrant-p0 python3 scripts/run_disk_probe.py --profile full-text

docker run --rm --memory=5g --cpus=2 --network=none \
  --tmpfs /pgq-p0-faults:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700 \
  --env PG_QDRANT_ENOSPC_DIR=/pgq-p0-faults \
  pg-qdrant-p0 python3 scripts/run_disk_probe.py --profile full-text --clean
```

The clean control creates no filler and reports a different experiment kind; it
cannot satisfy ENOSPC acceptance. CI5 observed SIGBUS after reaching
`recovery_reopen` in the full-text fault profile. Added diagnostics retain
stage-specific logical/allocated bytes, owned mapping RSS, filesystem free
blocks and post-exit metadata without labeling those observations as the cause.
The new instrumented tmpfs runs have no positive result yet.

A local clean full-text reopen on an ordinary filesystem passed and recorded
289,807,352 logical bytes, 331,776 allocated bytes and approximately 215,756 KiB
of owned mapping RSS after reopen. Mapping RSS exceeds the 128 MiB experiment
capacity, but it is not a measurement of tmpfs allocation or proof of the
failing operation. Eager mutable-text loading is a hypothesis to test; neither
these measurements nor a clean ordinary-filesystem pass establishes full-text
ENOSPC recovery.
See the [fixture and fixed-source details](../crates/edge-probe/README.md#why-the-small-disk-fixture-excludes-mutable-text-indexes).

Separate corruption and explicitly flushed SIGKILL tests retain their phrase
assertions. WAL exhaustion, dirty ingestion, source-event ACK durability,
actual kernel OOM and power-loss behavior remain independent open gates.

## Keeping evidence current

```sh
python3 scripts/check_contracts.py
python3 scripts/dependency_report.py --check
```

The first command verifies preserved capability scope, work-item references,
direct pins, tool alignment, lock checksums and evidence consistency. The second
re-resolves the locked target's actual metadata and checks the committed feature
and license inventory. Neither command upgrades support status by itself.

When changing dependencies, read `dependencies.md`, `dependency-baseline.json`
and `capabilities.md` first. Regenerate and review the graph, compile exhaustive
public-API probes, and run engine/SQL/quality/security/recovery/migration tests.
The Dependabot configuration groups engine and pgrx dependencies; the contract
validator rejects a pgrx update without the matching cargo-pgrx tool change.
The configuration becomes active only when present on the repository's default
branch and enabled by GitHub. No automatic release or merge is configured.

Open gates include product transactions and source authorization, complete
memory/disk fault coverage, a settled process architecture, full lexical quality,
index upgrades/rollback, native package reproducibility and clean release
installation. Their owners and required evidence remain in `work-items.json`.

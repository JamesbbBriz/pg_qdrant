# Build and feasibility verification

The code retains the P0 investigation harness and adds the
[installed development source-to-Edge path](p1-integration.md). Ordinary table
capture, flush-before-ACK consumption and scoped public queries are tested
separately from diagnostic probes. Full product acceptance, upgrades and public
distribution remain open; successful probes do not constitute a release.

## Exact inputs

| Input | Selected value | Evidence boundary |
| --- | --- | --- |
| Rust | 1.96.0 | Combined engine/PG17 build and scoped SQL probes verified at the recorded CI revision |
| Qdrant Edge | 0.8.0 | Published archive checksum matches Cargo.lock; no declared crate features |
| pgrx / cargo-pgrx | 0.19.3 / 0.19.3 | Matching library and build tool; no substitution with a network client |
| PostgreSQL | 17, headers/package 17.11 | Other PostgreSQL majors are outside the tested target |
| pgrx features | `pg17`, `cshim` | Exactly one PostgreSQL major; unsafe fault injection is off by default |
| Charabia | 0.9.9 in current lockfile | Upstream-owned transitive dependency; not a separate analyzer implementation |
| CPU policy | Generic Rust target plus explicit conservative admission for upstream Haswell C code | [Shared runtime guard and generated-object audit](p0-cpu-baseline.md); broader CPUs are not claimed |
| Container base | Digest-pinned Ubuntu 24.04 | Exact digest and selected native inputs in Dockerfile.p0 |

`Cargo.lock` records the entire resolution, including platform-specific packages.
`dependency-graph.json` records the selected Linux PG17 resolution, enabled
features, package checksums, dependency edges and package-declared licenses. It
omits local build paths. The graph is not a substitute for compilation or a
complete third-party license audit.

The [native freeze](p0-native-build.md) now fixes Ubuntu to snapshot
`20261008T061600Z`, pins and verifies all seven PGDG archives, and compares the
entire 246-entry installed inventory with the successful CI7 build. The base
image digest, Rust, cargo-pgrx and core Cargo graph remain fixed. The new native
configuration passed clean TLS/APT installation in all four CI8 diagnostic
profiles. The [native evidence](evidence/p0-native-ci.json) binds the exact 246
installed entries, seven archive pins, compiler/PG/ELF observations and CPU
reports. Bit-identical compiler output and full license review are not claimed.

CI permits at most three identical Docker build attempts when the failing
download is an Ubuntu Snapshot HTTP 500/502/503/504 transport error, with
10- and 20-second delays. All failed-attempt output remains in the job log.
The wrapper preserves every build argument, URL, version and checksum;
persistent transport failure fails the step. Compilation, tests, unexplained
checksum mismatches and signature failures are not retried or converted to success.
If BuildKit reports a checksum mismatch for a Snapshot `ADD`, the wrapper can
classify it as transport failure only when a fresh, TLS-verified HTTP
500/502/503/504 response from that exact pinned URL has the identical rejected
body digest. This observation is bounded to 64 KiB with a ten-second socket timeout. The error
body is never installed, and every retry must still pass the pinned checksum.
This bounded retry policy does not establish upstream availability. Local
builds can use `python3 scripts/retry_snapshot_build.py docker build ...`
or invoke the identical Docker build directly.

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

The method/input local locked checkpoint passes 25 normal harness tests, including owned-child
invalid-input characterization and unclean-generation refusal/reconstruction.
The [method/input/rebuild record](evidence/p0-method-input-rebuild-local.json)
binds its exact source and all four PG compile profiles. CI7 separately passed
23 tests at its earlier source; CI8 passes 27 ordinary engine tests at `57c58fc`,
including the two CPU policy tests. Private OOM guard tests have their own count.
The engine coverage includes read-only
loading and refresh with an explicitly test-supplied manifest, snapshot-manifest
inspection, and update-only preview/no-write replay. Fixed Edge 0.8.0's
update-only Store, Delete and empty bootstrap paths trigger unimplemented panics
and are reported unavailable; the ordinary `EdgeShard` mutation path is separate.
The bounded public-lifecycle test requires Linux `taskset`, verifies singleton
CPU affinity before engine pools start, and uses only owned temporary fixtures.
These tests do not establish snapshot archive production/restoration or SQL
source/authorization behavior. The [probe inventory](../crates/edge-probe/README.md)
records the exact coverage.

## Independent lexical experiment

The nested [Tantivy workspace](../experiments/tantivy-probe/README.md) pins its own
0.26.2 dependency and lockfile. It does not enter the extension's Cargo graph.
Five local semantic experiments cover fuzzy expansion, phrase/parser behavior,
source snippets and reader/update visibility, with concrete gaps retained.

```sh
cargo build --locked --manifest-path experiments/tantivy-probe/Cargo.toml
timeout 45s cargo run --locked --manifest-path experiments/tantivy-probe/Cargo.toml
python3 experiments/tantivy-probe/inventory.py --check
```

The separate `Dockerfile.p0-lexical` image compiles this binary after the core
image succeeds. Its failure cannot skip the independent core SQL/fault paths.
CI runs it as the ordinary user
in a network-disabled 512 MiB container with one CPU, 64 PIDs and a 45-second
execution deadline, then retains its JSON report. All five cases passed in
[CI8](evidence/p0-tantivy-ci.json), independently from the core image and SQL
profiles. The small fixture is not a held-out quality comparison,
a selected production lexical adapter or a completed license review.

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

The [latest recorded CI](evidence/p0-current-ci.json), run
[37743504259](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37743504259),
passed at implementation `57c58fcee51efb0067f04b03ffb44e300f76ce72`, merge
`716cb2871d325dfd629a40177c9cfee613af4cfc`, tree
`8b1d6d863be52229f8b15b49ad0b58b96b28608f`. All four images built/installed
and SQL 20/22/20/26, helper pipes 7/8, 27 ordinary engine tests, two child tests
and 38 Python tests passed. The private native OOM guard suite passed three
tests in each private build. All four disk/capacity profiles and both
kernel-attributed OOM comparisons passed their stated assertions. Direct-worker
OOM containment remains a negative finding. The independent lexical image
also passed its five scoped cases. The artifact ZIP digest and all 100 members
were verified; [native/CPU evidence](evidence/p0-native-ci.json) separately
records the frozen build inputs and their limits. Historical results, including
CI7's earlier 53-member artifact, remain separate in the [P0 report](p0-report.md).
New source or dependencies must earn their own validation.

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

The full-text experiment now requires **exactly 384 MiB**. Both ordinary entry
paths reject every other capacity before writing. Use separate disposable
containers with initially empty dedicated mounts:

```sh
docker run --rm --memory=5g --cpus=2 --network=none \
  --tmpfs /pgq-p0-faults:rw,noexec,nosuid,nodev,size=384m,uid=10001,gid=10001,mode=0700 \
  --env PG_QDRANT_ENOSPC_DIR=/pgq-p0-faults \
  pg-qdrant-p0 python3 scripts/run_disk_probe.py --profile full-text

docker run --rm --memory=5g --cpus=2 --network=none \
  --tmpfs /pgq-p0-faults:rw,noexec,nosuid,nodev,size=384m,uid=10001,gid=10001,mode=0700 \
  --env PG_QDRANT_ENOSPC_DIR=/pgq-p0-faults \
  pg-qdrant-p0 python3 scripts/run_disk_probe.py --profile full-text --clean

docker run --rm --memory=5g --cpus=2 --network=none \
  --tmpfs /pgq-p0-faults:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700 \
  --env PG_QDRANT_ENOSPC_DIR=/pgq-p0-faults \
  pg-qdrant-p0 python3 scripts/run_disk_probe.py --profile full-text \
    --characterize-128-capacity-refusal
```

The third command tests only the exact no-write capacity refusal through both
ordinary entry paths; it does not open Edge or verify ENOSPC/recovery. The clean
control creates no filler and cannot satisfy the positive ENOSPC gate. A crash,
`not_run`, wrong report or unrelated refusal cannot pass either recovery or the
separate capacity characterization.

[CI6](evidence/p0-capacity-and-sql-ci.json) observed SIGBUS in both original
128 MiB full-text runs, including the no-filler control. Each had 99,254,272 bytes
free before reopen and zero afterward; owned allocation reached the complete
mount capacity. A larger fixed experiment budget addresses the demonstrated
fixture-sizing problem, not the upstream loader or a production resource policy.
The original failures remain recorded. CI7 passed the two independent 384 MiB
reopen experiments and the exact zero-write 128 MiB guard. Reopen allocation in
each 384 MiB fixture reached 220,508,160 bytes; this is not a product capacity
formula. See the
[capacity follow-up](p0-full-text-capacity.md) and
[fixed-source fixture details](../crates/edge-probe/README.md#why-the-small-disk-fixture-excludes-mutable-text-indexes).

Separate corruption and explicitly flushed SIGKILL tests retain their phrase
assertions. The later dirty-generation fixture proves only preserved refusal
and reconstruction from a complete synthetic source. WAL growth, transactional
dirty-ingestion recovery, exact-event PostgreSQL ACK and power loss remain open.

## Bounded kernel OOM comparison

The [separate OOM procedure](p0-oom-experiment.md) requires fresh disposable CI
containers using both private images. It validates a fixed 768 MiB memory/no-swap
limit, UID, process and cgroup isolation, one-shot ownership markers and resource
budgets before native allocation. It records the actual victim and limiting
cgroup from available kernel evidence. It does not elevate privileges or turn
missing attribution into success. The ordinary cluster and disk runners never
invoke this allocator. [CI8](evidence/p0-current-ci.json) repeated the earlier
CI7 comparison with exact kernel victim/cgroup attribution in both profiles:
direct-worker death caused PostgreSQL collateral, while helper death preserved
the supervisor and companion SQL. Both replacement engine smokes passed; no
external, supervisor or client kill caused either OOM outcome.
Production memory isolation and unflushed-index recovery remain unverified.

The [profile comparison](dependency-profiles.json) records exact manifests,
lockfile and enabled features for all four builds. Check it with:

```sh
python3 scripts/dependency_report.py --comparison-output docs/dependency-profiles.json --check
```

## Source backup boundaries

The [physical recovery procedure](physical-source-restore.md) tests a matching
PG17 hot backup with external native storage, old-receipt refusal and actual
BM25/dense/sparse/MaxSim replay in an independent clone. Default native storage
inside PGDATA exceeds PostgreSQL 17 base-backup path limits and remains
unsupported for physical backup. The documented external layout must be
provisioned separately for source and restored clusters before helper startup.
PITR, replication, failover and release support remain open.

## Keeping evidence current

Failed local product tests leave their stopped disposable PostgreSQL cluster
inside the owned test container until diagnostic export. `p1-failed-cluster.json`
records its path and test/stop exit codes; a shutdown failure also fails the run.
Successful runs remove their cluster. `p1-product-progress.json` records completed assertions,
completed crash cuts and the current cut's observations, including a failed
durability wait. An incomplete progress file cannot replace a successful
product result or satisfy an acceptance gate. Retained clusters contain only
synthetic fixtures and are diagnostic evidence, not backup or recovery proof.

Local CI, installation preview and quality measurement retire their exact
run-labelled containers after exporting evidence. A failed gate no longer keeps
all successful containers from that run. The host records each container's
state, image identity, logs and filesystem change list, archives available test
artifacts, and archives `/tmp` for failed or interrupted tests before removal.
Archive streams have a 512 MiB and 90-second limit each; incomplete exports fail
cleanup and retain the original container. These archives do not include
database volumes or already-unmounted tmpfs contents. Historical unlabelled
containers are not swept automatically.

The runner is retired last, together with only its exact workspace and environment
volumes. Shared tool caches, unrelated containers, images and business volumes
are not pruned. Cleanup receipts live under the local run's `resources/` directory.
A remaining managed run blocks the next launch rather than accumulating another
set of containers. After resolving an export failure, retry the exact run:

```sh
python3 scripts/ci_resources.py --run-id <run-id> \
  --directory artifacts/local-ci/<run-id> --kind verify
```

Use `--kind preview` or `--kind quality` with that run's original directory for
the other entry points. A forced host termination or unavailable Docker daemon
can prevent immediate cleanup; the next launch reports the outstanding resources.
Old failed runs remain failed after successful diagnostic retirement.

`python3 scripts/local_ci.py --profile lifecycle_failure` is a deliberate
failure exercise: one synthetic container exits 7 and an independent container
succeeds. The launcher must return nonzero, preserve both diagnostics and remove
both containers and the runner. This diagnostic profile is excluded from the
product's full gate registry and cannot serve as release evidence.

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

The [P0 decision](evidence/p0-feasibility.json) distinguishes feasibility from
product implementation. Current source and native/CPU build verification passed
the finite CI8 gates for the recorded inputs; product transactions,
source authorization, resource admission, lexical quality, index upgrades/rollback
and clean release installation retain their P1–P5 owners in `work-items.json`.

The separate [bounded bilingual quality workload](bounded-quality.md) runs
locally with act against a selected installed product image. It records frozen
public input hashes, per-query rankings and aggregate metrics without treating
measurement completion as quality or full CI acceptance.

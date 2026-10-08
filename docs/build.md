# Build and feasibility verification

The current code is a P0 investigation harness. It does not implement indexing an
ordinary source table, automatic source change capture, or the proposed product
search functions. Successful probes do not constitute a production release.

## Exact inputs

| Input | Selected value | Evidence boundary |
| --- | --- | --- |
| Rust | 1.96.0 | Combined engine/PG17 shared-library build verified; SQL runtime tracked separately |
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
reports. The workflow's presence is not a passing CI result; record each actual
run and code revision before advancing evidence status.

For intentionally destructive *disposable-cluster* experiments only:

```sh
docker build --file Dockerfile.p0 --build-arg P0_FAULTS=1 --tag pg-qdrant-p0-faults .
docker run --rm --memory=5g --cpus=2 pg-qdrant-p0-faults \
  bash crates/pg_qdrant/tests/run-faults.sh
```

Fault entry points are compiled out of a normal build. A successful fault test
may demonstrate collateral PostgreSQL session termination; that finding is
evidence against the tested isolation boundary, not a production pass.

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

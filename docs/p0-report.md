# P0 feasibility checkpoint

Observed: **2026-10-08**. Status: **P0 in progress; direct-worker isolation is insufficient; managed-helper comparison in progress; no overall go/no-go decision or release support**.

The published Edge dependency now builds, installs and executes through PostgreSQL 17 in a clean, non-root container. The recorded CI run passed 10 normal diagnostic checks and 12 private-fault checks, including real SQL-to-Edge retrieval, owner uniqueness, permissions, deadlines, caller cancellation and queue admission. It also established a negative architecture result: killing the direct PostgreSQL owner worker with SIGKILL or aborting its native engine thread terminated an unrelated SQL session. A previously committed PostgreSQL marker survived recovery. Passing these fault assertions records the failure domain; it does not establish adequate isolation.

The next topology experiment uses a Rust helper installed and managed with the same package. Its owned pipe protocol and standalone engine execution have local evidence; PostgreSQL integration and containment for that profile remain pending. Source-table indexing, the transactional outbox, committed-change tickets and the public search API remain unimplemented.

The starting repository revision is `f3b4cff09e3abc2aaaa3c647028aef4942d89636`. It contained the README, 54-capability contract, dependency policy and candidate-version baseline. [Engine evidence](evidence/p0-engine.json) records the current engine probe's source/lockfile hashes and results. [PostgreSQL CI evidence](evidence/p0-postgresql-ci.json) records the completed run and its precise checkout identity. [Dependency evidence](dependency-baseline.json) records component build status. Later local changes are reported separately and are not attributed to the earlier CI run.

| Recorded CI identity | Value |
| --- | --- |
| Run | [37716391262](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37716391262) |
| Implementation head | `f5bda3519421ef294bac82b17c17957f9727b648` |
| Tested pull-request merge checkout | `982adef4faaadd9831c2144b14bf155e89928ce0` |
| Identical head/checkout tree | `b296fccec91f2485b17d8c843fff90e30189622f` |

## What changed from the documentation baseline

| Area | Starting state | Current artifact and limits |
| --- | --- | --- |
| Product contract | Product/API sketches and 54 required capabilities | [Eight user journeys](user-journeys.md), [stage acceptance](acceptance.md), [architecture boundary ADR](adr/0001-embedded-engine-boundary.md), and [70 work items](work-items.json); proposed product SQL remains unimplemented |
| Dependencies | Candidate versions; no resolved project graph | Exact manifests, Cargo.lock, Rust 1.96.0, matching cargo-pgrx 0.19.3, explicit PG17/cshim and [resolved features/licenses](dependency-graph.json) |
| Retrieval | Fixed-release source review | Direct `qdrant-edge =0.8.0` library integration, compile sentinels, and actual BM25/vector/filter/fusion/grouping/persistence queries |
| PostgreSQL | Candidate owner-worker architecture | Restricted diagnostic SQL executes through a unique owner and bounded IPC; direct-worker failure affects companion sessions; optional managed-helper comparison is implemented and undergoing separate verification |
| Build and upgrades | Requirements only | [Build container](../Dockerfile.p0), successful normal/fault CI at the recorded tree, grouped dependency updates and manifest/lock/feature drift checks; new profile regression, index upgrade and rollback remain pending |
| Distribution | No project license or package | Apache-2.0 project license and notices with package license metadata; complete third-party review and installable release remain pending |

Applications are still intended to keep one PostgreSQL business-data write path. The extension will maintain the derived search indexes internally and asynchronously. This checkpoint has no source capture, durable outbox, committed-change ticket, production search function, or index-generation catalog.

## Reproducible checks and observed results

The recorded CI used an Ubuntu 24.04 x86_64 container, Rust 1.96.0, Qdrant Edge 0.8.0, pgrx/cargo-pgrx 0.19.3 and PostgreSQL 17.11. Runtime used UID 10001 with a 5 GiB container memory limit and two CPUs. These resource settings are execution inputs, not OOM tests or performance measurements. Earlier local PostgreSQL compilation used GCC 13, libclang 18.1.1 and ICU 74.2 headers. [Build instructions](build.md) describe the required toolchain, native libraries and selected `pg_config`; neither environment establishes a wider platform or CPU-support claim.

| Command/check | Observed result | Scope |
| --- | --- | --- |
| `cargo check --locked -p pg-qdrant-edge-probe` | Passed | Published public API sentinels and probe code type-check |
| Current `cargo test --locked -p pg-qdrant-edge-probe -- --test-threads=1` | **3 passed, 0 failed, 0 ignored** | Actual engine smoke, explicit-flush/SIGKILL recovery, and malformed copied-metadata rejection with intact-copy recovery; current source hashes in the engine evidence |
| `target/debug/pg-qdrant-edge-probe` | **16 check groups passed** | Eight synthetic points, two search threads; no quality or latency benchmark |
| `cargo check --locked -p pg_qdrant --no-default-features --features pg17` | Passed | Normal PostgreSQL Rust types, bindings and cshim; not final linking or SQL execution |
| `cargo check --locked -p pg_qdrant --no-default-features --features pg17,p0-fault-injection` | Passed | Private fault-feature compilation; no fault-experiment result |
| `cargo build --locked -p pg_qdrant --no-default-features --features pg17` | Passed | Normal x86-64 ELF shared library links against the selected PostgreSQL/Edge/pgrx build |
| Normal schema generation, command below | Passed | Nine entities: two schemas, six diagnostic functions and the final privilege block; reviewed defaults/volatility/parallel declarations and absence of a normal-build fault function |
| Normal local installation, command below | Passed | cargo-pgrx forwards locked mode and installs the control file, shared library and versioned SQL; the separate CI rows establish database execution |
| Private-fault schema generation with compilation, command below | Passed | Private-feature shared library links and seven functions are generated; the fault function remains absent from the installed normal SQL |
| `cargo pgrx --version` | `0.19.3` | Matching published CLI installed in locked mode and version checked |
| Normal Docker build and `run-p0.sh` at the recorded CI tree | **10 checks passed** | Actual `CREATE EXTENSION`, honest 54-ID discovery, ACL/runtime superuser checks, two-session ownership, deadlines, active/queued cancellation, bounded admission, 16 real Edge groups, and absence of the fault function |
| Private-fault Docker build and `run-faults.sh` at the recorded CI tree | **12 checks passed** | Normal diagnostic checks plus caught panic, worker SIGKILL and native abort; both process failures terminated the companion session and retained the committed PostgreSQL marker after recovery |
| `python3 scripts/check_contracts.py` | Passed | All 54 IDs and 70 work items retained; pins/checksums/defaults/resolved features and inventory hashes agree; unsupported completion claims remain false |
| Contract-validator negative cases | Rejected as required | Engine-only evidence cannot support a combined linked-build claim; incorrect PG feature/default-feature baselines fail validation |

The evidence files identify source inputs and normalized results. The current [dependency inventory](dependency-graph.json) reports its exact lockfile and selected-feature scopes; adding the helper/protocol workspace crates changes that inventory without retroactively changing the earlier CI tree. Package-declared license metadata is not a complete distribution-license audit. Native apt inputs are not fully locked even though the declared Docker environment built and ran successfully.

The schema and installation commands use the selected PostgreSQL 17 `pg_config`:

```sh
cargo pgrx schema --manifest-path crates/pg_qdrant/Cargo.toml \
  --pg-config "$PGRX_PG_CONFIG_PATH" --no-default-features --features pg17 \
  --cargo=--locked --skip-build --out pg_qdrant-p0-schema.sql
cargo pgrx install --manifest-path crates/pg_qdrant/Cargo.toml \
  --pg-config "$PGRX_PG_CONFIG_PATH" --no-default-features --features pg17 \
  --cargo=--locked
cargo pgrx schema --manifest-path crates/pg_qdrant/Cargo.toml \
  --pg-config "$PGRX_PG_CONFIG_PATH" --no-default-features \
  --features 'pg17 p0-fault-injection' --cargo=--locked \
  --out pg_qdrant-p0-fault-schema.sql
```

The schema output names above are reproducible destinations, not part of the binary compatibility contract. The private-feature cargo-pgrx command uses a quoted, space-separated feature list; a comma-separated list fails its schema PG-feature discovery in the tested CLI. Database execution is established by the separate normal and fault CI runs, rather than inferred from linking or copying files. Their disposable non-root clusters do not exercise a source-table search product or establish a production package.

## Direct-worker failure result and helper comparison

| Experiment in the recorded PostgreSQL CI | Observed behavior | Engineering consequence |
| --- | --- | --- |
| Caught Rust engine-thread panic | Diagnostic request fails; owner worker survives | Only this caught-panic path was demonstrated; arbitrary engine-state recovery is not implied |
| SIGKILL of the PostgreSQL owner worker | Companion SQL session terminates; PostgreSQL recovers; committed marker remains; a new owner starts | The shared-memory worker is an inadequate production default for native-failure containment |
| Native abort in the engine thread | Same companion-session termination and committed-marker recovery | Moving Edge to a thread within that worker does not create a process isolation boundary |
| Caller cancellation / statement timeout | Caller stops waiting; active native owner remains until completion; a cancelled queued request never executes | SQL cancellation works within the documented boundary; native in-flight cancellation remains unavailable |

The [architecture ADR](adr/0001-embedded-engine-boundary.md) therefore requires the managed-helper comparison before accepting a production topology. The direct-worker profile remains the default P0 control experiment. The optional helper keeps the PostgreSQL worker as supervisor for SQL permissions, queues and signals, while an installed Rust executable owns Edge after exec. It validates build/protocol/process/request identities, retains a separate process-lifetime ownership lock, stops on supervisor-pipe EOF and limits replacement attempts. A 125-second process-stop budget is distinct from native cancellation and from a future durable-index shutdown contract.

The [current standalone helper evidence](evidence/p0-helper-local.json) records **7 passing normal-profile checks and 8 passing private-fault checks**, bound to their source hashes. These include real 16-group Edge execution, owner contention, request/build identities, EOF, pure-controller SIGKILL, replacement ownership, replay and oversized-input refusal. A normal-profile attempt correctly rejected a private-feature handshake from a reused build directory; after cleaning that helper package and rebuilding, the complete normal suite passed. This failure and resolution are retained in the evidence. The PostgreSQL helper suite still needs a recorded result demonstrating that native helper failure preserves companion SQL sessions; standalone process tests cannot establish that property. Restart exhaustion, abnormal PostgreSQL supervisor death, OOM, disk behavior and the hard-stop policy retain their own evidence requirements.

The standalone commands, run separately for each feature build, are:

```sh
cargo build --locked -p pg-qdrant-helper
python3 crates/pg_qdrant-helper/tests/probe.py target/debug/pg_qdrant_p0_helper \
  --out artifacts/p0-helper-normal.json
cargo build --locked -p pg-qdrant-helper --features p0-fault-injection
python3 crates/pg_qdrant-helper/tests/probe.py target/debug/pg_qdrant_p0_helper \
  --faults --out artifacts/p0-helper-faults.json
```

These commands do not start PostgreSQL. The helper-enabled normal/private PostgreSQL feature combinations also pass local compilation checks; their actual SQL and containment results remain pending. The CI-only hard-stop experiment must stop an active helper and prove that the configured 125-second budget, owner termination and replacement behavior occur; authoring that experiment is not a passing result.

## Later local engine fault checks

The expanded locked engine suite passed **3 tests, 0 failed or ignored**: the existing smoke and flushed-SIGKILL tests plus the owned metadata-corruption/unsafe-disk-path test. The latter corrupts separate copies of explicitly flushed fixture metadata, requires both loads to fail, and reopens an intact recovery copy and the unchanged original with equal representation records and correct phrase/MaxSim results. It tests malformed `edge_config.json` and one segment JSON file; it is not general bit-rot detection or in-place repair. The [fault-probe source and bounds](../crates/edge-probe/README.md#standalone-fault-experiments) describe the owned directories and sparse-copy limits.

The positive ENOSPC experiment remains **not run** at this checkpoint. Its implementation permits only a dedicated, empty, bounded tmpfs distinct from PostgreSQL data; it rejects ordinary host directories and `/dev/shm`. A successful future run must observe actual errno 28, distinguish persisted configuration from in-memory settings, remove the owned filler, retry and reopen successfully. A missing configuration intentionally returns `not_run` with exit code 0, so acceptance must require a report with `status: "passed"`. A small tmpfs limits storage, not engine RSS, and does not count as an OOM experiment.

## Narrow engine behavior verified

| Behavior | Actual assertions | Remaining boundary |
| --- | --- | --- |
| Offline BM25 | Local document/query encoding, expected lexical hits and an explicit synthetic tenant corpus for IDF | No model downloads; complete per-field analyzer parity and real relevance judgments are pending |
| Dense and supplied sparse | Named dot-product vectors, separate learned sparse weights, expected leading IDs/scores and exclusion of the fixture's other tenant | Fixture sparse values are not a trained SPLADE/miniCOIL model or model-provenance check |
| Prefetch and MaxSim | Nested fusion recall followed by MaxSim, expected score 2 for the leading fixture, candidate budget 3 | Exactness applies to the candidate domain; no full-corpus top-k claim |
| Weighted RRF | Pinned-version weighted score formula and expected leading score | Tie policy, invalid combinations and complete parameter coverage remain pending |
| DBSF | All seven fused scores checked against independent analytical moments; singleton, constant-score, empty and all-empty cases | Candidate-distribution semantics only; no interchangeable raw-score scale |
| Text matching | AND/OR, Boolean exclusion, contiguous phrase, and phrase constraints in dense/BM25/learned fusion branches | Arbitrary field/array boundaries, slop, synonyms and controlled query-language acceptance remain pending |
| Prefixes and identifiers | Separate token-prefix and keyword-prefix indexes; keyword case sensitivity and whole-value exact IDs | Instant ranking, typo expansion, full Unicode limits and completion policy remain pending |
| Chinese/default analysis | A Chinese lexical hit plus English-default stemming versus explicitly neutral configuration | Small synthetic checks do not establish multilingual search quality, full normalization or source offsets |
| Grouping/counts | Three distinct document groups, bounded authorized source hydration, seven authorized points | Fixture filters are not PostgreSQL identity/RLS proof; global document totals are not claimed |
| Persistence | Clean explicit-flush reopen and a separate live-child SIGKILL after flush; reopened phrase, sparse, MaxSim and BM25 queries with expected permissions/scores | No unflushed-WAL replay, power-loss, PostgreSQL ACK atomicity, index-upgrade or timeline proof |

The [engine probe documentation](../crates/edge-probe/README.md) links the selected registry release's source for each behavior. The [engine test](../crates/edge-probe/src/lib.rs) and [abrupt recovery test](../crates/edge-probe/tests/persistence.rs) contain the actual fixtures and assertions.

## Findings that affect the architecture

**The direct embedded Rust route is executable.** Retrieval is supplied by the exact released crate, not by a network client or a replacement implementation. The remaining decision is how to package, own and isolate it safely inside the PostgreSQL product.

**BM25 and payload text analysis remain independent.** The probe explicitly disables the BM25 English defaults for its mixed-language fixture. A product analyzer policy still needs separate compilation into BM25/text/keyword settings, effective configuration hashes, field/array boundaries and Unicode offset parity. Sharing the upstream tokenizer is insufficient.

**Fusion semantics must remain versioned.** Edge 0.8.0 weighted RRF contributes `1 / ((p + 1) / w + k - 1)` for zero-based rank `p` and positive weight `w`. DBSF normalizes each candidate distribution using its sample mean and standard deviation before summing; the tests include degenerate cases. Public SQL options need their own validation and must not substitute unnormalized BM25/cosine/MaxSim addition.

**Grouping needs explicit provenance hydration.** The fixed Edge grouping path projects the group field rather than supplying all requested source payloads. The probe retrieves the bounded group sources through an additional authorized HasId query. The product must budget that work and recheck PostgreSQL source identity, version and permissions before returning snippets or source rows.

**Caller cancellation is not native cancellation.** The reviewed public query and optimize paths have no caller-provided cancellation token. The direct-worker SQL tests now demonstrate removal of cancelled queued work, caller timeout, and retention of active ownership until completion. They also demonstrate the unacceptable companion-session failure domain. The managed-helper comparison must preserve those queue and ownership properties while measuring a smaller native-failure domain; no cancellation result establishes durable shutdown of a production index.

**Flush evidence is deliberately narrow.** A flushed disposable shard survives SIGKILL and supports subsequent real queries. That does not establish recovery from unflushed Edge WAL or an atomic transaction with PostgreSQL WAL. Source outbox replay, durable ACK, primary-key incarnation and generation recovery are P1/P3 work.

**Native bit-vector input is absent from the audited public input variants.** Dense floating-point, sparse and multivector input are distinct from binary quantization of a supported vector. The capability contract retains this distinction; SQL input validation and its explicit unsupported-input response are still product work.

**CPU and lexical-library choices remain open gates.** The selected Edge C SIMD build uses Haswell-oriented compilation flags. The general x86_64 target triple alone does not prove broader CPU compatibility. F13–F20 remain formal scope. The [lexical gap ADR](adr/0002-lexical-gap-strategy.md) selects an isolated direct-Tantivy 0.26.2 experiment and compares it with fixed pg_search 0.26.0. Adoption remains pending measured quality, lifecycle, outer-fusion and license acceptance.

## Phase exit assessment

The exit IDs below are defined by the [acceptance contract](acceptance.md). Passing a narrow experiment does not complete every condition of its parent gate.

| P0 exit | Status | Passed portion | Work still required |
| --- | --- | --- | --- |
| P0-BUILD | Partial | Real lock/feature graph; exact tools; executable engine; normal/fault linked libraries and installation; recorded clean-container first database load | Current-source and helper-profile regression; complete native package/CPU/license acceptance |
| P0-API | Partial | Exhaustive public enum sentinels, typed advanced-query construction and mapped method references | Real supported input construction/calls for every mapped operation, negative cases and complete combination inventory |
| P0-ENGINE | Partial | Required initial BM25/dense/sparse/MaxSim/prefetch/fusion/filter/grouping paths and stronger persistence tests | Full declared invalid-input, parameter, score/tie and combination acceptance beyond the synthetic probe |
| P0-LEXICAL | Partial | Independent defaults, basic Chinese, phrase, Boolean and separate-prefix runtime checks; documented F13–F20 gaps | Full analyzer/offset/field/array tests, frozen real quality corpus and the measured adoption gates in the lexical gap ADR |
| P0-PG | Partial diagnostic runtime | Actual extension load, two SQL sessions, unique owner, runtime ACL, deadlines/cancellation/backpressure and real Edge calls in the recorded direct-worker CI | Helper-profile PostgreSQL regression; product array/vector shapes; source-identity/visible-result recheck prototype |
| P0-FAULT | Partial; direct isolation requirement fails | Flushed child SIGKILL/reopen; caught PostgreSQL-worker panic; worker SIGKILL/native-abort collateral and recovery; local malformed copied-metadata checks | Actual helper containment; abnormal supervisor death/restart exhaustion/hard-stop evidence; isolated OOM; positive bounded ENOSPC; dirty index and durable recovery behavior |
| P0-DECISION | Pending overall | Direct worker is not accepted as the production default; concrete packaged-helper comparison is required | Helper/lexical findings, remaining necessary gates, topology acceptance and a justified overall go/no-go ADR |

**No complete P0 exit is declared at this checkpoint.** P1–P5 and the source-table product remain pending. No capability is marked complete or release-supported merely because a probe exercises one part of it.

## Remaining work and estimate review

The completed direct-worker CI reduces uncertainty about installation and the diagnostic SQL/IPC path, and resolves the initial failure-domain question negatively. The managed-helper condition is now triggered. Full module estimates remain provisional until the helper's PostgreSQL/fault results support a topology decision. The ranges below retain the earlier P0 planning allocations; they are not measurements of time spent or a newly established estimate of remaining work.

| P0 stream | Accountable module | Retained planning allocation, engineering days | Evidence adjustment and remaining work |
| --- | --- | --- | --- |
| Clean container installation and first SQL load | Build / SQL | 1–4 | Initial direct-worker CI completed this diagnostic slice; current-source/profile regression and platform/license gates remain |
| Complete public-call/negative-input inventory | Engine / registry | 2–5 | Uses the already buildable published API; no internal-API fork |
| SQL ownership, deadlines, backpressure and failure observations | Worker / SQL / recovery | 4–10 | Direct-worker checks passed and exposed unacceptable collateral; helper and remaining bounded memory/storage faults require evidence |
| Analyzer parity, richer-lexical evaluation and selection ADR | Lexical / quality | 4–9 | Existing mature dependencies can be evaluated with licensed data and realistic queries |
| Managed-helper comparison | Worker / packaging | 4–10, formerly conditional | Condition is met; implementation and standalone execution reduce API uncertainty, while PostgreSQL containment and production lifecycle work remain |

These ranges are planning assumptions, not elapsed-time measurements or calendar commitments, and overlap shared integration work. They must not be added to the existing capability ranges without allocation. The original 5–10 day P0 investigation budget is a stop-loss budget, not an acceptance criterion or complete-product estimate.

The immediate critical path is: managed-helper PostgreSQL regression and measured failure domain; bounded memory/storage and lifecycle experiments; accepted process topology and completed necessary P0 exits. Source capture/outbox/backfill/wait then depend on that ownership and persistence contract. The full lexical decision is also required before the core Alpha. Every original requirement remains in the work ledger with separate probe evidence and product completion status.

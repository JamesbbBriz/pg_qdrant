# P0 feasibility checkpoint

Observed: **2026-10-08**. Status: **P0 in progress; no go/no-go decision and no release support**.

The published Edge dependency now compiles and runs the required initial retrieval paths in an independent Rust process. PostgreSQL 17 bindings and both normal/private-fault prototype feature sets pass `cargo check`. The normal PostgreSQL shared library links, schema generation succeeds, and the matching cargo-pgrx tool installs the control file, library and versioned SQL into the selected PostgreSQL installation. The explicitly enabled private-fault build also links and generates its additional diagnostic function. Actual `CREATE EXTENSION`, SQL runtime and PostgreSQL process-failure behavior have not yet been verified. This is useful embedded-engine and packaging evidence, not completion of the source-table indexing product.

The starting repository revision is `f3b4cff09e3abc2aaaa3c647028aef4942d89636`. It contained the README, 54-capability contract, dependency policy and candidate-version baseline. The current P0 source follows that revision. [Engine evidence](evidence/p0-engine.json) records exact source/lockfile hashes, commands and observed results; use those identities rather than treating the starting revision as the tested implementation. [Dependency evidence](dependency-baseline.json) records each component's separate build status.

## What changed from the documentation baseline

| Area | Starting state | Current artifact and limits |
| --- | --- | --- |
| Product contract | Product/API sketches and 54 required capabilities | [Eight user journeys](user-journeys.md), [stage acceptance](acceptance.md), [architecture boundary ADR](adr/0001-embedded-engine-boundary.md), and [70 work items](work-items.json); proposed product SQL remains unimplemented |
| Dependencies | Candidate versions; no resolved project graph | Exact manifests, Cargo.lock, Rust 1.96.0, matching cargo-pgrx 0.19.3, explicit PG17/cshim and [resolved features/licenses](dependency-graph.json) |
| Retrieval | Fixed-release source review | Direct `qdrant-edge =0.8.0` library integration, compile sentinels, and actual BM25/vector/filter/fusion/grouping/persistence queries |
| PostgreSQL | Candidate owner-worker architecture | Restricted diagnostic functions, dynamic worker, bounded Unix IPC, engine thread and disposable-cluster tests; normal linked library, generated SQL and local installation pass, SQL runtime is pending |
| Build and upgrades | Requirements only | [Build container](../Dockerfile.p0), normal/fault CI configuration, grouped dependency updates, manifest/lock/feature drift checks; container/CI execution and index upgrade/rollback remain unverified |
| Distribution | No project license or package | Apache-2.0 project license and notices with package license metadata; complete third-party review and installable release remain pending |

Applications are still intended to keep one PostgreSQL business-data write path. The extension will maintain the derived search indexes internally and asynchronously. This checkpoint has no source capture, durable outbox, committed-change ticket, production search function, or index-generation catalog.

## Reproducible checks and observed results

The execution environment used Linux x86_64, Rust 1.96.0, Qdrant Edge 0.8.0, and pgrx/cargo-pgrx 0.19.3. PostgreSQL compilation used released PostgreSQL 17.11 headers, GCC 13, libclang 18.1.1, and ICU 74.2 headers. These are observed inputs, not a wider supported-platform claim. [Build instructions](build.md) describe the required toolchain, native libraries and selected `pg_config`.

| Command/check | Observed result | Scope |
| --- | --- | --- |
| `cargo check --locked -p pg-qdrant-edge-probe` | Passed | Published public API sentinels and probe code type-check |
| `cargo test --locked -p pg-qdrant-edge-probe` | **2 passed, 0 failed, 0 ignored** | Actual engine smoke test and explicit-flush/SIGKILL recovery test; latest compilation includes the strengthened assertions |
| `target/debug/pg-qdrant-edge-probe` | **16 check groups passed** | Eight synthetic points, two search threads; no quality or latency benchmark |
| `cargo check --locked -p pg_qdrant --no-default-features --features pg17` | Passed | Normal PostgreSQL Rust types, bindings and cshim; not final linking or SQL execution |
| `cargo check --locked -p pg_qdrant --no-default-features --features pg17,p0-fault-injection` | Passed | Private fault-feature compilation; no fault-experiment result |
| `cargo build --locked -p pg_qdrant --no-default-features --features pg17` | Passed | Normal x86-64 ELF shared library links against the selected PostgreSQL/Edge/pgrx build |
| Normal schema generation, command below | Passed | Nine entities: two schemas, six diagnostic functions and the final privilege block; reviewed defaults/volatility/parallel declarations and absence of a normal-build fault function |
| Normal local installation, command below | Passed | cargo-pgrx forwards locked mode and installs the control file, shared library and versioned SQL; no database load or SQL execution implied |
| Private-fault schema generation with compilation, command below | Passed | Private-feature shared library links and seven functions are generated; the fault function remains absent from the installed normal SQL |
| `cargo pgrx --version` | `0.19.3` | Matching published CLI installed in locked mode and version checked |
| `python3 scripts/check_contracts.py` | Passed | All 54 IDs and 70 work items retained; pins/checksums/defaults/resolved features and inventory hashes agree; unsupported completion claims remain false |
| Contract-validator negative cases | Rejected as required | Engine-only evidence cannot support a combined linked-build claim; incorrect PG feature/default-feature baselines fail validation |

The engine evidence includes source hashes and normalized test results. The workspace lockfile contains 498 package entries; the selected Linux feature inventory contains 438 resolved nodes. These are different scopes, not two compilation counts. Package-declared license metadata is not a complete distribution-license audit. Native apt inputs are not fully locked and the Docker environment has not been executed at this checkpoint.

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

The schema output names above are reproducible destinations, not part of the binary compatibility contract. The private-feature cargo-pgrx command uses a quoted, space-separated feature list; a comma-separated list fails its schema PG-feature discovery in the tested CLI. A linked library and copied installation files do not establish that `CREATE EXTENSION`, worker startup, driver calls or cancellation work at runtime. The normal and fault runners create disposable non-root PostgreSQL clusters; their source is present, but no successful SQL/fault run is claimed here.

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

**Caller cancellation is not native cancellation.** The reviewed public query and optimize paths have no caller-provided cancellation token. The prototype is designed to remove cancelled queued work and keep a running job's ownership until it really finishes. Its process-lifetime lock survives normal returns and unwinding. Actual SQL cancellation, overload behavior and fault isolation are still unmeasured; a packaged helper remains a candidate if PostgreSQL-worker failure affects other sessions unacceptably.

**Flush evidence is deliberately narrow.** A flushed disposable shard survives SIGKILL and supports subsequent real queries. That does not establish recovery from unflushed Edge WAL or an atomic transaction with PostgreSQL WAL. Source outbox replay, durable ACK, primary-key incarnation and generation recovery are P1/P3 work.

**Native bit-vector input is absent from the audited public input variants.** Dense floating-point, sparse and multivector input are distinct from binary quantization of a supported vector. The capability contract retains this distinction; SQL input validation and its explicit unsupported-input response are still product work.

**CPU and lexical-library choices remain open gates.** The selected Edge C SIMD build uses Haswell-oriented compilation flags. The general x86_64 target triple alone does not prove broader CPU compatibility. F13–F20 remain formal scope. The [lexical gap ADR](adr/0002-lexical-gap-strategy.md) selects an isolated direct-Tantivy 0.26.2 experiment and compares it with fixed pg_search 0.26.0. Adoption remains pending measured quality, lifecycle, outer-fusion and license acceptance.

## Phase exit assessment

The exit IDs below are defined by the [acceptance contract](acceptance.md). Passing a narrow experiment does not complete every condition of its parent gate.

| P0 exit | Status | Passed portion | Work still required |
| --- | --- | --- | --- |
| P0-BUILD | Partial | Real lock/feature graph; exact tools; executable engine; normal/fault PG checks; normal linked library, schema generation and local installation | Clean container/first database load, native package/CPU/license completion |
| P0-API | Partial | Exhaustive public enum sentinels, typed advanced-query construction and mapped method references | Real supported input construction/calls for every mapped operation, negative cases and complete combination inventory |
| P0-ENGINE | Partial | Required initial BM25/dense/sparse/MaxSim/prefetch/fusion/filter/grouping paths and stronger persistence tests | Full declared invalid-input, parameter, score/tie and combination acceptance beyond the synthetic probe |
| P0-LEXICAL | Partial | Independent defaults, basic Chinese, phrase, Boolean and separate-prefix runtime checks; documented F13–F20 gaps | Full analyzer/offset/field/array tests, frozen real quality corpus and the measured adoption gates in the lexical gap ADR |
| P0-PG | Pending runtime | Normal and private-feature code checks; normal linked/installed diagnostic prototype; owned IPC/worker/guard implementation | Actual extension load, two SQL sessions, cancellation/backpressure, SQL shapes and source-identity/recheck experiment |
| P0-FAULT | Partial engine experiment only | Standalone child SIGKILL after explicit flush | PostgreSQL panic/native-crash/SIGKILL and companion-session observations; isolated OOM/disk-full; corruption and recovery outcomes |
| P0-DECISION | Pending | Product boundary and alternatives recorded | Process/lexical findings, measured failure domain, completed necessary gates and a justified go/no-go ADR |

**No complete P0 exit is declared at this checkpoint.** P1–P5 and the source-table product remain pending. No capability is marked complete or release-supported merely because a probe exercises one part of it.

## Remaining work and estimate review

The successful direct-engine tests reduce uncertainty about basic API integration. Public cancellation limits, grouping hydration and the lexical gaps make process ownership, result handling and FTS completion explicit work. Full module estimates remain provisional until the PostgreSQL runtime/failure experiment selects a topology.

| Remaining P0 stream | Accountable module | Provisional remaining engineering days | Assumption |
| --- | --- | --- | --- |
| Clean container installation and first SQL load | Build / SQL | 1–4 | Local normal linking/schema/install results reproduce with provisioned native dependencies |
| Complete public-call/negative-input inventory | Engine / registry | 2–5 | Uses the already buildable published API; no internal-API fork |
| SQL ownership, deadlines, backpressure and failure observations | Worker / SQL / recovery | 4–10 | Disposable process and bounded memory/storage test environments are available |
| Analyzer parity, richer-lexical evaluation and selection ADR | Lexical / quality | 4–9 | Existing mature dependencies can be evaluated with licensed data and realistic queries |
| Helper-process prototype, if worker results require it | Worker / packaging | 4–10 additional, conditional | Preserves one-package installation; lifecycle and protocol are reused where demonstrated safe |

These ranges are planning assumptions, not elapsed-time measurements or calendar commitments, and overlap shared integration work. They must not be added to the existing capability ranges without allocation. The original 5–10 day P0 investigation budget is a stop-loss budget, not an acceptance criterion or complete-product estimate.

The immediate critical path is: disposable SQL execution of the installed prototype; observed worker/instance failure domain; selected process topology; completed necessary P0 exits. Source capture/outbox/backfill/wait then depend on that ownership and persistence contract. The full lexical decision is also required before the core Alpha. Every original requirement remains in the work ledger with separate probe evidence and product completion status.

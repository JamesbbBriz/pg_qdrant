# P0 feasibility checkpoint

Observed: **2026-10-08**. Status: **P0 in progress; managed helper is the preferred topology candidate after scoped PostgreSQL fault tests; narrow ENOSPC recovery passed, but the latest private-helper regression failed before SQL; no overall go/no-go decision or release support**.

The published Edge dependency builds, installs and executes through PostgreSQL 17 in clean, non-root containers. The helper comparison run **37720263848** passed all four PostgreSQL diagnostic profiles: direct-worker normal/private-fault checks **10/12**, and managed-helper normal/private-fault checks **10/15**. Helper SIGKILL and native abort preserved the companion SQL session, the same PostgreSQL supervisor and a committed PostgreSQL marker. The direct-worker control reproduced companion-session termination for the corresponding failures. These results support the helper as the preferred candidate; they do not establish a production isolation or durability contract.

The same run exercised the helper's 125-second process-stop budget, supervisor SIGTERM/pipe-EOF cleanup, exclusive replacement and bounded restart exhaustion. Its standalone pipe suites passed **7 normal and 8 private-fault checks**. The overall workflow nevertheless **failed**: the provisioned 32 MiB tmpfs experiment returned no JSON report. The child exit code was not retained by that version of the wrapper, so that failure's exact cause remains unknown. Forced PostgreSQL-supervisor SIGKILL, actual OOM, source-table indexing, the transactional outbox, committed-change tickets and the public search API remain unverified or unimplemented.

The third [disk retry CI](evidence/p0-disk-ci.json), run **37724223470** at head `cce2b41`, also failed, this time with captured diagnostics: the filler misclassified a genuine ENOSPC error because `NamedTempFile` added path context that hid the raw OS error code. The run reached `fill`, but never reached configuration-save or recovery checks; **all four PostgreSQL SQL profiles were skipped** on this head. The direct-file write correction was subsequently exercised in the fourth CI run described below. This diagnosed second disk failure does not explain the earlier missing-report failure.

The starting repository revision is `f3b4cff09e3abc2aaaa3c647028aef4942d89636`. It contained the README, 54-capability contract, dependency policy and candidate-version baseline. [Engine evidence](evidence/p0-engine.json) records engine probe source/lockfile hashes and results. [Initial PostgreSQL CI evidence](evidence/p0-postgresql-ci.json), [helper comparison CI evidence](evidence/p0-helper-ci.json) and [disk retry evidence](evidence/p0-disk-ci.json) preserve those runs and their precise checkout identities. The [fourth regression record](evidence/p0-regression-ci.json) separately retains the narrow disk success and private-helper preflight failure. [Dependency evidence](dependency-baseline.json) records component build status. Subsequent changes do not inherit earlier runs' results.

| Recorded CI identity | Initial direct-worker run | Helper comparison run |
| --- | --- | --- |
| Run | [37716391262](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37716391262) | [37720263848](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37720263848) |
| Job | `113113694758` | `113126054532` |
| Implementation head | `f5bda3519421ef294bac82b17c17957f9727b648` | `a22d3c73ad838ff606a080345b65d634f5144c39` |
| Tested pull-request merge checkout | `982adef4faaadd9831c2144b14bf155e89928ce0` | `6584812ae40f8850c1d43658ccbdfba184e1846d` |
| Identical head/checkout tree | `b296fccec91f2485b17d8c843fff90e30189622f` | `9db0bd4a20780be31b07b018fdbc0d029983e5ab` |
| Workflow conclusion | Passed | Failed at the provisioned disk experiment; all four PostgreSQL profiles passed |

The subsequent disk retry was run **37724223470**, job **113138590878**, at head `cce2b41d1399424e845e135fac9e87df51764d13`, merge checkout `63340ba610236a6fe237ccf1bc9976493ea4a6eb` and identical tree `22b98d60a653b3f7521f48a6dda6bff38bb3e32f`. Its normal image build/extension installation and four engine tests passed; its disk step failed before any PostgreSQL runtime profile executed. The [normalized record](evidence/p0-disk-ci.json) retains the native report, reached stages and skipped profiles.

The fourth run **37725563600**, job **113142806882**, tested head `1f9d8bb9de82f07a6d9440d8461d86505abf21c2`, merge checkout `773889cdb1b6a840ae9a35fc9b3bb1dde798ccd5` and identical tree `1d7e4a6582eabb9d0e32cdb14ff2b22c7a3584e4`. All four images built; the historical four-test engine suite passed. The narrow 32 MiB ENOSPC experiment, direct normal/private SQL **10/12**, normal-helper SQL **10**, and normal-helper pipe **7** checks passed. The workflow **failed** after six private-helper pipe checks: the controller-SIGKILL cleanup observation raised `ProcessLookupError` (ESRCH) while reading `/proc/<pid>/stat`. That check was attempted but incomplete; the following private native-abort pipe check and private-helper PostgreSQL suite were not reached. This result is narrower than the earlier complete four-profile runtime record.

The current local engine record now passes **15 tests, 0 failed or ignored**, after the advanced-query, lifecycle and schema probes were integrated. Two real-child bookkeeping tests also pass. Current normal/private standalone helper reruns each pass six checks and then fail the stricter live-PID observation precondition before controller SIGKILL. The current-source helper PostgreSQL regression, newly added PostgreSQL-supervisor SIGKILL test and separate 128 MiB full-text ENOSPC experiment await the next CI; no result is inferred from their implementation.

## What changed from the documentation baseline

| Area | Starting state | Current artifact and limits |
| --- | --- | --- |
| Product contract | Product/API sketches and 54 required capabilities | [Eight user journeys](user-journeys.md), [stage acceptance](acceptance.md), [architecture boundary ADR](adr/0001-embedded-engine-boundary.md), and [70 work items](work-items.json); proposed product SQL remains unimplemented |
| Dependencies | Candidate versions; no resolved project graph | Exact manifests, Cargo.lock, Rust 1.96.0, matching cargo-pgrx 0.19.3, explicit PG17/cshim and [resolved features/licenses](dependency-graph.json) |
| Retrieval | Fixed-release source review | Direct `qdrant-edge =0.8.0` library integration, compile sentinels, and actual BM25/vector/filter/fusion/grouping/persistence queries |
| PostgreSQL | Candidate owner-worker architecture | Restricted diagnostic SQL executes through a unique owner and bounded IPC; helper native-failure containment and bounded process lifecycle pass the named tests; forced PG-supervisor SIGKILL, OOM and production indexing remain open |
| Build and upgrades | Requirements only | [Build container](../Dockerfile.p0), four PostgreSQL profiles passing at the helper comparison tree, grouped dependency updates and manifest/lock/feature drift checks; the fourth run passed three SQL profiles but failed private-helper preflight; current-source regression and index upgrade/rollback remain pending |
| Distribution | No project license or package | Apache-2.0 project license and notices with package license metadata; complete third-party review and installable release remain pending |

Applications are still intended to keep one PostgreSQL business-data write path. The extension will maintain the derived search indexes internally and asynchronously. This checkpoint has no source capture, durable outbox, committed-change ticket, production search function, or index-generation catalog.

## Reproducible checks and observed results

The recorded CI runs used Ubuntu 24.04 x86_64 containers, Rust 1.96.0, Qdrant Edge 0.8.0, pgrx/cargo-pgrx 0.19.3 and PostgreSQL 17.11. Runtime used UID 10001 with a 5 GiB container memory limit and two CPUs. These resource settings are execution inputs, not OOM tests or performance measurements. Earlier local PostgreSQL compilation used GCC 13, libclang 18.1.1 and ICU 74.2 headers. [Build instructions](build.md) describe the required toolchain, native libraries and selected `pg_config`; neither environment establishes a wider platform or CPU-support claim.

| Command/check | Observed result | Scope |
| --- | --- | --- |
| `cargo check --locked -p pg-qdrant-edge-probe` | Passed | Published public API sentinels and probe code type-check |
| Current local `cargo test --locked -p pg-qdrant-edge-probe -- --test-threads=1` | **15 passed, 0 failed, 0 ignored** | Smoke, flushed recovery, corruption and clean disk reopen, plus seven advanced-query and four lifecycle/schema tests; current tested source hashes are recorded in the engine evidence |
| `target/debug/pg-qdrant-edge-probe` | **16 check groups passed** | Eight synthetic points, two search threads; no quality or latency benchmark |
| `cargo check --locked -p pg_qdrant --no-default-features --features pg17` | Passed | Normal PostgreSQL Rust types, bindings and cshim; not final linking or SQL execution |
| `cargo check --locked -p pg_qdrant --no-default-features --features pg17,p0-fault-injection` | Passed | Private fault-feature compilation; no fault-experiment result |
| `cargo build --locked -p pg_qdrant --no-default-features --features pg17` | Passed | Normal x86-64 ELF shared library links against the selected PostgreSQL/Edge/pgrx build |
| Normal schema generation, command below | Passed | Nine entities: two schemas, six diagnostic functions and the final privilege block; reviewed defaults/volatility/parallel declarations and absence of a normal-build fault function |
| Normal local installation, command below | Passed | cargo-pgrx forwards locked mode and installs the control file, shared library and versioned SQL; the separate CI rows establish database execution |
| Private-fault schema generation with compilation, command below | Passed | Private-feature shared library links and seven functions are generated; the fault function remains absent from the installed normal SQL |
| `cargo pgrx --version` | `0.19.3` | Matching published CLI installed in locked mode and version checked |
| Direct-worker normal Docker build and `run-p0.sh`, first two CI trees | **10 checks passed in each run** | Actual `CREATE EXTENSION`, honest 54-ID discovery, ACL/runtime superuser checks, two-session ownership, deadlines, active/queued cancellation, bounded admission, 16 real Edge groups, and absence of the fault function |
| Direct-worker private-fault Docker build and `run-faults.sh`, first two CI trees | **12 checks passed in each run** | Caught panic, worker SIGKILL and native abort; both process failures terminated the companion session and retained the committed PostgreSQL marker after recovery |
| Managed-helper normal Docker build and `run-p0.sh`, helper comparison tree | **10 checks passed** | Normal diagnostic contract passes with distinct supervisor/engine processes; normal build has no private fault function |
| Managed-helper private-fault Docker build and `run-faults.sh`, helper comparison tree | **15 checks passed** | Native helper crash containment, exact configured process-stop path, supervisor SIGTERM/EOF cleanup and four-start/three-restart exhaustion, in addition to the shared diagnostics |
| Standalone helper pipe suites in the helper comparison containers | **7 normal / 8 private-fault checks passed** | Real Edge execution and bounded protocol/lifecycle checks; pure-controller SIGKILL is distinct from PostgreSQL-supervisor SIGKILL |
| Provisioned 32 MiB tmpfs via `scripts/run_disk_probe.py`, helper comparison tree | **Failed; workflow exit 1** | Child returned no JSON and stderr was empty; its exit code/signal was not recorded. Actual ENOSPC behavior and cause remain unproven |
| Provisioned 32 MiB tmpfs, disk retry tree `cce2b41` | **Failed; child exit 1 at `fill`** | Path-context error wrapping hid errno 28 from the filler classification. Configuration save and recovery were not reached; no signal or timeout was reported |
| Four PostgreSQL SQL profiles, disk retry tree `cce2b41` | **Skipped after disk failure** | The earlier normal image build/installation and four engine tests passed; this does not establish SQL runtime for this head |
| `python3 scripts/check_contracts.py` | Passed | All 54 IDs and 70 work items retained; pins/checksums/defaults/resolved features and inventory hashes agree; unsupported completion claims remain false |
| Contract-validator negative cases | Rejected as required | Engine-only evidence cannot support a combined linked-build claim; incorrect PG feature/default-feature baselines fail validation |

The evidence files identify source inputs and normalized results. The [dependency inventory](dependency-graph.json) reports its exact lockfile and selected-feature scopes. The helper/protocol crates were exercised at the second tree; future fixes require their own evidence. Package-declared license metadata is not a complete distribution-license audit. Native apt inputs are not fully locked even though the declared Docker environments built and ran the diagnostic profiles successfully.

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

## Measured process comparison

| Direct-worker experiment, reproduced in both PostgreSQL CI runs | Observed behavior | Engineering consequence |
| --- | --- | --- |
| Caught Rust engine-thread panic | Diagnostic request fails; owner worker survives | Only this caught-panic path was demonstrated; arbitrary engine-state recovery is not implied |
| SIGKILL of the PostgreSQL owner worker | Companion SQL session terminates; PostgreSQL recovers; committed marker remains; a new owner starts | The shared-memory worker is an inadequate production default for native-failure containment |
| Native abort in the engine thread | Same companion-session termination and committed-marker recovery | Moving Edge to a thread within that worker does not create a process isolation boundary |
| Caller cancellation / statement timeout | Caller stops waiting; active native owner remains until completion; a cancelled queued request never executes | SQL cancellation works within the documented boundary; native in-flight cancellation remains unavailable |

The [architecture ADR](adr/0001-embedded-engine-boundary.md) now selects the managed helper as the preferred candidate based on the exercised failures. The direct-worker profile remains the default P0 control experiment in the code; no production default is accepted. The optional helper keeps the PostgreSQL worker as supervisor for SQL permissions, queues and signals, while an installed Rust executable owns Edge after exec. It validates build/protocol/process/request identities, retains a separate process-lifetime ownership lock, stops on supervisor-pipe EOF and limits replacement attempts.

| Managed-helper experiment in run 37720263848 | Observed behavior | Remaining boundary |
| --- | --- | --- |
| Helper SIGKILL and native abort | Companion SQL session completes, the same PostgreSQL supervisor survives, committed marker remains and a distinct replacement helper starts | Applies to the named native-owner failures; no kernel OOM or production-index recovery claim |
| Active helper frozen with SIGSTOP; caller then cancelled | Active ownership is retained until the configured 125-second budget kills that helper with SIGKILL; replacement starts; recorded stop reason is `execution_budget`; the same supervisor and a 130-second companion SQL query survive | Process termination, not native Edge cancellation or graceful durable shutdown; observed remaining wait was approximately 125.36 seconds |
| PostgreSQL supervisor SIGTERM | Pipe EOF stops the old helper; a replacement supervisor obtains a ready owner | Forced PostgreSQL-supervisor SIGKILL and its instance-level effects remain untested |
| Repeated helper termination | Four total starts / three automatic restarts; supervisor survives; exhaustion is visible and unavailable requests are refused | No unlimited restart loop; product generation validation and durable replay are not implemented |

The historical [standalone helper evidence](evidence/p0-helper-local.json) records **7 passing normal-profile checks and 8 passing private-fault checks**, bound to their source hashes; the second CI independently repeated both complete suites. Later local reruns exposed a missing live-PID visibility precondition in the original observer, so the earlier local controller-SIGKILL cleanup result cannot independently prove that gate. The stricter current local normal/private reruns each pass six checks and then fail before sending controller SIGKILL. The second CI remains separate observed evidence; the fourth CI private suite instead failed during cleanup observation after six checks. The complete historical suites include real 16-group Edge execution, owner contention, request/build identities, EOF, pure-controller SIGKILL, replacement ownership, replay and oversized-input refusal. A normal-profile attempt correctly rejected a private-feature handshake from a reused build directory; after cleaning that helper package and rebuilding, the complete normal suite passed. This failure and resolution remain in the local evidence. PostgreSQL containment is established only by the separately recorded SQL experiments above. Neither the pure-controller SIGKILL test nor PostgreSQL supervisor SIGTERM proves forced PostgreSQL-supervisor SIGKILL behavior.

The standalone commands, run separately for each feature build, are:

```sh
cargo build --locked -p pg-qdrant-helper
python3 crates/pg_qdrant-helper/tests/probe.py target/debug/pg_qdrant_p0_helper \
  --out artifacts/p0-helper-normal.json
cargo build --locked -p pg-qdrant-helper --features p0-fault-injection
python3 crates/pg_qdrant-helper/tests/probe.py target/debug/pg_qdrant_p0_helper \
  --faults --out artifacts/p0-helper-faults.json
```

These commands do not start PostgreSQL. The [build instructions](build.md) and [PostgreSQL prototype](../crates/pg_qdrant/README.md) describe the separate helper-enabled container profiles used for the actual SQL and fault results. Compilation, standalone pipe tests and PostgreSQL fault execution retain separate evidence scopes.

## Engine fault checks and disk evidence

The historical four-test engine suite passed in the fourth CI. The current locked engine suite passes **15 tests, 0 failed or ignored**: those four tests plus seven advanced-query tests, two lifecycle tests and two dynamic-schema tests. The additions check analytical scores and typed input failures, conditional point/vector/payload mutations, filtered reads/facets/matrices, dynamic payload-index and named-vector operations, and explicit-flush reopen. They remain engine feasibility fixtures, not SQL product or full-capability acceptance. The metadata-corruption test damages separate copies of explicitly flushed fixture metadata, requires both loads to fail, and reopens an intact recovery copy and the unchanged original with equal representation records and correct phrase/MaxSim results. It tests malformed `edge_config.json` and one segment JSON file; it is not general bit-rot detection or in-place repair. The [fault-probe source and bounds](../crates/edge-probe/README.md#standalone-fault-experiments) describe the owned directories and sparse-copy limits.

The positive ENOSPC experiment was **attempted and failed** in run 37720263848 on a dedicated, empty 32 MiB tmpfs owned by the test user. The wrapper reported `Probe did not return a JSON report`; stderr was empty and the wrapper exited 1, making the overall workflow fail after all PostgreSQL profiles passed. That wrapper did not retain the child return code or signal, so neither ENOSPC, SIGBUS nor OOM can be assigned as the exit cause from this record. The missing report is a failed experiment, not `not_run` or a passed disk-failure assertion.

The [fixed-release source and ordinary-filesystem measurements](../crates/edge-probe/README.md#why-the-small-disk-fixture-excludes-mutable-text-indexes) establish a fixture-sizing problem: writable mutable-text reopening eagerly populates backing pages. Public optimization did not remove that requirement. These findings do not establish the historical child signal. The revised disk-only fixture retains eight source rows, all four representations and tenant/document/SKU keyword indexes, with cold payload/vector storage. It explicitly omits both mutable text indexes. Its clean reopen, complete record/vector equality, authorized keyword-prefix and exact MaxSim assertions pass locally. Full phrase/ENOSPC/reopen remains an open combination gate, and every phrase assertion in the separate corruption and flushed-SIGKILL tests remains intact.

The narrow fixture was then attempted in [run 37724223470](evidence/p0-disk-ci.json). Fixture creation, index creation, upsert, flush and the initial query completed, but the filler returned `No space left on device (os error 28)` wrapped with path context. In the pinned tempfile implementation, `NamedTempFile`'s `Write` wrapper creates an outer `io::Error` whose `raw_os_error()` no longer exposes errno 28. The probe therefore rejected a genuine disk-full condition as a non-ENOSPC error and exited 1 at `fill`. Configuration save, failure recovery and all four later SQL profiles were not reached. This is a diagnosed error-classification defect in the probe, not a successful engine ENOSPC/recovery test.

The correction writes through the same owned file using `filler.as_file_mut().write`, preserving raw errno classification and the existing byte/time/device and cleanup guards. The fourth CI passed the corrected narrow experiment: filling returned real errno 28, configuration save failed with ENOSPC, persisted configuration remained unchanged, and in-memory configuration was observed unchanged in that run. After removing the owned filler, configuration retry, explicit flush, reopen, all eight records/four representations and keyword/tenant/MaxSim queries passed. Phrase recovery was not tested by this fixture. A separate 128 MiB full-text fixture is now implemented but its positive ENOSPC run remains pending; the full mutable-text combination gate stays open. Both historical failures remain recorded. The experiment still permits only a dedicated, empty, bounded tmpfs distinct from PostgreSQL data and refuses ordinary host directories or `/dev/shm`. A missing configuration can return `not_run` with exit code 0, so acceptance requires an actual report with `status: "passed"`. A small tmpfs limits storage, not engine RSS, and is not an OOM experiment.

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

**Result projections need enforcement in the adapter.** The new root-MMR fixture confirms that Edge 0.8.0 returns its named vector even when `with_vector` is false. The SQL adapter must strip unrequested vectors and validate the requested projection. The same fixture verifies bounded MMR selection and original Dot score return values; leaf-prefetch MMR lowering is only source-reviewed. The [advanced tests](../crates/edge-probe/tests/advanced.rs) and work ledger retain this compatibility requirement.

**Caller cancellation is not native cancellation.** The reviewed public query and optimize paths have no caller-provided cancellation token. Both process profiles demonstrate removal of cancelled queued work, caller timeout, and retention of active ownership. The helper additionally demonstrates killing a frozen owner only when its independent 125-second process budget expires, then replacing it without terminating the companion SQL session or supervisor. This is a measured smaller native-failure domain for the exercised faults; it does not establish durable shutdown of a production index.

**Flush evidence is deliberately narrow.** A flushed disposable shard survives SIGKILL and supports subsequent real queries. That does not establish recovery from unflushed Edge WAL or an atomic transaction with PostgreSQL WAL. Source outbox replay, durable ACK, primary-key incarnation and generation recovery are P1/P3 work.

**Native bit-vector input is absent from the audited public input variants.** Dense floating-point, sparse and multivector input are distinct from binary quantization of a supported vector. The capability contract retains this distinction; SQL input validation and its explicit unsupported-input response are still product work.

**CPU and lexical-library choices remain open gates.** The selected Edge C SIMD build uses Haswell-oriented compilation flags. The general x86_64 target triple alone does not prove broader CPU compatibility. F13–F20 remain formal scope. The [lexical gap ADR](adr/0002-lexical-gap-strategy.md) selects an isolated direct-Tantivy 0.26.2 experiment and compares it with fixed pg_search 0.26.0. Adoption remains pending measured quality, lifecycle, outer-fusion and license acceptance.

## Phase exit assessment

The exit IDs below are defined by the [acceptance contract](acceptance.md). Passing a narrow experiment does not complete every condition of its parent gate.

| P0 exit | Status | Passed portion | Work still required |
| --- | --- | --- | --- |
| P0-BUILD | Partial | Real lock/feature graph; exact tools; executable engine; all four PostgreSQL profiles build, install and execute at the second recorded tree | Complete current-source regression after private-helper observer correction; native package/CPU/license acceptance |
| P0-API | Partial | Exhaustive matches for the enumerated public types, typed advanced inputs and mapped method references | Real supported input construction/calls for every mapped operation, negative cases and complete combination inventory |
| P0-ENGINE | Partial | Required initial BM25/dense/sparse/MaxSim/prefetch/fusion/filter/grouping paths and stronger persistence tests | Full declared invalid-input, parameter, score/tie and combination acceptance beyond the synthetic probe |
| P0-LEXICAL | Partial | Independent defaults, basic Chinese, phrase, Boolean and separate-prefix runtime checks; documented F13–F20 gaps | Full analyzer/offset/field/array tests, frozen real quality corpus and the measured adoption gates in the lexical gap ADR |
| P0-PG | Partial diagnostic runtime | At the helper comparison tree, all four profiles prove extension load, two SQL sessions, unique owner, diagnostic ACL, deadlines/cancellation/backpressure and real Edge calls | Current-source SQL regression after the failed private-helper pipe preflight; product array/vector shapes; source-identity/visible-result recheck prototype; complete source permissions |
| P0-FAULT | Partial; scoped helper faults and narrow disk recovery pass | Flushed child SIGKILL/reopen; caught panic; direct-worker collateral; helper crash containment, actual 125-second stop, supervisor SIGTERM/EOF and restart exhaustion; malformed copied-metadata checks | Current helper regression and forced PostgreSQL-supervisor SIGKILL; isolated OOM; full-text ENOSPC combination; dirty index and durable recovery behavior |
| P0-DECISION | Pending overall | Direct worker is not accepted as production default; measured helper comparison supports the helper as preferred candidate | Remaining memory/storage, persistence and lexical findings; topology acceptance and a justified overall go/no-go ADR |

**No complete P0 exit is declared at this checkpoint.** P1–P5 and the source-table product remain pending. No capability is marked complete or release-supported merely because a probe exercises one part of it.

## Remaining work and estimate review

The first two runs reduce uncertainty about installation, diagnostic SQL/IPC and the exercised native-failure boundary. Helper containment and bounded process lifecycle have PostgreSQL evidence. The third run diagnosed the narrow disk probe's errno-classification defect. The fourth passed narrow configuration recovery and three SQL profiles, then failed the private-helper pipe observer before that SQL profile ran. Current local engine coverage reaches 15 tests; current helper runtime regression remains pending. Full module estimates remain provisional until the remaining memory/storage, persistence and lexical gates support a topology decision. The ranges below retain the earlier P0 planning allocations; they are not measurements of time spent or a newly established estimate of remaining work.

| P0 stream | Accountable module | Retained planning allocation, engineering days | Evidence adjustment and remaining work |
| --- | --- | --- | --- |
| Clean container installation and first SQL load | Build / SQL | 1–4 | All four profiles completed this diagnostic slice at the tested tree; future-source regression and platform/license gates remain |
| Complete public-call/negative-input inventory | Engine / registry | 2–5 | Uses the already buildable published API; no internal-API fork |
| SQL ownership, deadlines, backpressure and failure observations | Worker / SQL / recovery | 4–10 | Both layouts pass diagnostics; helper native faults and bounded lifecycle pass; forced PG-supervisor SIGKILL, OOM, private-helper regression and full-text ENOSPC remain |
| Analyzer parity, richer-lexical evaluation and selection ADR | Lexical / quality | 4–9 | Existing mature dependencies can be evaluated with licensed data and realistic queries |
| Managed-helper comparison | Worker / packaging | 4–10, formerly conditional | Required comparison now has scoped PostgreSQL fault evidence; helper is preferred, with resource isolation and production lifecycle still open |

These ranges are planning assumptions, not elapsed-time measurements or calendar commitments, and overlap shared integration work. They must not be added to the existing capability ranges without allocation. The original 5–10 day P0 investigation budget is a stop-loss budget, not an acceptance criterion or complete-product estimate.

The immediate critical path is: complete the corrected helper observer and current-source SQL regression; verify the open full-text/ENOSPC combination; bounded memory and remaining lifecycle experiments; accepted process topology and completed necessary P0 exits. Source capture/outbox/backfill/wait then depend on that ownership and persistence contract. The full lexical decision is also required before the core Alpha. Every original requirement remains in the work ledger with separate probe evidence and product completion status.

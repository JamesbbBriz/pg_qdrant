# Stage acceptance and release gates

Status: required acceptance with source-bound diagnostic evidence. The latest
recorded CI6 ran all four PostgreSQL profiles successfully but failed both
128 MiB full-text disk experiments. New identity/recheck, capacity and OOM
changes require their own verification. No complete P0 exit or release is
declared. The [capability contract](capabilities.md), [work ledger](work-items.json)
and [user journeys](user-journeys.md) retain every formal requirement; the
[P0 checkpoint](p0-report.md) separates executed results from proposed product SQL.

## Evidence rules

Each passing result must identify the code revision, exact dependencies/lockfile, build features, platform/PG major, fixture revision, command, observed result, and relevant limits. A failing or blocked test remains visible. A source citation, written test, successful dependency resolution, and passing engine runtime are different evidence.

Capability progression is:

1. `source/API-reviewed`: a fixed-version public API and behavior contract are inspected, or an upstream absence is recorded.
2. `compile-verified`: a real call/configuration compiles with the selected release and features; existence of a type name alone is insufficient.
3. `engine-runtime-verified`: the operation has reproducible expected behavior in the embedded engine.
4. `SQL-integration-verified`: input validation, authorization, transaction/source rules, cancellation and results pass through PostgreSQL.
5. `release-supported`: the declared platform package, example, upgrade behavior, quality checks and operational boundaries also pass.

Absence of a native upstream primitive is not a passed SQL feature. A compile-time exhaustive inventory can prove that the absence was reviewed; an alternative needs its own evidence chain. A capability can be partially implemented, but unsupported subfeatures and combinations stay explicit.

Evidence artifacts contain reproducible fixtures, assertions, normalized summaries, and commands. Committed build reports exclude credentials and transient workstation data. Build reports must be regenerated for the current code revision before release.

## P0 — Embedded feasibility

P0 is a decision gate. A 5–10 engineering-day investigation budget is a stop-loss assumption, not a complete-product estimate. Budget exhaustion does not make a failed gate pass. Subsequent stages depend on a documented viable architecture or an explicitly costed replacement.

| Gate | Required artifact and acceptance |
| --- | --- |
| P0-BUILD | Real manifests; Cargo.lock; exact compiler; matching pgrx/cargo-pgrx; explicit PG17/features; repeatable locked Linux build; fixed container/system and link inputs; PG headers/pg_config; CPU baseline; actual dependency tree and license inventory |
| P0-API | Exhaustive public query/match/vector/storage/payload/read/update/lifecycle inventory against the selected published Edge crate, mapped to F/V/Q/L IDs or an explicit scope exclusion; callable compile probes and absent/private API decisions |
| P0-ENGINE | Runtime BM25, dense, learned-shape sparse, MaxSim, prefetch, fusion, structured filter, and grouping tests, including wrong-input cases and actual result assertions |
| P0-LEXICAL | Independent BM25/text configuration, Chinese/mixed-language defaults, AND/OR, phrase positions, token prefix versus keyword prefix, fields/arrays, identifier exactness, and an F13–F20 gap/lexical-library ADR |
| P0-PG | A loadable minimal pgrx extension with SQL backend to unique engine-owner request/response; two sessions; owned serialization; type/shape validation; deadlines/cancellation; bounded queue and response; source identity and visible-result recheck prototype |
| P0-FAULT | Disposable-cluster experiments for Rust panic, native process crash, SIGKILL, constrained-memory/OOM and disk-full/I/O failure; observe affected PostgreSQL sessions, instance restart, owner reacquisition, on-disk state and recovery |
| P0-DECISION | Go/no-go ADR cites the preceding evidence, accepts a process topology, records unavailable capabilities and alternatives, and revises work/critical-path estimates |

The production process choice remains open until the necessary fault, resource
and persistence evidence supports it. [CI6](evidence/p0-capacity-and-sql-ci.json)
at head `d843706` passed SQL 13/15/13/19 across direct-normal/private and
helper-normal/private, plus 7/8 standalone helper checks. The workflow failed
at the independent full-text disk experiments. Prior failed or skipped slices
remain in the [historical records](p0-report.md#retained-historical-outcomes).

These distinctions remain mandatory:

- Direct-worker SIGKILL/native abort terminate companion SQL, while native helper
  SIGKILL/abort preserve the companion and PostgreSQL supervisor. Passing the
  direct fault assertion records an unacceptable production failure domain.
- Actual PostgreSQL-supervisor SIGKILL is now observed separately: PostgreSQL
  recovery terminates companion SQL, retains the postmaster/committed marker,
  stops the old helper before replacement and reacquires the same owner fence.
  Helper containment does not protect against death of the PG supervisor itself.
- The measured 125-second stop budget, cancelled queued work and restart limits
  are process/queue contracts. They do not establish native Edge cancellation,
  production index readiness or graceful durable shutdown.
- CI6's 20 engine tests retain phrase, malformed copied-metadata and explicitly
  flushed SIGKILL/reopen assertions. Read-only/manual-manifest paths and
  update-only preview/no-write branches have narrow evidence. Store, Delete and
  empty bootstrap in the fixed update-only type remain unavailable; manifest
  inspection does not prove snapshot archive creation or restoration.
- All three candidate-vector format groups passed in each CI6 SQL profile,
  including shape/null/budget/float32/ACL checks. The new tagged-identity/source
  fixture rechecks have compile/link evidence and six authored SQL groups,
  with actual runtime still pending. Neither diagnostic is a public API freeze
  or a production source SELECT/tenant/RLS proof.
- A positive disk gate needs a dedicated bounded filesystem, actual errno 28,
  retained child status, configuration observations, cleanup, retry, explicit
  flush and successful reopen/query assertions. `not_run`, a crash or a timeout
  cannot pass it. The narrow 32 MiB vector/keyword pass excludes mutable text
  indexes. Both 128 MiB full-text fault and no-filler clean controls failed with
  SIGBUS at reopen and exhausted the mount. Their evidence stays failed; a new
  capacity profile and a separately named no-write refusal test do not fix the
  upstream loader or prove dirty recovery. The older missing-exit failure is
  still of unknown cause.
- OOM requires bounded fresh-container evidence and exact victim/limiting-cgroup
  attribution. Counter correlation or SIGKILL alone is insufficient. Safe
  negative tests, compile success and absent journal access do not pass the gate.
- The [durability ADR](adr/0003-edge-durability-and-recovery.md) records no
  logical WAL replay in the fixed load path and no public durable cursor or
  reclamation API. Update return is not durable ACK; load can repair/mutate
  state. Exact-event flush/ACK ordering, dirty-generation readiness, preservation,
  reconstruction and WAL growth remain implementation and crash-test gates.

All original requirements, including source authorization, complete analysis,
resource faults and the overall topology decision, remain. Future source or
feature changes must earn new regression evidence.

For full dependency-graph failures, record the exact missing package/compiler/API/native requirement and whether it is an environmental acquisition problem or a version incompatibility. Do not substitute qdrant-client, advertise an unbuilt dependency graph, or change pins without reviewing the [upgrade contract](dependencies.md).

P0 does not establish SQL API stability, ingestion correctness, production readiness, or a supported release. Pure engine probes alone do not satisfy P0-PG or P0-FAULT.

## P1 — Source-to-index data lifecycle

The implementation must make the source table authoritative and every projection rebuildable.

| Gate | Required acceptance |
| --- | --- |
| P1-CATALOG | Index registration persists owner/source/configuration/generation, installs capture transactionally, validates a supported single primary key, and accurately reports task/index states |
| P1-BACKFILL | Capture is installed before a consistent backfill; interleaved inserts/updates/deletes are replayed without gaps; concurrent DDL has an explicit lock, wait, or rejection contract |
| P1-TRANSACTION | Full rollback, savepoint rollback, trigger errors, multi-row writes and COPY leave no committed indexing event for uncommitted source data |
| P1-ORDER | Concurrent commit-order inversions, duplicate deliveries and retries cannot regress source revisions; event sequence allocation is not used as a complete commit watermark |
| P1-IDENTITY | bigint/uuid/text identities round-trip; delete/reinsert creates a new incarnation; stale delete/upsert events cannot remove or revive the wrong incarnation |
| P1-MODEL | Fingerprints are separate from row revisions; text edits invalidate only affected representations; late outputs for old fingerprints/incarnations/models are rejected; partial slot readiness is observable |
| P1-DURABILITY | ACK follows the precise tested Edge persistence condition; crash after Edge apply but before ACK replays harmlessly; uncertain durability leaves the event pending |
| P1-WAIT | Tickets seal fixed event membership; wait before own commit is rejected; after commit success implies applied and durable; timeouts/failure/drop cannot become false success |
| P1-DDL | TRUNCATE, DROP source/index, column type changes, model changes, retries, cancellation and concurrent DDL have tested outcomes and safe ownership cleanup |

Required concurrency scenarios include a slow earlier transaction committing after a faster later transaction, a deleted key reused while an old model job finishes, a backfill snapshot interleaved with source deletion, and multiple events for one key applied in adversarial order. Assertion targets are the current source incarnation and representation fingerprint, not only point counts.

## P2 — Core search Alpha

P2 delivers the first complete knowledge-base journey on top of the proven data lifecycle. It does not imply completion of all advanced lexical requirements or platform distribution.

| Gate | Required acceptance |
| --- | --- |
| P2-TEXT | Offline BM25 with compiled shared analysis policies, all/any and Boolean predicates, contiguous phrases, token/whole-value prefixes, identifier exact constraints, and explicitly tested multi-field ranking |
| P2-HYBRID | Text/balanced/precision plans with validated dense/learned/token inputs; true pinned-version RRF/DBSF semantics and tie rules; candidate-domain MaxSim; explicit required-slot and fallback rules |
| P2-PERMISSION | Index and source SELECT/column access are required; unsafe RLS is rejected; mandatory authorization constraints reach every recall/rerank branch; source/version rechecks and bounded refill protect exposed hits |
| P2-RESULT | Stable proposed hit/page/explain schemas; traceable chunk/document keys; real original-text excerpts and supported exact lexical highlights; no invented semantic spans; accurate candidate/group budgets and underfill reasons |
| P2-STATS | Count unit, match domain, exactness and authorization are explicit; unavailable correct totals are null; point counts never masquerade as document counts |
| P2-DISCOVERY | One registry drives planner and capabilities API; upstream support, adapter implementation, release validation and index readiness are separate; unknown options and unsupported combinations fail explicitly |
| P2-EXAMPLE | All applicable basic [SQL journeys](user-journeys.md) are executable from a clean example database, including ordinary writes and post-commit wait |
| P2-QUALITY | Frozen Chinese/English knowledge-base, identifiers and long documents; text/dense/learned/fusion/MaxSim ablations; correctness and relevance metrics with all configuration/model inputs recorded |

F17 has a core slice for true excerpts/basic analyzed highlights and a later slice for normalization/fuzzy/expansion completeness. F19 has a core scoped-statistics slice and a later full lexical-domain slice. F20 has a core explicit-identifier constraint and a later evaluated default-ranking policy. Passing those slices must not mark their remaining P4 requirements complete.

A core Alpha cannot claim phrase protection if it filters only the final top-k. Candidate predicates must enter BM25, dense, learned sparse, and every nested prefetch/fusion branch. It cannot claim full MVCC relevance because post-search source checks removed stale rows.

## P3 — Bounded performance and operations

| Gate | Required acceptance |
| --- | --- |
| P3-FAST | Quantization and MRL are model/CPU/storage-specific tested plans; short/full model compatibility is declared; exact candidate rescoring does not claim recovery of discarded float32 precision |
| P3-FILTER | Required payload indexes are built before dependent vector indexes; restrictive-filter HNSW/ACORN behavior is measured; native bit input is distinguished from binary quantization |
| P3-RESOURCE | Bounded requests/candidates/HasId/tokens/matrix cells/response bytes/threads/time/memory; backpressure and cancellation apply to each stage; work_mem is not treated as the engine memory limit |
| P3-MAINTENANCE | Optimize scheduling, blocking/resource effects and cancellation behavior are measured; unoptimized points remain visible under documented settings; indexed_only/prevent_unoptimized effects are explicit |
| P3-GENERATION | Build/catch-up/atomic switch while concurrent SQL pins the old generation; old-generation reference drain and rollback retention; cancelled/failed rebuild preserves a usable predecessor |
| P3-RECOVERY | Restart, exclusive owner recovery, partial writes, disk failure/corruption and crash replay; damaged/mismatched generations cannot report ready; source/BYOV rebuild procedure actually runs |
| P3-UPGRADE | Grouped dependency updates after real manifests; full probes, SQL/quality/security/resource gates; old-index reopen or rebuild migration; actual downgrade/rebuild rollback test |
| P3-PLATFORM | Recorded supported compiler/PG/platform/CPU/dependency matrix based on complete builds and runtime tests; unsupported combinations explicitly listed |

Backup and replication modes remain individual L12 decisions. The initial required safe recovery path is rebuilding from consistent PostgreSQL source/configuration/retained BYOV data. A supported mode needs its own end-to-end restore/timeline test; documenting an unsupported mode does not implement it.

## P4 — Advanced product scope

| Gate | Required acceptance |
| --- | --- |
| P4-VISUAL | Compatible global/patch BYOV, query/document model relationship, grouping, token/matrix budgets, visibility and result-source checks |
| P4-EXPLORE | Recommendation strategies, discover/context, relevance feedback and MMR have actual fixed-version semantics, bounded authorized examples and explicit combinations |
| P4-SCORING | Typed Formula expressions and business/time/geo behavior, safe defaults, bounds and no unsupported external-score claims |
| P4-READ | Order-by/scroll/sample/retrieve/count/facet/matrix have independently tested input, scope, ordering, generation, permission and output-size contracts |
| P4-LEXICAL | F13 typo/fuzzy prefix, F14 proximity/slop, F15 syntax, F16 synonyms, F17 real Unicode source highlighting, F18 instant suggestions, F19 authorized statistics, F20 measured exact-match/field behavior all have owner, examples and quality tests |
| P4-DUAL | If a second lexical engine is chosen, shared IDs, authorization, idempotent dual writes, per-engine readiness/generations, backfill/rebuild/recovery, explicit outer fusion and coordinated upgrade/rollback all pass |
| P4-COVERAGE | Every F01–F20, V01–V08, Q01–Q14, L01–L12 item has completed required deliverables or an explicit product decision preserving its unsupported scope; no blanket indefinite TODO declared complete |

When Edge lacks a native function, use a measured mature dependency where appropriate or make an explicit product-scope decision with consequences and an executable alternative. Listing Tantivy or pg_search as a candidate is not delivery. Native bit-vector input may remain unsupported after a documented type audit; binary quantization must not be substituted in the capability report.

Rich lexical quality needs a chosen path before the core Alpha; the full P4 acceptance remains required even if a partial earlier plan is useful. Cross-engine scores cannot be passed as native Edge fusion branches without a real supported adapter mechanism; outer fusion is separately owned work.

## P5 — Distribution and public delivery

| Gate | Required acceptance |
| --- | --- |
| P5-PACKAGE | Reproducible locked build/package; native extension/control/SQL installation and supported upgrade scripts; exact system/CPU requirements; clean-install and uninstall checks |
| P5-DOCS | README status matches binaries; versioned SQL/config/model contracts; examples, recovery/maintenance and migration/rollback runbooks; compatibility and unsupported modes |
| P5-LICENSE | Project license decision, actual locked dependency/native-library licenses, notices and source obligations; no unreviewed copied/forked upstream internals or brand claims |
| P5-CI | Required compile, engine, SQL, concurrency, security, failure, quality and upgrade gates are reproducible and protected; failures remain visible and related tests are not skipped |
| P5-JOURNEY | Fresh environment installs the artifact and completes the declared source-to-search journey without a separate Qdrant service; offline core/BYOV boundaries are demonstrated |
| P5-RELEASE | Changelog, version/code identity, artifacts/checksums and precise supported scope agree; publication occurs only within the current authorized scope |

## Preview, core, and complete product gates

| Release class | Minimum honest claim | Required exits |
| --- | --- | --- |
| Source feasibility work | Build/probe source and specifically evidenced diagnostic installation/runtime, with exact passed and blocked results | Diagnostic evidence is named individually; no source-table indexing or production claim; all unfinished P0 requirements remain |
| `0.0.x` preview | A working declared subset in a clean tested environment | Executable declared journey, corresponding transaction/permission/failure gates, scoped P5 packaging/docs/licenses; explicit remaining P0–P4 work |
| `0.1` core | Reliable local text plus declared balanced/precision and operating model | All necessary P0, P1, P2 and P3 correctness/recovery/security exits; scoped P5 clean installation and upgrade; pending advanced scope retained |
| Complete formal product | Full 54-item product coverage with explicit supported/unsupported decisions | P4 and P5, all required slices/combinations, quality and operational evidence; a justified unsupported native input is clearly distinct from implemented support |

A preview version number does not waive data-corruption or access-control requirements for any functionality it exposes. A complete coverage document is not complete product delivery. A scoped core release does not close unfinished F13–F20 or advanced retrieval items.

## Cross-capability acceptance matrix

| Combination | Required observable assertions |
| --- | --- |
| Phrase/Boolean/identifier × BM25/dense/learned/fusion | Mandatory predicates applied before each candidate stage; no incorrect matching from another branch |
| Authorization × MaxSim/MMR/recommend/discover/group/facet/matrix/explain/cache | No forbidden IDs, scores with revealing sources, snippets, counts, samples, examples, cache reuse or diagnostics |
| Quantization/MRL/storage × exact/rescore/HasId | Correct representation and candidate-domain semantics; recall/resource measurements; no false float32 restoration |
| Chinese normalization × fuzzy/prefix/synonyms/highlights | Correct analysis and original Unicode offsets, boundary/identifier preservation, bounded expansion |
| Late encoding × delete/reinsert/out-of-order/rebuild | Correct current incarnation/fingerprint in both serving and building generations |
| Optimize/restart/upgrade × concurrent SQL/write/cancel | Stable owner/generation rules, bounded work, no false ACK/readiness and executable recovery |

## Quality and efficiency evidence

Freeze the corpus, query set, relevance judgments, models/revisions and licenses, preprocessing/chunking, tokenizer/dictionaries, sparse vocabulary/IDF, candidate budgets, hardware, CPU baseline, concurrency and cold/warm-cache conditions. Fixture vectors test mathematical/transaction correctness; they do not establish model quality.

Use two separate comparisons: identical input/configuration where possible to evaluate engine efficiency, and each product's reasonable configuration to evaluate the overall developer/search experience. Baselines include standalone Qdrant, pgvector, a reasonable pg_search combination, applicable VectorChord paths, and the project's text/dense/learned/fusion/MaxSim/quantization ablations. Fixed-version primary-source/API checks precede claims about those alternatives; no assumption that all PostgreSQL engines lack MaxSim is allowed.

Required metrics are Recall@K, NDCG/MRR, identifier false matches, authorized-filter fill rate, document duplication/diversity, representation coverage, end-to-end p50/p95, encoding cost, memory/disk, and build/update/optimize/recovery durations. Report sample sizes, failed queries and workload limitations. Establish thresholds from the real baseline and product requirements; do not invent a speedup, latency SLA, compatibility claim or calendar commitment.

## Stage report template

Each stage report records the code revision, target and exact tool/dependency graph; exit IDs actually passed with reproducible commands/results; blocked/failed gates and cause; capability and combination evidence changes; architecture/model/format decisions; revised workload assumptions and critical path; remaining formal scope. Advance a stage only on its exits, and preserve an explicit `blocked` result where the environment or dependency baseline prevents verification.

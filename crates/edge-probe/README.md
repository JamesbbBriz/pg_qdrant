# Published Edge API feasibility probe

This crate depends directly on the published `qdrant-edge =0.8.0` package.
It is not a PostgreSQL extension or an installable search product. It contains
synthetic engine tests and exhaustive Rust enum matches for upgrade review.

## Run

From the repository root, with its pinned Rust toolchain:

```sh
cargo check --locked -p pg-qdrant-edge-probe
cargo run --locked -p pg-qdrant-edge-probe
cargo test --locked -p pg-qdrant-edge-probe
```

The runner creates and removes a temporary shard, uses two search threads,
and prints a JSON check report. A failed check exits unsuccessfully. It uses
eight synthetic source rows and bounded queries. It performs no model
downloads or network calls. Source text and vectors are fixture data.

The Unix integration test `explicit_flush_survives_sigkill_without_drop` starts
a separate fixture process, waits at most 30 seconds for its explicit flush,
sends SIGKILL, and reopens the shard in the test process. It checks point count,
payload revision, and retention of all four named representations, then executes
authorized phrase, learned sparse, MaxSim, and offline BM25 queries. Sparse and
MaxSim scores are checked against fixture goldens; a private point that would
otherwise rank highly must remain excluded. This test never kills PostgreSQL
and does not test power loss or recovery of updates before an explicit flush.
The runner's own `flush_close_reopen` report continues to describe its clean-close
test separately.

### Recorded engine verification

On 2026-10-08, with Rust 1.96.0 on Linux x86_64 and the repository lockfile:

| Command | Observed result |
| --- | --- |
| `cargo test --locked -p pg-qdrant-edge-probe` | Actual engine smoke, explicit-flush/SIGKILL recovery, owned metadata corruption, unsafe disk-environment refusal and a clean disk-fixture reopen passed; 4 tests, 0 failed or ignored. |
| `target/debug/pg-qdrant-edge-probe` after the locked test build | All 16 synthetic check groups passed; engine version `0.8.0`. |
| `target/debug/pg-qdrant-edge-probe --corruption-probe` | Both malformed copied-metadata loads returned errors; intact-copy recovery, original preservation, representation equality and phrase/MaxSim queries passed. |
| `target/debug/pg-qdrant-edge-probe --disk-fixture-probe` | Clean reopen of the disk-specific eight-row/four-representation fixture passed, including keyword/tenant filtering and exact MaxSim; no filler or ENOSPC injection. |
| `target/debug/pg-qdrant-edge-probe --disk-full-probe` without `PG_QDRANT_ENOSPC_DIR` | `status: "not_run"`, no disk writes attempted; a dedicated tmpfs is required for positive ENOSPC evidence. |

These commands compiled and linked the engine harness, including exhaustive
surface sentinels. They do not prove that the PostgreSQL extension links,
installs, or survives engine failure. A dependency or source change requires
running the commands again; this record is not a blanket compatibility claim.

`run_smoke() -> Result<serde_json::Value, String>` accepts no PostgreSQL
pointers and makes no PostgreSQL calls. A SQL feasibility harness may invoke
it only in its designated engine owner. Running it in an engine thread does
not establish crash isolation from the process owning that thread.

## Standalone fault experiments

These binary modes are opt-in. PostgreSQL never invokes them. The default test
suite includes safe tests for corrupted owned copies, refusal of unsafe disk
paths, and a clean reopen of the disk-specific fixture. It does not fill a
filesystem. Reports use separate `kind` values, so they must not be confused
with the 16-group normal smoke report.

```sh
cargo run --locked -p pg-qdrant-edge-probe -- --corruption-probe
cargo run --locked -p pg-qdrant-edge-probe -- --disk-fixture-probe
cargo run --locked -p pg-qdrant-edge-probe -- --disk-full-probe
```

The corruption probe builds the same eight source identities and four named
representations with a 64 KiB test WAL segment capacity. It explicitly flushes
and closes this owned fixture, then copies it into separate owned temporary
directories. Copying preserves zero extents in Edge's large sparse storage
pages. Each copy is limited to 512 MiB of logical file content scanned,
64 MiB of content written, 512 files and 16 directory levels. It writes invalid
JSON into `edge_config.json` in one copy and
`segments/<owned-segment>/segment.json` in another. Each `EdgeShard::load` must
return a real error, and each damaged copy reports `ready: false`. An intact
recovery copy and the original must reopen with identical retrieved records and
pass phrase/MaxSim queries. This tests these two metadata corruption cases;
it does not demonstrate arbitrary bit-rot detection or in-place repair.
The file names and load paths come from the fixed release's
[shard configuration](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/config/shard.rs)
and [segment state code](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/segment/segment_ops.rs).

The disk probe requires `PG_QDRANT_ENOSPC_DIR=/pgq-p0-faults`. Before any write,
it checks all of these conditions:

- Linux, the exact canonical path `/pgq-p0-faults`, and no symlink substitution.
- An initially empty mount, owned by the effective UID with permissions `0700`.
- Kernel mountinfo identifies that path as the root of a dedicated writable
  tmpfs, with no other mount aliases for its device in the current namespace.
- `/usr/bin/stat` confirms tmpfs and reports total capacity no greater than
  64 MiB. Missing/unparseable filesystem information refuses the experiment.
- No PostgreSQL data marker in the path's ancestors; any configured `PGDATA`
  or `PG_QDRANT_PGDATA` must resolve, be disjoint, and use a different device.

An ordinary temporary directory or shared `/dev/shm` never qualifies. Missing
configuration returns JSON `status: "not_run"` and exit code 0. An explicitly
unsafe environment returns `status: "not_run"`, `reason_code:
"unsafe_environment"`, and exit code 2, without attempting disk writes.

The disk-specific fixture retains all eight source identities, all four named
representations, and the `tenant`, `document` and `sku` keyword indexes. It uses
the public `on_disk_payload: true` and per-vector `on_disk: true` settings plus
the 64 KiB WAL configuration. Its two mutable text indexes are explicitly
excluded. A passing disk report therefore verifies keyword/tenant filtering
and exact MaxSim recovery, and reports `phrase_query: "not_tested"`. The full
phrase/ENOSPC/reopen combination remains an open gate; the separate corruption
and explicit-flush/SIGKILL tests retain their complete phrase assertions.

`--disk-fixture-probe` creates this fixture in an ordinary owned temporary
directory, flushes it, reopens it, and compares every retrieved source record
and representation. It runs the keyword/tenant and exact MaxSim assertions
before and after reopening. It never creates a filler. On Linux it also reports
logical file size, allocated file bytes and the summed resident pages of the
owned shard's mappings from `/proc/self/smaps`. Those measurements are neither
total process RSS nor positive evidence of operation on a bounded tmpfs.
The recorded ordinary-filesystem run observed 67 files, 220,534,047 logical
bytes and 258,048 allocated bytes. After the actual fixture queries, summed
mapping RSS was 204 KiB before close and 16,024 KiB after reopen. Rerunning
this mode reproduces the measurement procedure; resident pages can vary by
kernel and access history, so these values are observations, not test goldens.

For a locally built `Dockerfile.p0` image, the intended isolated invocation is:

```sh
docker run --rm --user 10001:10001 --memory=5g --cpus=2 \
  --tmpfs /pgq-p0-faults:rw,noexec,nosuid,nodev,size=32m,uid=10001,gid=10001,mode=0700 \
  --env PG_QDRANT_ENOSPC_DIR=/pgq-p0-faults \
  pg-qdrant-p0 /src/target/debug/pg-qdrant-edge-probe --disk-full-probe
```

After preparing and flushing the bounded fixture, the probe writes an owned
filler until the OS returns ENOSPC (errno 28), with a hard total-byte limit no
greater than the validated filesystem capacity and a 15-second fill limit.
It requires the public `set_hnsw_config` persistence call to report ENOSPC and
the persisted configuration bytes to remain unchanged. It separately records
whether the in-memory configuration changed; disk atomicity is not assumed to
imply an in-memory transaction. The filler is removed before retry, reopen or
normal error unwinding. A successful retry must survive explicit flush/reopen
and preserve all fixture records and the keyword/tenant/MaxSim results. Only
the owned experiment directory is removed; the mount must be empty after
cleanup. Stages are emitted on stderr as `pg_qdrant_p0_stage=<name>` before
native fixture creation, index creation, insertion, flush, filling, config
save and recovery. The external supervisor must retain the child exit code
and signal information when a native termination prevents JSON output.

#### Why the small disk fixture excludes mutable text indexes

The fixed release's [payload storage constructor](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/payload_storage/payload_storage_impl.rs)
populates its backing pages for RAM placement. The
[mutable full-text loader](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/index/field_index/full_text_index/mutable_text_index/lifecycle.rs)
uses `Populate::Blocking` regardless of text-index `memory`/`on_disk` settings.
Each [default blobstore page](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/blobstore/config.rs)
is 32 MiB. Two text indexes plus their metadata therefore prevent treating
this full fixture as a bounded 32 MiB writable-reopen test.

An ordinary-filesystem measurement of the original full fixture observed
34,072 KiB of mapped fixture RSS after creation. Cold payload/vector settings
reduced that to 260 KiB, but reopening still observed 83,468 KiB because the
mutable text and keyword indexes populated their backing pages. Public
`optimize` returned false for the original eight rows at the smallest nonzero
indexing threshold. A separate bounded experiment crossed that threshold with
one temporary sparse point, optimized successfully, then deleted that point,
restoring the count to eight. Writable reopen still observed
83,364 KiB. The [load path](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/edge_shard/mod.rs)
ensures an appendable segment, and its [segment constructor](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/shard/segment_holder/mod.rs)
copies the payload-index schema into that segment. Optimization therefore did
not remove the eager mutable-text reopen requirement. No private initializer,
patched dependency, or larger fault mount is used to evade it.

The first CI attempt with the full fixture on a 32 MiB tmpfs failed before
usable child diagnostics were captured; its terminating signal is unknown.
The measurements identify a concrete fixture-sizing problem, but do not prove
the exact cause of that historical process termination.

This is a configuration-save failure/retry experiment. WAL growth, dirty vector
ingestion, PostgreSQL outbox ACK semantics, power-loss durability, cgroup OOM and
whole-instance isolation remain separate gates. A 32 MiB tmpfs is a storage
limit, not an engine RSS limit or evidence of OOM handling. The positive ENOSPC
path of the revised, narrower fixture still requires the dedicated mount and
remains unverified until a report with `status: "passed"` is captured there.

## Checks and limits

| Check | Scope | Capability IDs |
| --- | --- | --- |
| Offline BM25 | Explicit neutral multilingual policy; sparse IDF corpus filter | F01, F04, F05, V02, Q14 |
| Dense and supplied sparse | Named vectors, configured dot scoring, separate sparse IDF contract | F02, V01, V02, Q01 |
| Nested candidate retrieval and MaxSim | Prefetch fusion followed by exact scoring of its candidate domain | V03, Q01, Q02 |
| Weighted RRF and DBSF | Native Edge fusion, fixed-version weight semantics, all fixture DBSF scores and singleton/constant/empty distributions | Q03 |
| AND, OR, Boolean exclusion | Payload text predicates and structured filters | F06, F07, Q12 |
| Contiguous phrases | Phrase index; constraints in every dense/sparse/fusion branch | F08, Q02, Q12 |
| Token and keyword prefixes | Separate indexes; case-sensitive whole-value identifiers | F09, F10, F11 |
| Document grouping | Three distinct groups and traceable authorized fixture rows | Q09 |
| Scoped counts | Exact authorized point counts, with no global document claim | Q11, Q12 |
| Explicit flush and reopen | Clean close and SIGKILL after explicit flush, with actual persisted-query checks | L01, L04 |

Passing these checks advances only their narrow engine-runtime evidence.
The synthetic tenant payload is not a PostgreSQL identity or RLS proof.
No recall, NDCG, speed, concurrent SQL, crash safety, or release support
claim follows from this runner. All stages still require broader acceptance
in `docs/capabilities.md`.

## Exact source basis

The reviewed archive is
[`qdrant-edge-0.8.0.crate`](https://static.crates.io/crates/qdrant-edge/qdrant-edge-0.8.0.crate),
with SHA-256
`0b8072302c87506a34bffec9bc16dbdcd36df8ab1321406b6e141530348c7e54`.
Source findings refer to this archive, not Qdrant Server documentation.

| Published source | Finding |
| --- | --- |
| [Manifest](https://docs.rs/crate/qdrant-edge/0.8.0/source/Cargo.toml) | Apache-2.0 declaration, Rust edition 2024, no package `rust-version`, no declared Cargo feature table. This is not a complete dependency license audit. |
| [BM25 encoder](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/bm25_embed.rs) | `EdgeBm25::new`, `embed_document`, `embed_query` are public. Query and document encodings differ. Defaults include English stemming/stopwords and fixed expected average document length 256. |
| [Text/keyword schemas](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/data_types/index.rs) | `TextIndexParams` defaults differ from BM25: stemming and stopwords are disabled unless configured. Phrase matching requires `phrase_matching`. Token prefixes and keyword `prefix` are independent. |
| [Query request](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/requests/query.rs) | `QueryRequest` and `Prefetch` are owned synchronous request types. No public request deadline or cancellation token is present. |
| [Query variants](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/shard/query/mod.rs) | Vector, Fusion, OrderBy, Formula, Sample, MMR. Recommendation, discovery/context and feedback are public vector-query variants. |
| [Weighted RRF](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/common/reciprocal_rank_fusion.rs) | For zero-based position `p`, contribution is `1 / ((p + 1) / weight + k - 1)`; upstream default `k=2`. This is not multiplication of ordinary RRF scores by weights. Equal-score order is not promised. |
| [DBSF](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/common/score_fusion.rs) | For nonconstant inputs, contribution is `0.5 + (score - mean) / (6 * sample_standard_deviation)` with variance denominator `n - 1`, then contributions are summed. Singleton and constant distributions contribute `0.5` per point; empty branches add no points. Inputs are candidate distributions, not corpus statistics. |
| [Shard lifecycle](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/edge_shard/mod.rs) | `new`, `load`, `flush`, settings and clean `Drop` are public. `load` inspected here loads segments/configuration; it does not invoke automatic WAL replay in this code path. |
| [Updates](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/edge_shard/update.rs) | `update` writes the Edge WAL and applies operations. Success alone is not the extension's durable ACK contract. |
| [Optimization](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/edge_shard/optimize.rs) | `optimize` blocks until no plans remain and uses an internally created stop flag. No external cancellation argument. |
| [Read trait](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/read_view/shard_read.rs) | `query_groups` and `search_matrix` require the exported, sealed `EdgeShardRead` trait. |
| [Grouping execution](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/read_view/ops/grouping.rs) | Grouping projects only the group field and does not hydrate requested full payloads. The probe explicitly hydrates its bounded group sources using an authorized `HasId` query before asserting provenance. |
| [Vector inputs](https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/types/vector.rs) | Public inputs are dense floating point, sparse, and multivector matrices. Native binary/bit similarity input is not a variant. |
| [C SIMD build](https://docs.rs/crate/qdrant-edge/0.8.0/source/build_quantization.rs) | x86_64 C code compiles with `-march=haswell`, `-O3`, `-mpopcnt`. Broader CPU support requires runtime-dispatch and binary testing, not just a Rust target triple. |

The published archive's own lockfile resolves Charabia 0.9.9 and includes
`serde_json` 1.0.151 and `tempfile` 3.27.0. The project lockfile must independently
record the actual combined graph. Core Edge has native C SIMD components and
a substantial transitive dependency graph even without inference libraries.

Some publicly exposed fields use types not re-exported under nameable module
paths. The harness uses public constructors, inferred types, or a controlled
constant deserialized into a public configuration. It imports nothing from
`qdrant_edge::internal`. Public upstream serde layouts remain confined to the
probe, never treated as a public SQL JSON API.

DBSF goldens are derived independently from this fixture's branch scores. The
dense branch has mean `3/7` and sample variance `17/84`; the supplied sparse
branch has mean `0.9` and sample variance `0.02`. The runner checks all seven
fused scores with a `1e-5` absolute tolerance, then checks disjoint singleton
branches, a constant dense branch plus an empty sparse branch, and all-empty
branches. It does not assert an order among equal-score points.

`api_inventory.rs` matches all current scoring/vector query variants, match
variants, conditions, vector input/storage/quantization variants, payload schema
variants, and update-operation variants without wildcard arms. These sentinels
detect new variants in the enums explicitly matched. They do not detect newly
exported methods, new configuration fields, or variants of nested enums without
their own exhaustive matches. The file also references selected lifecycle and
read methods; a method-item reference checks name resolution, not a call with
concrete arguments. Typed constructor expressions likewise do not establish
engine execution or accepted input semantics. Raw storage operations and native
callback filters are explicitly outside the proposed SQL surface.

### Remaining public API coverage

The following fixed-version gaps remain in addition to the runtime limits above.
Source paths are relative to the published
[`qdrant-edge 0.8.0` archive](https://docs.rs/crate/qdrant-edge/0.8.0/source/).
Owners refer to the existing [work ledger](../../docs/work-items.json); the
complete [54-capability contract](../../docs/capabilities.md) remains unchanged.
References and constructors in this table are not full capability compilation,
runtime, PostgreSQL integration, or release evidence.

| IDs | Public surface and source path | Current probe evidence | Owner and next gate |
| --- | --- | --- | --- |
| Q01 | `SearchRequest`, `EdgeShard::search` / `EdgeShardRead::search`; `src/edge/requests/search.rs`, `src/edge/read_view/shard_read.rs` | Neither referenced nor called. Upstream documents this alternate entry point as deprecated in favor of `query`, which the runner uses. | `engine/query`: record an explicit deprecated-entry exclusion, or add a concrete call if retaining it. |
| Q04–Q08, Q10 | Recommendation, discover/context, feedback, MMR, Formula and Sample; `src/shard/query/{mod,query_enum,formula}.rs` | `construct_advanced_queries()` contains typed inputs but is never called by the runner/tests and never submits an engine query. | `planner/explore`, `engine/diversity`, `planner/formula`, `engine/read`: construct compatible fixture inputs, call `query`, and verify scoring, authorization and invalid inputs. |
| F07, Q12 | `RangeInterface`, geo/range/value-count/null field conditions, `Filter::min_should`; `src/segment/types.rs` | Outer `Condition` variants are matched. Concrete fixture predicates cover selected text/keyword/ID filters; the other field forms and nested range enum are not covered. | `planner/filters`, `engine/filters`: exhaustive nested mappings, actual filtered calls and numeric/datetime/geo/cardinality boundaries. |
| Q10, Q12, L09 | `WithPayloadInterface::{Fields,Selector}`, `PayloadSelector::{Include,Exclude}`, `WithVector::Selector`; `src/segment/types.rs` | Actual retrieval uses Boolean projection; the selector variants lack exhaustive mappings and calls. | `engine/read`, `engine/filters`: selective-return calls, result-shape assertions and authorization tests for omitted/returned fields. |
| Q08, Q10 | `OrderByInterface`, `Direction`, `StartFrom`, `OrderValue`, `DecayKind`, `Sample`; `src/segment/data_types/order_by.rs`, `src/segment/index/query_optimization/rescore_formula/parsed_formula.rs`, `src/shard/query/mod.rs` | Outer OrderBy/Decay arms do not cover nested variants. `Sample::Random` is constructed, without an exhaustive Sample match or runtime query. | `engine/read`, `planner/formula`: nested mappings, concrete ordering/decay/sample inputs and result/budget assertions. |
| V04, V05, Q13 | `CompressionRatio`, `ScalarType`, quantization configs, `QuantizationSearchParams`, `AcornSearchParams`; `src/segment/types.rs`, `src/edge/config/vectors.rs` | Quantization families and datatypes have sentinels. No quantized fixture or ACORN request is constructed/executed; nondefault precision/rescore combinations remain unverified. | `engine/quantization`, `engine/storage`, `engine/indexing`: real configurations and queries, rejected combinations, optimization/reopen and rescore goldens. |
| L02 | `UpdateMode::{Upsert,InsertOnly,UpdateOnly}` and partial vector/payload/delete/schema operations; `src/shard/operations/{point_ops,vector_ops,payload_ops,vector_name_ops}.rs` | UpdateMode is not mapped. Ordinary checked-in fixture updates exercise point upsert and payload-index creation; other operation variants are sentinels. | `engine/source`: typed conditional-mode and mutation calls, idempotency and persisted result assertions. |
| L04, V05, L10 | Read-only `open`, `refresh`, `refresh_with`, request `load_profile()` and `LoadProfile::{for_search,for_scroll,for_retrieve,merge}`; `src/edge/read_only/{lifecycle,refresh}.rs`, `src/segment/data_types/load_profile.rs` | Only `open_mmap` is referenced; `Option<LoadProfile> = None` is a type-name check. No follower or profile is exercised. | `lifecycle/recovery`, `engine/storage`: public manifest/setup decision, concrete follower calls, flush/refresh visibility and memory-placement tests. |
| L02, L04 | `UpdateOnlyEdgeShard::{open,preview_batch,apply_batch,segment_configs}`, `UpdateBatchPlan::build`, `PointAction`; `src/edge/update_only/{mod,lifecycle,apply,preview}.rs`, `src/edge/update_only/batch/plan.rs` | Only `open_mmap` is referenced. Batch construction, action variants and execution are absent. | `engine/source`, `lifecycle/recovery`: explicit adoption/exclusion decision; if retained, existing-shard calls, negative bootstrap/operation probes and a separate persistence contract. |
| Q10, Q11, L04 | `scroll`, `facet`, `search_matrix`, `info`, snapshot inspection/unpack/recovery; `src/edge/read_view/shard_read.rs`, `src/edge/edge_shard/snapshots.rs` | Method-item references only. This does not extend the existing retrieve/count/group runtime evidence to these operations. | `engine/read`, `results/statistics`, `lifecycle/recovery`: concrete requests, authorized bounded results and disposable-target restore/rollback tests. |

Read-only loading requires a manifest: `open_mmap` selects the manifest
enumerator, while ordinary manifest writing defaults off in
`src/common/flags.rs` and is checked by `src/edge/edge_shard/mod.rs`.
`refresh()` is caller-driven and can return success after three attempts
without converging to the newest manifest. It does not establish a committed
change wait. Load profiles do not demote mutable components; their construction
alone cannot solve the mutable-text memory limit described above.

The update-only writer has no WAL, optimizer or `EdgeConfig`. In this release,
opening an empty directory reaches an unimplemented bootstrap path. Its batch
planner rejects filter-selected operations, point sync, conditional upserts and
schema operations. It must not inherit the ordinary `EdgeShard::update`
persistence or operation contract by name alone.

`recover_partial_snapshot` moves, replaces and deletes target files before its
final `EdgeShard::load`. It is not an atomic generation switch. A future restore
probe must use a disposable destination and independently prove cutover and
rollback; the current method reference proves none of those behaviors.

## Lexical work retained

| ID | Native surface reviewed | Required next evidence / implementation |
| --- | --- | --- |
| F13 | No fuzzy edit-distance query primitive in the public query/match enums | Evaluate pinned Tantivy fuzzy primitives; bounded expansions and identifier protection. |
| F14 | Contiguous phrase primitive, no public slop/proximity parameter | Position-aware lexical adapter and multilingual position tests. |
| F15 | Typed filters and scoring queries, no user query-language parser | Controlled parser, validation, budgets, escapes, and plain-text behavior. |
| F16 | No built-in public synonym dictionary contract | Versioned expansion policy, multiword semantics, scoring and reindex rules. |
| F17 | Payloads and scores; no original-source match offsets returned | Source-aware offset mapping and snippets, with exact analysis/normalization parity. |
| F18 | Prefix primitives only | Suggestion corpus, ranking, authorization, freshness and instant latency contract. |
| F19 | Count/facet primitives with filters | Lexical match-domain versus candidate scope, document cardinality and permissions. |
| F20 | Exact keyword and phrase predicates | Explicit identifier/field ranking policy validated on held-out queries. |

Tantivy remains a candidate, not an adopted dependency. A future second
lexical engine must include dual-index lifecycle, authorization, recovery and
outer fusion. None of F13–F20 is marked complete by this crate.

## Open P0 gates

The [recorded direct-worker PostgreSQL CI](../../docs/evidence/p0-postgresql-ci.json)
verified diagnostic concurrency, cancellation and bounded queues. Its native
abort and SIGKILL experiments terminated companion SQL sessions; this is a
negative isolation result. Current-source and managed-helper PostgreSQL results
need separate evidence. OOM, positive bounded ENOSPC, dirty-index/source
recovery, transaction rollback, authority mapping, generations and cross-version
migration remain open. The owned metadata-corruption test covers only the
malformed copied JSON files and intact recovery source described above.
The explicit-flush/SIGKILL test establishes only the stated process-reopen
boundary; it does not establish unflushed WAL recovery or machine power-loss
durability. Internal cancellation flags are not application-controllable through
this version's public request API.

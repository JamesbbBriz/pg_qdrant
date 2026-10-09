# Capability coverage contract

Status: formal product scope with the selected P0 feasibility baseline verified. The [P0 decision](evidence/p0-feasibility.json) accepts an implementation route; it does not complete these capability contracts. This document maps the intended embedded retrieval surface to dependencies, extension work, and acceptance conditions. The managed-helper development build now provides a restricted installed source-table capture, durable consumer and single-field BM25 SQL path, described in [the integration status](p1-integration.md). No capability is release-supported; the complete contracts below remain open.

Baseline: [dependency versions and upgrade policy](dependencies.md). Public API/source inspection refers to Qdrant Edge `0.8.0`; rolling Qdrant Server documentation is supplementary, not a replacement for that version's embedded API.

## Coverage and evidence rules

Each capability ID must acquire: input/configuration contract, dependency/API mapping, owner, SQL exposure or managed internal use, budgets, authorization semantics, compile probe, runtime acceptance test, example, and upgrade/rebuild classification.

Evidence progresses through source/API-reviewed, compile-verified, engine-runtime-verified, SQL-integration-verified, and release-supported. Complete product acceptance remains pending. Public API compile sentinels and synthetic runtime probes are recorded in the [P0 inventory](../crates/edge-probe/README.md); their scope is narrower than full product acceptance. Source review is not implementation evidence. Every ID has a detailed owner, contract and acceptance entry in the [work ledger](work-items.json).

The [historical P0 CI record](evidence/p0-current-ci.json) binds 27 named normal engine tests, four SQL profiles with seven source groups each, and the bounded fault experiments to head `57c58fc`. Its [native/CPU evidence](evidence/p0-native-ci.json) has a restricted platform scope. That fixture evidence does not imply full capability acceptance or validate later development code.

The [fixed-version shard method audit](p0-edge-methods.md) maps 44 declarations
on four selected public shard/read surfaces. Its 43 concrete call bodies are
compile-verified; `refresh_with` retains a documented public-construction
exclusion. This covers the listed methods, not every auxiliary type or runtime
combination, and does not promote them to SQL or release support.

Every public Edge query/match variant, vector/storage kind, payload-index schema, and read/update/lifecycle operation must be mapped to an ID, used internally, or explicitly marked outside the product scope. Newly introduced upstream variants require a mapping decision before an upgraded release. A complete coverage inventory does not mean all capabilities are implemented or that every combination is valid.

## Development capability discovery

The versioned `qdrant.capabilities(index_name text DEFAULT NULL) -> jsonb` interface retains all 54 IDs. The managed-helper build reports its installed catalog and partial SQL adapters separately from release support. [Index-aware discovery](capability-discovery.md) checks registered-owner, source/column permissions, generation and declared-slot readiness; it does not execute a query. [Dense representation contracts](dense-representations.md) define freshness and the whole-source readiness requirement. [Hybrid search](hybrid-search.md) defines native RRF/DBSF version math and candidate scope. The `explore` class connects bounded [source recommendations](source-recommendations.md), [Discover/Context](source-context-discovery.md), [scored feedback](source-feedback.md) and [MMR](source-mmr.md) to current source-key/model admission. [Score Formula](source-formula.md) wraps bounded non-explore query stages with typed arithmetic. [Source-key retrieval](source-retrieve.md) uses native Edge reads with current authorized SQL version rechecks. [Declared scalar payload](source-payload.md) connects typed native field indexes to the same source identity, separate payload fingerprint, durable delivery and current-source retrieval. Generic filters, grouping, facets, payload Formula and ordering remain open. Direct-worker feasibility builds do not enable product indexing.

Without an index it should report extension/engine versions, build features, API support and capability status. With an index it should also report effective lexical backend, generation, configured representations, and readiness or rejection reasons. Keep these distinctions explicit: upstream primitive exists, adapter supports it, feature is release-tested, and this index has the necessary data.

The planner must consult the same registry used by this interface. A missing capability or unsupported combination yields a structured error, or an explicitly permitted ready fallback recorded in execution metadata. New upstream features do not become public SQL support merely by updating a dependency.

## FTS: ranking, matching, analysis, and presentation

FTS is not synonymous with BM25. Local ranking and text predicates need separate derived structures and a shared versioned analysis policy. [Local BM25](https://qdrant.tech/documentation/edge/edge-bm25/), [text-filter semantics](https://qdrant.tech/documentation/search/text-search/text-filtering/).

### Upstream primitives

| ID | Capability | Dependency / exposure | Required acceptance |
| --- | --- | --- | --- |
| F01 | Local BM25 ranking | `EdgeBm25`, named sparse vector with declared IDF policy | Offline document/query encoding; scoring and delete/update goldens |
| F02 | Learned lexical ranking | Supplied SPLADE/other sparse model vectors | Model/vocabulary/dimensions contract; stale outputs rejected |
| F03 | miniCOIL-compatible retrieval | Model-supplied representation, distinct contract | Document/query encoders and scoring/IDF agree; fixed-model quality evaluation |
| F04 | Tokenization and normalization | Edge BM25 configuration and text-index parameters | Word/whitespace/prefix/multilingual; lowercase/folding/stopwords/stemming goldens |
| F05 | Chinese and multilingual analysis | Configured multilingual path and upstream build features | Chinese/English mixed text, normalization, punctuation, names, and identifiers |
| F06 | All/any term matching | `Match::Text` / `TextAny` | AND/OR semantics, indexed/no-index contract, analyzer parity |
| F07 | Boolean exclusion/composition | `Filter` and controlled compiler | must/should/must_not/min_should/nesting; authorization cannot be overridden |
| F08 | Contiguous phrase matching | `Match::Phrase`, phrase-enabled text index | Order and adjacency; field/array boundaries and stopword behavior |
| F09 | Token-prefix matching | Prefix tokenizer, distinct input-processing rules | Document/query asymmetry, Unicode limits, token and candidate budgets |
| F10 | Whole-value exact identifiers | Keyword `Match::Value` | Case policy and exact SKU/URL/path preservation |
| F11 | Whole-value identifier prefixes | `Match::Prefix`, prefix-enabled keyword index | Distinct from F09; keyword-schema rebuild and prefix-filter semantics |
| F12 | Field selection and weighting | Named field representations and declared score policy | Field boundaries, weights and normalization; no unsupported BM25F claim |

Edge's [Match API](https://docs.rs/qdrant-edge/0.8.0/qdrant_edge/enum.Match.html), [text-index parameters](https://docs.rs/qdrant-edge/0.8.0/qdrant_edge/struct.TextIndexParams.html), and [keyword-index parameters](https://docs.rs/qdrant-edge/0.8.0/qdrant_edge/struct.KeywordIndexParams.html) establish the primitive surface. Ranking does not imply all-term matching. A text predicate's analysis configuration does not change BM25's analysis configuration.

SPLADE and miniCOIL are not offline models automatically instantiated by `EdgeBm25`. They require compatible supplied representations or an explicitly separate model integration. [SPLADE](https://qdrant.tech/documentation/fastembed/fastembed-splade/), [miniCOIL](https://qdrant.tech/documentation/fastembed/fastembed-minicoil/).

### Required lexical product work and gaps

The following are product requirements, not verified native Edge features. Evaluate upstream libraries where they avoid maintaining a new search engine; the named optional candidate is Tantivy, with adoption subject to the dependency/lifecycle decision.

| ID | Capability | Proposed owner / library candidate | Required acceptance |
| --- | --- | --- | --- |
| F13 | Bounded typo tolerance and fuzzy prefixes | Lexical adapter; Tantivy fuzzy primitives candidate | Edit-distance/expansion limits; exact-ID protection; multilingual misspelling set |
| F14 | Proximity/phrase slop | Position-aware lexical adapter; Tantivy candidate | Explicit positions, distance and field semantics; not mislabeled contiguous phrase filtering |
| F15 | Controlled advanced query syntax | Query compiler; parser-library decision | Field scoping, quotes, escapes, Boolean grouping and boosts; plain-text mode remains predictable |
| F16 | Synonyms and query expansion | Versioned dictionary/query policy | Directional/multiword behavior, scoring and expansion budgets, rebuild classification |
| F17 | Snippets and highlighting | Source-aware analyzer/result layer; Tantivy snippet candidate | Real original-text offsets; correct Unicode and normalization mapping; permission/version checks |
| F18 | Autocomplete and query suggestions | Instant query plan and suggestion policy | Prefix recall, ranking, typo interaction, freshness, ACL and latency budgets |
| F19 | Facets/counts for lexical results | Statistics planner with explicit match domain | Full lexical domain versus candidates; document versus point counts; authorized scope |
| F20 | Exact-match protection and field relevance UX | Product query/ranking policy | Identifiers, phrase requirements and title/body expectations on held-out queries |

Fuzzy matching, synonyms, or prefix tokenization alone do not establish Meilisearch-equivalent behavior. Expanded sparse terms do not establish where a word occurred in source text; semantic hits must not receive invented lexical highlights.

The [standalone Tantivy 0.26.2 experiment](../experiments/tantivy-probe/README.md)
has a separate locked dependency graph. Its [five CI semantic cases](evidence/p0-tantivy-ci.json) repeat the [local observations](evidence/p0-tantivy-local.json) on the identified current build.
It exercises fuzzy matching and returned-expansion rejection, phrase/slop and
parser behavior, literal snippets, and writer/reader visibility. These are
candidate-engine runtime checks. Tantivy is not linked into the extension or
Edge adapter, and pg_search remains an unexecuted fixed comparator. Hard fuzzy
work bounds, shared analyzer/offset policy, authorization, two-engine lifecycle,
held-out relevance and all F13–F20 acceptance requirements remain open under
[ADR 0002](adr/0002-lexical-gap-strategy.md).

### Analyzer configuration contract

Declare one lexical policy per field/representation: tokenizer, language defaults, normalization, token length, stopwords, stemmer, phrase positions, prefix strategy, field identity, and dictionary/model version.

Compile that policy separately into `EdgeBm25Config`, `TextIndexParams`, and `KeywordIndexParams` as needed. They are distinct configuration types and may have different defaults. Explicitly handle BM25's English defaults for Chinese/mixed-language content; multilingual tokenization alone does not disable those defaults.

Persist the effective configuration hash. Incompatible policy changes require a new index generation, and affected representations must be re-encoded. Prefix representation and primary BM25 are separate when their scoring/tokenization contracts differ. A phrase constraint must be applied during retrieval to the declared fields, not just after a top-k result has been collected.

The fixed-version [lexical probe](../crates/edge-probe/tests/lexical.rs) observes an additional F09 boundary: the prefix tokenizer truncates query tokens beyond `max_token_len`. The planner must reject a required prefix beyond the proven analyzed Unicode-scalar bound before calling Edge, or execute an explicitly permitted ready alternative that preserves the entire predicate. If the configured normalization/stemming parity cannot be established, reject the combination. Do not infer that bound from sparse IDs, silently truncate a query, substitute whole-value F11 semantics, or attempt to repair the predicate after top-k retrieval. The probe's word/phrase/array/Unicode cases are engine observations; the SQL compiler and complete multilingual parity remain work.

The [negative-input matrix](evidence/p0-negative-inputs-local.json) also records
an F08 prerequisite gap: a phrase query without a text index is accepted with
raw case-sensitive substring behavior; a text index with phrase matching
disabled accepts the query but returns no fixture hits. The phrase-capable
control returns the expected token matches. The planner must therefore require
and validate the declared phrase-capable index and analysis policy before
calling Edge. Neither an empty result nor substring behavior is an acceptable
silent fallback for a required phrase predicate. This adapter check remains
unimplemented.

## Vector representations and storage

| ID | Capability | Dependency / exposure | Required acceptance |
| --- | --- | --- | --- |
| V01 | Dense and named vectors | Edge vector schema | Dimensions, configured distance, normalization, model version and schema updates |
| V02 | Sparse vectors | Edge sparse schema | Valid/unique indices, values, vocabulary identity, IDF modifier/scope/corpus policy |
| V03 | Multivectors/MaxSim | Configured multi-vector comparator | Token width, bounded token count, candidate scoring and no global-exact claim |
| V04 | Quantization | Edge scalar/product/binary/Turbo configurations | Actual supported mode/CPU/precision combinations; recall/resource and reopen checks |
| V05 | Stored precision and memory placement | Edge storage parameters | Supported float32/float16/uint8/Turbo modes; source precision and rescore semantics |
| V06 | Matryoshka representations | Compatible model contract plus query plan | No arbitrary truncation; declared short/full relationship and rescoring |
| V07 | Visual global/patch representations | Compatible supplied model outputs | Cross-modal model agreement; grouping, dimensions and matrix budgets |
| V08 | Native binary/bit input distinction | Input-type audit gate | Separate bit-vector inputs from binary quantization; explicitly reject unsupported input kinds |

Evidence: [vector concepts](https://qdrant.tech/documentation/manage-data/vectors/), [quantization](https://qdrant.tech/documentation/manage-data/quantization/), [Edge configuration](https://qdrant.tech/documentation/edge/edge-api/configuration/). This inventory is a review scope; it does not promise all listed representations or storage combinations are valid in Edge `0.8.0`.

The [17-case engine input characterization](evidence/p0-negative-inputs-local.json),
whose named harness also passes in [current CI](evidence/p0-current-ci.json), observes typed errors for missing vector names and wrong dense/MaxSim widths,
and checked-constructor errors for ragged tokens and malformed/duplicate sparse
indices. It also records acceptance of nonfinite values, nonfinite scores or
empty results, and an actual caught Rust panic after a public sparse struct
bypasses shape validation. These observations do not satisfy V01–V03 or L08
input rejection. The adapter must validate finite components, dimensions,
rectangular token rows, equal sparse lengths and unique indices before any
engine call. Each case uses its own bounded child; no safe post-error owner
reuse, rollback or SQL error mapping is established.

## Query execution and filtering

| ID | Capability | Dependency / exposure | Required acceptance |
| --- | --- | --- | --- |
| Q01 | Nearest retrieval and exact candidates | Edge vector query and search parameters | Dense/sparse/multivector combinations, limits, score direction and candidate domain |
| Q02 | Nested prefetch and candidate reranking | Edge `QueryRequest` / `Prefetch` | Stage budgets, required representations, filters in every branch, cancellation |
| Q03 | RRF with k/weights and DBSF | Edge `Fusion` | Fixed-version ranking/tie behavior; missing branches; no unnormalized score summation |
| Q04 | Recommendation strategies | Edge query variants | Positive/negative inputs, supplied-vectors/ID resolution policy and authorization |
| Q05 | Discover/context | Edge query variants | Context pairs, budgets and authorized examples |
| Q06 | Relevance feedback | Edge feedback query | Supported strategy semantics and filtered example domain |
| Q07 | MMR | Edge MMR | Actual vector relevance/diversity behavior, candidate limits; no arbitrary external-score promise |
| Q08 | Formula scoring | Edge formula/configuration adapter | Supported score/payload/time/geo expressions, typed inputs, defaults and limits |
| Q09 | Grouped search | `EdgeShardRead::query_groups` | Distinct-document budget and authorized traceable source rows |
| Q10 | Order-by, scroll, sample, retrieve | Edge read/query APIs | Ordering/offset semantics, bounded samples, authorization and generation pinning |
| Q11 | Count/facet/search matrix | Edge read APIs | Scope, exact/estimated flags, sample/neighbor permissions and response budgets |
| Q12 | Payload indexes and structured filters | Edge field-index schema and conditions | All mapped field kinds, ranges, geo, null/missing, arrays/nested conditions, HasId/HasVector |
| Q13 | Filter-aware HNSW and ACORN | Edge search/index configuration | Trigger conditions, restrictive filters, index-build ordering, quality/resource evaluation |
| Q14 | IDF scopes and schema changes | Edge sparse/vector-name operations | Corpus/scope statistics, add/drop representations, IDF changes classified and tested |

Evidence: [Edge reading API](https://qdrant.tech/documentation/edge/edge-api/reading-data/), [ScoringQuery](https://docs.rs/qdrant-edge/0.8.0/qdrant_edge/enum.ScoringQuery.html), [QueryEnum](https://docs.rs/qdrant-edge/0.8.0/qdrant_edge/enum.QueryEnum.html), [Fusion](https://docs.rs/qdrant-edge/0.8.0/qdrant_edge/enum.Fusion.html), [filtering](https://qdrant.tech/documentation/search/filtering/), [hybrid queries](https://qdrant.tech/documentation/search/hybrid-queries/).

Grouping and search matrix require the Rust `EdgeShardRead` trait. Externally ranked Tantivy results need an explicit outer fusion design; a shared point-ID mapping does not make their scores native Edge prefetch results.

## Lifecycle and PostgreSQL integration

| ID | Capability | Owner | Required acceptance |
| --- | --- | --- | --- |
| L01 | Create/load/flush/close and unique shard ownership | Edge adapter and owner process | No concurrent directory owners; persistent reopen and bounded resources |
| L02 | Point/vector/payload/index-schema updates | Engine adapter | Mapped update operations, source incarnation/version checks and replay |
| L03 | Optimization and unoptimized-data visibility | Maintenance scheduler | Synchronous optimize behavior, scheduling, query/cancel interaction and no silent loss of new points |
| L04 | Snapshots/read-only/WAL/load policy | Engine adapter capability audit | Supported read/restore/inspection modes; public/private API boundary; PG timeline validation |
| L05 | Transactional outbox, backfill, committed-change wait | PostgreSQL/source lifecycle | Rollback/savepoint safety, commit-order handling, fixed-event tickets and idempotent ACK |
| L06 | Representation freshness and model changes | Model/source contract | Late/stale outputs rejected; per-slot readiness and coverage |
| L07 | Index status, tasks, reconfiguration and rebuild | Catalog/lifecycle | Accurate states, new-generation catch-up/switch, cancellation and old-generation retention |
| L08 | SQL API/errors/driver interface | PostgreSQL adapter | PG types, SQLSTATE, actual volatility/parallel behavior and prepared-query contracts |
| L09 | Authorization, supported RLS and source rechecks | PostgreSQL/query policy | Every query/diagnostic/statistics path; unsupported policies rejected |
| L10 | Threads/IPC/resource control and recovery | Runtime/lifecycle | No PG calls from engine threads; deadlines, crash/kill/OOM/disk failure and startup checks |
| L11 | Package and dependency upgrades | Build/lifecycle | Exact version graph, supported PG/platform matrix, index migration and rollback |
| L12 | Backup/PITR/replication/failover support boundaries | Recovery contract | Independently validated modes; unsupported cases documented |

Evidence: [Edge lifecycle](https://qdrant.tech/documentation/edge/edge-api/shard-lifecycle/), [Edge vs Server](https://qdrant.tech/documentation/edge/edge-vs-qdrant-cluster/), [PG background workers](https://www.postgresql.org/docs/17/bgworker.html), [pgrx threading constraints](https://github.com/pgcentralfoundation/pgrx#caveats--known-issues).

The [public-lifecycle probe](../crates/edge-probe/tests/public_lifecycle.rs) verifies read-only fixture coverage and manual manifest refresh, snapshot-manifest inspection, update-only preview, and no-write skip/missing replay. Edge 0.8.0 update-only Store, Delete and empty bootstrap instead trigger actual unimplemented panics; its flush body is also source-reviewed as unimplemented. These subpaths are unavailable, not supported mutation alternatives. Ordinary `EdgeShard` writes have separate tests. The read-only experiment supplies its own manifest and does not establish automatic publication, snapshot archive production/restoration, production panic recovery or a PostgreSQL timeline contract. L01–L04 and L12 retain those remaining requirements.

The [fixed-source durability audit](evidence/p0-edge-durability-source.json) and
[ADR 0003](adr/0003-edge-durability-and-recovery.md) additionally constrain L04/L05:
update return is not a durable ACK, the inspected load path does not replay
logical WAL records, and loading can repair/mutate state. There is no reviewed
public durable cursor or WAL reclamation API. Successful load cannot alone
justify READY; exact-event PostgreSQL ACK after serialized explicit flush,
unclean-artifact preservation, reconstruction and WAL-growth handling remain
required. These are source-reviewed boundaries, not completed SQL behavior.

The [dirty-generation experiment](evidence/p0-dirty-rebuild-local.json), also
executed by the named harness in [current CI](evidence/p0-current-ci.json), adds a
narrow controller-policy observation for L01/L04/L05/L07. After a known applied
mutation and before an explicit post-mutation flush, an owned child is killed.
The test controller refuses to reopen that dirty generation, preserves its
identity/file hashes through the experiment, and reconstructs a separate
generation only from a complete authoritative source fixture. Explicit flush
and reopen of that new generation preserve expected deletions, replacement
incarnation, vectors and filtered query identities. This does not inspect old
WAL survival, implement PostgreSQL capture/ACK, preserve artifacts after test
cleanup, or verify concurrent catch-up, production readiness/cutover or
machine-loss durability.

The [CI6 record](evidence/p0-capacity-and-sql-ci.json) adds diagnostic SQL evidence
for L08 array conversion and L10 supervisor/fence recovery. The
[CI7 record](evidence/p0-source-capacity-oom-ci.json) executes the original six
superuser-only [identity/recheck groups](p0-source-recheck.md). The [current CI
record](evidence/p0-current-ci.json) passes all seven groups in each of the four
SQL profiles, including cancellation during SPI planning, namespace-guard
cleanup and a matched retry in the same backend. The tested source includes
the heap-only restriction; this does not claim an adversarial custom-AM runtime
case. These diagnostics do not complete L05 identity allocation, L09 product
permissions/RLS or any product capability. The full 54-ID registry remains open.

## Feature combinations and release gates

Validate combinations, not only isolated functions:

- Phrase/Boolean/identifier constraints with BM25, dense, learned sparse and each fusion branch.
- Restrictive authorization with prefetch, MaxSim, MMR, recommendation, grouping, facets and matrix sampling.
- Quantization/MRL/HasId/exact search/rescoring with the selected storage type and model.
- Text updates with late sparse/token encodings, deleted rows, primary-key reuse and rebuilding generations.
- Chinese analysis, normalized offsets, prefixes, fuzzy expansion and identifier protection.
- Optimize/rebuild/restart/upgrade during concurrent SQL queries and changes.

A capability is release-supported only after its contract, compiled/runtime/SQL evidence, permissions, resource behavior, quality checks, example and migration rule exist. Missing capabilities keep their ID and explicit status; they cannot disappear from the scope to make an upgrade pass.

Distributed clustering, server collection aliases, server REST/gRPC endpoints, cloud inference, and server-managed replication are outside the embedded product scope. Arbitrary historical MVCC exact top-k, same-transaction read-your-writes, and unrestricted RLS are outside the initial support contract. Their absence must remain explicit.

# ADR 0002: Evaluate richer lexical search without weakening the source contract

Status: **next experiment selected; dependency adoption pending**.

Reviewed: 2026-10-08. Owners: lexical adapter, query planner, results, source/lifecycle, PostgreSQL integration, quality, and distribution maintainers.

## Decision

Keep published Qdrant Edge as the core retrieval dependency. Select an isolated **direct Tantivy 0.26.2 experiment** as the first investigation of fuzzy and positional matching. Compare it against extension-owned query/presentation policy and a separately installed **pg_search 0.26.0**. This selects work to perform, not an additional production dependency.

No Tantivy or pg_search dependency is adopted by this ADR. Neither candidate has been compiled, installed, or quality-tested in this project's dependency graph. Adoption requires the semantic, resource, authorization, synchronization, lifecycle, quality, and distribution gates below. F13–F20 remain formal requirements with individual owners; an unsuccessful experiment does not remove them from the product.

The [capability contract](../capabilities.md), [dependency policy](../dependencies.md), [baseline](../dependency-baseline.json), and [work ledger](../work-items.json) remain authoritative. This ADR refines the lexical decision in [ADR 0001](0001-embedded-engine-boundary.md). It does not complete P0 or authorize a full-FTS product claim.

## Fixed source evidence

The following evidence is from published packages or immutable release sources, not rolling Qdrant Server documentation.

| Candidate | Fixed source and declaration | Evidence reached here |
| --- | --- | --- |
| Qdrant Edge | Registry `qdrant-edge =0.8.0`; Apache-2.0 declaration; archive SHA-256 `0b8072302c87506a34bffec9bc16dbdcd36df8ab1321406b6e141530348c7e54` | Source review and the existing [engine probe](../../crates/edge-probe/README.md). Its narrow runtime checks do not implement F13–F20. [E1] |
| Direct Tantivy | Registry `tantivy =0.26.2`, published 2026-09-08; MIT declaration; declared Rust minimum 1.86; archive SHA-256 `861facfabd71044968f364837f9a083b56464ba5a59079f88706ee5c451ca069` | Registry metadata, archive checksum, public source/API review only. No combined build or relevance evidence. [T1] [T2] |
| pg_search | Stable ParadeDB release `v0.26.0`, published 2026-10-03; commit `aa7f9dd407018036d144a54a3f875c5a00d20cda`; workspace license declaration `AGPL-3.0` | Release, immutable manifests, license, lexical APIs/docs and regression-fixture review only. This was the latest stable release returned by the upstream release endpoint on the review date. [P1] [P2] [P3] |

Tantivy's published defaults enable `mmap`, `stopwords`, `lz4-compression`, `columnar-zstd-compression`, and `stemmer`. Those are build features, not a field's effective analysis policy. Its compiler declaration does not establish compatibility of the combined graph. [T2]

pg_search is a PostgreSQL extension, not a drop-in use of registry Tantivy. This release pins pgrx `0.19.2` and ParadeDB's Tantivy Git revision `7540730ee070f1e668017bf0f003024554eaab0a`, with additional fork features. Its PG17 feature exists, but its default is PG18. Its graph also includes DataFusion and a Linux OpenBLAS-backed component. A PG17 comparison must select that feature explicitly and retain the release lockfile; direct integration into this project's pgrx `0.19.3` graph is not assumed. [P2] [P4]

### What the primitives establish

Edge provides local BM25, text AND/OR, Boolean filters, contiguous phrases, token prefixes, and independent whole-value keyword matching/prefixes. BM25 and text-index configurations are distinct, including different language defaults. The reviewed public query/match surface supplies no fuzzy-distance, phrase-slop, synonym-dictionary, suggestion-ranking, or source-highlight-offset contract. The existing tests cover selected primitives, not a complete lexical experience. [E2] [E3] [E4] [E5]

Tantivy supplies concrete candidates:

- `FuzzyTermQuery::new` and `new_prefix` support edit distances 0–2 and an explicit transposition-cost choice. The public constructors provide no expansion-limit argument. A small `TopDocs` limit therefore must not be advertised as a bound on fuzzy term enumeration. [T3]
- `PhraseQuery` requires indexed positions and exposes slop. Slop is a total movement budget; it can admit reordered terms, with an adjacent transposition costing two. That differs from a strict ordered-distance requirement and from Edge's contiguous phrase predicate. [T4]
- `QueryParser` exposes field selection, boosts, Boolean composition and phrase syntax. Its default composition is OR. The project must compile its own validated contract instead of inheriting defaults or silently accepting lenient parsing errors. [T5]
- `SnippetGenerator` retokenizes text using the field analyzer and derives terms through the query's term visitor. `FuzzyTermQuery` does not populate that visitor in this release. `snippet_from_doc` joins repeated field values with spaces. Fuzzy highlights, array boundaries, source offsets and field provenance therefore require explicit additional work. [T3] [T6]
- `PhrasePrefixQuery` has a separate expansion limit. Counts/facets and reader/writer APIs provide useful primitives; they do not create a permission-safe completion corpus or a cross-engine commit protocol. [T7] [T8] [T9]

The fixed pg_search release already exposes fuzzy SQL options, ordered/unordered proximity, phrase prefixes, snippets and snippet positions. Its current documentation still excludes fuzzy highlighting, and its regression inputs explicitly exercise unsupported fuzzy/phrase and fuzzy/proximity combinations. Reuse cannot be credited with those combinations without new evidence. [P5] [P6] [P7] [P8]

Neither candidate is accepted as a complete synonym system or Meilisearch-equivalent ranking experience. A shared tokenizer library, fuzzy primitive, or snippet helper does not establish that product behavior.

## Alternatives and costs

| Route | Useful work to reuse | Project work and cost | Decision |
| --- | --- | --- | --- |
| Supplemental query/presentation policy over Edge | Existing text/keyword predicates, BM25, filtering and fusion; controlled parser; explicit dictionary expansion; identifier policy; source-based presentation | Lowest additional storage/topology cost. Full typo/proximity support still needs a mature term/position engine or an explicitly limited contract. Final top-k text scanning cannot recover missing candidates or implement a required retrieval predicate. Analyzer parity and trustworthy offsets are still substantial work. | Keep as the baseline and implement product policy needed by every route. Do not create a new fuzzy/positional engine as an assumed inexpensive default. |
| Direct Tantivy | Published Rust fuzzy/phrase/query/parser/snippet/collector primitives; direct control of schema, analysis, budgets and source identity | A second derived index, commit/reload boundary, memory/disk/merge schedule, recovery path and format upgrade. The project owns the SQL adapter, shared authorization, cross-engine freshness and outer fusion. Fuzzy work bounds and fuzzy highlights are open gates. | First isolated experiment. Potentially fits the existing owned-data engine boundary, subject to measurement and topology evidence. |
| Reuse pg_search through its SQL extension interface | Existing PostgreSQL indexing/query integration, lexical operators, ordered proximity, snippets and aggregate execution | Additional installed extension and independently versioned SQL/PG/platform support. Must prove co-installation, hooks/plans, cancellation, permissions, source-ID extraction, visibility alignment with asynchronous Edge, and coordinated rebuilds/upgrades. Its forked dependency graph and AGPL distribution obligations need their own review. | Fixed comparator and a real alternative if it materially reduces total work or improves quality. No source copying or direct Rust linkage is selected. |

The pg_search route may avoid manually dual-writing its PostgreSQL-maintained index. It does **not** remove the need to align its committed view with Edge's asynchronous projection, define when a combined plan is ready, or recover a generation pair. Conversely, linking Tantivy does not provide pg_search's PostgreSQL integration for free.

The license declarations above are source facts. Before adoption, distribution must inventory the complete resolved dependency/native/dictionary licenses and notices. Assess the actual integration and redistribution obligations of pg_search; calling its SQL API does not by itself settle that assessment. Do not copy its AGPL implementation into Apache-2.0 project files or relabel it. Preserve the notices of any adopted MIT dependency. [T2] [P3]

## Ownership and acceptance for every gap

These budgets are **initial experiment limits**, not published performance promises. Unsupported combinations must produce an explicit unavailable/error result; they cannot disappear from the ledger.

| ID | Accountable owner | Next implementation/evaluation | Acceptance and migration boundary |
| --- | --- | --- | --- |
| F13 | Lexical adapter, with planner | Compare whole-token fuzzy and fuzzy-prefix behavior at distances 0/1/2; use mature upstream matching. Test a public-API way to bound enumeration, or reject the route for production use until one exists. | Exact identifiers never enter fuzzy expansion by default. Bound matches to 32 terms per input term and 256 total; overflow is an explicit error, not silent truncation. Test Latin transpositions and separately specified Chinese/mixed-language cases. New indexed representation requires a generation. |
| F14 | Lexical adapter | Compare Edge adjacency, Tantivy slop, and pg_search ordered/unordered proximity using the same token positions. | Goldens distinguish order, repeated terms, stopword gaps and array/field boundaries; initial distance/slop budget 0–4. Required predicates apply before ranking/candidate limits. Position-policy changes require reindexing. |
| F15 | Planner | Define a project-owned AST for fields, quotes, Boolean groups, exclusions and bounded boosts; evaluate parser reuse behind that AST. | Plain text has no hidden operators. Maximum 32 input tokens, 64 AST nodes and nesting depth 8. Unknown fields, invalid escapes and illegal combinations fail. Grammar changes are versioned query/config changes; index changes are classified separately. |
| F16 | Lexical policy and model contract | Version a small directional dictionary; compare query-time alternatives, including multiword alternatives and phrase interaction. | Record dictionary revision/hash/license, direction, positions, expansion weights and the shared F13 expansion budget. Query-time dictionary changes are normally query-only; document-time expansion requires reindexing and possibly representation re-encoding. No automatic synonym-support credit from fuzzy/snippet APIs. |
| F17 | Results and analyzer policy | Evaluate source-text snippets and position mapping; separately solve literal, normalized, prefix, fuzzy and synonym match attribution. | UTF-8 byte ranges are half-open offsets into the original individual field value, with a field/array-element identity. Validate Unicode boundaries and escape output. At most two 240-scalar-character fragments per hit. Semantic-only hits have no invented lexical highlights. Analyzer changes require new offset evidence and the classified rebuild. |
| F18 | Instant planner and lexical policy | Compare token/phrase prefixes, fuzzy prefixes, and suggestions from an explicitly authorized title/identifier vocabulary. | Declare a minimum prefix length, expansion/candidate budgets, deterministic ranking, and freshness. Query logs are not the default suggestion corpus. Measure latency and zero-result recovery separately from ordinary search. A new suggestion representation has independent readiness/rebuild requirements. |
| F19 | Statistics planner and authorization | Compare exact authorized lexical-domain counts/facets with bounded candidate statistics, including grouped documents. | Expected point and distinct-document counts must agree with the fixture oracle. Candidate counts are labeled; unknown full-domain totals are `null`. Bound facets to 10 fields and 100 buckets. No private corpus frequency or suggestion count is exposed. |
| F20 | Product ranking policy and quality | Compare explicit identifier equality, field boosts and lexical/semantic blends on held-out queries. | Zero violations of an explicitly required identifier equality. An ordinary keyword is not always forced above a semantic match. Freeze ranking/tie/fusion policy and retain ablations; query-only tuning never silently changes a must-match predicate. |

The integration owners for all eight rows are source/lifecycle (L01, L05–L07), PostgreSQL authorization/results (L08–L10), and distribution/upgrade (L11). Completion requires their evidence as well as an isolated lexical query test.

## Contracts required for a second lexical engine

### Analysis and source positions

One field policy records language, tokenizer, lowercase/folding, stopwords, stemmer, token-length rules, positions, array boundaries and dictionaries. Compile it independently into BM25, Edge text/keyword indexes, and the candidate lexical representation; persist the effective hashes. Test both document and query processing. English-default stemming/stopwords must not accidentally alter Chinese/mixed-language queries.

Tantivy's public `PreTokenizedString` can carry original text and tokens, so it is a candidate transport for an independently validated common analysis output. It does not prove Edge parity, and the query/snippet analyzer must agree with the indexing stream. A direct Charabia or dictionary dependency would need its own exact pin/features and normalization/offset tests. pg_search's `chinese_compatible` tokenizer emits individual CJK characters; it cannot be assumed equivalent to Edge's multilingual segmentation. [T10] [P9]

Do not derive words or highlight locations from BM25/learned sparse IDs. When normalization changes byte lengths, retain an explicit original-source span mapping. Never concatenate field/array values and then present the resulting offsets as original offsets.

### Identity, authorization and required predicates

Both engines use the extension's stable source key, incarnation, revision, representation fingerprint and document key. Tantivy segment/doc addresses and PostgreSQL `ctid` are internal lookup details, never cross-engine identity. An older event or encoding cannot overwrite a newer incarnation or content fingerprint.

The trusted PostgreSQL caller domain constrains every lexical and Edge recall, rerank, suggestion, grouping, facet, cache and explanation path. SQL `WHERE` output tests are necessary but do not prove filter pushdown or prevent intermediate disclosure. Unsupported RLS remains rejected. Final source/permission/version rechecks remain mandatory.

For a required fuzzy/proximity predicate unavailable in Edge, prototype an authorized complete match-ID set with a 4,096-ID limit, then apply that set to every Edge branch. If the complete set exceeds the limit, reject that plan or use an explicitly selected plan with a different documented candidate domain. Truncating a top-k lexical result list and calling it the complete predicate domain is invalid. Final result filtering cannot substitute for this contract.

### Commit, readiness and generations

For direct Tantivy, a PostgreSQL outbox event has per-engine application/durability status. Replaying the same source version must produce one live representation, not duplicate Tantivy documents. Prototype stable-ID delete/add ordering and record source revision in the stored record. Tantivy `commit()` and reader `reload()` are separate from Edge persistence and from PostgreSQL commit. No atomic three-engine transaction is assumed. [T9]

A combined-plan wait ticket succeeds only when its sealed events meet the tested persistence and query-visibility conditions in every required representation. A failure after one engine commits leaves the combined representation catching up/degraded. An explicitly permitted Edge-only plan may remain usable and must report its effective plan; it cannot silently satisfy a plan requiring the missing lexical representation.

Rebuild a generation pair, backfill and catch up both sides, validate, then atomically switch the catalog pointer. Queries pin that pair. Keep the old pair until references are released and rollback requirements are satisfied. Test interruption before either commit, between commits, before ACK, during cutover and during cleanup. The pg_search alternative must demonstrate equivalent readiness/visibility and rebuild coordination through its supported SQL/lifecycle interface.

### Outer fusion, budgets and upgrades

External lexical ranks are not an input variant of Edge `Prefetch`/`Fusion`. The project owns any outer fusion, deduplication, source validation and distinct-document refill. The first comparison uses a separately named unweighted rank-fusion baseline, `sum(1 / (60 + one_based_rank))`, with a deterministic source-ID tie breaker. This is an experiment setting, not an inferred Edge weighting rule or a tuned product default. Do not add raw BM25, cosine and MaxSim scores. [E4] [E6]

Limit the initial experiment to two indexing/search threads per engine, 200 candidates per branch, 20 returned chunks or 10 distinct documents, and 128 KiB result frames. Record RSS, mappings, disk size and merge work independently of PostgreSQL `work_mem`. Stress the fuzzy path with high-cardinality vocabularies: collector limits alone cannot enforce enumeration, deadline or memory limits. A route that cannot enforce its declared budget remains unavailable.

Every engine/analyzer/dictionary/SQL change receives a migration classification. Upgrade evidence includes public API/features/license review, new lockfiles, semantic/offset/quality goldens, authorization and replay tests, old-generation reopen or rebuild, and an executed rollback. A pg_search upgrade additionally pins its release and fork revisions and checks co-installation on the supported PG major. Preserve compatible generations when an older binary cannot read a new format.

## Bounded comparison work

Allocate an initial **40 engineer-hour investigation budget**, with actual time recorded per work item. This is a stop-and-report budget, not a completion estimate or permission to drop a gap. If a build, budget boundary, license/distribution question or correctness gate blocks a route, record the evidence and next decision at that point.

| Work item | Owner and executable deliverable | Exit evidence |
| --- | --- | --- |
| LX-01: source and build baseline | Distribution + lexical adapter. Create an isolated evaluation workspace with `tantivy =0.26.2`; explicitly select features, generate/archive its lockfile and dependency/license inventory. Build the fixed pg_search release separately with PG17 and its matching pgrx tooling, or record a verified PG17 release package. | Locked compile logs, exact versions/checksums/features/native requirements. Candidate compilation cannot silently modify the core lockfile. |
| LX-02: matching and budgets | Lexical adapter. Public-API probes for fuzzy, phrase/slop, prefix, token positions, commit/reload and actual source spans. Run the seed below and a vocabulary-growth stress fixture. | Expected ID sets, scores where meaningful, expansion/scan counts, timeout and memory outcomes. Explicitly resolve the missing fuzzy expansion cap before a production proposal. |
| LX-03: analyzer and experience | Results + lexical policy. Create token/offset goldens and original bilingual relevance fixtures, including F13–F20 combinations. | Original-source spans, protected identifiers, no cross-field/array matches, correct declared phrase/slop semantics, and actual fuzzy/synonym highlighting evidence or an explicit open gap. |
| LX-04: comparative quality | Quality. Freeze a 64-document/256-chunk set and 160 queries: 20 per F13–F20, half English and half Chinese/mixed; split development/held-out queries by document topic before tuning. Include one 20-chunk document, two tenant domains, and published content/license provenance. | Hashes, qrels/expected counts, text-only and hybrid ablations, Recall@20/NDCG@10/MRR on applicable queries, identifier errors, fill rate, duplication and coverage. These small fixtures do not replace the later real-corpus benchmark. |
| LX-05: integration cost | Source/lifecycle + PostgreSQL integration. Prototype duplicate/out-of-order events, deletion/key reuse, one-engine failure, wait, paired cutover, role checks and cancellation. | No stale resurrection, false readiness or unauthorized result/snippet/count; measured recovery and rebuild work. A pg_search-only test does not establish Edge alignment. |
| LX-06: adoption decision | Maintainers for lexical, quality, lifecycle and distribution. Compare correctness, quality, installation, RSS/disk, build/update/merge/recovery costs and maintenance ownership. | A follow-up adoption or rejection ADR, plus updated ledger and dependency contract if adopted. No option wins merely by exposing more function names. |

These work items are planned; their new harnesses and fixtures do not exist merely because this ADR names them. Existing reproducible Edge evidence remains:

```sh
cargo test --locked -p pg-qdrant-edge-probe
```

For the Tantivy source smoke, extract the checksum-verified archive into an isolated directory and set `lexical_source` to that directory. The archive does not ship a Cargo.lock; the first command creates a separate evaluation baseline, which must be retained. These commands exercise published examples, not the product acceptance tests, and have not been run for this ADR:

```sh
cargo generate-lockfile --manifest-path "$lexical_source/Cargo.toml"
CARGO_BUILD_JOBS=2 cargo run --locked --manifest-path "$lexical_source/Cargo.toml" --example fuzzy_search
CARGO_BUILD_JOBS=2 cargo run --locked --manifest-path "$lexical_source/Cargo.toml" --example snippet
```

### SQL seed for the fixed pg_search comparator

Run only in a disposable PG17 database with **pg_search 0.26.0** installed. These are candidate-comparator calls, not pg_qdrant SQL. The operators/index syntax are present in the pinned upstream regression fixtures; this seed has not been executed here. [P8] [P10]

```sql
SELECT extversion FROM pg_extension WHERE extname = 'pg_search'; -- require 0.26.0
CREATE SCHEMA lexical_adr_probe;
CREATE TABLE lexical_adr_probe.chunks (
    id bigint PRIMARY KEY,
    tenant text NOT NULL,
    document_id bigint NOT NULL,
    body text NOT NULL,
    sku text NOT NULL
);
INSERT INTO lexical_adr_probe.chunks VALUES
    (1, 'a', 10, 'transaction recovery restores committed records', 'PG-001'),
    (2, 'a', 10, 'transaction reliable recovery', 'PG-002'),
    (3, 'a', 20, 'recovery transaction', 'DOC-001'),
    (4, 'b', 30, 'transaction recovery private', 'PG-SECRET'),
    (5, 'a', 40, 'catalog product identifier', 'PG-OO1'),
    (6, 'a', 50, '事务回滚不应进入检索索引', 'ZH-001');
CREATE INDEX lexical_adr_search ON lexical_adr_probe.chunks
USING paradedb (id, (tenant::pdb.literal), (body::pdb.simple), (sku::pdb.literal))
WITH (key_field = 'id');

-- Expected IDs: 1, 2, 3. Transposition cost one is explicit.
SELECT id FROM lexical_adr_probe.chunks
WHERE tenant = 'a' AND body === 'transactoin'::pdb.fuzzy(1, f, t)
ORDER BY id;

-- Expected IDs: 1, 2. Reverse order in row 3 is excluded.
SELECT id FROM lexical_adr_probe.chunks
WHERE tenant = 'a'
  AND body @@@ pdb.proximity_in_order('transaction', 1, 'recovery')
ORDER BY id;

-- Expected ID: 1. The snippet must refer to that source row only.
SELECT id, pdb.snippet(body) FROM lexical_adr_probe.chunks
WHERE tenant = 'a' AND body ### 'transaction recovery'
ORDER BY id;

-- Expected point count 2 and distinct-document count 1.
SELECT count(*) AS points, count(DISTINCT document_id) AS documents
FROM lexical_adr_probe.chunks
WHERE tenant = 'a'
  AND body @@@ pdb.proximity_in_order('transaction', 1, 'recovery');
```

Extend this seed with composed/decomposed accents, full-width identifiers, emoji, repeated terms, empty/all-stopword input, field/array boundaries, Traditional/Simplified Chinese policy, parser escapes, dictionary direction, private suggestions, and update/delete/key-reuse schedules. Keep the simple English analyzer above as a control; run separately named multilingual policies rather than claiming it supplies Chinese segmentation. Inspect execution plans and protected intermediate paths separately from these output assertions.

Report two comparisons: identical inputs/policies/budgets where semantics permit, and each candidate's explicitly documented reasonable configuration. If analyzer or scoring semantics differ, label the difference. Collect cold/warm p50/p95, build/update/reload/merge/recovery times, RSS and disk on the same recorded hardware/concurrency. Freeze numeric quality and operational thresholds after the baseline and before held-out evaluation. Authorization, identity, scope and offset correctness are hard gates regardless of average relevance gains.

## Consequences

Product query policy, dictionaries, snippets, suggestions, facets and ranking remain owned work under every route. Direct Tantivy is a plausible way to reuse mature lexical machinery while retaining the current adapter design, but the public-API budget and match-attribution gaps may change that decision. pg_search remains a credible comparator with meaningful PostgreSQL-specific capabilities and a separate integration/distribution cost.

Until the comparison and follow-up decision pass, capability discovery must report the rich lexical features as unavailable or pending with specific reasons. Core Alpha documentation must state its actually implemented subset, and full FTS acceptance must retain every F13–F20 gate.

## Primary sources

Source references above resolve to the exact package version or immutable pg_search commit. The reviewed registry archives are [Edge 0.8.0](https://static.crates.io/crates/qdrant-edge/qdrant-edge-0.8.0.crate) and [Tantivy 0.26.2](https://static.crates.io/crates/tantivy/tantivy-0.26.2.crate); the fixed extension release is [pg_search 0.26.0][P1].

[E1]: https://crates.io/api/v1/crates/qdrant-edge/0.8.0
[E2]: https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/bm25_embed.rs
[E3]: https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/data_types/index.rs
[E4]: https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/requests/query.rs
[E5]: https://docs.rs/qdrant-edge/0.8.0/qdrant_edge/enum.Match.html
[E6]: https://docs.rs/crate/qdrant-edge/0.8.0/source/src/shard/query/mod.rs
[T1]: https://crates.io/api/v1/crates/tantivy/0.26.2
[T2]: https://docs.rs/crate/tantivy/0.26.2/source/Cargo.toml
[T3]: https://docs.rs/crate/tantivy/0.26.2/source/src/query/fuzzy_query.rs
[T4]: https://docs.rs/crate/tantivy/0.26.2/source/src/query/phrase_query/phrase_query.rs
[T5]: https://docs.rs/crate/tantivy/0.26.2/source/src/query/query_parser/query_parser.rs
[T6]: https://docs.rs/crate/tantivy/0.26.2/source/src/snippet/mod.rs
[T7]: https://docs.rs/crate/tantivy/0.26.2/source/src/query/phrase_prefix_query/phrase_prefix_query.rs
[T8]: https://docs.rs/crate/tantivy/0.26.2/source/src/collector/facet_collector.rs
[T9]: https://docs.rs/crate/tantivy/0.26.2/source/src/indexer/index_writer.rs
[T10]: https://docs.rs/crate/tantivy/0.26.2/source/src/tokenizer/tokenized_string.rs
[P1]: https://github.com/paradedb/paradedb/releases/tag/v0.26.0
[P2]: https://github.com/paradedb/paradedb/blob/aa7f9dd407018036d144a54a3f875c5a00d20cda/Cargo.toml
[P3]: https://github.com/paradedb/paradedb/blob/aa7f9dd407018036d144a54a3f875c5a00d20cda/LICENSE
[P4]: https://github.com/paradedb/paradedb/blob/aa7f9dd407018036d144a54a3f875c5a00d20cda/pg_search/Cargo.toml
[P5]: https://github.com/paradedb/paradedb/blob/aa7f9dd407018036d144a54a3f875c5a00d20cda/pg_search/src/api/operator/fuzzy.rs
[P6]: https://github.com/paradedb/paradedb/blob/aa7f9dd407018036d144a54a3f875c5a00d20cda/pg_search/src/api/builder_fns/proximity.rs
[P7]: https://github.com/paradedb/paradedb/blob/aa7f9dd407018036d144a54a3f875c5a00d20cda/docs/reference/full-text/highlight.mdx
[P8]: https://github.com/paradedb/paradedb/blob/aa7f9dd407018036d144a54a3f875c5a00d20cda/pg_search/tests/pg_regress/sql/fuzzy.sql
[P9]: https://github.com/paradedb/paradedb/blob/aa7f9dd407018036d144a54a3f875c5a00d20cda/docs/reference/tokenizers/available-tokenizers/chinese-compatible.mdx
[P10]: https://github.com/paradedb/paradedb/blob/aa7f9dd407018036d144a54a3f875c5a00d20cda/pg_search/tests/pg_regress/sql/proximity.sql

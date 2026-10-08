# Dependencies and upstream upgrade policy

Status: active dependency and upgrade contract. The [machine-readable baseline](dependency-baseline.json) records exact build inputs and the validation reached by each component. The repository now includes P0 source, Cargo.lock, a [resolved dependency inventory](dependency-graph.json), and CI/update-bot configuration. A successful engine probe is not a successful PostgreSQL integration or release gate; see [build evidence](build.md).

## Dependency ownership

| Component | Dependency decision | Upgrade owner |
| --- | --- | --- |
| Retrieval, local BM25, sparse/dense/multivector indexing | Direct published `qdrant-edge` crate, candidate `=0.8.0` | Engine adapter |
| PostgreSQL types, functions, triggers, hooks, workers | Direct `pgrx`, candidate `=0.19.3`, PG17 feature | PostgreSQL adapter |
| Extension build/package tool | `cargo-pgrx 0.19.3`, matching the library | Build and distribution |
| Tokenization used by Edge | Edge-owned dependency graph and analyzer configuration | Engine adapter and lexical policy |
| Application vector generation | Optional application/model integration, outside the core build | Model contract |
| Rich lexical gaps | Evaluate Tantivy `0.26.2`; not adopted or required | Lexical adapter decision |
| SQL schema and migrations | Versioned project-owned API | Extension lifecycle |

The core integration should use published Edge types and methods. The `qdrant-client` REST/gRPC SDK is not the embedded engine dependency. FastEmbed/ONNX and model downloads are not mandatory dependencies for local BM25 or BYOV retrieval.

Keep the upstream engine as a dependency rather than copying its BM25, tokenizer, HNSW, sparse-index, or quantization implementations into the extension. The registry crate can bundle its own internal implementation modules; those remain upstream-owned. Imports should use its supported public surface. Anything under an upstream `internal` namespace needs a recorded exception and compatibility tests, even if technically exported.

## Candidate baseline evidence

The Edge `0.8.0` registry archive was checked against the registry SHA-256 recorded in the baseline. Its manifest uses Rust edition 2024 and does not declare a package-level `rust-version`; this is not evidence that all dependency versions build on an arbitrary older compiler. [Published Edge manifest](https://docs.rs/crate/qdrant-edge/0.8.0/source/Cargo.toml), [registry record](https://crates.io/api/v1/crates/qdrant-edge/0.8.0).

pgrx and cargo-pgrx `0.19.3` registry records declare Rust `1.96` as the minimum. Freeze an exact toolchain only after resolving and building the complete Linux dependency graph. [pgrx record](https://crates.io/api/v1/crates/pgrx/0.19.3), [cargo-pgrx record](https://crates.io/api/v1/crates/cargo-pgrx/0.19.3).

Edge `0.8.0` requests Charabia `0.9.9` with selected multilingual features. This is a version requirement, not a resolved transitive version. Its multilingual tokenizer uses Charabia with additional engine processing, including a distinct Japanese path. Charabia is also used by Meilisearch; sharing a tokenizer does not provide Meilisearch's ranking, typo policies, or UI behavior. [Edge package source](https://docs.rs/crate/qdrant-edge/0.8.0/source/), [Charabia](https://github.com/meilisearch/charabia).

If highlighting or a future Tantivy adapter needs a direct analyzer dependency, declare it explicitly and prove parity with the selected engine: normalization, stemming, stopwords, token boundaries, offsets, and field positions. Do not assume importing the same Charabia version reproduces the whole Edge analysis pipeline.

## P0 dependency deliverables

P0 must complete and validate the following. The manifest, lockfile and probes now exist; presence alone does not complete the gate:

- A real Cargo workspace with exact engine and PostgreSQL integration requirements.
- The matching cargo-pgrx tool, exact Rust toolchain, explicit PG major feature, and package feature list.
- A committed Cargo.lock with the resolved direct/transitive graph and checksums.
- Reproducible container/build inputs, PG development headers, pg_config selection, linker/system-library requirements, and CPU baseline.
- A dependency graph and license inventory from the actual build, including native components and model licenses when applicable.
- Compiled API probes for every upstream capability in the [coverage contract](capabilities.md).

The actual manifests use these exact dependency requirements, with the `cshim` feature explicitly selected for PostgreSQL integration:

```toml
[dependencies]
qdrant-edge = "=0.8.0"
pgrx = { version = "=0.19.3", default-features = false, features = ["cshim"] }

[features]
default = ["pg17"]
pg17 = ["pgrx/pg17"]
```

The actual pgrx template, needed features, and supporting dependencies must be resolved by compilation. Exact pins select an intentional direct dependency version; Cargo.lock records the resolved graph. Use locked builds for release reproducibility. [Cargo version requirements](https://doc.rust-lang.org/cargo/reference/specifying-dependencies.html), [Cargo.lock](https://doc.rust-lang.org/cargo/guide/cargo-toml-vs-cargo-lock.html).

## Boundaries that make upgrades manageable

Keep project-owned SQL request/response types and configuration schemas outside upstream engine structs. Translate them in one adapter. Upstream Rust/serde layout changes must not silently change the public SQL JSON contract.

The adapter owns public-API mapping, model/vector validation, filters, query plans, persistence calls, and normalization of results/errors. Product logic owns authorization, query policy, facets scope, snippets, fallback, and budgets. PostgreSQL logic owns transactions, source capture, tasks, and visibility checks.

Persist and report these separately:

- Extension/SQL API and configuration schema versions.
- Upstream engine build version and enabled build features.
- Analyzer policy version and dictionary version/hash.
- Model ID/revision, tokenizer revision, dimensions, metric, normalization, and sparse vocabulary/IDF policy.
- Index generation and supported on-disk format range.

Changing a model or analyzer is not just changing a crate version. Classify each change as query-only, reindex-required, or reencode-required. Do not use moving model aliases as reproducible model identities.

## Upgrade sequence

1. Review the registry release, public API/configuration changes, on-disk format changes, tokenizer/scoring changes, native requirements, and license changes. Review the published crate used by the build rather than assuming server release notes describe Edge.
2. Update the exact direct requirement and resolved lockfile together. Keep pgrx and cargo-pgrx aligned. Record new/deprecated/removed capabilities and all version-sensitive combinations.
3. Compile the full capability probe set, then execute SQL-to-engine contract and feature-combination tests. An enum/type being exported is not a passing runtime test.
4. Run lexical goldens and retrieval-quality regressions: analyzer outputs, phrase/prefix semantics, identifiers, sparse IDs/IDF, RRF/DBSF behavior, MaxSim, filtering, grouping, and model freshness.
5. Run transaction, authorization, cancellation, concurrency, resource, crash/replay, rebuild, backup/recovery, and upgrade tests on supported PG/platform targets.
6. Validate old-index reopen behavior. If unsupported or behavior changes require reindexing, build/catch up a new generation and switch; never overwrite the only usable old generation as an experiment.
7. Validate rollback against the actual index format. If a binary downgrade cannot reopen a newer index, preserve a compatible generation or document rebuilding from retained source data and vectors.
8. Update baseline, capability evidence, compatibility matrix, migration instructions, and release notes before shipping.

An update-bot configuration now accompanies the real manifest; prefer reviewable grouped engine and pgrx/tool updates. Automatic version detection does not mean automatic release or safe index-file upgrade. An update must not silently remove features, change defaults, or enable external inference.

## Lexical gap decision

The initial direct dependency remains Edge. Rich FTS requirements are tracked, not dropped: fuzzy/prefix-fuzzy queries, proximity, query parsing, synonyms, and verified snippets need their own owners and evidence.

Tantivy is the named library candidate for position-based/fuzzy lexical primitives. It exposes a [fuzzy query](https://docs.rs/tantivy/0.26.2/tantivy/query/struct.FuzzyTermQuery.html) and a [snippet generator](https://docs.rs/tantivy/0.26.2/tantivy/snippet/struct.SnippetGenerator.html); it is not automatically a complete Meilisearch experience. Synonym policy, query UX, multilingual analysis, and ranking evaluation still belong to the product.

Before adopting it, compare quality and maintenance cost with extension-owned query policy. Adoption must include stable source-ID mapping, shared authorization, two-index generation/readiness rules, idempotent dual writes, maintenance, and explicit outer fusion. External Tantivy ranks cannot be treated as native inputs to Edge's prefetch fusion. A change to the engine topology needs an ADR and updated dependency baseline.

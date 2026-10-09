# Dependencies and upstream upgrade policy

Status: active dependency and upgrade contract. The [machine-readable baseline](dependency-baseline.json) records exact build inputs and the validation reached by each component. The repository now includes P0 source, Cargo.lock, a [resolved dependency inventory](dependency-graph.json), and CI/update-bot configuration. A successful engine probe is not a successful PostgreSQL integration or release gate; see [build evidence](build.md).

## Dependency ownership

| Component | Dependency decision | Upgrade owner |
| --- | --- | --- |
| Retrieval, local BM25, sparse/dense/multivector indexing | Direct published `qdrant-edge` crate, P0-verified `=0.8.0` | Engine adapter |
| PostgreSQL types, functions, triggers, hooks, workers | Direct `pgrx`, P0-verified `=0.19.3`, PG17 feature | PostgreSQL adapter |
| Extension build/package tool | `cargo-pgrx 0.19.3`, matching the library | Build and distribution |
| Tokenization used by Edge | Edge-owned dependency graph and analyzer configuration | Engine adapter and lexical policy |
| Application vector generation | Optional application/model integration, outside the core build | Model contract |
| Rich lexical gaps | Direct Tantivy `=0.26.2` for bounded query-local RAM fuzzy/proximity; [ADR 0006](adr/0006-query-local-lexical.md); full lexical acceptance open | Lexical adapter decision |
| Lexical body integrity | Direct `sha2 =0.11.0`, already present in the locked registry graph; actual native body bytes are checked before lexical indexing | Query/source proof |
| SQL schema and migrations | Versioned project-owned API | Extension lifecycle |

The core integration should use published Edge types and methods. The `qdrant-client` REST/gRPC SDK is not the embedded engine dependency. FastEmbed/ONNX and model downloads are not mandatory dependencies for local BM25 or BYOV retrieval.

Keep the upstream engine as a dependency rather than copying its BM25, tokenizer, HNSW, sparse-index, or quantization implementations into the extension. The registry crate can bundle its own internal implementation modules; those remain upstream-owned. Imports should use its supported public surface. Anything under an upstream `internal` namespace needs a recorded exception and compatibility tests, even if technically exported.

## Pinned baseline evidence

The Edge `0.8.0` registry archive was checked against the registry SHA-256 recorded in the baseline. Its manifest uses Rust edition 2024 and does not declare a package-level `rust-version`; this is not evidence that all dependency versions build on an arbitrary older compiler. [Published Edge manifest](https://docs.rs/crate/qdrant-edge/0.8.0/source/Cargo.toml), [registry record](https://crates.io/api/v1/crates/qdrant-edge/0.8.0).

pgrx and cargo-pgrx `0.19.3` registry records declare Rust `1.96` as the minimum. Rust `1.96.0` is fixed after locked Linux dependency resolution and the recorded combined PostgreSQL build. [pgrx record](https://crates.io/api/v1/crates/pgrx/0.19.3), [cargo-pgrx record](https://crates.io/api/v1/crates/cargo-pgrx/0.19.3).

Edge `0.8.0` requests Charabia `0.9.9` with selected multilingual features. The current Cargo.lock resolves that dependency to `0.9.9`; the upstream requirement alone would not establish the resolved version. Its multilingual tokenizer uses Charabia with additional engine processing, including a distinct Japanese path. Charabia is also used by Meilisearch; sharing a tokenizer does not provide Meilisearch's ranking, typo policies, or UI behavior. [Edge package source](https://docs.rs/crate/qdrant-edge/0.8.0/source/), [Charabia](https://github.com/meilisearch/charabia).

If highlighting or a future Tantivy adapter needs a direct analyzer dependency, declare it explicitly and prove parity with the selected engine: normalization, stemming, stopwords, token boundaries, offsets, and field positions. Do not assume importing the same Charabia version reproduces the whole Edge analysis pipeline.

## P0 dependency deliverables

The following define the finite P0 dependency/build scope. [CI8](evidence/p0-current-ci.json) and the [native evidence](evidence/p0-native-ci.json) verify the current diagnostic inputs at implementation `57c58fcee51efb0067f04b03ffb44e300f76ce72`, tree `8b1d6d863be52229f8b15b49ad0b58b96b28608f`, run [37743504259](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37743504259). Capability mapping includes explicit fixed-version exclusions; probe success does not make all 54 product requirements supported:

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

The pgrx integration, selected features, and supporting dependencies have compiled in the recorded four diagnostic profiles. Exact pins select an intentional direct dependency version; Cargo.lock records the resolved graph. Use locked builds for release reproducibility. [Cargo version requirements](https://doc.rust-lang.org/cargo/reference/specifying-dependencies.html), [Cargo.lock](https://doc.rust-lang.org/cargo/guide/cargo-toml-vs-cargo-lock.html).

## Resolved P0 build profiles

`scripts/dependency_report.py` defaults to the existing PG17 direct-normal
inventory in `docs/dependency-graph.json`. Its explicit `--profile` selector also
supports `direct-private`, `helper-normal`, and `helper-private`. Each profile
uses locked, Linux x86_64 Cargo metadata with default features disabled at the
command boundary. The helper-private profile selects the private feature on
both the extension and helper binary, matching their separate image build
commands. The required pgrx `cshim` feature still comes from the manifest.

Generate or check the normal full inventory and all-profile comparison together:

```sh
python3 scripts/dependency_report.py --comparison-output docs/dependency-profiles.json
python3 scripts/dependency_report.py --comparison-output docs/dependency-profiles.json --check
```

For a full individual inventory, use `--profile helper-private --output PATH`.
Without `--output`, a nondefault profile writes `dependency-graph-PROFILE.json`
under `docs`, so it does not overwrite the normal baseline. The
[profile comparison](dependency-profiles.json) binds the exact lockfile and
manifest hashes, records selected feature arguments, gives each complete
inventory's content hash, and lists package/feature/dependency differences from
direct-normal. It is resolution evidence only. Cargo metadata includes workspace
packages; their presence does not show that every package is linked into a
selected executable.

The private Edge probe adds an optional exact `libc =0.2.189` dependency behind
`p0-fault-injection`. That version was already locked and used by the PostgreSQL
and helper integration. Only the private profiles enable the Edge probe's direct
`libc` edge; the normal profile gains no such edge. Its explicitly declared empty
`default` feature does not enable the allocator fault path. This change adds no
registry package, changes no registry version/checksum, and enables no inference
library or network Qdrant client. Local BM25 and BYOV retain their core dependency
contract. Profile resolution does not promote compilation, runtime, isolation,
or release support status.

## Native and independent lexical build inputs

The [native build contract](p0-native-build.md) supplements Cargo.lock with a
fixed Ubuntu snapshot, seven exact PGDG archive checksums, the unchanged image
digest and a complete installed-package comparison. Its source metadata and
verifier are reviewed. CI8 built and installed all four diagnostic image profiles
with matching 246-entry inventories and seven verified archive pins. The same
build records GCC 13.3.0, GNU ld/objdump 2.42, PostgreSQL 17.11, all 20 required
CPU features, audited native-object hashes, and direct ELF dependencies. Exact
libclang paths remain unobserved despite loaded-version 19.1.1 evidence. All 246
package copyright files have hashes; 125 yield machine-readable labels and 121
have explicit parsing gaps. This is an actual dependency/declared-license
inventory, not distribution clearance, a full loader/ISA proof, or bit-identical
binary reproducibility. A Rust edition or default target cannot certify the
upstream C compiler's ISA choices.

The [Tantivy experiment](../experiments/tantivy-probe/README.md) is a separate
Cargo workspace with its own 0.26.2 pin, lockfile and 109-package Linux inventory.
Only `mmap` and `lz4-compression` are enabled. Its public dictionary bridge pins
`levenshtein_automata 0.2.1` and `tantivy-fst 0.5.0`, both already transitive in
that graph. These are candidate evaluation inputs; the extension's root graph
is unchanged. The [five isolated CI8 primitive cases](evidence/p0-tantivy-ci.json)
passed under the configured 512 MiB/one-CPU/45-second bounds, preserving their
negative findings. They do not adopt Tantivy or fulfill the pre-Alpha
quality/lifecycle decision.

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

## Project license

Original project material uses **AGPL-3.0-only**, as declared by the current
project overview and [LICENSE](../LICENSE). Cargo declarations and the generated
workspace license inventory follow that choice. Third-party packages, native
libraries, dictionaries and model artifacts keep their own licenses. Package
metadata and this project license do not complete distribution review or
third-party notices.

# Isolated Tantivy feasibility probe

Status: **five local semantic experiments passed; dependency adoption pending**.

This separate Cargo workspace investigates the first candidate selected by
[ADR 0002](../../docs/adr/0002-lexical-gap-strategy.md). It does not add Tantivy to
the PostgreSQL extension, link it to Edge, or implement a product lexical adapter.
The repository root's manifests and lockfile remain a distinct dependency graph.
F13–F20 remain incomplete.

## Fixed inputs and commands

The experiment pins registry `tantivy =0.26.2`, with default features disabled
and only `mmap` and `lz4-compression` enabled. `levenshtein_automata =0.2.1` and
`tantivy-fst =0.5.0` are explicit public-API dependencies for the dictionary bridge;
they are also transitive dependencies of this Tantivy version. The bridge adapts
their interfaces; the upstream library constructs the edit-distance automaton.
No engine implementation, private namespace or patched dependency is used.

The Rust compiler is the repository's fixed 1.96.0. The independent lockfile and
[selected Linux inventory](dependency-inventory.json) record the complete
resolved versions, checksums, features and declared licenses. Tantivy's archive
checksum is `861facfabd71044968f364837f9a083b56464ba5a59079f88706ee5c451ca069`.
This inventory is not a completed distribution/license acceptance review.

From the repository root:

```sh
cargo build --locked --manifest-path experiments/tantivy-probe/Cargo.toml
timeout 45s cargo run --locked --manifest-path experiments/tantivy-probe/Cargo.toml
python3 experiments/tantivy-probe/inventory.py --check
```

Build first so the 45-second execution envelope is not a compiler-time budget.
The executable emits one JSON record only after every assertion passes. A failure
returns a nonzero exit status; no incomplete run is reported as a complete pass.
Each fixture uses an owned temporary directory, a single writer thread and a
15,000,000-byte writer buffer. The largest fixture contains 49 documents and
uses a complete-match assertion limit of 128 documents. These controls do not
measure or cap total process RSS, mmap residency or all internal query work.
The temporary data is deleted after the handles are released.

## What actually passed

| Experiment | Observed boundary | Product work retained |
| --- | --- | --- |
| Fuzzy and exact identifiers | Edit distances 0/1/2, transposition costs one/two and whole-token versus prefix matching differ as expected. Distance three is rejected when the query executes. Raw keyword equality preserves identifier case. | A project-owned analyzed input contract and opt-in identifier policy; model/field/analyzer compatibility. |
| Fuzzy budget | A native fuzzy query matches 40 distinct fixture terms/documents with a `TopDocs` return limit of one and constant score 1.0. A public dictionary bridge rejects expansion beyond 32 unique terms without returning partial results; the two diary/dairy expansions reproduce the native match set. | Hard time/work/memory limits, multi-token total budgets, authorization-safe vocabulary and ranking. Match-set equivalence does not mean score equivalence. |
| Phrase and parser | Slop zero requires adjacency in this fixture; slop one admits a gap and crosses repeated field values; slop two admits reversed terms. The upstream parser defaults to OR; explicit conjunction produces AND. Invalid field and quote syntax fail. | Strict field/array boundaries, ordered proximity, bounded project grammar, language/stopword positions and all required-predicate branches. |
| Snippets | A complete Unicode source value receives a valid half-open UTF-8 byte highlight and HTML escaping. Repeated values are concatenated by `snippet_from_doc`. A fuzzy query matches diary/dairy but supplies no terms to the snippet visitor and produces no fuzzy highlights. | Original field/array identity, offsets for arbitrary fragments and normalization, fuzzy/synonym attribution and semantic-only presentation. |
| Updates and readers | Repeated stable-ID delete/add leaves one live replacement. Commit does not reload a manually managed reader; old searchers retain their prior view. Writer rollback preserves the committed replacement. | PostgreSQL transactions, idempotent dual writes, crash recovery, paired generations and upgrade/rollback. |

The initial local phrase expectation excluded the repeated-value document at
slop one. That expectation failed: the actual match set included it. Fixed source
defines a one-position inter-value gap. The final characterization preserves
this negative product result; it does not weaken the required array boundary.

The dictionary bridge accepts only a bounded synthetic single term and at most
16 segments. Its current path uses whole terms, edit distance one and
transpositions costing one; bounded prefix/distance-two expansion is not tested.
It stops after observing one more unique matching term than the
allowed return budget. It does **not** interrupt a single FST traversal, bound
all examined states, or prove that enumerating a shared dictionary is safe for
multiple authorization domains. The fixture's tenant filter demonstrates an
ordinary Boolean predicate only; no PostgreSQL identity mapping, RLS, protected
statistics or production permission claim follows.

The analysis control is `SimpleTokenizer` followed by `LowerCaser`, with no
stemming, stopword removal, folding or Chinese segmentation. The mixed Unicode
fixture tests literal span handling, not analyzer parity with Edge or multilingual
search quality. No pretrained model or network inference is required.

## Decision boundary

These experiments establish a usable public-API route for bounded **returned**
fuzzy expansions and identify concrete position/highlight hazards. Direct Tantivy
remains a candidate. Acceptance requires the frozen lexical comparison in ADR
0002, protected match domains, common analysis, resource governance and the
costed two-engine lifecycle/fusion contract. Synonyms, instant suggestions,
facets, document-level statistics and held-out relevance have not been evaluated
by this executable. No throughput, latency advantage, full FTS or release claim
is made from the small fixture.

## Fixed public sources

- [Fuzzy query and constant-score automaton](https://docs.rs/crate/tantivy/0.26.2/source/src/query/fuzzy_query.rs)
- [Public term dictionary](https://docs.rs/tantivy/0.26.2/tantivy/termdict/struct.TermDictionary.html)
- [Inter-value position gap](https://docs.rs/crate/tantivy/0.26.2/source/src/postings/postings_writer.rs)
- [Phrase movement/slop](https://docs.rs/crate/tantivy/0.26.2/source/src/query/phrase_query/phrase_query.rs)
- [Snippet visitor and repeated values](https://docs.rs/crate/tantivy/0.26.2/source/src/snippet/mod.rs)
- [Levenshtein automata 0.2.1](https://docs.rs/levenshtein_automata/0.2.1/levenshtein_automata/)
- [Tantivy FST 0.5.0](https://docs.rs/tantivy-fst/0.5.0/tantivy_fst/)

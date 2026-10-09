# ADR 0006: Bounded query-local lexical execution

Status: development implementation; full F13/F14 and release acceptance remain open.

## Decision

Adopt published Tantivy `=0.26.2` in the managed helper for an explicit
`qdrant.search_lexical` entry point. Use RAM directories with only the
`lz4-compression` feature. The separate mmap experiment remains a historical
comparison. Published `levenshtein_automata =0.2.1` and `tantivy-fst =0.5.0`
provide edit-distance construction and dictionary traversal through public APIs.
The adapter implements neither a positional engine nor edit-distance matching.

This is a bounded first execution route, supplementing ADR 0002. Persistent
dual-engine indexing, external rank fusion, general query syntax, synonyms,
highlighting, suggestions, multilingual fuzzy matching and held-out quality
remain required work. This decision does not pass P4-LEXICAL or P4-DUAL.

## Alternatives

Edge `0.8.0` remains the persistent retrieval and local BM25 engine. Its reviewed
public API does not supply fuzzy/slop primitives. Extension-owned Boolean
composition can fill simpler policy gaps but is not a replacement positional
engine. A second persistent Tantivy index would require independent commit,
visibility, replay and generation receipts; that work remains unimplemented.
The source-reviewed pg_search alternative still requires a real PG17 combined
installation and lifecycle comparison. It is not adopted here.

## Source, permission and durability

Each query requires an entirely durable current Edge generation, at most 1000
live source identities and the existing owner/table/column permission domain.
RLS and unsupported snapshots are rejected. Edge matching and declared scalar
filters select the complete candidate domain before lexical indexing or top-k.
Native scroll retrieves at most 32 source bodies per page; aggregate text over
1 MiB refuses the query before Tantivy construction. No partial snapshot is
searched. One row remains one field value; arrays/fields are not concatenated.

The native owner builds a temporary RAM index from those Edge bodies, runs the
query, then drops its writer/readers/index. The existing complete SQL source
proof rechecks every live source identity, version, incarnation, text SHA-256,
payload SHA-256, pending event and owner epoch before exposing results. Results
carry typed source keys and can join ordinary source tables. Segment addresses,
native point IDs and private proof metadata are not public results.

There is no second persistent derived index, application write API or new ACK
condition. Tickets still promise tested Edge flush/exact PostgreSQL ACK only.
Helper replacement reconstructs Edge from retained source events; subsequent
lexical queries reconstruct their temporary index from that current generation.
Cancellation retains native ownership until completion. This is not arbitrary
native preemption, power-loss durability, an RSS quota or a latency guarantee.

## Query contract and limits

`kind='fuzzy'` accepts one ASCII alphabetic word of 3..32 letters. The upstream
automaton permits one edit including transposition. Complete dictionary
expansion is limited to 32 distinct terms; overflow fails instead of truncating.
`kind='proximity'` accepts 2..8 ASCII alphabetic words, each at most 32 letters,
and native phrase slop 0..8. Native slop is total movement, allowing an adjacent
transposition at cost two; it is not strict ordered proximity. Identifiers and
Unicode query words are explicitly refused by this initial route. Existing
exact identifier matching remains a separate Edge predicate.

The analyzer is `simple_lower_v1`: Tantivy SimpleTokenizer, LowerCaser and
positions, without stemming or stopwords. It is separate from Edge's
multilingual BM25/body analyzer. The response identifies it explicitly; no
shared Chinese or language-equivalence claim is made. Query bytes are limited
to 256, returned hits to 100, writer threads to one, writer memory budget to
15,000,000 bytes and public response size to 256 KiB. The writer budget is not
total process RSS. Complete source text bounds also bound the input vocabulary;
the 32-expansion cap alone is not a bound on all FST traversal work.

Counts describe exact lexical matches within the explicit filtered snapshot.
Scores are native Tantivy term/phrase scores in that snapshot, with its own
corpus statistics. They are not added to Edge BM25, cosine or MaxSim scores.
This path provides no hybrid fallback, fuzzy prefix, cross-engine fusion,
document/chunk grouping or global count outside its admitted domain.

## Dependencies, upgrades and remaining evidence

Cargo.lock and the four feature-profile inventories must preserve existing
package versions while adding the reviewed lexical dependency graph. Third
party licenses and notices retain upstream ownership. Source contract 21
requires matched development binaries/install SQL and a fresh catalog; upgrade
and rollback from older development schemas remain unimplemented.

Native tests distinguish typo transposition, adjacency, gaps, reordered terms,
expansion overflow and source byte refusal. Installed tests cover ordinary DML,
key reuse, durable tickets, permissions, RLS refusal, predicates, cancellation
and source changes. Full crash-replay CI and multilingual/held-out relevance,
performance, memory, comparison and distribution evidence remain necessary.

Primary fixed sources: [Tantivy 0.26.2](https://docs.rs/crate/tantivy/0.26.2/source/),
[phrase query](https://docs.rs/crate/tantivy/0.26.2/source/src/query/phrase_query/phrase_query.rs),
[dictionary FST](https://docs.rs/tantivy-fst/0.5.0/tantivy_fst/),
[Levenshtein automata](https://docs.rs/levenshtein_automata/0.2.1/levenshtein_automata/).

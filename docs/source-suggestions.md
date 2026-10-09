# Bounded source-word suggestions

Implementation status: partial SQL integration with scoped native and installed
PostgreSQL validation. Full F18, instant interaction, quality, latency and
release acceptance remain open.

`qdrant.suggest(index_name, prefix, limit_count DEFAULT 10, options DEFAULT '{}')`
completes one analyzed source word. It uses the same entire durable authorized
source admission as [bounded lexical queries](source-bounded-lexical.md), with
the same generation/epoch, exact event receipts, native body fingerprints,
source permissions and post-execution source checks. Explicit matching and
payload filters apply before constructing the query-local word dictionary.

```sql
SELECT qdrant.suggest('articles', 'trans', 5,
  '{"filter":{"field":"category","eq":"Allow"}}');
```

The example requires an existing ready index named `articles`, a declared
keyword payload alias `category`, and the usual index-owner/source SELECT
permissions. It can return words such as `transaction` and `transfer` when those
words exist in the authorized filtered body field. It does not fabricate query
phrases or suggestions from models or query logs.

Prefix matching is literal after lowercasing, with no fuzzy, synonym, stemming
or query-syntax interpretation. The prefix must be one alphabetic Unicode word
with at least two characters and at most 32 UTF-8 bytes before and after
lowercasing. Spaces, digits, punctuation, operator symbols and combining-accent
inputs are refused with `22023`. Chinese literal words use the existing simple
analyzer; segmentation and canonical accent normalization are unsupported.
Source terms must be alphabetic and at most 64 UTF-8 bytes. Tokenization can
split punctuation-bearing source identifiers into word fragments; this API does
not implement whole-value identifier completion or the full F20 contract.

Pinned [Tantivy term dictionaries](https://docs.rs/tantivy/0.26.2/tantivy/termdict/struct.TermDictionary.html)
and native bounded term-dictionary ranges enumerate the complete matching term domain before
ranking. Results sort by `point_count` descending, then normalized UTF-8 term
ascending. `point_count` counts source points containing the word once per point;
repeated occurrences within one body do not increase it. The query-local index
contains only the current filtered source snapshot and has no deleted documents.
Counts sum native segment document frequencies and are exact for that scope.
They describe points rather than grouped documents or query popularity.

The source snapshot admits at most 1000 live points and 1 MiB of body text,
with 64 KiB per body. Enumeration permits at most 128 distinct eligible terms
and 1024 visited segment-term entries, including ineligible terms. Exceeding
either limit refuses the whole request with `54000`, even when the requested
result limit is one. There is no pre-ranking term truncation. The returned
limit is 1–20; `total_terms` counts all eligible prefix terms, while
`terms_complete` states whether every eligible term was returned. Empty domains
return an empty `items` array and zero terms. No hit bodies, internal point IDs
or permission-excluded vocabulary are returned.

The query builds a bounded RAM index in the existing native owner, with one
writer thread and the existing 15-MB writer budget. It does not maintain a
second persistent derived index. Ordinary source DML, key reuse, rebuild and
recovery therefore use the existing authoritative-source lifecycle. A source
change during execution invalidates the whole response; cancelling SQL leaves
native ownership retained until execution finishes. RLS and unsupported source
snapshots are explicitly refused. The normal request timeout remains in force.

This scope establishes a development path for word-prefix completion. General
instant search, multiword/query-log suggestions, typo interaction, persisted
completion indexes, multilingual analysis, large corpora and measured latency
and relevance acceptance remain required.

Scoped local act `suggestions-9a45b99873e068bd` rebuilt the locked helper,
extension and installation SQL. Native tests passed (30 helper, 2 auxiliary and
23 protocol); seven installed PostgreSQL groups passed in 17.455 seconds.
They verify complete prefix frequencies/ranking, Unicode, input and exact
candidate limits, inherited-owner/source privileges, DML/key reuse and durable
tickets, concurrent source mutation, cancellation ownership, RLS refusal, DDL
re-registration, and extension uninstall/rollback/reinstall. Input/log and
installed binary/SQL hashes are retained locally. This scoped validation does
not establish a full-revision crash-matrix or relevance/latency acceptance.

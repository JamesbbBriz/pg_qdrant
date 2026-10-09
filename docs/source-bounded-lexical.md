# Bounded lexical source queries

`qdrant.search_lexical(index_name, q, kind, top_k DEFAULT 10, slop DEFAULT 0,
options DEFAULT '{}')` builds a query-local RAM Tantivy index from a complete,
durable, authorized Edge source snapshot. Ordinary source-table DML supplies
the indexed facts. It requires the managed-helper development build and a fresh
source-contract-21 registration. Upgrade from older development catalogs is
not supported. The [integration decision](adr/0006-query-local-lexical.md)
retains the complete lexical requirements beyond this bounded implementation.

```sql
CREATE TABLE lexical_articles(id text PRIMARY KEY, body text NOT NULL);
INSERT INTO lexical_articles VALUES
  ('a', 'transaction recovery'),
  ('b', 'transaction durable recovery'),
  ('c', 'recovery transaction');
SELECT qdrant.create_index('lexical_articles', 'lexical_articles', 'id',
  '{"text":{"fields":["body"]}}');
SELECT qdrant.index_status('lexical_articles'); -- require ready and zero pending
SELECT qdrant.search_lexical('lexical_articles', 'transactoin', 'fuzzy');
SELECT qdrant.search_lexical('lexical_articles', 'transaction recovery', 'proximity', 10, 1);
SELECT qdrant.search_lexical('lexical_articles', 'body:transaction AND NOT durable', 'syntax');
SELECT qdrant.search_lexical('lexical_articles', '"transaction recovery"~1', 'syntax');

WITH result AS (
  SELECT qdrant.search_lexical('lexical_articles', 'transactoin', 'fuzzy') AS value
)
SELECT a.id, a.body, h->>'rank' AS rank, h->>'score' AS score, h->'snippet' AS snippet
FROM result, LATERAL jsonb_array_elements(value->'hits') h
JOIN lexical_articles a ON a.id = h->'source_key'->>'value';
```

Fuzzy queries accept one ASCII alphabetic word of 3–32 letters. Upstream
Levenshtein automata provide distance one with adjacent transpositions. More
than 32 distinct matching dictionary terms rejects the whole query; expansions
are never truncated. Identifiers containing digits/punctuation are refused.
Fuzzy prefixes and multilingual query words remain unsupported.

Proximity accepts 2–8 ASCII words, each at most 32 letters, and native phrase
slop 0–8. Slop measures total term movement: adjacent reordering costs two.
Slop zero requires contiguous positions. The analyzer uses Unicode simple
word tokenization, lowercase and positions, without stemming or stopwords.
It is distinct from the registered Edge BM25 analyzer.

Syntax is explicit through `kind = 'syntax'`; existing text and fuzzy modes do
not interpret operators. The pinned upstream strict parser and native query
compiler support AND/OR/NOT, parentheses, default AND, the single `body` field,
quoted phrases with native slop 0–8, escapes and finite boosts 0.1–10. Unicode
literal words use the same simple tokenizer; this is not Chinese segmentation,
stemming or multilingual typo tolerance. Syntax requires external `slop = 0`.
Only body literals are admitted: internal fields, wildcards/prefixes, regexes,
ranges, sets and match-all syntax are refused. Literal `*` and `?` are refused
even when escaped. There is no lenient parser or silent unsupported fallback.

Admission caps the parsed AST at depth eight, 32 nodes and 16 literal leaves;
each literal has at most 128 bytes and eight whitespace-separated words. Native
analysis must yield 1–32 term occurrences. Redundant syntax may be normalized
by the upstream grammar; the 256-byte raw-input cap still applies. Negative-only
subgroups receive a native positive universe after admission. That universe is
the complete already authorized and filtered snapshot, so `NOT` cannot widen
permissions or escape a mandatory source predicate. All-empty analysis errors
rather than returning a match-all response. Syntax boosts affect native lexical
scores only. This query-only addition changes no persisted generation or ACK
format; helper and installed SQL must come from the same development build.

The complete live source must have at most 1000 entirely durable points before
filtering. The filtered snapshot permits at most 1 MiB of body bytes and 64 KiB
per body, with at most 100 returned hits and a 256-byte query. Snapshot reads
are paged in batches of 32. A writer uses one indexing thread and a 15 MB
Tantivy writer budget; this is not a complete process RSS limit. Native work
retains its owner after SQL cancellation and remains subject to the helper
operation deadline. Query-local objects are released after execution.

Options accept the existing `matching`, declared scalar `filter` and
`timeout_ms` contracts. Matching/filter constraints enter the complete native
snapshot before lexical candidate selection. Registered-owner and all source
SELECT permissions are required; inherited owner membership is checked through
PostgreSQL. RLS, non-READ-COMMITTED snapshots and uncommitted source changes are
refused. Generation, epoch, identity, incarnation, revision, text/payload
fingerprints and durable receipts are rechecked before any result is exposed.
Concurrent source changes invalidate the whole response.
Before lexical indexing, the helper recomputes SHA-256 over every filtered
native body, including bodies outside the final top-k, and compares it with
the same identity proof later checked against PostgreSQL. Damaged body bytes
with an intact stored fingerprint reject the whole query with `XX000`.

Results include typed source keys, rank, native scores, exact matched-point
count within the filtered snapshot, snapshot scope, generation, epoch and
execution budgets. They expose no native point IDs; source text is limited to
authorized bounded snippets. Scores
use the query-local filtered corpus statistics and are not combined with Edge
BM25, cosine or MaxSim scores. No cross-engine fusion, suggestion,
pagination or release-support claim is made. Budget overflow returns `54000`;
invalid input returns `22023`; stale/uncommitted source returns `55000`.

Each lexical hit includes a `snippet` from pinned Tantivy's `SnippetGenerator`
over the original body string. A ready snippet contains raw `text`, the current
`source_field` and SHA-256 `source_fingerprint`, and half-open `highlights` ranges
in UTF-8 bytes relative to that fragment. Case, accents, combining characters
and markup remain literal source bytes; no HTML is generated. Applications must
escape this text when rendering HTML and interpret offsets as bytes rather than
Unicode scalar or UTF-16 indices. The current simple analyzer does not normalize
canonically equivalent accents.

`source_byte_start` is an absolute UTF-8 byte offset only when the fragment has
one occurrence in the original body. Repeated occurrences return null and
`source_occurrence_ambiguous = true`. Native fragments use a 150-character
target; a fragment exceeding 512 bytes or 32 highlight ranges returns
`fragment_budget_exceeded` with no text or ranges. Pure-negative queries and
hits without positive terms return `no_positive_term_match`. These snippet
states do not change the matching count or discard hits.

Fuzzy highlights use actual expanded dictionary terms. Syntax highlighting
conservatively removes every negative subtree, including nested negatives, and
boost wrappers from its term projection. Highlights describe positive lexical
term occurrences; they do not prove Boolean branch satisfaction, phrase spans,
or semantic correspondence. The search query and scores are unchanged.
Source permissions, current source fingerprints and generation/receipt fences
cover snippets before exposure and after native execution. Helper and SQL must
come from the same build. General search-mode snippets, configurable analysis,
normalization mappings and full F17 acceptance remain open.

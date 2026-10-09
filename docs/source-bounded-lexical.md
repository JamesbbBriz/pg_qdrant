# Bounded fuzzy and positional source queries

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

WITH result AS (
  SELECT qdrant.search_lexical('lexical_articles', 'transactoin', 'fuzzy') AS value
)
SELECT a.id, a.body, h->>'rank' AS rank, h->>'score' AS score
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

Results include typed source keys, rank, native scores, exact matched-point
count within the filtered snapshot, snapshot scope, generation, epoch and
execution budgets. They expose no native point IDs or source bodies. Scores
use the query-local filtered corpus statistics and are not combined with Edge
BM25, cosine or MaxSim scores. No cross-engine fusion, highlight, suggestion,
pagination or release-support claim is made. Budget overflow returns `54000`;
invalid input returns `22023`; stale/uncommitted source returns `55000`.

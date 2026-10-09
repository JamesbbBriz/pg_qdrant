# Bounded current-source grouping

`qdrant.search_groups(index_name, q, field, group_limit DEFAULT 10,
group_size DEFAULT 3, mode DEFAULT 'text', query_vectors DEFAULT '{}',
options DEFAULT '{}')` executes the pinned Edge native grouping API in the
managed helper. The group field is a declared keyword or integer payload
alias. Text mode uses the registered local BM25 field. Semantic mode uses
one declared, completely ready dense representation and its model contract.

```sql
CREATE TABLE grouped_articles(id bigint PRIMARY KEY, body text NOT NULL, document text);
INSERT INTO grouped_articles VALUES
  (1, 'red crate', 'manual-a'), (2, 'blue crate', 'manual-a'),
  (3, 'green crate', 'manual-b');
SELECT qdrant.create_index('grouped_articles', 'grouped_articles', 'id',
  '{"text":{"fields":["body"]},"payload":{"document":{"field":"document","kind":"keyword"}}}');
SELECT qdrant.index_status('grouped_articles'); -- require ready and zero pending events
SELECT qdrant.search_groups('grouped_articles', 'crate', 'document', 2, 2);

WITH result AS (
  SELECT qdrant.search_groups('grouped_articles', 'crate', 'document', 2, 2) AS value
)
SELECT g->>'value' AS document, a.id, a.body, h->>'score' AS score
FROM result, LATERAL jsonb_array_elements(value->'groups') g,
  LATERAL jsonb_array_elements(g->'hits') h
JOIN grouped_articles a ON a.id=(h #>> '{source_key,value}')::bigint;
```

Each group contains a typed `value` and `hits` with typed `source_key`, native
`score` and within-group `rank`. Null group values are omitted. The native
algorithm orders groups by the best hit and fills groups with additional
requests. Grouping ordinary truncated search results is insufficient: native
group discovery can reach a lower-scored group beyond a dominant top-k.

The adapter preserves the fixed 0.8.0 discovery and fill budgets: at most five
requests of each kind. `groups_complete=false` makes that bounded coverage
explicit. `exact_scores=true` describes scoring of returned hits; it does not
promise every possible group, global exact group top-k, or total group counts.
`filtered_points` is an independently exact point count in the admitted source,
including points with null group values. It is separate from group coverage.

Bounds are 1000 live source points, 1–32 groups, 1–16 hits per group, at most
256 requested hits and 256 KiB output. Dense work additionally requires
`10 * live_points * dimensions <= 20000000`, including discovery and fill
passes. Budget overflow raises `54000`; malformed ranges, unknown fields,
unsupported options or modes raise `22023`. Boolean/float grouping raises
`0A000`. Hybrid, sparse, precision and explore grouping remain unsupported.

Options admit only `matching`, `filter` and `timeout_ms` under the
[statistics contract](source-statistics.md). Every native discovery/fill
branch inherits those predicates before candidate truncation. Before and
after execution, SQL checks owner/index access, source-table SELECT and all
participating columns. Column-only grants do not satisfy the current table
policy. RLS and unsupported transaction snapshots raise `0A000`.

The complete durable source generation must agree with private native
identity, revision, incarnation and text/payload fingerprints, including
excluded rows. Semantic grouping additionally rechecks model readiness and
model-source columns. SQL verifies every returned group's value against the
actual current source row before converting native point IDs to typed source
keys. A concurrent source/model change, dirty generation or unacknowledged
event refuses the entire result with `55000`. Cancellation retains the sole
native owner until execution completes; a canceled result is discarded.

Source contract 20 requires matching development extension, helper and install
SQL with a fresh catalog. No upgrade migration or release support is claimed.
General grouping combinations, grouped pages, larger domains, quality,
configurable document/chunk policy and full Q09 acceptance remain open.

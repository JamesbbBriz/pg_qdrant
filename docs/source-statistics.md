# Bounded current-source statistics

`qdrant.count(index_name, options)` and
`qdrant.facet(index_name, field, limit_count, options)` execute native Edge
`CountRequest` and `FacetRequest` through the single managed helper owner.
They admit at most 1000 live source points. Exceeding that domain raises
`54000`; the functions do not return an exact label for a truncated sample.

```sql
CREATE TABLE statistics_demo(id bigint PRIMARY KEY, body text NOT NULL, category text);
INSERT INTO statistics_demo VALUES (1, 'red crate', 'tools'), (2, 'blue crate', NULL);
SELECT qdrant.create_index('statistics_demo', 'statistics_demo', 'id',
  '{"text":{"fields":["body"]},"payload":{"category":{"field":"category","kind":"keyword"}}}');
SELECT qdrant.index_status('statistics_demo'); -- wait for ready and no pending events
SELECT qdrant.count('statistics_demo', '{"matching":{"all":"crate"}}');
SELECT qdrant.facet('statistics_demo', 'category', 10,
  '{"filter":{"field":"category","eq":"tools"}}');
```

The sole options are `matching`, `filter` and `timeout_ms`, with the same
[lexical](lexical-matching.md) and [declared scalar](source-filters.md)
admission as source search. Options are capped at 16 KiB; the default timeout
is 5000 ms and the permitted range is 1–30000 ms. Facets accept a declared
keyword, integer or boolean alias and a limit of 1–100. Unknown aliases,
arbitrary native paths, query/scoring options and malformed values raise
`22023`. Float facets are unsupported and raise `0A000`.

`points` is an exact count of the entire current durable owner source that
matches these explicit predicates. It is independent of search top-k and
candidate limits. The response names generation/storage epoch, `live_points`,
`max_live_points`, predicates and the `points` unit. `documents` is null:
document/chunk deduplication has not been implemented. No document total or
global multi-index/tenant statistic is inferred.

Facet hits contain typed `value` and exact point `count`. Null values are
omitted. Hits sort by count descending, then typed value ascending for ties.
The helper requests all possible scalar buckets within the admitted domain,
then applies the requested limit. `values_complete` reports whether distinct
values were omitted; it does not describe search candidate coverage. The
public response is bounded to 256 KiB, with no partial success on overflow.

Before execution, every source event must have an exact durable ACK for the
current storage epoch and backfill must be complete. The helper first proves
that its whole owned point count equals the declared live identity set. It
retrieves metadata for every identity in batches of 100, including identities
excluded by the matching predicates. After native count/facet, SQL checks the
entire current source in a fresh statement against incarnation, revision,
source key and independent text/payload fingerprints. This proof remains
private. A missing, extra, stale or concurrently changed source identity
refuses the entire result with `55000`, including an excluded row that newly
enters the matching domain. Native success is not sufficient for exposure.

The established owner-domain policy checks index/source/key/text/payload
column privileges before work and again before exposure. Unsupported RLS or
non-read-committed transaction isolation raises `0A000`. Binding locks span
execution/recheck and use `NOWAIT` (`55P03` on conflicts); ordinary source DML
can proceed. These checks establish a current-source validation point, not a
cross-engine historical MVCC snapshot. Cancellation retains the sole native
owner until the actual operation completes and discards the canceled response.

Source protocol 20 requires matching extension, helper and install SQL with a
fresh development catalog. No direct dependency or lockfile changes are made.
Installed SQL tests compare native facets to independent PostgreSQL GROUP BY,
exercise integer extrema, nulls, filtering, permission refusals, stale output
and in-flight cancellation/source changes. F19 and Q11 remain partial: large
domains, document counts, general matrix representations, comprehensive lexical analysis, quality,
upgrade/rollback and release acceptance are still required.

Bounded named-dense sampled-set matrices reuse this complete proof through
[search_matrix](source-matrix.md); their neighbor scores retain an explicit
non-exact native-default scope, separately from the exact filtered point count.

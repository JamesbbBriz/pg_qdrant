# Bounded source distance matrices

`qdrant.search_matrix(index_name, representation, sample_size DEFAULT 10,
neighbors DEFAULT 5, options DEFAULT '{}')` executes the pinned Edge
`SearchMatrixRequest` in the same managed helper owner as durable delivery.
The representation must be a declared, completely ready dense BYOV slot.
The response identifies its model ID/version and distance. Arbitrary vectors,
native paths, incomplete slots and implicit fallback are refused.

```sql
-- Use the populated dense slot from dense-representations.md.
SELECT qdrant.search_matrix('articles', 'dense', 10, 5,
  '{"matching":{"all":"crate"}}');
```

Native Edge first randomly samples matching points carrying the named vector.
It then finds each sample's neighbors **within that sampled set**. The API
does not search every corpus point for each sample's neighbors. Native 0.8.0
returns an empty matrix when fewer than two points are sampled, including
when `sample_size=1`. Sampling is nondeterministic. Distances retain native
model scoring conventions; scores from different models are not combined.

`samples` contains typed PostgreSQL source keys. Each `rows` entry contains a
`source_key` and `neighbors` with source keys and finite scores. Internal point
IDs, vectors, source bodies and identity proofs are stripped. `exact=false`
and `search_params="native defaults"` describe neighbor retrieval. The separate
`filtered_points` count is exact within the admitted whole source and has
`filtered_count_exact=true`; it is not a promise of global matrix neighbors.

Limits are 1000 live source points, sample size 1–64, neighbors 1–32, and
`live_points * sample_size * dimensions <= 20000000` scalar work. Invalid
sample/neighbor values raise `22023`; source/work/result budgets raise `54000`.
The public response is capped at 256 KiB. Options admit only `matching`,
`filter` and `timeout_ms` under the [statistics contract](source-statistics.md).

Both native sampling and its neighbor domain inherit the admitted lexical and
scalar filters. Every sampled and neighboring key is resolved only after the
complete native identity proof agrees with a fresh SQL source recheck. That
proof includes excluded source points. Before and after execution, every live
source row must have a current ready model slot and all participating model
columns must be selectable by the effective PostgreSQL role. A concurrent
model/source mutation, missing slot, dirty generation or undurable event
refuses the whole result with `55000`. RLS and unsupported snapshots raise
`0A000`. Cancellation retains native ownership until execution completes.

Source contract 20 requires matched development binaries and fresh install
SQL. This is a partial Q11 adapter, not full release support or an upgrade
migration. Large source domains, general sparse/token matrices, document
statistics, quality and complete stage/release acceptance remain open.

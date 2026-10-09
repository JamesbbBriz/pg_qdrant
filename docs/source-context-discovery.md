# Development source-key discovery and context

The managed-helper build adds pinned Edge 0.8.0 `DiscoverQuery` and
`ContextQuery` to the `explore` class. The registered owner domain, complete
named dense slot, current source identities and model contract are shared with
[source-example recommendations](source-recommendations.md). This is partial
Q05 implementation; full P4-EXPLORE, quality, migrations and release support
remain open. Build and installed regression evidence are required before use.

For a ready dense index with model `example-model/r1`, source keys `1`, `2`
and `3` identify a target, positive example and negative example:

```sql
SELECT * FROM qdrant.search('articles','','explore',10,
 '{"dense":{"model_id":"example-model","model_version":"r1",
   "strategy":"discover","target":"1",
   "context":[{"positive":"2","negative":"3"}]}}',
 '{"matching":{"all":"local search"}}');

SELECT qdrant.explain_search('articles','','explore',10,
 '{"dense":{"model_id":"example-model","model_version":"r1",
   "strategy":"context",
   "context":[{"positive":"2","negative":"3"}]}}');
```

`discover` requires a target. `context` refuses a target. Context pairs are
nonempty; all example keys must be distinct, including the target. There are at
most 32 source examples: at most 15 pairs with a target or 16 without one.
Keys use canonical bigint, UUID or text strings. Raw vectors, native point IDs,
unknown pair fields and caller tenant domains are refused. Resolved input is
bounded to 64 KiB. The helper checks target/pair vector count times dimensions
times its owned live corpus against 20 million scalar operations before native
execution. This bound does not establish an RSS or latency guarantee.

The target and every positive/negative example are excluded before candidate
truncation. Shared body and key matching predicates constrain native recall.
The text argument is not used for scoring. The current exact dense execution
cannot be combined with fusion, token reranking, sparse or token examples.

Discover sums each pair's rank: 1 for a closer positive, -1 for a closer
negative, and 0 for equal similarities. It adds the target's bounded native
similarity, `0.5 * (s / (1 + abs(s)) + 1)`. Context sums bounded losses:
`min(positive_similarity - negative_similarity - float32_epsilon, 0)` divided
by `1 + abs(loss)`. Context deliberately produces ties in the satisfied region;
a tie order is not a relevance promise. Similarities follow the declared dense
distance, including negative squared Euclidean or negative Manhattan distance.

All source examples pass effective-role, source/column SELECT, incarnation,
source fingerprint and representation fingerprint checks. RLS remains refused.
Target and pair roles are included in the seed digest. The complete query is
refused if examples change during native execution. Paging revalidates this
digest even when every captured candidate row remains unchanged. Current source
seed admission does not replace the separate durable change-ticket contract.

The development source protocol is version 11 and requires the matching
extension library, installation SQL and managed helper. Fresh installation is
required; upgrading earlier development catalogs is unsupported. Tests cover
independent native score formulas for all four dense distances, installed Dot
scores, multiple pairs, source identities, before-cap filters/exclusion, invalid
inputs, seed-sensitive paging and actual cancellation/concurrent seed mutation.
The retained crash replay suite also rechecks both native query strategies.

# Development source-key MMR

The `explore` class connects pinned Edge 0.8.0 MMR to one complete named dense
BYOV representation. A current PostgreSQL source key supplies the target:

```sql
SELECT * FROM qdrant.search('articles','','explore',10,
 '{"dense":{"model_id":"example-model","model_version":"r1",
   "strategy":"mmr","target":"1","lambda":0.25}}',
 '{"candidate_limit":50}');
```

The caller supplies finite lambda in [0,1]. The native algorithm selects the
first point by relevance, then maximizes `lambda * relevance - (1-lambda) *
maximum similarity to selected points`. Zero emphasizes diversity after the
first point; one emphasizes relevance. Ties do not promise a stable order.

Results retain native selection rank and original nearest-query scores. They
are not sorted again by score. Dot/Cosine scores are similarities; Euclid and
Manhattan scores are positive distances. The returned score is not the MMR
objective, and may increase at a later rank. No unrequested vectors are exposed
through IPC, PostgreSQL result rows or provenance.

`candidate_limit` bounds the nearest candidate domain and MMR selection,
between the requested result count and 1000. The result count remains at most
100. The target is excluded and mandatory body/key predicates apply before
nearest truncation. This is exact MMR over that candidate domain, not a global
diversity optimum. Text is not used for scoring. Other query strategies,
fusion and token reranking cannot be combined with this adapter.

The helper bounds conservative work before filters:
`(owned live points + min(candidate_limit, owned live points)^2) * dimensions`
must not exceed 20000000. This includes exact nearest recall and worst-case
pairwise scoring. It is not an RSS or latency guarantee.

Target admission shares [source-example model, identity and column permission
checks](source-recommendations.md), including whole-slot readiness and RLS
refusal. Target role and lambda bind the seed digest. Revalidation refuses
an entire in-flight response or cached page after target mutation, including
changes to the excluded target alone. Current source visibility is separate
from durable change-ticket waiting. Cancellation retains native ownership.

Development source protocol 13 requires matching extension, installation SQL
and managed helper with a fresh catalog. Earlier development-catalog upgrades
remain unsupported. Native tests use independent greedy objectives and
original score formulas for all four distances at lambda 0, 0.25 and 1.
Installed tests cover Dot selection, before-cap predicates/target exclusion,
candidate-domain truncation, non-monotonic original scores, actual UUID/text
targets, permissions/RLS, current incarnations and numeric rejection,
rank-preserving paging, excluded-target mutation, cancellation and concurrent
target changes. The retained crash replay suite also rechecks MMR. These
cases verify the bounded adapter, not full Q07 acceptance. General representations,
quality, full Q07/P4-EXPLORE, combinations, migration and release acceptance
remain open.

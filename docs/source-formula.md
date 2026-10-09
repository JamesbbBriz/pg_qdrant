# Development score Formula

An optional `formula` object in `qdrant.search`, `explain_search` and
`search_page` wraps the entire preceding search stage in a native Edge 0.8.0
prefetch. Formula scores and sorts those authorized, bounded candidates:

```sql
SELECT * FROM qdrant.search('articles','example','semantic',10,
 '{"dense":{"model_id":"example-model","model_version":"r1","vector":[1,0]}}',
 '{"candidate_limit":50,"formula":{"op":"negate","arg":{"op":"score"}}}');
```

This reverses relevance within the admitted candidate domain. It does not
retrieve the global least relevant documents. `candidate_limit` remains
between the result count and 1000, and the result count is at most 100.
Mandatory predicates apply before truncation in every branch and on the
Formula stage. Source results pass the existing PostgreSQL permission,
generation and identity/fingerprint rechecks. Unrequested vectors and source
payload fields are not exposed by the native response.

Expressions use a strict tagged object:

| op | Fields | Native expression |
| --- | --- | --- |
| score | none | `$score[0]`, the preceding stage's score |
| constant | value | float32 constant |
| add / multiply | args, 2..8 expressions | sum / product |
| negate / abs / sqrt | arg | negation / absolute value / square root |
| divide | left, right; optional by_zero_default | quotient; explicit zero fallback |

The root has depth one. Maximum depth is 8, with at most 64 nodes and 8192
serialized JSON bytes. Constants and zero defaults must be finite with
absolute value at most 1000000, checked before float32 conversion. Arithmetic
uses pinned native semantics and ordinary float32 input rounding. Division by
zero without a default, negative square root and nonfinite output reject the
complete response. An empty candidate domain returns no hits. Ties do not
promise a deterministic order.

Text, semantic, learned sparse, hybrid, MaxSim and precision stages can be
wrapped. Formula over hybrid consumes the preceding fixed RRF/DBSF fusion
score, rather than adding incompatible BM25 and dense raw scores. Precision
preserves nested recall/fusion and candidate-domain MaxSim before Formula.
The score variable uses native prefetch scores. Dot/Cosine are similarities;
Euclid and Manhattan are positive distances after native nearest-score
postprocessing. Formula itself sorts descending, so an identity score formula
over those distances ranks the farthest admitted candidates first; use explicit
negation when smaller distance should score higher.
Explain records the expression, bounded scope and at most
`64 * candidate_limit` extra node evaluations. These bounds do not replace
the existing MaxSim or query budgets and are not an RSS or latency guarantee.

Explore strategies are refused: Formula ordering would change their existing
rank contract. Payload fields, arbitrary variables, condition/geo/date
expressions and additional native operators require separate declared
capture, synchronization and permission contracts. Those requirements remain
open, along with general Q08/P4-SCORING acceptance and quality evaluation.

The whole expression binds the existing page request hash. Changing it
invalidates a continuation. Cancellation retains the native owner until
completion. Development source protocol 14 requires matching extension,
installation SQL and managed helper with a fresh catalog. Upgrades of earlier
development catalogs remain unsupported. The retained regression cases cover
the scoped adapter; they do not establish release support or full Q08.

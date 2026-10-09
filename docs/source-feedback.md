# Development source-key feedback

The managed helper connects pinned Edge 0.8.0 `FeedbackNaiveQuery` to the
`explore` SQL class. A target and scored examples resolve current PostgreSQL
source keys and one complete named dense BYOV slot. This is partial Q06 work;
general representations, quality, full combinations, upgrades and release
acceptance remain open.

For an index with a ready `example-model/r1` dense slot:

```sql
SELECT * FROM qdrant.search('articles','','explore',10,
 '{"dense":{"model_id":"example-model","model_version":"r1",
   "strategy":"feedback","target":"1",
   "feedback":[{"key":"2","score":2},{"key":"3","score":0}],
   "coefficients":{"a":1,"b":2,"c":0.25}}}');
```

Feedback scores express a relative preference where higher is better; they are
not engine similarity scores. Native scoring is `a * target_similarity` plus,
for every feedback pair with a strictly positive score difference,
`difference^b * c * (positive_similarity - negative_similarity)`. Equal
feedback scores form no pair. A single example also forms no pair. These
cases retain the explicit target coefficient. Similarity follows the declared
distance; no application combines different score scales.

The caller must supply `a`, `b` and `c`. Finite scores, `a` and `c` are bounded
to [-16,16]; `b` is bounded to [0,4]. There is no implicit coefficient training.
These bounds plus the existing dense norm contract bound intermediate native
arithmetic. A nonfinite native result still refuses the complete response.

The target and 1 to 31 scored source examples must be globally distinct.
Canonical bigint, UUID and text keys are supported. Raw vectors, native IDs,
unknown fields and caller tenant domains are refused. Current incarnation,
source/model fingerprints and ready slot validation share the established
[source-example admission](source-recommendations.md). Resolved input is
bounded to 64 KiB. Every example is excluded before candidate truncation;
shared body/key predicates constrain native candidates. The text argument is
not used for scoring. Fusion, sparse/token examples and reranking are refused.

With `n` scored examples, native extraction can form `n*(n-1)/2` pairs, each
scoring two vectors. The helper admits the conservative work bound
`(1 + n*(n-1)) * dimensions * owned live points <= 20000000`, before filters.
Equal-score inputs still use this worst-case admission. It is not an RSS or
latency guarantee.

Source-key roles, ordinal positions, scores and coefficients bind the seed
digest. Permission checks and post-native seed revalidation apply to the
whole query. Paging refuses an old digest even when only an excluded target
changes. Cancellation retains the native owner until its operation finishes.
Current-source admission is separate from durable change-ticket waiting.
Unsupported RLS remains refused.

Development source protocol 13 requires a matching extension, installation
SQL and helper with a fresh catalog. Upgrading earlier development catalogs
is unsupported. Native regression uses independent score formulas for Dot,
Cosine, Euclid and Manhattan. Installed tests cover multipair, equal and single
ratings, coefficient boundaries, source constraints and exclusion before cap,
typed source keys, permissions/RLS and current incarnations, the resolved 64 KiB
bound, seed-sensitive paging, retained-owner cancellation and concurrent seed
mutation. The retained crash replay suite also rechecks feedback scores after
recovery. These cases verify the bounded adapter, not full Q06 acceptance.

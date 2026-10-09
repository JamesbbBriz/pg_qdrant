# Development source-example recommendations

The managed-helper build connects the pinned Edge 0.8.0 dense recommendation
primitives to `qdrant.search`, `explain_search` and bounded `search_page` through
`mode='explore'`. This is partial Q04 integration in the registered owner
domain. It does not complete P4-EXPLORE or establish recommendation quality,
general planning or release support.

[Source-key discovery and context](source-context-discovery.md) use the same
owner and source/model admission with explicit target/context pair contracts.

Declare and populate a named [dense BYOV slot](dense-representations.md) through
ordinary source-table DML. Each current live row must have a ready slot with the
declared model ID/version, source fingerprint and incarnation. Source keys use
the index's bigint, UUID or text identity; they are not native point IDs.

For an existing ready index `articles` with a `dense` slot and source examples
`1` and `2` under model `example-model/r1`:

```sql
SELECT * FROM qdrant.search('articles','','explore',10,
 '{"dense":{"model_id":"example-model","model_version":"r1",
   "positive":["1"],"negative":["2"],"strategy":"sum_scores"}}',
 '{"candidate_limit":50,"matching":{"all":"local search"}}');

SELECT qdrant.explain_search('articles','','explore',10,
 '{"dense":{"model_id":"example-model","model_version":"r1",
   "positive":["1"],"negative":["2"],"strategy":"best_score"}}');
```

The text argument may be empty and does not score this mode. Explicit matching
clauses use the shared native lexical indexes. All examples are excluded in
the native filter before candidate truncation; lexical and exact/prefix key
constraints enter that same candidate domain. No arbitrary payload path or
caller tenant identity is admitted. Effective PostgreSQL identity, registered
owner access and full source/participating-column SELECT are required. RLS is
unsupported and returns 0A000. Final results JOIN and recheck the source table.

Only `best_score` and `sum_scores` are supported. `sum_scores` sums positive
similarities minus negative similarities. `best_score` compares the best
positive and negative similarity: a strictly larger positive returns
`0.5 * (x / (1 + abs(x)) + 1)`; a tie or larger negative returns its negative
transform. Similarity follows the declared dense distance and native vector
preprocessing. Raw scores from different strategies are not interchangeable.
Average-vector, sparse/token recommendations, discovery, feedback, MMR,
fusion/reranking and fallback remain separate requirements.

Exactly one declared dense slot, at least one positive example and at most 32
distinct examples in total are admitted. Empty negatives are permitted. The
five input fields shown above are required; vectors and internal IDs cannot be
supplied. Unknown, deleted, stale or missing examples fail with 55000. Wrong
models, duplicate keys and malformed requests fail with 22023. Source/model
updates in an uncommitted transaction cannot masquerade as a ready native
generation. Use committed change tickets to wait for durable model updates.

Resolved example vectors are limited to 64 KiB before helper submission. The
helper independently validates the generation's model and finite norm contract,
then refuses exact native work when `examples * dimensions * owned live points`
exceeds 20,000,000. This counts the owned corpus before filtering, rather than
assuming the requested top-k limits exact work. Arithmetic saturates on
overflow. This bound does not prove a time or RSS limit. The existing candidate,
timeout and [finite score](finite-scores.md) contracts still apply. Cancellation
ends SQL waiting but retains ownership of an executing native operation until
it finishes; another operation cannot seize that owner.

Explain records the actual strategy and a SHA-256 digest of resolved example
identities, revisions, incarnations, source and representation fingerprints.
Search re-resolves this digest after native execution. Seed changes reject the
whole query instead of returning scores based on a changed example snapshot.
The digest is added to result provenance and private paging snapshots. Changing
only an excluded example invalidates an existing cursor with 55000 even when
every cached hit is unchanged. [Paging](search-pages.md) continues to describe
a bounded candidate domain, not global coverage. Seeds are taken from current
visible PostgreSQL representations; explain is not an Edge durability receipt.

`crates/pg_qdrant/tests/verify_recommendations.py` provides executable fixture
setup and independent Dot goldens, before-cap constraints/exclusion, source
permissions, RLS/invalid inputs, typed keys, stale identity and byte-bound
negatives, seed-sensitive paging and retained-owner SQL cancellation. Its
ranking callback also runs after the installed crash/rebuild replay cuts.
Native tests exercise independent goldens for both strategies under Dot,
Cosine, Euclid and Manhattan, plus before-cap exclusion and work-boundary/overflow
refusal. SQL ranking goldens currently cover Dot. These synthetic fixtures do
not establish relevance quality or the complete recommendation capability
across every supported combination.

Source protocol ABI 12 and the added development snapshot metadata require a
matching helper and fresh installation. Existing development-catalog upgrades
and disk-format/model migrations remain unsupported until separately tested.

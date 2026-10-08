# Declared learned sparse representations

The managed-helper development build accepts named `learned_sparse` slots alongside
BM25 and dense slots. At most four BYOV slots are declared at registration, with
typed model output columns. Source facts and model output use ordinary table
updates, followed by the same committed-event apply, flush and exact ACK contract.
This is partial F02, V02, P1-MODEL and P2-HYBRID integration. Model migration,
user-selectable IDF scopes, tokenizer quality and release support remain open.

```sql
CREATE TABLE sparse_articles (
 id bigint PRIMARY KEY, body text NOT NULL,
 lexical jsonb, lexical_source text, lexical_incarnation uuid,
 lexical_model text, lexical_version text
);
INSERT INTO sparse_articles(id,body) VALUES (1,'local sparse example');
SELECT qdrant.create_index('sparse_articles','sparse_articles','id',
 '{"text":{"fields":["body"]},"representations":{"learned":{
   "kind":"learned_sparse","model_id":"example-lexical","model_version":"r1",
   "tokenizer":"example-tokenizer-r1","dimensions":10000,"distance":"dot",
   "normalization":"none","storage_precision":"float32",
   "vocabulary":"example-vocabulary-r1","idf_policy":"none","idf_revision":"not-applied",
   "vector_field":"lexical","fingerprint_field":"lexical_source",
   "incarnation_field":"lexical_incarnation","model_id_field":"lexical_model",
   "model_version_field":"lexical_version"}}}');
-- Commit registration, wait for index_status to report engine_index_ready,
-- and use these explicit inputs for the declared external encoder:
SELECT qdrant.encoding_inputs('sparse_articles','learned');
```

Each encoding input includes source text, tagged key, incarnation, source SHA-256
and the full model contract. Write the resulting vector and these exact identifiers
to its five declared source columns in one ordinary UPDATE. `track_changes` and
post-commit `await_changes` provide the durable wait. Updating source text makes
previous output stale and removes its old native slot. Late completions, including
those from a deleted/reused key, are rejected. No encoder or model download is
performed by the extension.

A sparse vector is a JSON object with `indices` and `values` arrays. IDs must be
strictly increasing, unique unsigned 32-bit integers below the declared vocabulary
size (`dimensions`, 1..4294967296). Lengths must match and contain at most 2048
entries. Weights must be finite nonzero float32 values. Empty vectors are valid
ready output with no sparse matches. Distance is dot, normalization is none and
storage precision is float32. Dense and sparse shapes cannot be substituted.

Three IDF policies are explicit:

| Policy | Stored/query weights | Native modifier |
| --- | --- | --- |
| `none` | Supplied model weights, unchanged | None |
| `external` | Supplied weights already follow the declared external IDF revision | None |
| `engine` | Supplied model weights; IDF revision must be `qdrant-edge:0.8.0` | Edge IDF at query time |

For `engine`, the adapter explicitly selects pinned Edge 0.8.0 live-corpus
statistics with an empty corpus filter. N counts live points with that named
vector (including ready empty vectors); df counts their term postings.
Query weights are multiplied by
`ln(1 + (N - df + 0.5) / (df + 0.5))`. Default global posting counts retain deleted
offset history until optimization; they are deliberately not used for the installed
pipeline. Statistics cover the owned generation's live corpus; tenant filtering
and user-supplied corpus filters remain unimplemented. BM25 uses its separate
native IDF configuration with the same live-corpus selection. The
declared vocabulary, tokenizer and model revision are compatibility identities;
the extension cannot inspect the behavior of an external model implementation.

```sql
SELECT * FROM qdrant.search('sparse_articles','example','sparse',10,
 '{"learned":{"model_id":"example-lexical","model_version":"r1",
   "vocabulary":"example-vocabulary-r1","idf_revision":"not-applied",
   "vector":{"indices":[7,19],"values":[0.8,0.2]}}}');
SELECT * FROM qdrant.search('sparse_articles','example','hybrid',10,
 '{"learned":{"model_id":"example-lexical","model_version":"r1",
   "vocabulary":"example-vocabulary-r1","idf_revision":"not-applied",
   "vector":{"indices":[7,19],"values":[0.8,0.2]}}}', '{"fusion":"rrf"}');
```

Queries require an exact model/version/vocabulary/IDF revision match and ready
output for every live source row. Missing or stale output raises `55000`; no
fallback is inferred. Native hybrid executes two bounded prefetch branches and
fixed-version equal-weight RRF (k=2) or DBSF, then joins authorized source rows.
Provenance names the actual `hybrid_bm25_learned_sparse_rrf` or `..._dbsf` plan.
The owner/SELECT permission domain, generation fences and resource bounds match
the installed source pipeline. This is not arbitrary nested fusion or a relevance
benchmark.

`verify_sparse.py` checks independent score goldens for all three policies, empty
slots, maximum ID/length admission, malformed contracts/output, missing and stale
output, source edits, key reuse, permissions, native hybrid and actual shadow
generation switching. The installed crash suite reruns these queries after native
cuts and PostgreSQL restart. Hosted evidence must correspond to the exact head.

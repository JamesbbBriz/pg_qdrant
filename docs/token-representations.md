# Token vectors and native MaxSim

The managed-helper development build accepts `token_vectors` BYOV slots through
ordinary source-table model output. This is partial V03, P1-MODEL, P2-HYBRID and
P3-RESOURCE integration. Models, quality evaluation, model/analyzer migration,
general nested query plans and release support remain open.

A token slot uses the same thirteen required model/source fields as a dense slot,
plus `max_tokens` (integer 1..128) and `comparator: "maxsim"`. `dimensions` is
the token width (1..4096), distance is `dot`, normalization is `none` or `unit`
per token, and storage precision is `float32`. The stored/query vector is a
nonempty rectangular JSON matrix with 1..max_tokens rows. Every row has the
declared width and finite float32 values. Empty, ragged, oversized and incompatible
model outputs are rejected. Each token's squared norm is at most
`float32_max / (2 * max_tokens)`, leaving headroom for finite dot-product sums;
large finite coordinates that could overflow scoring are rejected. Native
bit-vector input is not supported by this slot.

```json
{
  "kind": "token_vectors", "model_id": "example-token-model", "model_version": "r1",
  "tokenizer": "example-tokenizer-r1", "dimensions": 2, "distance": "dot",
  "normalization": "none", "storage_precision": "float32",
  "max_tokens": 128, "comparator": "maxsim",
  "vector_field": "token_output", "fingerprint_field": "token_source",
  "incarnation_field": "token_incarnation", "model_id_field": "token_model",
  "model_version_field": "token_version"
}
```

Declare this slot under `representations` when creating an index. The vector
column is jsonb, incarnation is uuid, and the other three output columns are text.
Use `encoding_inputs(index_name, slot)` to obtain the exact source fingerprint,
incarnation and model contract; update all five columns together. Commit and await
the fixed-member change ticket. Source edits invalidate old output and delete its
native slot; late output and deleted/reused-key incarnations are rejected.

```sql
-- The example slot is named tokens and its output is ready for all live rows.
SELECT * FROM qdrant.search('articles','example','maxsim',10,
 '{"tokens":{"model_id":"example-token-model","model_version":"r1",
             "vector":[[1,0],[0,1]]}}');
SELECT * FROM qdrant.search('articles','example','precision',10,
 '{"tokens":{"model_id":"example-token-model","model_version":"r1",
             "vector":[[1,0],[0,1]]}}', '{"candidate_limit":50}');
```

`maxsim` performs exact native nearest search over live named token vectors,
subject to the work bound. `precision` recalls bounded BM25 candidates and
performs exact native MaxSim within that candidate set. Supplying one dense,
one learned sparse, or both adds bounded recall branches, fused using fixed
Edge 0.8.0 RRF (k=2, equal weights) or DBSF before the outer MaxSim stage. Exactly
one token query and at most one query of each recall kind are admitted. A fusion option
requires the additional branch. The candidate cap applies to each recall branch
and the fused candidate set; it must be between top_k and 1000. A higher-scoring
document outside this set does not enter reranking. Candidate-domain exactness
does not promise whole-corpus exact top-k.

MaxSim is the sum over query tokens of the maximum dot product against document
tokens. Fusion scores select candidates; they are not added to MaxSim scores.
Provenance identifies the actual precision plan. Results use the same authorized
PostgreSQL source JOIN and generation/version recheck as other installed modes.
Missing/stale slots fail closed across the live source corpus; no fallback is
inferred. Permissions currently require the registered owner and complete source
SELECT; RLS and tenant query predicates remain unsupported.

Before native scoring, the sole owner bounds scalar dot-product work by
`query_tokens * declared_max_document_tokens * token_width * candidates`, capped
at 20,000,000. Checked arithmetic rejects overflow. Standalone search uses the
native owned shard's live point count, conservatively including points without
the slot; precision uses its prefetch cap. SQL precision preflight applies the
same bound and explain exposes its scope and upper bound. The 128 KiB search
request, existing queue, timeout and result limits apply independently. This work
bound does not establish a wall-time, RSS or relevance guarantee.

`verify_tokens.py` compares independent dot-product goldens, whole-corpus and
restricted-domain results, lexical/dense/sparse RRF/DBSF recall, malformed inputs,
model/permission failures, over-budget rejection, source edits, rollback, reused
keys and real shadow-generation switch. The installed crash suite reruns token
and precision goldens after native recovery cuts and PostgreSQL restart.

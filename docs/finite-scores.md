# Development numeric score budgets

Finite float32 vector components can still overflow dot products, squared
distances or the pinned Edge DBSF variance calculation. The installed adapter
therefore checks the squared L2 norm after float32 conversion in both
PostgreSQL and the managed owner:

| Representation | Maximum squared L2 norm |
| --- | --- |
| Dense, each source/query vector | 1e16 |
| Learned sparse, all nonzero weights combined | 1e14 |
| Token vectors, each token | float32 maximum / (2 × declared max_tokens) |

The dense/sparse bounds apply to all supported distances, normalization and
IDF policies. They leave headroom for native DBSF's float32 variance over the
existing maximum 1000 candidates. Dense squared distance is bounded by 4e16;
the squared difference accumulation in DBSF remains below float32 maximum.
Sparse scoring allows a conservative 64-fold native IDF factor for the live
corpus on Linux x86_64. The score budget never silently clips, scales or
normalizes input; the declared distance and normalization contract still apply.

Invalid source model output raises 22023 inside the business transaction;
the source update, identity revision and outbox insertion roll back together.
`encoding_inputs` returns the applicable converted-vector budget beside each
model contract; `explain_search` exposes the current input/result policy.
Invalid query output raises 22023 before native execution. Unit-vector and
nonzero cosine constraints remain separate. The final native result gate
rejects every non-finite score before JSON serialization, and SQL rejects
null/non-numeric scores rather than returning them to callers.

Source contract version 8 requires matched extension/helper builds. This
development catalog requires a fresh installation. An upgrade or migration
for earlier development catalogs or out-of-contract persisted vectors is
not release supported. Broader precision, quantization, model/configuration
migrations and full quality acceptance remain open.

Installed tests exercise four dense distances, three sparse IDF policies,
atomic rejection of oversized finite model output, native nearest/RRF/DBSF
results at the admitted limits and independent Dot DBSF goldens after replay.
A native unit test deliberately writes an out-of-contract point through Edge
to confirm an infinite score is rejected before serialization.

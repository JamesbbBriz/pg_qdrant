# Declared dense BYOV representations

The managed-helper development build accepts up to four named dense slots at
registration. Each slot is fixed for its index generation and uses JSONB source
columns for vectors, alongside text fingerprint, UUID incarnation, model ID and
model revision columns. Source facts and model results use ordinary table DML;
there is no second document-writing API.

This is partial V01 and P1-MODEL integration. [Learned sparse](sparse-representations.md)
has a separate declared vocabulary and IDF contract. Token vectors,
visual models, changing an existing generation's schema, model-job failure
reporting and online model migration remain open acceptance requirements.
Neither model quality nor release support is implied by the synthetic fixture.

```sql
CREATE TABLE articles (
 id bigint PRIMARY KEY, body text NOT NULL,
 embedding jsonb, embedding_source text, embedding_incarnation uuid,
 embedding_model text, embedding_version text
);
INSERT INTO articles(id,body) VALUES (1,'local search example');
SELECT qdrant.create_index('articles','articles','id',
 '{"text":{"fields":["body"]},"representations":{"dense":{
   "kind":"dense","model_id":"example-model","model_version":"r1",
   "tokenizer":"example-tokenizer-r1","dimensions":2,"distance":"dot",
   "normalization":"unit","storage_precision":"float32",
   "vector_field":"embedding","fingerprint_field":"embedding_source",
   "incarnation_field":"embedding_incarnation","model_id_field":"embedding_model",
   "model_version_field":"embedding_version"}}}');
-- Commit registration, then wait for the asynchronous source scan to finish.
SELECT qdrant.index_status('articles');
SELECT qdrant.encoding_inputs('articles','dense',100);
```

Encoding inputs contain the original indexed text, typed source key, source
SHA-256, current incarnation/revision and complete model contract. The model ID,
revision and tokenizer are application-owned identifiers: the extension does not
download or run that model. Do not substitute a production model with a synthetic
two-dimensional vector. After encoding, use the supplied fingerprint/incarnation
and the declared model ID/revision in a normal UPDATE:

```sql
-- Replace the placeholders with the encoding request's exact values.
BEGIN;
UPDATE articles SET embedding='[1,0]', embedding_source='<source_fingerprint>',
 embedding_incarnation='<incarnation>'::uuid,
 embedding_model='example-model', embedding_version='r1' WHERE id=1;
SELECT qdrant.track_changes('articles');
COMMIT;
-- In a fresh READ COMMITTED transaction:
-- SELECT qdrant.await_changes('<ticket>',5000);
SELECT * FROM qdrant.search('articles','example','semantic',10,
 '{"dense":{"model_id":"example-model","model_version":"r1","vector":[1,0]}}');
```

A slot declares dimensions (1–4096), dot/cosine/Euclidean/Manhattan distance,
`none`/`unit` normalization, and float32 storage. Values must be finite float32;
unit-vector squared norm must differ from one by at most 0.0001. Cosine vectors
must be nonzero; Edge applies its cosine normalization. The adapter fingerprints
the canonical float32 input, source identity and full model contract.

Output UPDATEs are checked even if the assigned value equals its previous value.
An old fingerprint, old incarnation or incompatible model revision raises
SQLSTATE 55000 and rolls back the entire source mutation. Invalid dimensions,
normalization or float32 range raise 22023. NULL vectors clear a slot. Text-only
updates retain source columns but mark their old vectors stale, and remove those
vectors from the actual Edge point. DELETE/reuse assigns a new incarnation and
cannot reuse an old model completion. Named slots can complete independently.

Every ready vector enters the same transaction ledger and committed consumer
batch as the source version. The helper replaces the complete owned point/vector
set, explicitly flushes, then returns a versioned exact-event receipt. Dirty or
replaced helpers replay current source/representation state into a new storage
epoch. Source text and vectors together have a 448 KiB serialized projection
budget; over-budget mutations fail with 54000 before committing a partial event.
Native search frames have a separate 128 KiB bound, supporting 4096 float32
dimensions without changing the 16 KiB diagnostic-probe budget. Helper startup
and flush receipts both check source contract version 5; mismatched builds fail
closed. This development SQL requires a fresh installation; no upgrade script
from earlier 0.0.1 development catalogs is claimed.

Semantic search currently requires its selected representation to be ready for
every live captured source row. Missing/stale/failed rows cause 55000; there is no
silent text fallback or partial-vector coverage. Query vectors must name exactly
one declared slot and its matching model ID/revision. Scores are native Edge
scores for that slot's distance. Results use the same authorized source JOIN,
revision, incarnation and SHA-256 rechecks as text search. [Native hybrid fusion](hybrid-search.md)
combines the same declared representation with BM25. Excerpts are plain source
prefixes, not model-generated highlights.

The initial permission domain remains the registered owner and inheriting roles,
with complete source SELECT. Encoding inputs additionally require UPDATE on all
declared output columns. RLS is rejected. Any source/trigger DDL invalidates the
index; benign column changes also require reconstruction. Model contracts cannot
be changed in place. Until online generation migration is implemented, drop and
register the index again; expect encoding inputs to require fresh incarnations.

`verify_models.py`, invoked by the installed `verify_p1.py` suite, checks actual
semantic ranking, stale-slot removal in native candidates, late outputs, named
slot independence, delete/key reuse, rollback, permissions and model/schema
validation. The same suite verifies model replay across four native crash
cuts and PostgreSQL immediate-stop recovery. Full P1/P2 acceptance remains open.
The additional cut aborts after deleting the old point but before its replacement
upsert, proving that a partial named-vector replacement cannot produce an ACK.

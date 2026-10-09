# Declared scalar source payload

`create_index` can declare up to eight scalar source columns. Ordinary table
writes capture these columns in the existing transaction ledger; applications
do not write a second document store. The same point identity, incarnation,
revision, generation fence, explicit Edge flush and exact event ACK apply to
text, model vectors and payload together.

```sql
CREATE TABLE products (
  id bigint PRIMARY KEY, body text NOT NULL, category text,
  quantity bigint, price double precision, available boolean
);
INSERT INTO products VALUES (1,'blue cotton shirt','clothing',12,29.5,true);
SELECT qdrant.create_index('products','products','id',
 '{"text":{"fields":["body"]},"payload":{
   "category":{"field":"category","kind":"keyword"},
   "quantity":{"field":"quantity","kind":"integer"},
   "price":{"field":"price","kind":"float"},
   "available":{"field":"available","kind":"bool"}}}');
SELECT qdrant.index_status('products'); -- wait for a ready generation
BEGIN;
UPDATE products SET quantity=11 WHERE id=1;
SELECT qdrant.track_changes('products'); -- retain this ticket
COMMIT;
-- SELECT qdrant.await_changes('<retained ticket>',30000);
SELECT qdrant.retrieve('products','["1"]');
```

| Kind | Required PostgreSQL type | Value contract |
| --- | --- | --- |
| `keyword` | `text` | At most 1024 UTF-8 bytes, case and punctuation preserved |
| `integer` | `bigint` | Exact signed 64-bit integer |
| `float` | `double precision` | Finite double; NaN and infinities rejected |
| `bool` | `boolean` | Boolean scalar |

Every kind allows SQL NULL, represented by JSON null. Aliases match
`[a-z][a-z0-9_]{0,31}`; source column names are resolved and quoted as PostgreSQL
identifiers, including Unicode names. Arbitrary paths, arrays, JSON objects,
implicit type conversions, UUID/datetime/geo kinds and undeclared columns are
not admitted by this subset. Their applicable full capability requirements
remain open.

Only the declared columns are read into a projection. The complete projection
is bounded to 8 KiB of PostgreSQL JSON text; overflow rejects and rolls back
the source write. The existing 448 KiB text/vector projection budget includes
the new payload. Dispatch selects at most 16 events and a conservative 480 KiB
event prefix, leaving room within the unchanged 512 KiB IPC envelope for the generation
and model contracts. Unselected events remain pending and cannot be ACKed.
This prefix is an exact membership set, not a commit-order watermark.

The managed owner creates native keyword/integer/float/bool field indexes below
`attributes.<alias>` before applying points. This namespace cannot overwrite
the private source identity or text fields. Schema creation, mutation and flush
remain serialized by the single owner. Payload contracts cannot change inside
an owned generation. Shadow reconstruction and owner-loss replay carry the
same declared contract and current source projection.

`source_fingerprint` remains the SHA-256 of source text used by the model
completion contract. `payload_fingerprint` is a separate SHA-256 of the
PostgreSQL canonical JSON projection encoded in UTF-8. Updating payload alone
increments the row revision while preserving a model output whose source text,
model and incarnation still match. These fingerprints cover different inputs.

Retrieve returns `attributes` and `payload_fingerprint` only for found current
source rows. Native IPC carries only private identity/version fingerprints,
not attribute contents. SQL rechecks the native revision/incarnation and both
fingerprints, then derives attributes from the authorized current source.
Search also rechecks the payload fingerprint before exposing a result.
Declared column SELECT, source binding and the existing owner-domain policy
are required. Source type/DDL drift invalidates the generation; unsupported RLS
is refused. Cancellation and recovery retain the existing ownership rules.

This is the synchronization and typed-index foundation for Q08/Q09/Q11/Q12.
It does not expose generic payload filters, grouping, facets, statistics,
payload formula variables or payload ordering. Full product acceptance,
quality, configuration migrations, upgrade/rollback and release remain open.
Development source protocol 15 requires a matching helper, library and install
SQL with a fresh development catalog. Direct dependencies and Cargo.lock are
unchanged. Native helper tests and `verify_payload.py` exercise actual typed
indexes, replacement/deletion, registration rollback, boundaries, durable
tickets, byte-packed delivery, generation rebuilding and source retrieval;
`verify_models.py` checks that payload-only changes preserve ready vectors.

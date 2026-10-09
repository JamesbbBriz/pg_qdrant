# Bounded source-key retrieval

`qdrant.retrieve(index_name, source_keys, options)` executes the pinned Edge
`RetrieveRequest` in the managed helper, then joins its identity/version
metadata to the authoritative PostgreSQL source. It returns no native body,
vector or score. Each found item's excerpt is the first 240 characters of
the current authorized source text; it is not a match highlight.

```sql
CREATE TABLE read_demo(id bigint PRIMARY KEY, body text NOT NULL);
INSERT INTO read_demo VALUES (1, 'first source'), (2, 'second source');
SELECT qdrant.create_index('read_demo', 'read_demo', 'id',
  '{"text":{"fields":["body"]}}');
SELECT qdrant.index_status('read_demo'); -- wait until the generation is ready
SELECT qdrant.retrieve('read_demo', '["2","1","999"]', '{"timeout_ms":5000}');
```

Input is 1–100 distinct source key strings, each at most 1024 UTF-8 bytes;
the serialized key array is capped at 128 KiB. Bigint and UUID strings are
normalized using their registered PostgreSQL types; text preserves case,
Unicode and punctuation. Duplicates after normalization are rejected.
`timeout_ms` is the sole option, an integer in 1–30000, default 5000. Arbitrary
point IDs, payload selectors, paths, vectors and filters are not public inputs.
Malformed or over-budget input raises `22023`.

Items preserve normalized request order. `found` includes `source_key`,
`point_id`, `incarnation`, `revision`, `source_fingerprint`, `excerpt`,
`attributes` and `payload_fingerprint`. Attributes are the current authorized
[declared scalar projection](source-payload.md), or an empty object.
`source_missing` means the current source/ledger has no live identity;
`native_missing` means no record for its current point ID was retrieved;
`native_stale` means native metadata does not match the current incarnation,
revision, key, text fingerprint or payload fingerprint. Non-found items contain only the requested
key and status. They are not successful durability receipts. A newly reused
primary key cannot expose the old incarnation's point or text.

The response names the exact generation/storage epoch and explicitly labels
the scope as the requested current source keys. The item array is capped at
256 KiB and the existing IPC response cap remains enforced. Missing keys are
reported independently; there is no claim of global search coverage, count,
pagination, ordering by payload, scroll or sampling.

The current owner-domain policy checks index ownership and source/key/text
and declared payload column SELECT before native work and again before exposure. Unsupported RLS raises
`0A000`; capture/source drift, dirty or unavailable generations, and a changed
generation/epoch/consumer/helper identity raise `55000`. Catalog and source
binding locks use `NOWAIT` (`55P03` on a conflict); they span native execution
and SQL recheck, but do not lock every requested source row. A concurrent
commit can therefore produce a non-found status or a fail-closed readiness
error. Returned found rows are checked in one fresh SQL statement against
current visible source facts and ledger versions; this is not a cross-engine
transaction snapshot. PostgreSQL transaction visibility rules still apply.

Cancellation/timeout discards the caller's response while the supervisor
retains ownership until the native operation actually completes. Callers must
not interpret cancellation as completion or start a second owner. Normal and
private-fault builds use the same retrieval route; faults remain opt-in.

Development source protocol 15 requires matching extension/helper/install SQL
and a fresh development catalog. Direct dependencies and lockfile are unchanged.
This implements the retrieve portion of Q10. Order-by, scroll, sampling,
complete P4-READ combinations, quality, actual upgrade/rollback and public
release acceptance remain open. See `verify_retrieve.py` and the native helper
test for actual execution evidence; input validation alone is not verification.

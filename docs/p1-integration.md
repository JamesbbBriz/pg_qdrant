# Installed source-to-Edge integration

Status: installed Linux PG17 integration tests pass locally; full P1/P2 and
release acceptance remain open. Hosted exact-revision CI is still required.
[ADR 0005](adr/0005-p1-ledger-integration.md) records consolidation
of the two earlier capture branches.

The managed-helper build installs the canonical ledger via `CREATE EXTENSION`.
It currently admits one non-null text column (at most 65,536 UTF-8 bytes per row)
and a bigint, UUID or text primary
key on an ordinary permanent heap table. [Declared dense BYOV](dense-representations.md)
adds fixed-generation named vectors through ordinary source-table updates.
[Declared learned sparse BYOV](sparse-representations.md) uses independent vocabulary
and IDF contracts, including native BM25/sparse RRF and DBSF.
[Declared token-vector BYOV](token-representations.md) adds bounded native MaxSim
and lexical/dense/sparse candidate reranking with an explicit candidate domain.
Multiple text fields, RLS,
partitions and alternative table access methods remain unimplemented. These
requirements remain in the formal acceptance contract.

```sql
CREATE EXTENSION pg_qdrant;
CREATE TABLE documents (id bigint PRIMARY KEY, body text NOT NULL);
INSERT INTO documents VALUES (1, 'transaction recovery');
SELECT qdrant.create_index('documents', 'documents', 'id',
                          '{"text":{"fields":["body"]}}');
-- Commit registration and inspect the asynchronous build:
SELECT qdrant.index_status('documents');
BEGIN;
UPDATE documents SET body = 'reliable transaction recovery' WHERE id = 1;
SELECT qdrant.track_changes('documents'); -- retain the returned UUID
COMMIT;
-- In a new READ COMMITTED transaction:
-- SELECT qdrant.await_changes('<returned UUID>', 5000);
SELECT d.id, h.score
FROM qdrant.search('documents', 'recovery') h
JOIN documents d ON d.id = h.source_key::bigint
ORDER BY h.rank;
```

Capture precedes a bounded online scan of 32 source rows per transaction, using
primary-key ordering and row locks. Source table/row locks use NOWAIT; conflict
rolls back the scan batch without advancing its cursor, then retries. A locked
first key cannot starve another index's committed event set.
Catalog changes use non-key update locks,
so a long source transaction's foreign-key references do not block consumption
of another key's committed events. Existing tombstones and newer identities
are never overwritten by backfill. Registration has no 1,000-row limit.

The worker selects a committed exact event set and commits dirty state before
native work. The helper applies current-source reconciliation and explicitly
deletes obsolete point IDs. Explicit successful Edge flush returns a receipt
bound to generation, storage epoch, consumer and exact event membership. Only
a matching receipt permits PostgreSQL ACK. Caller deadlines do not release a
running native operation's owner.

Source dispatch materializes pending work before checking source bindings. An
idle index therefore avoids repeating catalog, trigger and event-history
validation on each poll; selected pending sources still pass the same capture
gate. The installed regression measures actual function calls with a positive
control and requires zero binding validations and unchanged consumer state for
an idle dispatch. This does not establish a latency or memory guarantee.

Helper replacement rotates storage epoch, preserves dirty directories, and
replays retained PostgreSQL events into a fresh shard. It uses a fresh random
execution identity rather than PID equality as its fence.
Readiness checks compare this identity with the live supervisor, including
catalog rows temporarily skipped during management.
Every retained event is reconciled with the current identity. Old deletes cannot remove a new point;
old upserts cannot resurrect old incarnations. Readiness stays false until
backfill and reconciliation complete. Event retention, storage bounds and
garbage collection are open operating gates.

Consumer transactions hold the extension object lock while using installed
functions. Catalog key-share locks protect consumer-state insertion against
concurrent index deletion without blocking unrelated non-key source updates.
Rows locked for management are skipped and retried; an in-flight receipt for
such a row is left unacknowledged and may be replayed after rollback.
Uninstalled consumers remain dormant; reinstalling creates new generations.

Tickets seal previously unsealed events in the current transaction. Later
writes require another ticket. Own uncommitted waits fail. Timeout returns
`durable=false`; cancellation raises a PostgreSQL error. Empty tickets still
require a validated generation. A historical ACK does not authorize serving a
later dirty generation. ALTER TABLE, ALTER TRIGGER and source/trigger drops
invalidate capture persistently; re-enabling a trigger does not repair missed
changes. Source writes remain possible in degraded state; drop and register
the index again to reconstruct it. The conservative policy also invalidates
benign source table alterations. The tested persistence fence is process-crash flush
ordering, not power-loss, PITR or replication support.

The current permission domain is the registered source owner and effective
roles inheriting that owner, with complete table and indexed-column SELECT.
Ordinary writers need no ledger access. Search rejects RLS and joins hits to
real source rows, checking incarnation, revision and source SHA-256. Candidate
underfill remains possible; candidate counts are not exact match counts.
Search pins the catalog binding and source relation across native execution,
uses NOWAIT for conflicting management locks, and repeats permission and DDL
checks before exposing source rows. A concurrent RLS change waits for the
in-flight search and makes subsequent searches fail closed.
Native candidate responses fetch only identity/version fields, keeping long
source bodies out of IPC; excerpts are read from the authorized source JOIN.

BM25 uses the pinned multilingual tokenizer, no stemming or stopwords,
lowercase, disabled extra ASCII folding and average length 16. The tokenizer
still performs its own lossy multilingual normalization. This fixed configuration is
not a validated language quality policy. [Native candidate matching](lexical-matching.md)
adds bounded term/phrase and distinct token/whole-key prefixes across recall
and reranking branches. Full lexical quality, configurable analyzers and
advanced APIs remain separate implementation gates.

Run `crates/pg_qdrant/tests/run-p1.sh` against the installed helper build.
`experiments/p1-ledger/run.sh` preserves the original independent transaction
regressions. P0 automatic CI also runs the installed source-to-search and crash
integration. Remaining model kinds/migration, comprehensive DDL, storage/OOM faults, upgrade/rollback,
quality, resource limits and public packaging still require acceptance.

The dedicated storage fixture fills a 384 MiB tmpfs hosting only Edge indexes;
PostgreSQL WAL, source tables and IPC remain on another device. Native I/O
failure or mapped-page SIGBUS must leave events unacknowledged. After capacity
is released, helper replacement (or explicit supervisor restart after budget
exhaustion) reconstructs a fresh storage epoch and proves exact-ticket durability
and source search. Failed directories are retained. This is a bounded process
recovery test; public recovery tasks and comprehensive capacity management remain
open. The guarded kernel OOM fixture additionally checks pending delete/key-reuse
events across helper replacement; kernel victim attribution remains required.

Same-contract asynchronous rebuild tasks now construct, catch up and switch a
separate native generation; see [generation lifecycle](p3-resource-generation.md)
for precise receipts, long-transaction fencing, cleanup and remaining limits.
This does not repair invalidated source capture or migrate model/analyzer contracts.

Index removal returns a committed cleanup task rather than equating catalog
deletion with native storage deletion. Rollback performs no native retirement;
owner changes preserve uncertain epochs and report failure. Await the returned
task before uninstalling. See the generation lifecycle for the full boundary.

# Native generation builds and resource admission — incomplete phase

`qdrant.rebuild_index(name)` now commits an asynchronous same-configuration
build task. The managed helper creates a separate native Edge shard while
ordinary source DML and queries continue on the serving generation. This is
partial L07/P3-GENERATION implementation, not full P3 acceptance.

## Execution and durability

Each task has its own generation, storage epoch, consumer and precise event
receipts. It reconciles retained committed outbox events against the trusted
source ledger, including obsolete point deletion and current named vectors.
Each bounded batch is acknowledged only after the exact native flush receipt.
No maximum event ID is treated as a committed completion watermark.

Cutover requires receipts for every committed event and an exclusive catalog
binding lock. Source writes retain a foreign-key key-share lock until commit;
admitted searches pin the binding. A long transaction or admitted search
therefore prevents a premature switch. Once the lock and catch-up conditions
hold, PostgreSQL atomically replaces the generation and consumer identity.
Tickets for a superseded generation explicitly report `generation_invalidated`.

After cutover, the sole native owner flushes and drops the old shard's
references before removing its validated child directory. Retirement receipts
are replayable for the exact task/consumer identity. A changed owner or
uncertain path preserves storage and reports cleanup failure. Task success
and `retired_storage_cleanup` are separate results.

```sql
SELECT qdrant.rebuild_index('articles'); -- returns task_id
SELECT qdrant.task_status('returned-task-uuid');
SELECT qdrant.await_task('returned-task-uuid', 60000);
SELECT qdrant.cancel_task('returned-task-uuid');
```

Waits require READ COMMITTED and reject an uncommitted task created by the
calling transaction. Timeouts return `timed_out`; cancellation is idempotent
and prevents cutover without claiming to interrupt a running native call.
Management task IDs are distinct from change-ticket IDs. Task operations
require the index owner; starting a build additionally requires source SELECT.

## Verification and remaining scope

`verify_generations.py` tests actual flush/receipt/cutover, out-of-order commits,
queries during construction, deletion/key reuse, timeout, rollback, committed
cancellation, denied roles and dense/hybrid replay. Targeted shadow crashes
before and after flush leave the build failed and unacknowledged; the serving
identity is retained, recovered and queried before a fresh successful build.
The helper unit test verifies exact retirement replay, mismatched identities,
directory removal and refusal to reopen or mutate a retired epoch.

Rebuilding currently requires a trusted, capture-enabled source with completed
backfill and a ready serving generation. It does not repair disabled capture,
rescan changed schemas, change model/analyzer contracts or migrate disk formats.
Failed/cancelled shadow directories and uncertain old epochs are retained.
The helper's 32-open-shard limit and 256-retirement-receipt limit remain bounded
capacity constraints; disk/RSS admission and failed-build cleanup remain open.

The protocol resource validators and narrower SQL limits do not establish full
native memory/thread/optimization budgets. Backup/PITR/replication, format
upgrade and rollback, platform distribution, full fault combinations and
quality gates remain required. No release support or complete stage gate is
promoted by this implementation.

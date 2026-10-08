# Native generation builds and resource admission — incomplete phase

`qdrant.rebuild_index(name)` now commits an asynchronous same-configuration
build task. The managed helper creates a separate native Edge shard while
ordinary source DML and queries continue on the serving generation. This is
partial L07/P3-GENERATION implementation, not full P3 acceptance.

## Helper process limit

The Linux helper sets both soft and hard `RLIMIT_AS` to at most 8 GiB before
creating native threads or opening an engine. A tighter inherited administrator
limit is preserved; less than 512 MiB refuses startup. Failure to install or
verify the limit refuses the ready handshake. The PostgreSQL supervisor requires
the observed limit in that handshake and exposes active `helper_resource_limits`
in index owner status. PostgreSQL backend/supervisor limits are unchanged.

This bounds virtual address space, including libraries, stacks and file-backed
mmap. It is not an RSS/cgroup budget or an index sizing recommendation.
Linux returns ENOMEM for address-space allocations exceeding the limit;
native allocation failures can still abort the helper. Such failure cannot
produce a successful flush receipt, and existing dirty-epoch reconstruction
rules apply. Each opened source shard requests two Edge search threads; this
does not cap all process threads or optimization work.
See the [Linux resource-limit contract](https://man7.org/linux/man-pages/man2/getrlimit.2.html).

The private fault build probes a real oversized mmap without touching physical
pages, requires ENOMEM and queries the same live native index afterward. This
test proves address-space refusal and is separate from actual kernel OOM tests.
Standalone helper tests independently inspect `/proc`/prlimit and test preserved
tighter inherited limits and refusal of an unsupported startup budget.

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

Cancelling a committed build queues cleanup of its owned shadow without changing
the serving generation. A failed build keeps its original error and outcome;
cleanup has separate `abandoned_cleanup_state`, `abandoned_storage_cleanup`,
`abandoned_pending_epochs` and `abandoned_cleanup_error` fields. The helper
finishes any active native call before retiring its references. Exact flush and
retirement receipts are required; a logical cancellation alone cannot report
physical cleanup.

```sql
SELECT qdrant.cancel_task('returned-build-task-uuid');
SELECT qdrant.await_task('returned-build-task-uuid', 60000, true);
```

The three-argument wait additionally waits for cleanup of a cancelled/failed
shadow or a successful build's superseded generation. A cancelled result stays
`state=cancelled`, `succeeded=false`, even when cleanup completes. Cleanup failure
returns its separate failure status; a timeout sets both `timed_out` and
`cleanup_timed_out`. Owner/snapshot/uncommitted-task rules still apply. Archived
build cleanup belongs to its returned drop task. A build cancelled in its
creation transaction never allocates native storage.

## Transactional index removal

`qdrant.drop_index(name)` returns a distinct drop task. Source triggers and the
catalog are removed in the calling PostgreSQL transaction. A rollback restores
them and performs no native deletion. After commit, the sole helper flushes,
closes and retires each precisely recorded native generation/storage epoch.
The task reports `physical_cleanup_completed=true` only after all matching
retirement receipts are acknowledged. Logical removal alone is insufficient.

```sql
BEGIN;
SELECT qdrant.drop_index('articles'); -- retain task_id
COMMIT;
SELECT qdrant.await_task('returned-drop-task-uuid', 60000);
```

Drop tasks remain owner-protected after the catalog has disappeared. Completed
rebuild outcomes are archived, including their original failure or cancellation
state. Pending rebuilds become cancelled by index removal. A committed drop
cannot be cancelled: ownership cleanup remains necessary. A same-name new
registration has a different internal index identity and storage path, so the
old task cannot remove its shard.

If a helper has changed since an epoch was owned, cleanup fails closed and
preserves its uncertain directory. The task explicitly reports failure and
remaining epochs; it does not claim physical cleanup or reopen that storage.
Consumer epoch rotations retain prior ownership records, so recovery cannot
erase an old directory from subsequent cleanup accounting.
Automatic orphan reclamation across helper replacement is still unsupported.
Wait for drop tasks before uninstalling the extension; dropping the extension
does not itself prove that its asynchronous native cleanup completed.

## Verification and remaining scope

`verify_generations.py` tests actual flush/receipt/cutover, out-of-order commits,
queries during construction, deletion/key reuse, timeout, rollback, committed
cancellation, denied roles and dense/hybrid replay. Targeted shadow crashes
before and after flush leave the build failed and unacknowledged; the serving
identity is retained, recovered and queried before a fresh successful build.
The helper unit test verifies exact retirement replay, mismatched identities,
directory removal and refusal to reopen or mutate a retired epoch.
`verify_retirements.py` covers transactional rollback, uncommitted wait refusal,
owner admission, forged receipts, same-name replacement, archived task outcomes,
40 native create/drop cycles, unrelated directory preservation and actual helper
SIGKILL with explicit failed cleanup and usable fresh registration.
`verify_abandoned.py` exercises 40 actually flushed shadow builds followed by
cancellation and physical retirement while the serving generation stays bound,
plus rollback, stopped-helper timeout, forged receipts, owner/snapshot rejection,
unstarted cancellation and archived cleanup outcomes. A fault-build-only error
after actual native point mutations and before flush produces no event receipts;
the same owner retires that failed shadow while preserving the original failure.
A fresh build then switches successfully and cleans superseded serving storage.
This injected error is separate from the actual SIGKILL and storage/OOM tests.

Rebuilding currently requires a trusted, capture-enabled source with completed
backfill and a ready serving generation. It does not repair disabled capture,
rescan changed schemas, change model/analyzer contracts or migrate disk formats.
Same-owner failed/cancelled shadows now retire while the index remains registered.
Index removal transfers remaining owned epochs into its cleanup task. Uncertain
old epochs are retained and cannot be reported as cleaned.
The helper's 32-open-shard limit and 256-retirement-receipt limit remain bounded
capacity constraints; disk/RSS admission and old-owner orphan reclamation remain open.

The protocol resource validators and narrower SQL limits do not establish full
native memory/thread/optimization budgets. Backup/PITR/replication, format
upgrade and rollback, platform distribution, full fault combinations and
quality gates remain required. No release support or complete stage gate is
promoted by this implementation.

# P1 source ledger: initial executable slice

Status: **experimental; not part of CREATE EXTENSION's installed SQL**.
The P0 feasibility PR is the base of this stacked branch. Run the PostgreSQL
17 disposable test harness with:

~~~bash
docker build -f Dockerfile.p0 -t pg-qdrant-p1-base .
docker run --rm --memory=5g --cpus=2 --network=none \
  pg-qdrant-p1-base bash experiments/p1-ledger/run.sh
~~~

## Implemented in this slice

- Transactional PostgreSQL catalog, source-key projection and event outbox,
  installed by an explicit SQL experiment against the P0 extension's private
  schema. This is not a released install/upgrade script.
- One ordinary persistent built-in heap source per index with a default single-column
  bigint/uuid/text btree primary key, non-null text field, and no RLS,
  inheritance or partitioning; registration fails otherwise.
- One-transaction, source-table ACCESS EXCLUSIVE capture installation and
  initial backfill, limited to **1000 rows**. This protects correctness while
  deliberately not claiming non-blocking online backfill or scalability.
- Statement-level TRUNCATE tombstones, row-level INSERT/UPDATE/DELETE,
  persistent point identity and revision, new UUID incarnation and new point
  allocation on delete/reinsert, SHA-256 of the selected body field.
- Trigger capture extracts only the configured key/text fields through quoted
  identifiers, rather than converting an arbitrarily wide source row to JSON.
- Source-state diagnostics degrade if a source disappears, changes name,
  enables RLS, or disables/replaces its expected capture triggers. An event
  verdict function marks out-of-date key/revision/incarnation as stale, but
  is advisory rather than an atomic native apply fence.
- Source and outbox writes share one PG transaction. Rollback and savepoint
  rollback remove both. Tests cover DML by a non-superuser whose source rights
  do not grant access to internal ledgers.
- Explicit ticket membership: tracking only *currently unassigned events in
  the current top-level transaction*, not event-ID max/watermarks. Tickets
  created after further writes are distinct. Tickets cannot be polled from the
  writing transaction. An uncommitted/aborted ticket does not become visible.
- UUID/text keys, COPY, multirow updates, a wide irrelevant bytea field,
  deliberate trigger errors, bounded-backfill refusal and disabled-trigger
  assertions are covered by SQL. Two-session tests exercise cross-key commit
  inversion and same-key serialization. An immediate-stop/restart probe checks
  committed outbox and ticket persistence across PostgreSQL recovery.

## Not implemented / correctness boundary

**No Edge apply, flush or durable acknowledgement exists in this slice.**
Nonempty tickets are always pending, with durable=false; the probe must never
return a successful wait. The source/outbox event table is not an integrated
continuous worker and must not be advertised as automatic semantic search.
No application-facing qdrant.create_index or search function is declared.

P1 still needs bounded online backfill with DDL interlocks, consumer claims,
per-key CAS against current source state, persisted Edge generations,
post-flush exact-event ACK, replay/lease recovery, actual wait semantics,
model fingerprint/late-output contracts, DROP/rebuild and full lifecycle
coverage. P2 must add source/tenant authorization to every query branch.

This probe stores only the selected text field and a tagged stable key; it
does not claim multi-field analysis, BYOV model readiness, query-privacy
guarantees or full source-table mapping. Event sequences are allocation order,
not commit order. A source may disappear; the status reports degraded, and
the probe has no automatic source-DDL cleanup yet. This status observes only
current trigger/schema state. Disabling capture, making writes, then enabling
capture again may cause an undetectable historical gap. The product must
prevent unsafe source DDL or require source reconciliation/rebuild.

The immediate-stop experiment covers PostgreSQL WAL recovery only, not Edge
storage durability, a crash between Edge flush and PostgreSQL ACK, or automatic
index reconstruction. Those remain open acceptance gates.

**No public release or full P1 gate is considered passed by this experiment.**

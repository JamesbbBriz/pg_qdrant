# P1: transaction capture patch — incomplete phase

This draft installs an actual PostgreSQL-owned transactional capture foundation.
It is not a complete P1, not a complete ingestion pipeline and not a queryable
Qdrant index.

## Implemented in this patch

- Source-owner validated registry; permanently stored per-key revision and
  incarnation; projection-only fingerprint; immutable pending event journal.
- Registration under SHARE ROW EXCLUSIVE source lock, capture trigger installed
  before a complete table scan; the triggers, initial event log and index
  registration commit or roll back together.
- Source INSERT, UPDATE, DELETE, COPY-through-row-trigger, rollback/savepoint
  events are journaled in the same transaction. Per-key row locks serialize
  revisions even when transactions have inverted or delayed commit order.
- DELETE then INSERT changes incarnation without ever reviving an old model
  job. Updates to unindexed columns retain fingerprint while increasing
  revision. Identity supports bigint, UUID and text keys on plain heap tables.
- Unbounded event IDs and writer transaction IDs are NOT used as commit-order
  watermarks. No event has a pretend applied/durable state.
- Primary-key updates and TRUNCATE explicitly fail. RLS, partitions, inherited
  relations, nondeterministic text-key collations, unsupported text fields,
  derived columns and mutable/unverified source shapes are rejected.
- Registry inspection is limited to the source owner's role membership.
  Index status always reports search_ready=false and edge_applied=false.

The first source scan deliberately blocks writers. Nonblocking interleaved
backfill/catch-up is a later P1 gate; it must not be claimed from this patch.
Source owners can still disable/drop triggers or alter the table: full DDL event
guards and corruption detection are not implemented yet.

## Pending — phase must stay Draft

No Edge consumer, flush/durable ACK, retry cursor, generation cutover,
ticket/wait contract, full model output version validation, production SELECT
authorization, concurrency/fault injection suite or usable search SQL exists.
An outbox alone cannot satisfy P1-DURABILITY or P2.

SQL smoke fixture: crates/pg_qdrant/tests/p1_capture.sql.
Suggested commands after installing the P1 binary on a disposable PG17 cluster:

    cargo check --locked -p pg_qdrant --no-default-features --features pg17
    cargo pgrx install --manifest-path crates/pg_qdrant/Cargo.toml \
      --pg-config "$(command -v pg_config)" --cargo=--locked \
      --no-default-features --features 'pg17 p0-managed-helper'
    psql -X -v ON_ERROR_STOP=1 -d pgq_test \
      -f crates/pg_qdrant/tests/p1_capture.sql

These were NOT run on this revision. P0's passing CI cannot verify these
new SQL objects. See docs/ci-temporary-hold.md for restoration requirements.

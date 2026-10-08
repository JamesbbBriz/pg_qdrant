# ADR 0003: Edge durability boundaries and recovery ownership

Status: **accepted source-derived constraints; integration design proposed and unimplemented**.
Scope: published `qdrant-edge =0.8.0`, primarily L04 and L05, with dependencies
on L01–L03, L06–L07 and L10–L12. This ADR does not complete P0 or change any
capability's release-support status.

## Evidence and context

The [source audit](../evidence/p0-edge-durability-source.json) identifies the
registry archive checksum, exact source files, hashes, line ranges and selected
verified excerpts. It uses the released Rust package, not Qdrant Server behavior.
The [capability contract](../capabilities.md), [dependency contract](../dependencies.md)
and [baseline](../dependency-baseline.json) remain the maintained scope.

The existing [explicit-flush/SIGKILL test](../../crates/edge-probe/tests/persistence.rs)
exercises a completed flush followed by process death and reopen on the same
running kernel. It neither proves unflushed WAL replay nor simulates power loss.
No new crash-survival experiment accompanies this source audit.

| Fixed-version path | Source observation | Accepted boundary |
| --- | --- | --- |
| `EdgeShard::update` | Appends a WAL record, obtains an internal operation number, applies the operation and returns `Result<()>`. | Successful update return alone is not a durable acknowledgment. The call exposes no durable operation cursor. An application error may occur after the WAL append. |
| WAL append and opening | Append writes a mapped record. Opening rebuilds the log entry index and validates its header/layout/CRC; an invalid tail stops parsing. Rollover can initiate flushing. | Recovering the WAL container is distinct from applying its logical operations. Absence of an explicit caller flush does not prove that no internal persistence occurred. |
| `EdgeShard::flush` | Flushes WAL, then calls synchronous forced segment flushing. The segment holder's returned persisted sequence is discarded. | Explicit successful flush is the minimum candidate persistence fence. It is not an exposed durable cursor or an atomic transaction with PostgreSQL. |
| `EdgeShard::load` | Opens the WAL, loads segment state, checks/repairs segments, ensures an appendable segment and persists resolved configuration. It does not iterate or apply WAL records. | Automatic logical WAL replay is absent from this fixed load path. Successful load does not prove that every previously applied update survived. |
| Segment loading and repair | Loading uses persisted segment versions; compatibility handling can migrate older formats. Consistency repair can delete mappingless points and flush repairs. | Loading is not a read-only forensic operation. Preserve an unclean generation before attempting diagnostic reopen. |
| Public WAL surface | `WalOptions` exposes segment capacity, precreation queue length and closed-file retention. The inspected public surface has no replay, durable cursor, WAL acknowledgment or reclamation API. | Neither a sync-on-write contract nor bounded total WAL usage follows from these options. No private WAL API or custom WAL decoder is adopted. |

The package contains generic segment comments about recovery by replaying WAL.
Those comments do not establish a call from the released Edge loader to replay.
Likewise, a short durability comment on `SerdeWal::write` does not override its
append implementation or the segment append contract. The audit follows the
actual call path.

`Drop` calls `flush` and logs errors. A graceful exit can therefore persist data
that the caller did not explicitly flush, and cannot substitute for an abrupt
termination experiment. Snapshot partial recovery ends by calling the same
loader; it is not a public WAL replay alternative. The separately tested
update-only Store, Delete and empty-bootstrap paths remain unavailable and do
not provide a recovery writer.

## Proposed PostgreSQL acknowledgment protocol

The following is a design requirement for implementation and testing, not a
claim that the current prototype implements transactional source capture:

1. Capture source changes and their stable identities in a durable PostgreSQL
   outbox in the source transaction. Select a fixed set of committed events.
   A sequence value or `MAX(id)` is not a transaction commit-order watermark.
2. One owner applies that exact set to one generation. Revision, incarnation,
   content fingerprint and model rules reject stale or incompatible work.
   Serialize mutation, maintenance and the selected flush/ACK boundary until
   their concurrent behavior has an independently verified contract.
3. Call `EdgeShard::flush()` explicitly and require success. An update or flush
   error prevents acknowledgment; do not hide it behind an eventual destructor.
4. Only then commit PostgreSQL acknowledgments for the exact applied event set
   and generation. A committed-change ticket refers to its fixed event set,
   never to all events below the greatest numeric ID or to future changes.
5. Persist PostgreSQL-owned dirty-generation state before starting mutations.
   Define a durable clean/checkpoint transition after a successful quiescent
   flush. Dirty-state and ACK ordering, including PostgreSQL commit durability,
   need their own crash tests; they do not create two-phase commit with Edge.

| Crash or error boundary | Required behavior and limit |
| --- | --- |
| Update returns before explicit flush | Retain pending PostgreSQL events. In-process visibility is not a durable ACK. |
| Update or flush fails | Do not ACK; expose degraded/failed state. An already appended WAL record or partially changed storage is not a rollback guarantee. |
| Flush succeeds before PostgreSQL ACK commits | Pending events remain authoritative. Reapply them only through a verified idempotent recovery path on a validated base, or rebuild. |
| PostgreSQL ACK commits | The tested ordering completed. This alone proves neither power-loss durability nor backup, replication, PITR or timeline compatibility. |
| A later unclean mutation damages the same generation | Currently unacknowledged events may not reconstruct older acknowledged data. A complete reconstruction source or checkpoint/history path is required. |

Replay from PostgreSQL means deterministic logical operations with the project's
identity/version rules. It does not mean reading Edge's WAL or supplying its
private operation numbers. Replaying pending events cannot be presumed to repair
arbitrary partially persisted payload indexes, vectors, mappings or versions.

## Proposed recovery and readiness policy

An abnormal owner exit or persisted dirty state must not return a writable
generation to READY merely because `EdgeShard::load` succeeds. Mark it degraded,
retain its provenance and preserve unclean artifacts under an explicit storage
budget. If there is insufficient room for a safe diagnostic copy, leave the
original untouched rather than inspecting it through a mutating loader.

Two recovery candidates remain to be implemented and compared:

- **Fresh generation from PostgreSQL:** rebuild from source data, configuration,
  stable identity metadata and retained compatible BYOV representations; capture
  and catch up concurrent commits, then switch generations only after validation.
- **Proven checkpoint plus complete history:** preserve a validated clean base
  and every required PostgreSQL event after that checkpoint, including deletion
  and model/version effects. Restore/replay into a new generation and validate
  it before switching. Snapshot archive production, cutover and rollback remain
  separate gates; the current manifest inspection is insufficient.

The first route is the conservative baseline candidate while dirty-generation
reconciliation lacks evidence. PostgreSQL event retention/garbage collection
must agree with the chosen recovery route. A ticket that succeeded before a
later incident does not make that incident invisible or authorize serving an
unvalidated generation.

## WAL capacity and upgrade gate

The inspected Edge paths do not call `SerdeWal::ack` or WAL prefix truncation.
`retain_closed` controls retention when truncation occurs; it is not automatic
reclamation. Therefore total WAL growth is not bounded by setting a small
segment capacity or a retention count. Long-running ingestion requires measured
growth, admission/storage budgets, failure behavior and a supported reclamation
decision before release.

Candidate resolutions are an upstream public replay/checkpoint/reclamation
contract in a selected release, or a project generation-rotation/rebuild policy
with proven cutover and cleanup. Neither is adopted by this ADR. Dependency
upgrades must re-audit these paths and rerun durability/recovery tests; new
upstream APIs do not automatically become supported SQL behavior.

## Ownership and remaining gates

| Owner | Required deliverable | Exit evidence still required |
| --- | --- | --- |
| Engine adapter, L01–L04 | Map update/flush/load errors and the fixed public persistence boundary. | Actual failure cuts, corrupt/partial state behavior, compatibility and bounded WAL-growth measurements. |
| PostgreSQL/source lifecycle, L05–L06 | Transactional outbox, exact-set ACK/tickets, version arbitration and retention. | Rollback/savepoint, out-of-order commit, primary-key reuse, late encoding, duplicate replay and crashes before/after ACK. |
| Catalog/lifecycle, L07/L10 | Dirty/clean state, preserved artifacts, fresh-generation rebuild and safe readiness. | Owner death during mutation/flush/maintenance, honest degraded state, verified recovery/catch-up/switch and executable rollback. |
| Recovery/build, L11–L12 | Platform persistence and migration support matrix. | Supported storage failure model, actual power-loss methodology if promised, restore/PITR/replication/failover gates independently passed. |

A future bounded unflushed SIGKILL experiment can falsify the claim that update
return alone preserves every logical change after process death. If all values
survive, it records survival with the same kernel page cache, not proof of WAL
replay or power-loss durability. Such an experiment must preserve expected and
observed records and native exit status without relabeling data loss as a passed
recovery capability. L04, L05, all remaining capability requirements and the P0
go/no-go decision remain open.

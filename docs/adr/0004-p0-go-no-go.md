# ADR 0004: Conditional go for a packaged Edge helper and source rebuild recovery

Status: **accepted conditional go for product implementation; P0 exit pending**.
Reviewed: 2026-10-08. Owners: PostgreSQL integration, engine adapter, source,
lifecycle, lexical policy, quality and distribution.

This decision selects the implementation route after the finite conditions below
pass. It does not declare a production indexing service, a supported release or
completion of any of the 54 capabilities. It refines the process choice in
[ADR 0001](0001-embedded-engine-boundary.md), retains the lexical obligations in
[ADR 0002](0002-lexical-gap-strategy.md), and adopts the conservative recovery
direction in [ADR 0003](0003-edge-durability-and-recovery.md). The three maintained
contracts remain [capabilities](../capabilities.md), [dependencies](../dependencies.md)
and the [version baseline](../dependency-baseline.json).

## Decision

Proceed with Rust, pgrx and the published Qdrant Edge crate. Select a
**PostgreSQL supervisor plus an exec-created native helper installed in the same
package** as the next product implementation topology. The helper directly owns
Edge; it is not a separately operated Qdrant network server. Application writes
continue to target ordinary PostgreSQL tables. The project must implement the
internal asynchronous projection and its synchronization contract.

Use ordinary `EdgeShard` public mutation/query/flush APIs. Reject direct native
engine ownership inside a PostgreSQL shared-memory background worker as the
product default. Retain that diagnostic profile as a failure-domain control;
this documentation does not change the current compile-time default.

Select explicit successful flush before exact-event-set PostgreSQL ACK as the
minimum persistence fence to implement and validate. Select **refusal of an
unclean generation followed by construction of a fresh generation from a complete
authoritative source** as the initial recovery policy. Do not select automatic
Edge WAL replay, private WAL decoding, in-place dirty repair, or the incomplete
update-only writer as substitutes.

This is a conditional engineering go, not a statement that the current code
implements those catalog, source, ACK or generation policies. P1 starts only
after the P0 conditions below are recorded as passed or resolved by an explicit
costed replacement decision.

## Evidence and its limits

[CI7](../evidence/p0-source-capacity-oom-ci.json) identifies head
`88885db08d17c074af097ddca14bc85d6978980c`, merge checkout
`18282abe660b4a3a59a8222f072c5b1cd069a2ae` and their common tree
`7d02f181cc1c6ca8d14d5b82c03cc74d4a349265`. Its complete job log and downloaded
53-file artifact have verified hashes. All four SQL profiles passed at that
revision. Later additions have separate local evidence and need a new frozen
source/container run.

| Observation | Decision consequence | Limit retained |
| --- | --- | --- |
| Direct worker native abort, SIGKILL and genuine kernel OOM terminate companion SQL and trigger PostgreSQL recovery. | A native engine thread in that worker is an unacceptable default failure boundary. | A passing characterization assertion records collateral damage; it does not approve it. |
| Native helper abort/SIGKILL preserve companion SQL and the same supervisor. CI7 kernel records identify the exact helper PID and limiting memory cgroup; one OOM victim is recorded, with zero supervisor/client kill attempts. | Prefer the same-package helper for native code containment. | The controlled OOM test raises the target's `oom_score_adj` and limits the entire disposable container. It does not prove production memory isolation or guarantee the kernel chooses the helper under arbitrary pressure. |
| SQL cancellation retains active ownership; queued canceled work never executes. A frozen helper is stopped after the separate 125,000 ms budget, and replacement waits for exit/reaping. | Keep owned request IDs, bounded queues, independent operation deadlines and explicit process-stop recovery. | Caller cancellation is not native Edge query cancellation. Killing an owner can invalidate a writable generation. |
| PostgreSQL supervisor SIGKILL triggers PostgreSQL recovery; the old helper stops before the same fence is reacquired. Restart exhaustion is visible after four starts. | Keep the supervisor small, use EOF-driven child shutdown, and fail visibly after bounded restart attempts. | The helper does not protect PostgreSQL against failures in its own supervisor. The tested fence is a P0 owner fence, not a production multi-generation catalog. |
| Full-text configuration-save ENOSPC/retry/flush/reopen and an independent clean reopen pass on separate exact 384 MiB tmpfs mounts. A separate 128 MiB case refuses before writes. | A bounded installation can exercise the required engine paths; storage admission needs explicit headroom. | The earlier 128 MiB SIGBUS failures remain failures. Eight-row fixture capacity is not a sizing formula, and config-save recovery is not arbitrary partial-write recovery. |
| Six source/identity groups pass in each CI7 SQL profile; a seventh inside-SPI cancellation group and a heap-only source guard are later additions. | There is a concrete source/visibility integration route; retain explicit supported source definitions and native-error tests. | These functions are superuser-only P0 diagnostics. They do not establish production SELECT/tenant authorization, an incarnation allocator or transaction capture. The later seventh group is not a CI7 pass. |
| The new dirty-generation test kills an owned child after applied mutations and before explicit flush, never opens the old generation afterward, verifies unchanged old file hashes, and rebuilds matching records/vectors from a complete fixture into a different generation. | Fresh reconstruction is a demonstrated primitive for the conservative recovery route. | This is a local test-controller policy. It does not inspect survival in the dirty shard, replay Edge WAL, persist PG dirty state, recover ACKs or switch a live production generation. |
| Callable public-method probes and 17 isolated negative-input cases expose both typed errors and accepted malformed inputs/panic paths. | Validate names, dimensions, sparse structure, finite values, phrase prerequisites and budgets before every engine call; quarantine uncertain state after an engine error/panic. | A characterization pass does not mean the engine rejects all malformed data or that owner reuse after arbitrary errors is safe. |

The later evidence is [callable-method and integrated local validation](../evidence/p0-method-input-rebuild-local.json),
[negative inputs](../evidence/p0-negative-inputs-local.json), and
[dirty refusal/reconstruction](../evidence/p0-dirty-rebuild-local.json).
Method compile coverage and explicit public/private/unavailable dispositions
are recorded in the [method audit](../p0-edge-methods.md). A callable signature
does not confer runtime or product support for every argument combination.

## Persistence, ownership and availability consequences

The source-reviewed Edge 0.8.0 `flush` path flushes its WAL and forces synchronous
segment flushes. Its ordinary loader does not logically replay WAL operations,
and loading can repair or mutate persisted state. The public API does not expose
a durable operation cursor or WAL reclamation operation. The same-kernel
explicit-flush/SIGKILL/reopen test supports a process-death boundary; machine
power loss, storage-device guarantees and PostgreSQL/Edge atomicity remain
unproven.

The product protocol must therefore meet these requirements:

1. Commit source changes and persistent outbox events in the same PostgreSQL
   transaction. Seal ticket membership explicitly; numeric event allocation is
   not commit order. PostgreSQL commit-durability settings are part of that
   future supported contract.
2. Persist dirty-generation state before mutation, then serialize the selected
   event batch, maintenance and flush/ACK boundary under one generation owner.
   Validate incarnation, revision, fingerprint and model inputs before applying
   work. An update/flush error prevents ACK.
3. Explicitly flush successfully before committing acknowledgments for that
   exact applied event set and generation. A clean checkpoint requires a
   quiescent, validated transition; neither an update return nor an eventual
   destructor is the durability fence. Crash cuts around these transitions
   belong to P1 acceptance and are not implemented by this ADR.
4. After an abnormal owner exit or uncertain dirty state, refuse affected
   generations before opening them through the mutating loader. Preserve their
   provenance and files under a bounded policy; do not label them READY or
   silently replay only pending events into unknown partial state.
5. Reconstruct from PostgreSQL source/configuration/stable identity metadata and
   retained compatible BYOV data, capture/catch up later commits, validate,
   flush and switch to a new generation. Missing reconstruction inputs leave
   the required representation unavailable; they do not authorize silent
   fallback. Keep any independently validated serving predecessor until the
   new generation is ready and its readers have drained.

This choice spends rebuild time and temporary disk capacity to avoid assuming
an unavailable replay contract. If no validated predecessor exists, affected
queries can remain unavailable during reconstruction. Operators need truthful
status, cancellation/retry and a documented recovery procedure. A previously
successful ticket does not hide a later incident or make damaged data safe.

WAL growth remains an operating constraint. The implementation route is storage
admission/backpressure and generation rotation from authoritative source, subject
to P1/P3 correctness and resource tests. No manual WAL truncation or claimed
automatic reclamation is accepted. Before a user-facing indexing preview,
storage exhaustion must stop new index work without false ACK; before sustained
operation is supported, generation rotation, cleanup and retention must run
under concurrent writes. If that cost is unacceptable, a selected upstream
public lifecycle API or a new costed architecture decision is required.

## Lexical route and unchanged scope

Keep Edge for the core P1 data path and the P2 BM25/hybrid/MaxSim plans. Keep
project-owned matching/analysis/identifier/result policy regardless of the later
lexical backend. The [isolated Tantivy experiment](../../experiments/tantivy-probe/README.md)
and [its evidence](../evidence/p0-tantivy-local.json) make direct Tantivy 0.26.2
the leading mature-library candidate for F13/F14 primitives; they do not adopt
it into the core build.

Its five semantic cases establish useful primitives and material gaps:
TopDocs does not bound fuzzy enumeration; the public bridge bounds returned
terms but not all enumeration work; slop can cross repeated values; fuzzy
queries do not produce the terms needed by the native snippet helper; and
commit/manual-reader reload is a separate visibility boundary. They provide
no held-out bilingual relevance, cross-engine authorization, total work budget
or combined Edge/pgrx build evidence.

The finite lexical decision before core Alpha remains the ADR 0002 adoption
gate: compare the defined Edge policy baseline, direct Tantivy and fixed-version
pg_search on frozen semantic/quality inputs and integration costs. Direct
Tantivy adoption needs bounded matching, source offset/array semantics, a
compatible build, shared IDs/permissions, paired readiness/generations,
idempotent writes and explicit outer fusion. The alternative pg_search SQL
integration retains separate installation, version and license work. Complex
fuzzy/positional indexing is not assigned to an uncosted new implementation.

This staged choice is sufficient to choose the source/engine integration route;
it is not permission to omit F13–F20 or announce complete FTS. No capability ID,
work item or acceptance row is deleted: the 54 capabilities, 70 capability and integration work
items and 45 stage rows remain the formal product scope.

## Finite conditions to record P0 exit

| Condition | Required evidence | Current disposition |
| --- | --- | --- |
| Freeze and validate the complete selected source | One identified clean container/CI tree with callable-method, negative-input and dirty-rebuild tests; all four SQL profiles including the new inside-SPI group; both helper pipe profiles; normal fault-path exclusion; bounded disk/OOM cases. Retain earlier failures. | CI7 passed its earlier source. Later local engine/compile results are separate; new combined SQL/native-package run pending. |
| Freeze reproducible native and CPU inputs | Verify the revised native package snapshot/pins, installed package inventory and selected license metadata against that same build. Declare and enforce the minimum CPU features used by the actual native code, or remove the corresponding generic-CPU claim. | Revised native-input work and conservative CPU guard require their own recorded verification. Generic x86_64 compatibility is not inferred from one runner. |
| Reconcile and accept the decision | Update the three contracts and evidence/report references without promoting product capabilities; accept this topology/recovery boundary and the costed plan, including the unresolved lexical adoption before Alpha. | Topology/recovery and planning allocations accepted conditionally; current native/CPU/CI evidence reconciliation remains pending. [Effort and phase allocation](../p0-effort.md). |

P0 is an architecture feasibility gate. Production capture/backfill/tickets,
ACK crash cuts, source authorization, live generation switching, WAL admission
and rotation, quality evaluation, package/upgrade support and advanced lexical
delivery retain their original P1–P5 gates. Moving their implementation into P0
would not make the evidence stronger; claiming they already work would be false.

## Alternatives and reopening criteria

| Alternative | Decision and cost consequence |
| --- | --- |
| Native Edge inside PG worker | No-go default: measured native and OOM failures terminate unrelated SQL. Avoiding a helper saves packaging/IPC work but accepts the failure domain the product seeks to reduce. |
| Same-package managed helper | Conditional go: retains one installation and direct Rust Edge dependency; adds helper lifecycle, framing, ownership, resource and packaging work. Preliminary remaining worker allocation is 12–22 engineering days, within the broader plan, not a measured duration. |
| Separately operated Qdrant Server/client SDK | Not the selected product: adds a network service and operator/application integration, and does not meet the embedded-dependency contract. |
| Fork/internal WAL replay/reclamation | Not adopted: requires ownership of upstream disk format and recovery invariants, compatibility and license/notice work. A separate bounded investigation and ADR would be needed before estimating implementation. |
| Fresh source rebuild after uncertainty | Selected baseline: relies on retained authoritative data, costs source scan/encoding availability and temporary generations. Its implementation allocation is included in lifecycle work; no constant-time recovery claim. |

Reopen the decision if a clean build cannot enforce its CPU/native baseline, the
helper fails the required current fault cases, exact-event persistence cannot be
demonstrated, retained source/vector inputs cannot reconstruct the promised
representations, or bounded storage/rebuild costs exceed measured deployment
budgets. A failed condition produces an explicit no-go or replacement ADR;
elapsed investigation time cannot turn it into a pass.

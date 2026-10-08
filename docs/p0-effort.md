# P0 decision, remaining gates and preliminary effort

Status: **planning assumptions after CI7; no calendar or completion promise**.
Reviewed: 2026-10-08. This allocation supports
[ADR 0004](adr/0004-p0-go-no-go.md). It supplements the
[70-item work ledger](work-items.json), rather than replacing or adding to its
per-capability estimates. All [54 capabilities](capabilities.md) and
[45 acceptance rows](acceptance.md) remain required.

## What the evidence changes

The direct published Rust graph, four diagnostic PostgreSQL profiles, actual
SQL/Edge calls and the helper fault comparison have evidence at the exact CI7
revision. That reduces uncertainty about embedding and process layout. It does
not reduce source consistency, production authorization or retrieval-quality
work merely because a synthetic query is fast or an engine call compiles.

The constraints increase specific costs: native faults require a packaged
helper; caller cancellation cannot stop a native call directly; load is not a
logical WAL replay path; dirty state requires refusal and a complete rebuild
source; WAL growth requires admission/rotation; some malformed public inputs
reach accepted/panic paths; and Tantivy primitives do not provide bounded work,
array-safe proximity or fuzzy/source-offset highlights by themselves.

The [CI7 evidence](evidence/p0-source-capacity-oom-ci.json),
[later engine/compile checkpoint](evidence/p0-method-input-rebuild-local.json),
[negative-input characterization](evidence/p0-negative-inputs-local.json),
[dirty rebuild policy experiment](evidence/p0-dirty-rebuild-local.json), and
[isolated lexical probe](evidence/p0-tantivy-local.json) remain separate source
and scope records. None is a completed product work item.

## Remaining P0 work is finite

| Work package | Owner / existing IDs | Planning range | Exit condition |
| --- | --- | --- | --- |
| Freeze current source and rerun the clean combined/native baseline | Build, worker, engine; I-01–I-03, L08/L10/L11 | 2–5 engineering days | All required current tests run; failures resolved or architecture decision reopened. Includes the new seventh source/inside-SPI group and unchanged private/normal separation. |
| Final API/CPU/native/license evidence reconciliation | Engine and distribution; I-01/I-02/I-12, V05/V08/L11 | 1–3 engineering days | Public-call/absence dispositions agree with code; CPU minimum is enforced or claims narrowed; actual selected native package and license metadata are recorded. Full distribution notices remain P5 work. |
| Accept topology/recovery and update lexical gap/cost decisions | Source, lifecycle, lexical; I-03/I-07, L04/L05/L07/L10, F13–F20 | 1–3 engineering days | Costed ADR and phase ownership accepted without claiming production implementation. Lexical adoption criteria and pre-Alpha decision remain explicit. |

These ranges are a **4–11 person-day planning allocation if no new architecture
failure appears**, not measured work or a deadline. They exclude waiting for
external infrastructure and overlap review with CI execution. A new failure is
diagnosed and re-estimated; the range is not a reason to drop a check. The
original 5–10 day investigation stop-loss remains historical planning context.

## P0 exit versus later product acceptance

| Area | What P0 can decide | What remains after that decision |
| --- | --- | --- |
| Build/API | Exact platform/dependencies compile; supported public call routes or explicit exclusions exist. | Supported binary distribution, full upgrade/rollback and broader CPU/PG/platform matrix. |
| Core engine | Synthetic BM25/vector/fusion/MaxSim/filter/group semantics and wrong-input dispositions are known. | Validated product request compiler, all cross-capability contracts, permissions and real relevance/latency measurements. |
| PostgreSQL | Owned IPC, tested failure domain, bounded diagnostic requests, candidate formats/identities and scoped visible-result rechecks are feasible. | User-facing source/index privileges, transactional capture, persistent identities, tickets, source DDL and driver contracts. |
| Persistence | Explicit flush is the minimum source-reviewed fence; process-death and complete-fixture reconstruction primitives work in the tested scopes. | PostgreSQL dirty/clean and exact-ACK ordering, all crash cuts, concurrent source backfill/rebuild/switch, bounded retention and actual operating recovery. |
| Lexical gaps | Edge gaps and mature-library candidates are measured enough to cost the next route; no invented Meilisearch equivalence. | Before Alpha: decide adoption using the frozen quality/budget/lifecycle criteria. By P4: deliver each F13–F20 contract and any required second-engine integration. |

The seven P0 acceptance rows are not deleted or weakened. A failed prerequisite
keeps P0 conditional. A passed P0 slice also leaves the full associated
capability open: for example, source fixture checks do not complete L05/L09,
and successful dirty-refusal reconstruction does not complete L04/L07.

## Preliminary module allocation after P0

An engineering day means one experienced contributor's focused design,
implementation, review, relevant tests and documentation work. These are
judgment-based ranges with low confidence, not empirical facts, probability
intervals, billing quotes or dates. They assume Linux x86_64 on the explicit
verified CPU floor, PostgreSQL 17, Rust/pgrx and ordinary Edge 0.8.0 public APIs;
an owned-helper route; source tables with the selected simple key/heap contract;
BYOV; and no arbitrary RLS, same-transaction read-your-writes, transparent HA or
power-loss guarantee in the first supported scope.

The table allocates shared work once at the module level. It must not be summed
with the ledger's 70 older capability/integration ranges. Within this table,
module-local tests/review/docs belong to their row; the final integration row
owns only cross-module matrices and release regressions. Exact staffing and
subtask allocation still require review before turning these into a schedule.

| Module / stage | Retained capability and integration scope | Main deliverable and dependency | Remaining engineering-day assumption |
| --- | --- | --- | --- |
| SQL/catalog/tasks — P1/P2/P5 | L01/L07/L08; I-04/I-11/I-15 | Versioned index/source/config/generation/task catalog, errors, stable SQL/driver contract and internal privilege boundary; depends on accepted topology. | 16–28 |
| Source transactions/identity — P1 | L05; I-04 | Transactional outbox, capture-before-backfill, commit-order arbitration, persistent point/incarnation mapping, tombstones, exact-event tickets and idempotency; depends on catalog and serialized engine fence. | 24–42 |
| Model and representation state — P1/P2 | F02/F03, V01–V03/V06/V07, L06 | Fingerprints separate from revisions, versioned BYOV formats, late-output rejection and per-slot coverage/state; depends on source identity and durable encoding inputs. | 10–18 |
| Worker/IPC/helper — P1/P3 | L01/L10; I-03/I-14 | Product helper installation/handshake, request/generation identity, admission, queue/deadline/cancel, owner fences and bounded restarts; builds on P0 without reusing diagnostic permissions as product security. | 12–22 |
| Core engine/planner — P2 | F01–F12, V01–V03, Q01–Q03/Q09/Q12/Q14; I-02 | Validated text/balanced/precision plans, analyzer configuration compilation, mandatory predicates in every branch, score/tie/fallback/coverage contracts and capability registry. | 20–36 |
| Results and authorization — P2/P4 | F17/F19/F20, Q09/Q11, L08/L09; I-05 | Trusted domains, source/index/column checks, supported RLS refusal, bounded refill, groups/provenance, excerpts and honest totals; depends on source contracts and every planner branch. | 22–40 |
| Lifecycle/recovery/storage — P1/P3 | L01–L04/L07/L10/L12; I-09/I-14/I-16 | Dirty/ACK ordering, source rebuild, generation pin/cutover/cleanup, optimize policy, WAL admission/rotation and supported recovery boundaries; depends on outbox and model retention. | 26–48 |
| Advanced retrieval/storage plans — P3/P4 | V04–V08, Q04–Q08/Q10/Q11/Q13/Q14 | Quantization/MRL/rescore, visual/explore, recommendation/feedback/MMR/Formula, scoped read/matrix/facet operations with the same permissions/budgets. Native bit-input absence stays an explicit decision. | 22–40 |
| Lexical policy/features — P2 decision, P4 delivery | F13–F20; I-07 | Grammar, typo/proximity/array semantics, synonyms, original-source offsets, authorized suggestions/statistics and evaluated identifier policy; depends on the pre-Alpha backend decision. | 22–42 |
| Conditional second-engine integration — P2/P4 | F13–F20, L02/L04–L07/L09–L11; I-08 | If adopted: compatible build/licenses, shared IDs/authorization, idempotent dual writes, independent readiness, paired rebuild/recovery, outer fusion and coordinated upgrades. | 18–35, conditional and additional to feature policy |
| Quality and comparisons — P2/P3/P4 | All F/V/Q families; I-06/I-13 | Frozen bilingual/document/identifier judgments, model and preprocessing versions, ablations, fairness-controlled comparisons and measured budgets; no fixture-vector quality claims. | 16–30 |
| Build, upgrades and release — P3/P5 | L11/L12; I-01/I-10–I-12/I-15/I-16 | Locked native/core packages, CPU/PG matrix, old-index migration and executable rollback, notices, examples and supported-scope release/runbooks. | 18–32 |
| Cross-module reliability/security validation — P1–P5 | All L IDs and cross-capability matrix; I-04/I-05/I-09/I-10/I-14/I-15 | Adversarial source/model/DDL/rebuild interleavings, authorization on every path, fault and upgrade regressions, end-to-end declared journeys. | 20–38 |

These ranges express the scale and cost centers of a substantial project; they
are not evidence that any implementation will finish within the lower bound.
The conditional row cannot be removed merely because Tantivy is not yet linked:
it remains I-08 pending an adopted route or an explicitly justified alternative.
The simpler core Alpha is a subset, not completion of this full-product table.

## Critical path and decision triggers

The P1 critical chain is catalog and stable identity → transactional capture and
consistent backfill → version/fingerprint arbitration → serialized explicit
flush and exact ACK/ticket semantics → crash cuts and source rebuild. The
worker boundary, package plumbing and source test fixtures can progress beside
it, but cannot independently certify its consistency.

P2 depends on that data loop, then on shared analyzer/predicate compilation,
permission-safe core plans/results and a frozen bilingual evaluation set. The
lexical adoption comparison can run alongside P1 and must finish before Alpha;
it can change the data/readiness/generation workload, so it is a real decision
deadline. P3 depends on proven live generation switching and storage/resource
admission before sustained workloads or an operational core release. P4 adds
the remaining advanced/lexical contracts; P5 release work is gated by the
corresponding supported user journeys and reliability evidence.

Re-estimate after the first end-to-end P1 transaction/ACK crash matrix and after
the lexical comparison. Measured rebuild duration, retained-vector volume,
WAL growth, merge/optimization costs, filter fill rate and quality failures may
change the plan substantially. A new upstream public lifecycle contract could
reduce integration work only after its own upgrade, compatibility and rollback
tests. Parallel contributors reduce calendar time only where dependencies and
review capacity permit; no division of person-days is promised here.

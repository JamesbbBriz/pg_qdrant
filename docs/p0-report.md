# P0 feasibility checkpoint

Observed: **2026-10-08**. **P0 feasibility passed** at implementation head
`57c58fcee51efb0067f04b03ffb44e300f76ce72`, CI run `37743504259` (CI8).
The accepted implementation route is Rust/pgrx with a managed Edge helper in
the same installation package. This closes the architecture investigation and
allows P1 product implementation; it does not declare a source-indexing product,
a supported release or completion of all capabilities. The
[composite decision](evidence/p0-feasibility.json) and
[ADR 0004](adr/0004-p0-go-no-go.md) reconcile all seven P0 gates.

## Latest recorded execution

[CI8 evidence](evidence/p0-current-ci.json) records run
[37743504259](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37743504259),
job `113199465881`, implementation head
`57c58fcee51efb0067f04b03ffb44e300f76ce72` and actual pull-request merge checkout
`716cb2871d325dfd629a40177c9cfee613af4cfc`. Both have tree
`8b1d6d863be52229f8b15b49ad0b58b96b28608f`. All workflow steps passed. The
downloaded 605,977-byte artifact's SHA-256 and all 100 members were verified,
including the inner SQL/OOM reports and the native and lexical artifacts.

| Executed slice at that revision | Result | Evidence boundary |
| --- | --- | --- |
| Rust 1.96.0, Edge 0.8.0, pgrx/cargo-pgrx 0.19.3, PostgreSQL 17.11 | Four profiles built, installed and executed | Restricted Linux x86_64/native CPU baseline; not a release matrix |
| Ordinary engine suite | 27 passed, zero failed or ignored | Includes two CPU-policy tests; synthetic correctness, not relevance or performance |
| Child bookkeeping / Python diagnostics | 2 / 38 passed | Separate from SQL and positive native faults |
| Private OOM guard tests | Three distinct tests passed in both private builds | Guard tests alone cannot prove OOM |
| Direct-worker normal/private SQL | 20 / 22 passed | Expected collateral remains a negative topology finding |
| Managed-helper normal/private SQL | 20 / 26 passed | Seven source/identity groups in each of all four profiles |
| Helper normal/private pipe protocol | 7 / 8 passed | Exact owned process identities and lifecycle checks |
| Native package/CPU evidence | 246 package/version entries, seven PGDG archive hashes, 20 required CPU features verified in each profile | Scoped native-object/ELF and declared-license metadata; not complete redistribution clearance |
| Isolated Tantivy 0.26.2 experiment | Five semantic cases passed in its separate image | No core dependency adoption or completed F13–F20 product contract |
| Vector/keyword ENOSPC, 32 MiB | Passed | Eight rows/four representations; no mutable text indexes |
| Full-text ENOSPC and separate clean reopen, exactly 384 MiB | Both passed | Already-flushed fixture, representation/phrase/prefix/MaxSim checks |
| Full-text refusal, exactly 128 MiB | Passed before writes | No Edge open, ENOSPC or recovery claim at 128 MiB |
| Kernel OOM, two fresh 768 MiB/no-swap containers | Both strict attribution/behavior characterizations passed | Direct-worker containment failed; helper containment passed for this controlled experiment |

SQL counts include repeated common groups. The 16-group engine smoke nested in
SQL is one containing assertion group; counts cannot be added as completed
capabilities. The 128 MiB historical failures below remain failures.

## Preceding local source checkpoints

The [method/input/rebuild record](evidence/p0-method-input-rebuild-local.json)
binds exact source hashes for six successful commands: engine compile, the full
**25-test** normal suite and all four PostgreSQL compile profiles. It adds:

- **Callable API coverage:** 43 concrete argument-bearing call bodies covering
  44 reviewed method declarations, with one explicit `refresh_with` exclusion.
  Private compile-only markers prevent these methods from running as tests.
  [Method inventory](p0-edge-methods.md).
- **Invalid-input characterization:** 17 independent owned children expose
  missing adapter checks. Edge accepts some NaN/infinity inputs, a directly
  constructed malformed sparse vector panics, and phrase prerequisites affect
  semantics. These are preserved negative findings, not a validation gate pass.
  [Exact outcomes](evidence/p0-negative-inputs-local.json).
- **Dirty-generation policy:** after exact owned-child SIGKILL, the controller
  refuses to reopen the old generation, preserves its 36 files and builds a new
  generation from a complete four-row source fixture. No private WAL decoder or
  automatic replay is used. [Reconstruction evidence](evidence/p0-dirty-rebuild-local.json).
- **PostgreSQL cleanup:** a seventh test waits inside SPI planning on a secondary
  index, cancels the query, checks guard release and reuses the same backend.
  At that local checkpoint the built-in heap-only restriction and this test
  were compile-only additions absent from CI7. CI8 now executes that source and
  the new cancellation assertion; no adversarial custom-AM fixture is claimed.

An independent [Tantivy 0.26.2 workspace](../experiments/tantivy-probe/README.md)
passes five local primitive experiments with its own locked graph. Native fuzzy
queries lack the proposed term-expansion limit; a public dictionary bridge can
bound returned expansions but does not cap all FST work. Phrase slop can cross
repeated field values or admit reversal. Native fuzzy queries provide no snippet
terms, while repeated-value snippets lose value identity. These observations
preserve the product requirements and guide the lexical decision; they do not
adopt a second engine or complete F13–F20. Its five cases also passed in the
separate [CI8 lexical image](evidence/p0-tantivy-ci.json).

The subsequent [CPU-integrated local record](evidence/p0-cpu-native-local.json)
checks 33 exact source inputs and passes **27 ordinary engine tests**, all four
PostgreSQL compile profiles and both helper compile profiles. Two pure CPU
policy tests are included in the 27. Actual host admission reports all 20
required features; generated SSE/AVX2 objects pass the scoped HLE/RTM and
AVX-512 audit. One earlier saved compile log lost its completion line and no
longer matched its captured digest. That raw record is retained and excluded
as current verification; the same command passed again against unchanged
source with a verified new log. This checkpoint does not execute the revised
native package installation, inside-SPI SQL test or positive resource faults.

The [native package freeze](p0-native-build.md) fixes the Ubuntu snapshot and
seven PGDG archives and requires all 246 installed package/version entries to
match. Every image is configured to retain the [native build report](p0-native-inventory.md),
including tool and ELF hashes, actual libclang diagnostics when present,
package copyright hashes and explicit unparsed license gaps. The
[CI8 native record](evidence/p0-native-ci.json) now verifies these inputs
against all four clean images. The exact loaded libclang path remains unobserved
and 121 package license files are not parsed into structured declarations; their
metadata and gaps remain explicit. These are not completed distribution notices
or a fully resolved transitive link/license clearance.

## Foundation and product boundary

The starting revision `f3b4cff09e3abc2aaaa3c647028aef4942d89636` contained design
documents. The implementation now has a real exact-version workspace, lockfile,
explicit features, callable public API probes, executable engine fixtures,
loadable diagnostic SQL and bounded owner/IPC/helper experiments.

[Eight user journeys](user-journeys.md), [45 acceptance rows](acceptance.md),
[54 capabilities](capabilities.md) and [70 work items](work-items.json) retain the
intended scope. Index registration, transactional capture, backfill/catch-up,
committed-change tickets, public search, production source permissions,
generation cutover and an installable release remain unimplemented.

The [core inventory](dependency-graph.json) records 440 selected packages and
license metadata. [Four profile resolutions](dependency-profiles.json) preserve
normal/private and direct/helper differences; the private Edge-probe libc edge
uses an already locked package. The independent lexical workspace leaves those
core manifests and lockfile unchanged. Package-declared license metadata is not
a completed redistribution review. Grouped dependency-bot configuration exists;
upstream compatibility, index migrations and rollback must still earn evidence.

## Measured process and PostgreSQL behavior

| Experiment | Observed behavior | Engineering consequence |
| --- | --- | --- |
| Caught diagnostic Rust panic | Request fails; owner survives | Does not prove reuse of arbitrary corrupted engine state |
| Direct-worker native abort, SIGKILL and kernel OOM | Companion SQL terminates; PostgreSQL recovers; committed marker survives | Engine ownership in a shared-memory PG worker is rejected as the production default |
| Native-helper abort, SIGKILL and kernel OOM | Supervisor and companion survive; helper is replaced; committed marker remains | Supports the packaged helper's smaller native-failure domain |
| Caller timeout/cancellation and queue pressure | Caller stops waiting; active owner remains; cancelled queued work does not execute; queue limit eight enforced | Caller cancellation is distinct from native Edge cancellation |
| Frozen helper, then cancelled caller | Exact helper stopped at the 125,000 ms process budget; supervisor and companion survive | Process-stop containment, not graceful durable shutdown |
| PostgreSQL supervisor SIGTERM | EOF stops helper; replacement acquires ownership | Separate from native helper failure |
| PostgreSQL supervisor SIGKILL | PG recovery terminates companion; old helper stops before replacement; same fence inode released/reacquired; starters converge | A helper cannot make its PG supervisor crash-proof |
| Repeated helper failure | Four starts, three automatic restarts; visible exhaustion/refusal | No unbounded restart loop; generation validation still needed |

The [vector-format probe](p0-input-formats.md) passes three groups in each SQL
profile: shape, bounds, owned serialization, finite float32 extremes, signed-zero
bit evidence, error recovery and restricted access. JSONB numeric normalization
is not claimed to retain negative-zero sign. Backend serialization is not a
frozen public model/vector contract or cross-process transport benchmark.

The [source-recheck probe](p0-source-recheck.md) passes all seven groups
in every CI8 profile, including active-statement/repeatable-read visibility,
strict primary-key/source validation, RLS refusal, quoted/bound input,
namespace/DDL guards and pre-SPI cancellation cleanup. Fixture revisions,
incarnations and fingerprints are compared correctly; production identities,
capture and SELECT/tenant authorization do not exist yet. The superuser-only
boundary, pre-body relation conversion and broad conditional namespace lock
are explicit prototype restrictions. The tested source contains the heap-only
restriction. The observed secondary-index wait inside SPI planning passes; after
cancellation, the namespace guard is released and the same backend performs a
matched recheck. No custom-AM adversarial runtime fixture is claimed. This does
not exercise every possible executor error.

## Disk and OOM interpretation

CI8's two full-text reopens use independent exact 402,653,184-byte mounts. Each
fixture rises from 34,963,456 allocated bytes before reopen to 220,508,160 after,
leaving 182,145,024 bytes. Both preserve all eight rows/four representations and
actual phrase/token-prefix/MaxSim behavior. The fault profile additionally
requires genuine errno 28, persisted configuration checks, filler removal,
retry and explicit flush. The clean control never creates filler. This is a
small fixed fixture, not a sizing rule or a loader fix.

The separate 128 MiB path refuses before fixture creation and preserves zero
allocation. CI5/CI6 used that capacity for actual reopen and failed with SIGBUS.
CI6's no-filler control also exhausted the mount; both runs had 99,254,272 bytes
free before reopen and none afterward. This supports an undersized fixture
explanation but does not identify an exact native instruction or prove
corruption from the configuration write. Logical file length, allocated bytes
and mapping RSS remain distinct. [Capacity contract](p0-full-text-capacity.md).

The [OOM evidence](p0-oom-experiment.md) has an exact mapped host PID and matching
kernel `oom_memcg` selector/kill record for each container. Direct/helper local
counter deltas are respectively `max=204/203`, `oom=1`, `oom_kill=1`, with no
group kill. Direct faulting and companion clients exit 2; helper faulting client
exits 1 and companion exits 0. No supervisor, client or external kill intervenes.
Both replacements pass 16 engine groups without a second OOM kill. The helper's
reported recovery interval includes waiting for the companion's sleep and is
not a restart-latency measurement. The controlled OOM preference does not prove
production memory isolation, unflushed index recovery or PG/Edge atomicity.

## Fixed-release integration constraints

The capability matrix, method inventory and [lexical ADR](adr/0002-lexical-gap-strategy.md)
retain source/API/compile/runtime boundaries. Among the implementation-relevant
findings:

- BM25 and payload text analysis have independent configuration and defaults.
  Chinese/English policy, field/array boundaries and Unicode positions need
  explicit common-policy validation. Missing phrase indexes cannot silently
  substitute raw substring semantics.
- Sparse finite/shape/unique-index validation, dense/token dimensions and model
  readiness belong in the project adapter before an engine call.
- Weighted RRF, DBSF, MMR and Formula have version-specific semantics; scores
  of different units cannot be added without a defined policy. Candidate-only
  MaxSim does not prove whole-corpus exact top-k.
- Root MMR can add vectors to results; the adapter must remove unrequested data.
  Prefix analysis can truncate long input; guards and explicit semantics are
  required. Binary quantization is distinct from native bit-vector input.
- Update-only Store, Delete and empty bootstrap trigger unimplemented upstream
  branches. Ordinary `EdgeShard` mutations are the selected route. Public
  snapshot manifest calls do not provide an archive producer or PG restore.
- [Durability review](adr/0003-edge-durability-and-recovery.md) finds no logical
  WAL replay in `load`, no public durable ACK cursor and no WAL reclamation API.
  Update return is not durable acknowledgment. The proposed source loop must
  serialize mutation/flush before exact-event PostgreSQL ACK and refuse unclean
  generations until a valid reconstruction succeeds. Pending events alone are
  not a complete source after earlier acknowledged partial state is lost.

## Phase exit assessment

The [composite source-bound decision](evidence/p0-feasibility.json) records the
following outcomes without changing the original acceptance rows:

| P0 exit | CI8 evidence and accepted decision | Work retained after feasibility |
| --- | --- | --- |
| P0-BUILD — passed | Locked core graph and four clean installed/runtime profiles; exact native packages, CPU admission and scoped object/ELF/license metadata | Complete distribution notices, supported packages, broader platform/CPU matrix and upgrade/rollback |
| P0-API — passed | Full public type/variant mapping; 44 method declarations, 43 concrete compiled calls and explicit exclusion | Runtime argument combinations, stable project adapters and release support remain separately scoped |
| P0-ENGINE — passed | 27 ordinary tests including required BM25/dense/sparse/MaxSim/prefetch/fusion/filter/group paths and negative-input characterization | Product validation, permissions, relevance/latency budgets and cross-capability integration |
| P0-LEXICAL — passed | Fixed Edge lexical primitives/gaps and five isolated Tantivy cases; accepted gap strategy and costed pre-Alpha adoption gate | Backend adoption/quality decision before Alpha; all F13–F20 delivery and any dual-engine lifecycle work |
| P0-PG — passed | Four SQL profiles, two-session ownership, cancellation/queues, formats and seven source groups including inside-SPI cleanup | Production authorization, persistent identities, capture/backfill, exact tickets and user-facing API |
| P0-FAULT — passed as characterization | Native/direct/helper faults, exact kernel OOM attribution, explicit-flush SIGKILL, scoped ENOSPC/corruption and dirty-refusal/source reconstruction | PG/Edge ACK crash cuts, power loss, live generation recovery, production memory/storage admission and rotation |
| P0-DECISION — passed | [ADR 0004](adr/0004-p0-go-no-go.md) accepts the packaged helper, conservative recovery fence and [costed module/critical path](p0-effort.md) | Reopen the decision when its measured limits or future correctness gates fail |

**P0 feasibility is complete for this tested revision and restricted baseline.**
No individual smoke or fault report alone grants phase exit; raw records that
retain `p0_exit_passed: false` keep that narrower meaning. Negative direct-worker
containment and malformed-input findings are accepted design constraints, not
successful production behavior. The scoped P0 integration deliverables I-01 and
I-03 can close; all 54 capability scopes and the remaining work in P1–P5 stay
open. The 70 work items and 45 acceptance rows are retained in full.

## Retained historical outcomes

| Evidence | Recorded outcome |
| --- | --- |
| [CI1](evidence/p0-postgresql-ci.json), `f5bda35` | Passed workflow; direct SQL 10/12; direct native faults caused companion termination |
| [CI2](evidence/p0-helper-ci.json), `a22d3c7` | SQL 10/12/10/15 and pipes 7/8 passed; full-fixture 32 MiB failed without retained native exit/signal; cause unknown |
| [CI3](evidence/p0-disk-ci.json), `cce2b41` | Genuine errno 28 hidden by temporary-file wrapping; recovery and SQL not reached |
| [CI4](evidence/p0-regression-ci.json), `1f9d8bb` | Narrow disk and SQL 10/12/10 passed; private process observer raised ESRCH; later private SQL not reached |
| [CI5](evidence/p0-full-text-disk-ci.json), `77f9ecc` | 15 engine/2 child/6 Python and narrow disk passed; full-text 128 MiB SIGBUS; SQL skipped |
| [CI6](evidence/p0-capacity-and-sql-ci.json), `d843706` | All SQL 13/15/13/19 and pipes passed; both full-text 128 MiB fault and clean controls failed |
| [CI7](evidence/p0-source-capacity-oom-ci.json), `88885db` | Full workflow passed with source diagnostics, corrected capacity and exact kernel OOM attribution; later changes excluded |

Later evidence cannot identify an older unrecorded signal or turn its failure
into a pass. Every record retains source identity, commands, outcomes and limits.

## Next implementation dependency

The next implementation stage, P1, must add stable identity/incarnation,
same-transaction persistent capture, consistent backfill/catch-up and idempotent
revision decisions,
explicit-flush ACK and fixed committed-event-set waits. P2 connects
text/balanced/precision, permissions and result contracts to that source loop.
Optional M0–M2 design notes remain proposals, not implemented milestones or a
replacement for the P1–P5 acceptance gates. All formal scope remains in the
work ledger; the passed investigation is not completion of the product.

# P0 feasibility checkpoint

Observed: **2026-10-08**. **P0 remains in progress**, with a conditional go for
Rust/pgrx and the same-package managed helper. The latest recorded complete CI
passed its diagnostic and fault assertions. Current-source/native/CPU regression
remains a distinct gate under the accepted conditional decision. No source-indexing product or
release is declared.

## Latest recorded execution

[CI7 evidence](evidence/p0-source-capacity-oom-ci.json) records run
[37736655218](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37736655218),
job `113177607334`, implementation head
`88885db08d17c074af097ddca14bc85d6978980c` and actual pull-request merge checkout
`18282abe660b4a3a59a8222f072c5b1cd069a2ae`. Both have tree
`7d02f181cc1c6ca8d14d5b82c03cc74d4a349265`. The workflow **passed**; no regression
step was skipped. The downloaded 90,589-byte artifact's SHA-256 and all 53
members were independently checked, including the inner OOM and SQL reports.

| Executed slice at that revision | Result | Evidence boundary |
| --- | --- | --- |
| Rust 1.96.0, Edge 0.8.0, pgrx/cargo-pgrx 0.19.3, PostgreSQL 17.11 | Four images built and installed | Diagnostic Linux x86_64 environment; not a release matrix |
| Ordinary engine suite | 23 passed, zero failed or ignored | Synthetic engine correctness, not relevance or performance |
| Child bookkeeping / Python diagnostics | 2 / 21 passed | Separate from SQL and positive native faults |
| Private OOM guard tests | Three distinct tests passed in both private builds | Guard tests alone cannot prove OOM |
| Direct-worker normal/private SQL | 19 / 21 passed | Expected collateral is part of the fault characterization |
| Managed-helper normal/private SQL | 19 / 25 passed | Includes original six source-identity/recheck groups in each profile |
| Helper normal/private pipe protocol | 7 / 8 passed | Exact owned process identities and lifecycle checks |
| Vector/keyword ENOSPC, 32 MiB | Passed | Eight rows/four representations; no mutable text indexes |
| Full-text ENOSPC and separate clean reopen, exactly 384 MiB | Both passed | Already-flushed fixture, full representation/phrase/prefix/MaxSim checks |
| Full-text refusal, exactly 128 MiB | Passed before writes | No Edge open, ENOSPC or recovery claim at 128 MiB |
| Kernel OOM, two fresh 768 MiB/no-swap containers | Both attribution/behavior characterizations passed | Direct-worker containment failed; helper containment passed for this experiment |

SQL counts include repeated common groups. The 16-group engine smoke nested in
SQL is one containing assertion group; counts cannot be added as completed
capabilities. The 128 MiB historical failures below remain failures.

## Later local source checkpoint

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
  The built-in heap-only source restriction also compiles. These changes are
  absent from CI7 and still require their own SQL execution.

An independent [Tantivy 0.26.2 workspace](../experiments/tantivy-probe/README.md)
passes five local primitive experiments with its own locked graph. Native fuzzy
queries lack the proposed term-expansion limit; a public dictionary bridge can
bound returned expansions but does not cap all FST work. Phrase slop can cross
repeated field values or admit reversal. Native fuzzy queries provide no snippet
terms, while repeated-value snippets lose value identity. These observations
preserve the product requirements and guide the lexical decision; they do not
adopt a second engine or complete F13–F20. Its added CI execution is pending.

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
package copyright hashes and explicit unparsed license gaps. These hooks and
the current source still need their identified clean CI run.

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

The [source-recheck probe](p0-source-recheck.md) passes its original six groups
in all CI7 profiles, including active-statement/repeatable-read visibility,
strict primary-key/source validation, RLS refusal, quoted/bound input,
namespace/DDL guards and pre-SPI cancellation cleanup. Fixture revisions,
incarnations and fingerprints are compared correctly; production identities,
capture and SELECT/tenant authorization do not exist yet. The superuser-only
boundary, pre-body relation conversion and broad conditional namespace lock
are explicit prototype restrictions. Later heap and inside-SPI checks remain
separately pending.

## Disk and OOM interpretation

CI7's two full-text reopens use independent exact 402,653,184-byte mounts. Each
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
counter deltas are respectively `max=219/224`, `oom=1`, `oom_kill=1`, with no
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

| P0 exit | Current evidence | Finite remaining P0 work |
| --- | --- | --- |
| P0-BUILD | Real graph, exact Rust/tools/PG; CI7 four-profile build/install/runtime | Verify frozen native package/CPU inputs and latest integrated source in clean CI; record license scope |
| P0-API | Full public type/variant mapping; 44 method declarations, 43 concrete compiled calls and explicit exclusion | Current-source CI and evidence reconciliation; runtime/release levels stay separately scoped |
| P0-ENGINE | Required BM25/dense/sparse/MaxSim/prefetch/fusion/filter/group paths; 25 later local tests and negative matrix | Run added tests in fixed clean environment; accept mandatory adapter validation policy |
| P0-LEXICAL | Fixed Edge semantics and standalone locked Tantivy primitive experiments expose concrete gaps | Accept necessary lexical path and costed follow-up; run separate experiment in clean CI |
| P0-PG | CI7 owner/IPC/concurrency/cancellation/formats and original six source-recheck groups | Execute later inside-SPI cleanup and heap restriction; no requirement to finish P1 here |
| P0-FAULT | Scoped native containment/OOM; flushed SIGKILL; ENOSPC and full-text clean controls; local dirty-refusal/full-source reconstruction | Verify reconstruction fixture in clean CI; accept fail-closed recovery and resource limitations |
| P0-DECISION | Managed helper supported; direct engine-in-worker default rejected | Review conditional go, retained risks and module/critical-path allocations |

**No complete P0 exit is declared at this checkpoint.** The
[conditional decision](adr/0004-p0-go-no-go.md) and [effort model](p0-effort.md)
separate feasibility blockers from production work. Transactional outbox/ACK
crash cuts, source authorization, production memory/WAL admission and rotation,
dual-engine consistency, held-out retrieval quality and platform/upgrade tests
remain required P1–P5 deliverables; CI7 cannot establish them.

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

The immediate P0 path is fixed native/CPU build input, the latest integrated CI,
and the costed architecture/lexical decision. Once those exit conditions pass,
P1 implements stable identity/incarnation, same-transaction persistent capture,
consistent backfill/catch-up, idempotent revision decisions, explicit-flush ACK
and fixed committed-event-set waits. P2 then connects text/balanced/precision,
permissions and result contracts to that source loop. All formal scope remains
in the work ledger; an investigation budget cannot substitute for acceptance.

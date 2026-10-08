# P0 feasibility checkpoint

Observed: **2026-10-08**. **P0 remains in progress.** The managed helper is the
preferred topology candidate. No production topology, complete P0 exit, source
indexing product or release is declared.

## Latest recorded execution

[CI6 evidence](evidence/p0-capacity-and-sql-ci.json) records run
[37732857051](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37732857051),
job `113165715578`, implementation head
`d84370682e9a2a7f0455c9c6a877ad2d09d45b57`, and actual pull-request merge checkout
`5009fbbf07630690a4909133566ac5ed748f6fc8`. Both commits have tree
`baba67b93ffceca35eaadfbbf524d4c04b8efc5a`. The workflow **failed** at two full-text
disk experiments; independent later SQL experiments still ran.

| Executed slice at that revision | Result | Limit |
| --- | --- | --- |
| Exact Rust 1.96.0, Edge 0.8.0, pgrx/cargo-pgrx 0.19.3, PostgreSQL 17.11 | All four container profiles built and installed | Diagnostic Linux x86_64 environment; not a release/platform matrix |
| Engine suite | 20 passed, zero failed or ignored | Synthetic correctness fixtures, not relevance or performance benchmarks |
| Helper child bookkeeping / Python runner tests | 2 / 9 passed | Separate from PostgreSQL and positive resource faults |
| Direct-worker normal/private SQL | 13 / 15 passed | Includes expected companion-session termination after direct native faults |
| Managed-helper normal/private SQL | 13 / 19 passed | Scoped native containment, lifecycle, format and supervisor-recovery assertions |
| Standalone helper normal/private protocol | 7 / 8 passed | Includes verified live controller/child identity before SIGKILL |
| Narrow 32 MiB vector/keyword ENOSPC | Passed | Eight rows/four representations, configuration failure/retry/reopen; no mutable text indexes |
| Full-text fault and no-filler clean control on separate 128 MiB mounts | Both failed with SIGBUS at `recovery_reopen` | Neither is a full-text recovery pass; observations below constrain the diagnosis |

New source changes do not inherit these results. The integrated identity/recheck
and private OOM additions have passed all four local PG17 compile profiles.
Their new SQL assertions and positive OOM observations remain unexecuted at
this checkpoint. The [isolated source-recheck record](evidence/p0-source-recheck-local.json)
separately records its earlier normal linked schema. The [integrated local record](evidence/p0-integrated-local.json) passes 23 normal
engine tests, three private native guard tests, 21 Python tests, four extension
compile profiles and both helper binary compile profiles. Full-text mount and
positive OOM changes still require their own recorded run.

## Delivered foundation and product boundary

The starting revision `f3b4cff09e3abc2aaaa3c647028aef4942d89636` contained design
documents. The project now has a real exact-version workspace, lockfile,
explicit build/features, public API sentinels, executable engine fixtures,
loadable diagnostic SQL and bounded owner/IPC/helper experiments.

[Eight user journeys](user-journeys.md), [45 acceptance rows](acceptance.md),
[54 capabilities](capabilities.md), [70 work items](work-items.json), the
[architecture ADR](adr/0001-embedded-engine-boundary.md),
[lexical strategy](adr/0002-lexical-gap-strategy.md) and
[durability boundary](adr/0003-edge-durability-and-recovery.md) retain the full
intended scope. Source-table index registration, transactional capture,
backfill/catch-up, committed-change tickets, public product search, production
permissions, generation cutover and an installable release remain unimplemented.

The [dependency inventory](dependency-graph.json) records 440 resolved packages
and license metadata. The [four-profile comparison](dependency-profiles.json)
records the normal/private and direct/helper feature differences. The optional
private Edge-probe `libc =0.2.189` edge uses an already locked version; no registry
package/version/checksum or inference dependency changes. Metadata completeness
is not a completed third-party distribution-license review. Grouped update-bot
configuration is present; automatic upgrading and index migration are not proven.

## Measured process and SQL behavior

| Experiment | Observed behavior in CI6 | Engineering consequence |
| --- | --- | --- |
| Caught Rust panic in the diagnostic call | Request fails; owner survives | Does not establish recovery of arbitrary damaged engine state |
| Direct-worker SIGKILL/native abort | Companion SQL session terminates; PostgreSQL recovers; committed marker survives; fresh owner starts | Direct engine ownership inside the shared-memory PG worker is not accepted as the production default |
| Helper SIGKILL/native abort | Companion SQL and the same supervisor survive; committed marker remains; distinct helper replaces the old owner | Supports the smaller native-failure domain for these faults |
| Caller timeout/cancellation and queue pressure | Caller stops waiting; active owner remains; cancelled queued request does not execute; queue limit eight is enforced | Caller cancellation is distinct from cancellation of a native Edge operation |
| Frozen helper, then cancelled caller | The 125,000 ms execution budget stops the exact helper with SIGKILL; observed remaining interval about 125.35 s; supervisor and companion query survive | Process-stop containment, not graceful durable shutdown |
| PostgreSQL supervisor SIGTERM | EOF stops the old helper and a replacement acquires ownership | Separate from native helper failure |
| PostgreSQL supervisor SIGKILL | PostgreSQL crash recovery terminates companion SQL; postmaster identity and committed marker remain; old helper stops before replacement; the same fence inode is released/reacquired; concurrent starters converge | A packaged helper does not make its PostgreSQL supervisor crash-proof |
| Repeated helper termination | Four starts, three automatic restarts, visible exhaustion and refusal of unavailable requests | No unbounded restart loop; production generation validation remains work |

The [candidate vector-format probe](p0-input-formats.md) passed all three SQL
assertion groups in every CI6 profile: dense/sparse/token shape and bound checks,
owned JSON round-trip, float32 extremes/signed-zero bit evidence, error recovery,
resource limits and runtime superuser checks even after ACL grants. PostgreSQL
JSONB numeric normalization is not claimed to preserve a negative-zero sign.
This backend-only diagnostic does not freeze the public model/vector API or
prove cross-process vector transport.

The separate [identity/source-recheck prototype](p0-source-recheck.md) uses tagged
bigint/uuid/text keys and fixture revision/incarnation/fingerprint comparison.
It binds values, validates a supported primary key, rejects RLS and unsupported
relations, and requests the active PostgreSQL snapshot explicitly. Six SQL
groups are authored but not yet executed. It remains superuser-only; it does
not allocate identities/incarnations, capture changes or prove production
SELECT/tenant permissions. Its pre-body relation conversion and broad
conditional namespace lock are explicit P0 limitations.

## Full-text capacity and disk evidence

The CI6 fault and clean controls use the same original eight-row full-text
fixture with four representations, phrase and token-prefix indexes. They differ
only in the intended fill/configuration-failure path.

| Observation | Full-text fault run | No-filler clean control |
| --- | --- | --- |
| Mount capacity | 134,217,728 bytes | 134,217,728 bytes |
| Initial fixture logical bytes | 289,807,352 | 289,807,352 |
| Allocated before reopen | 34,963,456 bytes | 34,963,456 bytes |
| Available before reopen | 99,254,272 bytes after filler removal | 99,254,272 bytes; no filler created |
| Native exit | Signal 7 / SIGBUS; no timeout | Signal 7 / SIGBUS; no timeout |
| Allocated after child exit | 134,217,728 bytes | 134,217,728 bytes |
| Available after child exit | 0 | 0 |

Reopen consumes the remaining capacity even without the preceding fill. This is
strong evidence that the original 128 MiB fixture profile is undersized for the
fixed writable loader; it is not proof of the exact native fault instruction or
of corruption caused by the earlier configuration write. Post-exit metadata
shows allocation across payload/vector backing files. The ordinary-filesystem
clean control previously passed with about 211 MiB of mapped fixture RSS;
logical bytes, allocated storage and mapping RSS remain different measurements.

The follow-up uses an explicit fixed full-text capacity and separately verifies
refusal of the unsupported 128 MiB profile before writes. Increasing a test
budget does not fix Edge's loader or establish a production memory/storage
admission policy. The original CI5/CI6 failures remain retained, and every
positive full-text recovery assertion still requires actual fill errno 28,
configuration observations, cleanup, retry, explicit flush, reopen and equal
source representations with phrase/prefix/MaxSim queries.

The successful narrow 32 MiB test deliberately uses cold payload/vector storage
and keyword indexes without mutable text indexes. The separate corruption and
explicit-flush/SIGKILL tests retain their full-text assertions. Malformed copied
JSON rejection is not general bit-rot detection or in-place repair. A missing
configuration, `not_run`, crash or timeout cannot pass a positive recovery gate.

The [OOM experiment](p0-oom-experiment.md) is independent: fresh disposable
containers, fixed 768 MiB/no-swap limits, owned one-shot markers, bounded native
allocation and outer watchdog, precise process/cgroup identity, and exact
kernel victim attribution. Safe negative/parser tests and compile evidence do
not establish positive OOM containment. Missing kernel evidence remains a
failed or inconclusive gate; no permission or isolation bypass is used.

## Retrieval and upstream contracts

The [engine inventory](../crates/edge-probe/README.md) and
[extended evidence](evidence/p0-engine-extended.json) cover BM25, dense and
supplied sparse, candidate-domain MaxSim, prefetch, weighted RRF/DBSF, filtering,
grouping, advanced scoring/reads, schema changes, mutations and explicit-flush
reopen. Scores and expected IDs are asserted against synthetic fixtures.

Material fixed-version findings remain part of the adapter contract:

- BM25 and payload text analysis are independent. English defaults must be
  handled explicitly for mixed-language configuration; complete analyzer parity
  and original-source Unicode offsets remain product work.
- Prefix query tokens beyond the configured maximum can be truncated. A required
  predicate must be rejected or use an explicitly allowed ready alternative
  that preserves the whole predicate; post-top-k filtering cannot repair it.
- Weighted RRF and DBSF have version-specific mathematics. Unnormalized
  BM25/cosine/MaxSim addition is not the proposed fusion contract.
- Grouping needs bounded provenance hydration. Root MMR can return a named
  vector despite `with_vector=false`; the adapter must enforce the projection.
- Public read-only loading/refresh works with a test-supplied manifest, but this
  does not prove automatic publication or archive restore. Update-only Store,
  Delete and empty bootstrap hit unimplemented upstream panics; its flush body
  is also unimplemented. Ordinary EdgeShard mutation is a separate tested path.
- Native bit input is distinct from binary quantization and is absent from the
  audited public input variants. Reduced-precision rescoring cannot restore an
  unstored lossless float32 representation.
- The fixed source [durability audit](evidence/p0-edge-durability-source.json)
  finds no logical WAL replay in Edge load and no public durable cursor or WAL
  reclamation API. Update return is not a durable ACK; load can repair and
  mutate storage. The proposed protocol requires serialized explicit flush
  before exact-event-set PostgreSQL ACK, preserved unclean artifacts, honest
  degraded state and a validated reconstruction path. Pending-event replay
  alone cannot be presumed to repair arbitrary partial state. WAL-growth and
  reclamation remain release gates.

These findings do not remove F13–F20, quality evaluation, source reliability,
authorization or operating requirements. CPU support also remains narrower
than a generic x86_64 claim: the selected upstream native SIMD build includes
Haswell-oriented flags. Broader CPU portability requires a tested build policy.

## Phase exit assessment

| P0 exit | Status | Passed portion | Work still required |
| --- | --- | --- | --- |
| P0-BUILD | Partial | Real lock/feature/license metadata, exact tools and all four diagnostic profiles built/installed/executed at CI6 | Regression for later source changes; full native-package/CPU/license acceptance |
| P0-API | Partial | Enumerated public-type sentinels, typed inputs and increasingly broad actual calls; explicit unavailable paths | Complete mapped operation/combination and negative-input acceptance |
| P0-ENGINE | Partial | Required initial BM25/dense/sparse/MaxSim/prefetch/fusion/filter/grouping paths plus analytical advanced/lifecycle fixtures | Remaining invalid combinations, parameters, precision and score/tie contracts |
| P0-LEXICAL | Partial | Independent defaults; Chinese, phrases, Boolean, field/array and Unicode/prefix boundaries; lexical-gap ADR | Complete parity/offset goldens and measured necessary lexical-library selection |
| P0-PG | Partial diagnostic runtime | Four CI6 profiles prove load, two sessions, unique owner, diagnostic ACL, arrays, deadlines/cancellation/backpressure and real Edge calls | New source-identity/visible-result SQL execution and complete source/driver/type contracts |
| P0-FAULT | Partial | Flushed SIGKILL/reopen; direct collateral; helper crash containment and 125-second stop; supervisor SIGTERM/SIGKILL, fence/restart behavior; narrow ENOSPC and malformed copied metadata | Positive constrained-memory/OOM attribution; full-text capacity/recovery; dirty-state and durable recovery cuts |
| P0-DECISION | Pending overall | Direct worker rejected as production default; managed helper preferred for further integration | Memory/storage, durability/reclamation and necessary lexical findings; accepted topology and costed overall decision |

**No complete P0 exit is declared.** P1–P5 remain pending. An exercised primitive
or private diagnostic is not a completed or release-supported product capability.

## Retained historical outcomes

| Evidence | Recorded outcome |
| --- | --- |
| [CI1](evidence/p0-postgresql-ci.json), `f5bda35` | Workflow passed; direct SQL 10/12; direct native faults demonstrated unacceptable companion-session termination |
| [CI2](evidence/p0-helper-ci.json), `a22d3c7` | SQL 10/12/10/15 and helper pipes 7/8 passed; full-fixture 32 MiB experiment failed without retained native exit/signal, so its exact cause remains unknown |
| [CI3](evidence/p0-disk-ci.json), `cce2b41` | Real errno 28 hidden by temporary-file error wrapping; recovery and all four SQL profiles not reached |
| [CI4](evidence/p0-regression-ci.json), `1f9d8bb` | Narrow disk and SQL 10/12/10 plus normal pipes passed; private observer raised ESRCH during cleanup observation; later private SQL not reached |
| [CI5](evidence/p0-full-text-disk-ci.json), `77f9ecc` | Normal image, 15 engine, 2 child, 6 Python and narrow disk passed; full-text 128 MiB SIGBUS; all four SQL profiles skipped |
| [CI6](evidence/p0-capacity-and-sql-ci.json), `d843706` | Four SQL profiles and corrected pipe observers passed; full-text 128 MiB fault and clean controls both failed as documented above |

Later evidence cannot identify an older unrecorded signal or retroactively turn
a failed workflow into a pass. Source/tree identities, commands, assertions and
artifact metadata remain in the individual records.

## Workload and critical path

The [work ledger](work-items.json) keeps every capability and cross-cutting
integration deliverable. Existing ranges remain planning allocations, not
elapsed-time measurements or freshly established remaining-work estimates:

| P0 stream | Accountable module | Retained engineering-day range | Remaining evidence |
| --- | --- | --- | --- |
| Clean installation and first SQL load | Build / SQL | 1–4 | Later-source regression and platform/license gates |
| Complete public-call/negative-input inventory | Engine / registry | 2–5 | Remaining API and composition contracts |
| SQL ownership, budgets and failure observations | Worker / SQL / recovery | 4–10 | OOM, full-text capacity, dirty recovery and new source rechecks |
| Analyzer parity and lexical selection | Lexical / quality | 4–9 | Measured mature-dependency/quality/lifecycle comparison |
| Managed-helper comparison | Worker / packaging | 4–10 | Production resource isolation, shutdown, ownership and packaging |

The ranges overlap shared work and must not be summed without module allocation.
The initial 5–10 day investigation budget is a stop-loss assumption, not an
acceptance criterion. CI6 reduces uncertainty about format conversion and
supervisor/fence recovery. The WAL audit adds explicit reconstruction and
reclamation work; it does not justify shortening that work.

The immediate critical path is the new source-recheck SQL suite, bounded OOM,
capacity-correct full-text recovery, and a defensible persistence/reconstruction
boundary, followed by the necessary topology/lexical decision. Transactional
capture, backfill, exact-set wait and product query integration depend on those
contracts. No unfinished formal requirement is removed to advance a phase.

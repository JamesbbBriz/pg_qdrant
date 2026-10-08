# ADR 0001: Embedded retrieval and the PostgreSQL boundary

Status: **accepted product boundaries; packaged helper and source-rebuild route
conditionally selected by [ADR 0004](0004-p0-go-no-go.md); overall P0 exit pending**.
CI7 passes all four diagnostic SQL profiles, six source-recheck groups per
profile, bounded storage cases and the victim-attributed OOM comparison. Later
method/input/rebuild additions have separate local evidence. The current
combined source/container run, new inside-SPI SQL assertion and revised
native/CPU baseline remain pending. Production ACK, generation, resource and
authorization policies are not implemented by this decision.

## Context and decision scope

Developers already maintain source records in PostgreSQL and want text, vector, hybrid, and multistage search through SQL. The product should own indexing, change capture, plan selection, reranking, status and recovery without requiring a second application write path or an independently operated Qdrant network service.

The repository's [capability contract](../capabilities.md), [upgrade policy](../dependencies.md), and [version baseline](../dependency-baseline.json) are the shared sources of truth. The initial candidates were Edge `0.8.0`, pgrx/cargo-pgrx `0.19.3`, Linux x86_64 and PostgreSQL 17. Those versions have a locked Rust 1.96.0 build and actual diagnostic PostgreSQL 17.11 execution in the [initial direct-worker CI](../evidence/p0-postgresql-ci.json) and [helper comparison CI](../evidence/p0-helper-ci.json). These records validate only their tested combinations and source trees; later changes, other platforms and product indexing require their own evidence.

## Accepted boundaries

1. **Direct embedded dependency.** Retrieval and local BM25 use the published `qdrant-edge` Rust crate through a project-owned engine adapter. The qdrant-client REST/gRPC SDK cannot stand in for that dependency. Qdrant Server documentation supplements fixed Edge source/API inspection; server feature availability does not establish Edge availability.
2. **One PostgreSQL business-data API.** Ordinary source-table writes remain authoritative. The extension captures changes transactionally and manages a rebuildable local projection. Applications do not maintain a Qdrant change-consumer pipeline.
3. **Asynchronous first consistency model.** Internal index maintenance still exists. Commit atomically persists the source mutation and its PostgreSQL outbox event, not a joint PostgreSQL/Edge transaction. A fixed post-commit ticket waits for the tested engine persistence condition and ACK. Same-transaction read-your-writes and arbitrary historical MVCC exact top-k are outside the initial claim.
4. **Project-owned SQL contract.** Stable request, configuration and result types do not expose upstream Rust/serde layouts. Model, analysis, permission, status and budget semantics belong to the extension. Unknown or incompatible options fail explicitly.
5. **One owner per generation.** No two processes open the same writable shard generation. Query lifetimes pin a generation; rebuilds catch up a new generation and switch before releasing the old one.
6. **Local core with BYOV.** Offline text search requires no model service, ONNX runtime or model download. Dense, learned sparse, token and visual outputs have declared application-supplied model contracts. Source-version validation is mandatory; numeric vectors cannot certify actual model provenance.
7. **Truthful capability states.** Upstream existence, adapter implementation, SQL verification, release support and index readiness are distinct. The planner and capability-discovery API consult the same versioned registry.

## Candidate implementation

Use Rust for both the pgrx boundary and engine integration. A separate Zig/Go layer requires demonstrated FFI/build/isolation benefit, a lifecycle design and measured maintenance costs; no such benefit is assumed.

The first implemented experiment is a PostgreSQL-managed owner worker per database with bounded IPC and a dedicated engine thread. Its measured native-failure domain is unacceptable for the production default described below. The intended index owner must still own a bounded number of generations and a bounded engine runtime. SQL backends submit owned requests over bounded IPC. Engine threads never access PostgreSQL SPI, Datum, pointers, memory contexts or thread-local backend state. PostgreSQL catalog/source work stays on the correct PostgreSQL process thread. A runtime is created in its owning child process, not inherited as active threads from postmaster initialization.

The owner count, shard count, runtime thread count, request queue and per-stage budgets must be explicit. Limits include request/response bytes, candidate count, HasId count, token matrices, matrix cells, wall time, engine memory/RSS/mmap and worker concurrency. PostgreSQL `work_mem` alone does not bound the embedded engine.

A PostgreSQL background worker is not an unconditional crash-isolation mechanism. The direct-worker experiments measured caught Rust panic, native abort and SIGKILL in a disposable cluster, including companion sessions and recovery. A Rust helper installed and managed with the extension package preserved the supervisor and companion SQL session in the recorded native-failure experiments, including CI7's controlled kernel OOM. CI7 also passes full-text configuration-save ENOSPC/retry/flush/reopen at the exact measured capacity. Those bounded results do not establish production memory isolation or arbitrary dirty-index recovery. The helper is an implementation boundary; applications still do not separately deploy a network Qdrant service.

IPC must carry request IDs, generation identity, caller-authorized retrieval domain, budgets, cancellation/deadline and owned source/model metadata. It cannot carry live PostgreSQL values or arbitrary engine DAGs. A cancellation must stop queued work or signal an active bounded operation; dropping only the SQL receiver is insufficient. A dead owner cannot leave its generation marked query-ready without startup validation.

## Measured process result and comparison decision

The [initial CI evidence](../evidence/p0-postgresql-ci.json) identifies head `f5bda3519421ef294bac82b17c17957f9727b648`, merge checkout `982adef4faaadd9831c2144b14bf155e89928ce0` and their identical source tree. The direct-worker normal/private-fault profiles passed 10/12 checks. Run [37720263848](../evidence/p0-helper-ci.json), at head `a22d3c73ad838ff606a080345b65d634f5144c39` and tree `9db0bd4a20780be31b07b018fdbc0d029983e5ab`, repeated those checks and passed 10/15 helper-profile SQL checks plus 7/8 standalone pipe checks. Its overall workflow failed at the subsequent provisioned disk experiment. A passing fault assertion means that the recorded behavior occurred; the direct-worker assertions include an unacceptable failure domain.

| Direct-worker experiment | Observed result | Decision consequence |
| --- | --- | --- |
| Rust panic caught around the diagnostic engine call | Request fails; worker survives | Retain the caught-panic boundary; do not infer recovery of arbitrary engine state |
| Owner worker SIGKILL | Companion SQL session terminates; PostgreSQL recovers; committed marker remains and a fresh owner starts | Do not accept this native-owner topology as the production default |
| Native abort inside the engine thread | Companion SQL session terminates with the same committed-marker recovery | A thread inside the PostgreSQL worker does not provide the required native-failure containment |
| Caller/statement cancellation and queue pressure | Caller stops waiting; active native ownership is retained; cancelled queued work is not executed; admission is bounded | Preserve these verified semantics in the helper comparison |

**Decision:** [ADR 0004](0004-p0-go-no-go.md) conditionally selects the managed helper for product implementation, based on its measured containment of the exercised native failures and the stated remaining P0 conditions. Retain the direct-worker implementation as the default P0 control profile; this documentation does not change that compiled default or certify production operation. The helper ships beside the PostgreSQL executable. The PostgreSQL worker remains responsible for SQL authorization, queueing and signals; the helper owns Edge after exec, exchanges bounded owned frames, and performs no PostgreSQL calls. This remains one installed product with one application source-write path.

The helper has explicit ready/build/process/request identities, a separate
process-lifetime engine-owner fence, EOF-driven shutdown, reap-before-replacement
behavior and three restart attempts after initial startup.

The latest [CI7 evidence](../evidence/p0-source-capacity-oom-ci.json) at `88885db`
passes SQL 19/21/19/25 and standalone helper pipes 7/8. It repeats native-helper
SIGKILL/abort containment and the actual 125-second execution-budget stop path.
It also measures forced PostgreSQL-supervisor SIGKILL: companion SQL terminates
under PostgreSQL recovery, while the postmaster/committed marker remain and a
replacement supervisor/helper acquires the same fence only after the old owner
stops. The supervisor is itself inside PostgreSQL's failure domain. No criterion
was weakened to make the direct/helper comparison pass.

The earlier [CI6](../evidence/p0-capacity-and-sql-ci.json) completed all four
image/SQL profiles despite two independent disk failures. Both full-text fault
and no-filler clean controls received SIGBUS while reopening
on 128 MiB tmpfs, consuming all remaining capacity. The preceding deliberate
fill is not necessary to reproduce this failure. This supports correcting the
fixed experiment capacity while retaining the original failed profile; it does
not identify the exact native fault instruction, repair the loader or prove
production dirty-index recovery. Earlier unrecorded native exits retain their
unknown causes. See the [P0 report](../p0-report.md) for all historical outcomes.

CI7 passes separate exact 384 MiB full-text configuration-save ENOSPC/recovery
and clean-reopen cases, plus a separate exact 128 MiB zero-write refusal. These
are measured fixture-capacity observations, not a loader fix or production
sizing rule. The earlier 128 MiB failures remain recorded as failures.

The private [OOM comparison](../p0-oom-experiment.md) ran in two fresh
768 MiB/no-swap containers. Kernel records and cgroup identity attribute the
actual victim: direct-worker death terminates companion SQL; helper death
preserves the supervisor and companion, and a replacement runs Edge. Both
retain the committed PostgreSQL marker. Deliberate victim-score selection and
a limit on the whole container do not prove a production helper memory quota
or arbitrary-pressure containment.

The [identity/source-recheck prototype](../p0-source-recheck.md) passes its
original six groups in all four CI7 SQL profiles, alongside the candidate-vector
format assertions. A later heap-only guard and seventh inside-SPI cancellation
and same-backend retry group compile but await SQL execution. These superuser-only
diagnostics do not freeze the product API or establish source SELECT/tenant/RLS
authorization for ordinary users.

Fixed Edge read-only/manual-manifest paths have narrow runtime evidence, while
update-only Store, Delete and empty bootstrap reach unimplemented panics; its
flush body is also unimplemented. Ordinary EdgeShard mutation is a separate
candidate. The [durability ADR](0003-edge-durability-and-recovery.md) records that
update return is not a durable ACK, Edge load does not logically replay WAL,
loading can mutate/repair state, and no public durable cursor or reclamation API
was found. A later [local dirty-generation experiment](../evidence/p0-dirty-rebuild-local.json)
refuses the killed generation without opening it, verifies unchanged old-file
hashes and reconstructs expected records/vectors from an independent complete
fixture into a new generation. This is a test-controller policy, not unflushed
WAL replay or production cutover. Exact-event flush/ACK, persistent dirty-state
tracking, source reconstruction under concurrent writes and bounded WAL growth
retain their P1/P3 implementation and crash-test gates.

The [conditional go/no-go decision](0004-p0-go-no-go.md) and
[costed phase allocation](../p0-effort.md) separate the finite remaining P0
conditions from subsequent product work. Memory/storage admission, persistent
generation ownership, production recovery, packaging and upgrades remain
required. Workload ranges are planning assumptions; these diagnostic results
do not complete P0 or justify removing formal scope.

## Module responsibilities

| Module | Owns | Does not own |
| --- | --- | --- |
| `sql` | SQL types/functions, error translation, driver/prepared-query contract | Raw upstream serialization as a public API |
| `catalog` | Source/index/owner/config/model/task/generation records and ACL metadata | Application business records |
| `source` | Transactional capture, backfill, committed tickets, identity/revision/fingerprint | An externally operated application sync consumer |
| `planner` | Capability validation, typed filters, presets, per-stage budgets and explicit fallback | Unbounded user-provided engine graphs |
| `worker` | Exclusive ownership, owned IPC, scheduling, cancellation, bounded runtime | Engine-thread access to PostgreSQL internals |
| `engine` | Calls to fixed published public APIs, stable type translation, persistence/result normalization | Reimplemented BM25, HNSW, tokenizers, sparse indexes or quantizers |
| `results` | Source/permission/version rechecks, bounded refill, grouping, real snippets and scoped statistics | A claim that postfiltering restores missing MVCC candidates |
| `lifecycle` | Build/rebuild/optimize/cutover/cleanup, recovery, migration and rollback | Untested transparent PITR/failover |

## Identity and transactional consequences

An indexed source row has a stable business key plus an extension-owned incarnation and revision. A key reused after deletion obtains a new incarnation. Internal point-ID mapping must be stable for that incarnation and cannot use `ctid`, `xmin`, or an internal storage address. Event application must compare current identity and revision before overwriting, deleting or reviving engine data.

Encoding fingerprints depend on each representation's declared source fields and preprocessing/model contract. A source revision may change without changing a particular representation's input fingerprint. Returned model output must match the current incarnation, fingerprint and model/vocabulary version. Each representation reports missing/stale/failed/ready rows independently.

Capture is installed before consistent backfill and chase of concurrent committed updates. A queue sequence ID is not commit order. ACK follows the tested engine durability operation; a crash between engine application and PostgreSQL ACK requires idempotent replay. An event may be safely acknowledged as an obsolete no-op only after its supersession by the authoritative current source incarnation/revision is proven.

The proposed ticket seals explicit event membership already produced in its creating transaction. Waiting before that transaction commits is rejected. Later writes do not enlarge the ticket. A successful wait means each sealed event satisfies the promised applied/durable condition, including justified supersession, not that all future index work is complete.

## Permissions and results

Source SELECT access and index permission are the first supported gate. Unsafe RLS modes are rejected; arbitrary RLS support is not inferred from final source checks. Supported tenant domains must be derived from trusted PostgreSQL identity. They constrain every recall/rerank/recommend/matrix/statistics/cache/explain path and are not replaceable by a user-supplied tenant filter.

The SQL backend rechecks each exposed source and its identity/version/permission using its documented PostgreSQL visibility rules. Bounded refill can improve fill rate after removing stale candidates but cannot establish arbitrary-snapshot exact top-k. Grouping budgets count distinct documents; statistics identify points versus documents and candidate versus proven full domains. Unauthorized snippets, example IDs, counts and diagnostic fields are also data leaks.

## Lexical scope and library decision

Local BM25 ranking and payload text/keyword predicates have independent upstream configurations. A field's common analysis policy must be compiled separately for BM25, text and keyword structures and its effective hash retained. Chinese/mixed-language settings must explicitly handle English stemming/stopword defaults; a multilingual tokenizer does not prove analyzer parity.

F13–F20 remain formal product work. P0 must distinguish verified native primitives from fuzzy/proximity/query-syntax/synonym/highlighting/suggestion/statistics/ranking gaps. The decision compares extension query/presentation policy, direct Tantivy, and reuse of pg_search. Tantivy `0.26.2` is the leading mature-library candidate after five isolated local semantic cases, not an adopted core dependency. The [lexical ADR](0002-lexical-gap-strategy.md) retains the quality, bounded-work, analyzer/offset and integration gates before core Alpha. pg_search is an independent integration/license option, not another name for linking Tantivy. Meilisearch is not the embedded baseline.

The comparison needs fixed-version quality results on Chinese/English text, identifiers and long documents, plus installation cost, memory/disk, synchronization, permissions, generations, recovery, outer fusion, upgrade and license obligations. Complex fuzzy/positional indexing is not assumed to be inexpensive custom extension code. Shared Charabia does not supply a complete Meilisearch search experience.

If a second engine is justified, a separate adoption ADR must specify stable shared source IDs, permission filters, idempotent dual writes, per-engine readiness and generation rules, backfill/catch-up, failure recovery, outer score/rank fusion, and coordinated migration/rollback. Tantivy-ranked results do not automatically become native Edge prefetch/Fusion inputs. Analysis and real Unicode source offsets need parity tests for both engines.

## Upgrade and compatibility boundary

Track these version domains separately:

| Domain | Required persistent identity | Typical consequence |
| --- | --- | --- |
| SQL/configuration | API and configuration schema versions | Query-only change or explicit SQL migration |
| Engine | Exact published release, lock graph, build/features and native/CPU requirements | Rebuild/reopen review and regression gates |
| Analysis | Effective policy, dictionaries, tokenization and normalization hashes | Reindex and possibly re-encoding |
| Model | Model/query-document encoder revisions, dimensions, vocabulary/IDF, normalization and source fingerprint | Explicit new outputs and representation migration |
| Index storage | Generation and tested disk-format range | Preserve/reopen compatible generation or build/catch-up/switch |

Each dependency upgrade reviews the released archive/public API/format/features/licenses; changes exact pins and lockfile together; compiles every mapped capability; runs engine and SQL combinations, lexical/quality, authorization/transaction/cancellation/concurrency/crash tests; validates old-index reopen or generation migration; runs actual rollback; and updates compatibility and release documentation. A dependency-update robot can open reviewable grouped changes after a real manifest exists, but detecting a version is not permission to release it automatically.

No fork, vendor copy or `internal` namespace import is accepted without a documented public-API gap, fixed version/source, license review, compatibility tests and ongoing upgrade owner. Upstream implementation code stays upstream-owned. Original project material follows the repository's AGPL-3.0-only license. That choice does not replace upstream licenses or waive dependency notices and integration review.

## Backup, restore and lifecycle boundary

The first recovery contract rebuilds from consistent PostgreSQL source data, retained configuration and available BYOV inputs. Configuration records are included in the appropriate PostgreSQL backup plan only once restore behavior is tested. A restored old shard cannot be trusted merely because it opens; its source timeline/identity/generation relationship must be validated before serving.

`pg_dump` metadata restoration, base backup, PITR, physical replication and failover are independently gated. Any mode without a passing end-to-end restore/timeline test is reported unsupported. Edge WAL and snapshots do not by themselves provide PostgreSQL-integrated PITR or replication. TRUNCATE, source deletion, type changes, model replacement, DROP and concurrent DDL require explicit source capture/lifecycle outcomes.

## Evidence and open decisions

| Question | Current position | Evidence needed to close |
| --- | --- | --- |
| Direct Edge crate and Rust adapter | Accepted direction; [44 public method declarations audited, 43 concrete call bodies compiled](../p0-edge-methods.md), with one documented construction exclusion | Runtime combinations and validated product adapter acceptance; compile calls are not SQL support |
| Exact Edge/pgrx/tool/compiler combination | Recorded Linux/PG17 diagnostic build and runtime pass | Regression for changed sources/features; complete native/CPU/license and release matrix |
| Owner background worker or packaged helper | Direct worker fails containment; helper conditionally selected after CI7 native/OOM/lifecycle evidence | Current source/native/CPU regression and remaining ADR 0004 conditions; product ownership/admission/lifecycle follows in P1/P3 |
| Array/JSON SQL signatures and ticket shape | Candidate-vector formats and six source-recheck groups pass in all four CI7 profiles; later heap/inside-SPI additions compile | New inside-SPI SQL run; complete product driver, transaction/savepoint, ticket and concurrency tests |
| Engine persistence condition for ACK | Explicit successful flush selected as the minimum fence to implement; local refusal/rebuild primitive passes; no assumed logical WAL replay | Exact-set PG ACK, persistent dirty/clean transitions, concurrent reconstruction and WAL admission/rotation under ADR 0003/0004 |
| Analysis parity and richer lexical dependency | Edge semantic probes and five isolated Tantivy cases have narrow evidence; no core adoption or full FTS claim | Frozen bilingual quality/budget/offset comparison and the pre-Alpha adoption gate in ADR 0002 |
| Source recheck and supported authorization | Diagnostic ACL/runtime-superuser checks and six fixture groups pass; product policy still proposed | Source SELECT/column/RLS and every-path leakage tests in the product authorization implementation |
| Upgrade/reopen/rollback compatibility | Unverified | Old/new generation fixtures and executed rollback procedure |
| Package and platform support | All four Linux x86_64 / PG17 diagnostic profiles built/installed/executed at CI7; later local checkpoint passes 25 engine tests and four PG compile profiles | Current clean full-profile/native/CPU regression; complete declared product journey, platform/license acceptance and release gates |

See [stage acceptance](../acceptance.md) for P0 exit conditions and [work items](../work-items.json) for remaining scope. Progress is recorded with reproducible evidence rather than by changing a candidate into an accepted claim in this ADR alone.

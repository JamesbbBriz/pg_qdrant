# ADR 0001: Embedded retrieval and the PostgreSQL boundary

Status: **accepted product boundaries; provisional implementation topology**. The background-worker/helper decision and exact build combination require P0 evidence. This ADR does not assert that an extension is installable or that a dependency graph compiles.

## Context and decision scope

Developers already maintain source records in PostgreSQL and want text, vector, hybrid, and multistage search through SQL. The product should own indexing, change capture, plan selection, reranking, status and recovery without requiring a second application write path or an independently operated Qdrant network service.

The repository's [capability contract](../capabilities.md), [upgrade policy](../dependencies.md), and [version baseline](../dependency-baseline.json) are the shared sources of truth. At this ADR's initial review, their source evidence selected candidate Edge `0.8.0`, pgrx/cargo-pgrx `0.19.3`, Linux x86_64 and PostgreSQL 17. Candidate version metadata is not a successful combined build. The exact compiler and all resolved/transitive features must follow actual P0 compilation evidence.

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

The first topology candidate is a PostgreSQL-managed owner worker per database, owning a bounded number of index generations and a bounded engine runtime. SQL backends submit owned requests over bounded IPC. Engine threads never access PostgreSQL SPI, Datum, pointers, memory contexts or thread-local backend state. PostgreSQL catalog/source work stays on the correct PostgreSQL process thread. A runtime is created in its owning child process, not inherited as active threads from postmaster initialization.

The owner count, shard count, runtime thread count, request queue and per-stage budgets must be explicit. Limits include request/response bytes, candidate count, HasId count, token matrices, matrix cells, wall time, engine memory/RSS/mmap and worker concurrency. PostgreSQL `work_mem` alone does not bound the embedded engine.

A PostgreSQL background worker is not an unconditional crash-isolation mechanism. P0 must measure Rust panic, native crash, SIGKILL, constrained-memory/OOM and disk-full behavior in a disposable cluster, observing other sessions and the whole instance. If its failure domain is unacceptable, the alternative is a Rust helper process installed and managed with the extension package. That helper is an implementation boundary, not a requirement that applications separately deploy a network Qdrant service.

IPC must carry request IDs, generation identity, caller-authorized retrieval domain, budgets, cancellation/deadline and owned source/model metadata. It cannot carry live PostgreSQL values or arbitrary engine DAGs. A cancellation must stop queued work or signal an active bounded operation; dropping only the SQL receiver is insufficient. A dead owner cannot leave its generation marked query-ready without startup validation.

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

F13–F20 remain formal product work. P0 must distinguish verified native primitives from fuzzy/proximity/query-syntax/synonym/highlighting/suggestion/statistics/ranking gaps. The decision compares extension query/presentation policy, direct Tantivy, and reuse of pg_search. Tantivy `0.26.2` is a named candidate, not an adopted dependency. pg_search is an independent integration/license option, not another name for linking Tantivy. Meilisearch is not the embedded baseline.

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

No fork, vendor copy or `internal` namespace import is accepted without a documented public-API gap, fixed version/source, license review, compatibility tests and ongoing upgrade owner. Upstream implementation code stays upstream-owned. Project Apache-2.0 licensing remains a separate decision; it cannot relicense copied AGPL implementation code or waive dependency notices.

## Backup, restore and lifecycle boundary

The first recovery contract rebuilds from consistent PostgreSQL source data, retained configuration and available BYOV inputs. Configuration records are included in the appropriate PostgreSQL backup plan only once restore behavior is tested. A restored old shard cannot be trusted merely because it opens; its source timeline/identity/generation relationship must be validated before serving.

`pg_dump` metadata restoration, base backup, PITR, physical replication and failover are independently gated. Any mode without a passing end-to-end restore/timeline test is reported unsupported. Edge WAL and snapshots do not by themselves provide PostgreSQL-integrated PITR or replication. TRUNCATE, source deletion, type changes, model replacement, DROP and concurrent DDL require explicit source capture/lifecycle outcomes.

## Evidence and open decisions

| Question | Current position | Evidence needed to close |
| --- | --- | --- |
| Direct Edge crate and Rust adapter | Accepted product direction | Exact published package/API, full locked build and callable probes |
| Exact Edge/pgrx/tool/compiler combination | Candidate | Complete Linux/PG build and native linking; actual feature/checksum inventory |
| Owner background worker or packaged helper | Candidate worker first | Two-session SQL/IPC/cancellation plus measured crash/OOM/disk failure domain |
| Array/JSON SQL signatures and ticket shape | Candidate API | pgrx dimension/bound conversion, driver, transaction/savepoint and concurrency tests |
| Engine persistence condition for ACK | Open, blocking P1 wait guarantee | Crash-before/after-flush and reopen/replay tests on selected Edge |
| Analysis parity and richer lexical dependency | Open, blocking full FTS claims | Golden analysis/phrase/prefix/offset tests and F13–F20 quality/cost ADR |
| Source recheck and supported authorization | Candidate strict initial policy | Multi-role/column/RLS rejection and every-path leakage tests |
| Upgrade/reopen/rollback compatibility | Unverified | Old/new generation fixtures and executed rollback procedure |
| Package and platform support | Candidate Linux x86_64 / PG17 | Clean installation, complete declared user journey and release gates |

See [stage acceptance](../acceptance.md) for P0 exit conditions and [work items](../work-items.json) for remaining scope. Progress is recorded with reproducible evidence rather than by changing a candidate into an accepted claim in this ADR alone.

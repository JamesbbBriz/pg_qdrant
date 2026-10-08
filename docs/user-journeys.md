# User journeys and proposed SQL contracts

Status: product and integration specification. **The SQL interfaces in this document are proposed, not an executable installation guide.** Each journey becomes executable only when its SQL integration tests exist and pass for the advertised build. Ordinary PostgreSQL table definitions below are real SQL; the `qdrant.*` functions and their configuration shapes must still pass prototype and compatibility review.

The maintained scope is the [54-capability contract](capabilities.md), [dependency policy](dependencies.md), and [dependency baseline](dependency-baseline.json). [Stage acceptance](acceptance.md) defines when implementation evidence is sufficient. The [architecture decision](adr/0001-embedded-engine-boundary.md) separates selected product boundaries from unverified process choices.

## The application contract

Applications keep ordinary PostgreSQL tables as the source of truth. They install one extension package, declare an index, and use their PostgreSQL driver to write and search. They do not operate a separate Qdrant service, send a second business-data write to Qdrant, or implement their own change-consumer pipeline.

The extension still maintains a derived index internally. The initial design is asynchronous: a committed source change becomes searchable after the extension applies and persists it. A committed-change ticket provides an explicit wait for a fixed set of changes. Embedding applications supply versioned model outputs through ordinary source-table updates; the extension owns validation and indexing of those outputs.

This contract does not promise same-transaction read-your-writes, exact top-k for arbitrary historical PostgreSQL snapshots, a native PostgreSQL Index AM, or transparent replication/PITR. Such support would require additional implementation and evidence.

## Shared example and input conventions

The first journey uses one row per chunk and one parent row per document. Search results can represent chunks or distinct documents. Source keys initially support a single `bigint`, `uuid`, or `text` primary-key column. `ctid` and `xmin` are not search identities.

```sql
CREATE TABLE documents (
    id          bigint PRIMARY KEY,
    title       text NOT NULL,
    category    text NOT NULL,
    status      text NOT NULL DEFAULT 'published'
);

CREATE TABLE document_chunks (
    id          bigint PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES documents(id),
    title       text NOT NULL,
    body        text NOT NULL,
    identifier  text,
    category    text NOT NULL,
    status      text NOT NULL DEFAULT 'published',
    dense       real[],
    dense_contract jsonb,
    sparse_indices bigint[],
    sparse_weights real[],
    sparse_contract jsonb,
    tokens      real[][],
    token_contract jsonb
);

INSERT INTO documents (id, title, category) VALUES
    (10, 'PostgreSQL transaction recovery', 'database'),
    (20, 'PostgreSQL 事务与恢复', 'database'),
    (30, 'HTTP API error reference', 'reference');

INSERT INTO document_chunks
    (id, document_id, title, body, identifier, category)
VALUES
    (101, 10, 'Transaction recovery',
     'A committed transaction survives recovery. A rolled back update is discarded.',
     'PG-RECOVERY-01', 'database'),
    (102, 10, 'Search freshness',
     'Search indexing follows committed changes. A change ticket can wait for indexing.',
     'PG-SEARCH-02', 'database'),
    (201, 20, '事务恢复',
     '已提交的事务在恢复后仍然有效。回滚的数据不应进入检索索引。',
     'PG-RECOVERY-CN', 'database'),
    (301, 30, 'Request failure',
     'ERR_CONNECTION_RESET indicates that a connection was reset.',
     'ERR_CONNECTION_RESET', 'reference');
```

The duplicated title/category/status fields in this example are explicitly source-table data. The initial row-source contract does not imply automatic indexing of arbitrary joins or propagation of parent-table changes. Applications can denormalize these fields in their existing PostgreSQL transactions or maintain chunk tables with their own normal database rules. A joined-source feature needs its own capture and permission contract.

`real[]`, sparse index/weight arrays, and rectangular `real[][]` are proposed BYOV inputs. PostgreSQL array dimensions and bounds must be validated; a Rust vector conversion must not silently flatten a token matrix. Reject non-finite values, mismatched lengths, duplicate sparse indices, out-of-range indices, incompatible model versions, and oversized inputs. Four-dimensional values used below are deterministic test fixtures, not production embeddings or evidence of semantic quality.

## 1. Install, register existing data, and search offline

The release package must document its PostgreSQL major, architecture, required libraries, CPU baseline, and any preload/restart requirement. No package command is advertised until a clean-install test establishes it. The target first environment is a candidate Linux x86_64 / PostgreSQL 17 build.

The following is the complete proposed `psql` flow after a compatible package is installed. The examples use the source/index owner role. Registration and its change-capture metadata commit before the owner begins the asynchronous build.

```sql
CREATE EXTENSION pg_qdrant;

SELECT qdrant.capabilities();

SELECT qdrant.create_index(
    index_name => 'knowledge',
    source     => 'document_chunks'::regclass,
    key_field  => 'id',
    settings   => '{
      "schema_version": 1,
      "text": {
        "fields": {
          "title": {"policy": "mixed_v1", "weight": 2},
          "body": {"policy": "mixed_v1", "weight": 1}
        },
        "policies": {
          "mixed_v1": {
            "tokenizer": "multilingual",
            "lowercase": true,
            "stopwords": "none",
            "stemming": "none",
            "phrase_matching": true
          }
        },
        "field_fusion": {"method": "rrf", "k": 60}
      },
      "keywords": {
        "identifier": {"case": "preserve", "prefix": true}
      },
      "payload_fields": ["document_id", "category", "status"],
      "group_field": "document_id"
    }'::jsonb
) AS build_task \gset

SELECT qdrant.task_status(:'build_task'::uuid);
SELECT qdrant.index_status('knowledge');
SELECT qdrant.await_task(:'build_task'::uuid, timeout_ms => 30000);

SELECT c.document_id, c.title, h.rank, h.score, h.snippets, h.match_info
FROM qdrant.search(
    index_name => 'knowledge',
    q          => 'transaction recovery',
    mode       => 'text',
    interaction => 'search',
    top_k      => 10,
    options    => '{"plan":"text","fallback":"error"}'::jsonb
) AS h
JOIN document_chunks AS c ON c.id = h.key::bigint
ORDER BY h.rank;
```

The analyzer policy is compiled separately into BM25, text-index, and keyword settings. The example explicitly disables English stemming and stopwords for mixed-language content. It must be rejected if the fixed engine cannot realize a declared policy consistently. `field_fusion` proposes separately named field representations and weighted rank fusion; it is not a BM25F claim. Its `k`, weights, tie behavior, and any score conventions must be tested against the selected Edge release before this configuration is accepted.

Success criteria: build progress is observable; all expected committed rows become searchable; a full offline installation can encode and query BM25 without inference/model downloads; no unsupported lexical feature is advertised as ready. A wait timeout returns incomplete status, not a successful build.

## 2. Add representations to the same indexed object

Representations declare their own model, source fields, dimensions, metric, vocabulary/IDF policy, normalization, storage, and output metadata columns. The following settings add three fixture contracts; adding them must not make an already working text plan unavailable.

```sql
SELECT qdrant.configure_index('knowledge', '{
  "schema_version": 1,
  "representations": {
    "dense_v1": {
      "kind":"dense", "field":"dense", "contract_field":"dense_contract",
      "model_id":"fixture-dense", "model_version":"1",
      "dimensions":4, "distance":"cosine", "normalization":"l2",
      "storage":"float32", "source_fields":["title","body"]
    },
    "learned_v1": {
      "kind":"sparse", "indices_field":"sparse_indices",
      "weights_field":"sparse_weights", "contract_field":"sparse_contract",
      "model_id":"fixture-sparse", "model_version":"1",
      "vocabulary_id":"fixture-vocab-1", "idf":"none",
      "source_fields":["title","body"]
    },
    "tokens_v1": {
      "kind":"multivector", "field":"tokens", "contract_field":"token_contract",
      "model_id":"fixture-token", "model_version":"1",
      "token_width":4, "max_tokens":128, "distance":"dot",
      "comparator":"maxsim", "normalization":"none", "storage":"float32",
      "source_fields":["title","body"]
    }
  }
}'::jsonb) AS representation_task \gset

SELECT qdrant.await_task(:'representation_task'::uuid, timeout_ms => 30000);
SELECT qdrant.capabilities('knowledge');
```

An additional proposed read-only API, `qdrant.encoding_inputs(index_name, representation, limit)`, supplies authorized source inputs with the authoritative incarnation, content fingerprint, and required model contract. It is an API design item under L06/L08, not an implemented interface. It is needed so external model jobs can return results without inventing a fingerprint or relying on an unstable internal row address.

```sql
-- Fetch a job input. Keep this returned identity with the model output.
SELECT key, incarnation, fingerprint, model_id, model_version, source
FROM qdrant.encoding_inputs('knowledge', 'dense_v1', 20);

-- Prepared application update. $2 is real[]; $3 is the returned model contract
-- including incarnation and fingerprint. No second business-data API is used.
PREPARE write_dense(bigint, real[], jsonb) AS
UPDATE document_chunks
SET dense = $2, dense_contract = $3
WHERE id = $1;

-- Equivalent updates set sparse_indices/sparse_weights/sparse_contract and
-- tokens/token_contract using outputs for their independently named contracts.
```

The final source trigger validates an output against the current authoritative incarnation, fingerprint, and representation contract in the write transaction. An old job cannot overwrite a new document or a new incarnation reusing the same key. A text edit can make an existing representation stale without making the source text edit fail; explicitly submitting an already stale model output is an error. Merely checking the numeric values does not prove that an application used its declared model.

Once the required representations are ready, prepared query inputs use project-owned JSON, translated inside the adapter:

```sql
PREPARE hybrid_query(text, jsonb) AS
SELECT * FROM qdrant.search(
    index_name    => 'knowledge',
    q             => $1,
    mode          => 'hybrid',
    interaction   => 'search',
    top_k         => 10,
    query_vectors => $2,
    options       => '{
      "plan":"balanced",
      "representations":["dense_v1","learned_v1"],
      "fusion":{"method":"rrf","k":60,"weights":[1,1,1]},
      "candidate_budget":{"per_branch":100,"total":300},
      "fallback":"error"
    }'::jsonb
);

-- A query-vector envelope for the fixture contracts; not semantic test data.
EXECUTE hybrid_query('transaction recovery', '{
  "dense_v1":{
    "model_id":"fixture-dense","model_version":"1",
    "values":[1.0,0.0,0.0,0.0]
  },
  "learned_v1":{
    "model_id":"fixture-sparse","model_version":"1",
    "vocabulary_id":"fixture-vocab-1","idf":"none",
    "indices":[3,17],"weights":[0.8,0.4]
  }
}'::jsonb);

PREPARE precision_query(text, jsonb) AS
SELECT qdrant.search_page(
    index_name    => 'knowledge',
    q             => $1,
    mode          => 'hybrid',
    interaction   => 'search',
    top_k         => 10,
    query_vectors => $2,
    options       => '{
      "plan":"precision", "rerank_representation":"tokens_v1",
      "group_by":"document_id", "group_size":2,
      "candidate_budget":{"per_branch":100,"total":300,"distinct_groups":50},
      "fallback":"error"
    }'::jsonb
);
```

The precision input adds `tokens_v1` with the same declared query model and a rectangular `values` matrix. MaxSim is exact within the selected candidate domain. The result must report that domain; it must not describe the result as a full-corpus exact top-k. Learned sparse and BM25 are independently named; a learned model that already supplies its intended weighting must not silently receive additional IDF multiplication.

Success criteria: text still works with all BYOV slots missing; balanced/precision reject absent required query vectors or unsuitable coverage; a permitted fallback records the requested and effective plans; late outputs and incorrect shapes are rejected. Coverage is per representation and authorized source domain, not a single misleading index-wide percentage.

## 3. Ordinary writes, rollback, and waiting after commit

Management tasks and source-change tickets identify different things. `track_changes` below is a proposed name for sealing a fixed set of events already captured in the current transaction. It must be called after the writes being tracked. Subsequent writes require another ticket; the first ticket never expands to future transactions or later writes.

```sql
BEGIN;
UPDATE document_chunks
SET body = 'Committed source changes are replayed safely after recovery.'
WHERE id = 101;
INSERT INTO document_chunks
    (id, document_id, title, body, identifier, category)
VALUES
    (103, 10, 'Durability', 'Wait for the committed change ticket.',
     'PG-WAIT-03', 'database');
DELETE FROM document_chunks WHERE id = 102;
SELECT qdrant.track_changes('knowledge') AS change_ticket \gset
COMMIT;

SELECT qdrant.await_changes(:'change_ticket'::uuid, timeout_ms => 30000);
SELECT qdrant.index_status('knowledge');
```

Proposed wait result fields are `ticket`, `committed`, `applied`, `durable`, `pending_events`, `timed_out`, and a safe failure reason. `applied=true` alone is insufficient for a successful durability wait. On timeout, the ticket remains usable and the response is explicitly incomplete. Index drop, a failed generation, cancelled work, an invalid ticket, or loss of the promised durability condition must not be reported as success.

```sql
-- Negative acceptance case: this must raise the uncommitted-ticket error.
BEGIN;
UPDATE document_chunks SET body = body || ' pending' WHERE id = 101;
SELECT qdrant.track_changes('knowledge') AS uncommitted_ticket \gset
SELECT qdrant.await_changes(:'uncommitted_ticket'::uuid, timeout_ms => 100);
ROLLBACK;

-- Savepoint rollback must remove its indexing events as well as its row change.
BEGIN;
SAVEPOINT edit;
UPDATE document_chunks SET body = 'This must never be indexed.' WHERE id = 101;
ROLLBACK TO SAVEPOINT edit;
COMMIT;
```

Tickets refer to explicit event membership, not `MAX(event_id)`. Two concurrent transactions may allocate event IDs and commit in a different order. The queue must process committed events safely without interpreting sequence allocation as commit order. Source mutation and outbox insertion must be one PostgreSQL transaction. Edge durability and PostgreSQL ACK are separate operations; a crash between them requires harmless replay.

Success criteria: rollback/savepoint rows never enter a searchable generation; reordered events cannot resurrect a delete; primary-key reuse creates a new incarnation; tickets wait only for their fixed committed set and the documented persistence condition; text changes mark affected vectors stale until matching outputs arrive.

## 4. Filter, group, inspect provenance, and join

Query filters are typed product inputs. They cannot contain arbitrary SQL or replace mandatory authorization predicates. Filterable payload fields must be explicitly registered; identity and authorization fields are protected by the extension.

```sql
SELECT qdrant.search_page(
    index_name => 'knowledge',
    q          => 'recovery',
    mode       => 'text',
    interaction => 'search',
    top_k      => 10,
    options    => '{
      "plan":"text", "group_by":"document_id", "group_size":2,
      "filter":{"all":[
        {"field":"status","eq":"published"},
        {"field":"category","eq":"database"}
      ]},
      "candidate_budget":{"total":200,"distinct_groups":40},
      "facets":[{"field":"category","scope":"candidates","unit":"document"}],
      "fallback":"error"
    }'::jsonb
);

SELECT d.id, d.title, c.id AS representative_chunk, h.rank, h.score, h.match_info
FROM qdrant.search(
    index_name => 'knowledge', q => 'recovery', mode => 'text', top_k => 10,
    options => '{"group_by":"document_id","fallback":"error"}'::jsonb
) AS h
JOIN document_chunks AS c ON c.id = h.key::bigint
JOIN documents AS d ON d.id = h.group_key::bigint
ORDER BY h.rank;
```

Grouped `key` identifies the representative source row; `group_key` identifies the document. Additional chunk sources must pass the same source-version and permission recheck as the representative row. A candidate budget of 20 chunks is not a budget of 20 documents. Refill is bounded, and underfilled results report why they remain underfilled.

The SQL backend rechecks source visibility, current incarnation/revision, and access for every exposed source, snippet, and statistic. It removes stale/deleted candidates. It cannot recover relevant rows that the asynchronous index never recalled, so the response must not claim snapshot-complete top-k.

The first permission gate requires index permission and source-table SELECT permission, including columns used for matching, returned snippets, filtering, and grouping. Initial safe operation can be restricted to the index owner; delegation syntax must be frozen and tested before shared access is advertised. RLS-enabled tables are explicitly rejected until a particular supported policy class is proven safe. A user-provided `tenant_id` is never trusted authorization. Future tenant domains must come from a trusted PostgreSQL identity mapping and constrain retrieval, reranking, recommendation, matrix, statistics, caching, and explain.

Success criteria: snippets use visible original source text, traceable offsets, and the correct generation/revision; document and point counts remain distinct; candidate statistics are labeled; a correct full-domain total is either proven or returned as `null`.

## 5. Ordinary search and input-time search

The proposed default/override contract is:

| Mode | Interaction | Default pipeline | Explicit plans |
| --- | --- | --- | --- |
| `text` | `search` | `text` | `text`; compatible lexical extensions when ready |
| `hybrid` | `search` | `balanced` | `balanced`, `precision`, `fast`; compatible visual/explore inputs |
| `semantic` | `search` | Dense nearest over an explicitly configured default representation | `precision`, `fast`, `visual`, `explore` when their required inputs exist |
| `text` | `instant` | `instant` | `instant` |
| `hybrid` / `semantic` | `instant` | No implicit mapping | Reject until an explicit measured plan is supported |

The semantic default needs a stable internal plan ID at SQL API freeze; `semantic_nearest` is the proposed ID. It does not silently reinterpret `balanced` as a single vector branch. An explicit plan must be compatible with the requested mode and interaction. Missing query vectors produce an error, rather than an implicit switch to text. The index configuration chooses default representations; ambiguity is an error. In all cases the metadata names the actual stages and representations.

```sql
-- AND matching, independently of relevance ranking.
SELECT * FROM qdrant.search('knowledge', 'transaction recovery', mode => 'text',
  options => '{"match":{"field":"body","all_terms":"transaction recovery"}}'::jsonb);

-- A contiguous phrase is applied to every recall branch before top-k.
SELECT * FROM qdrant.search('knowledge', 'committed transaction', mode => 'text',
  options => '{"match":{"field":"body","phrase":"committed transaction"}}'::jsonb);

-- Whole-value identifiers use a keyword predicate, not a token-prefix query.
SELECT * FROM qdrant.search('knowledge', 'ERR_CONNECTION_RESET', mode => 'text',
  options => '{"identifier":{"field":"identifier","exact":"ERR_CONNECTION_RESET"}}'::jsonb);

SELECT * FROM qdrant.search('knowledge', 'PG-', mode => 'text',
  options => '{"identifier":{"field":"identifier","prefix":"PG-"}}'::jsonb);

-- Requires an independently configured and ready token-prefix representation.
SELECT qdrant.search_page('knowledge', 'transac', mode => 'text', interaction => 'instant',
  options => '{"plan":"instant","token_prefix":{"field":"body"},
              "typos":{"max_edits":1,"max_expansions":32},
              "fallback":"error"}'::jsonb);

SELECT * FROM qdrant.search('knowledge', '事务 恢复', mode => 'text');
```

`instant` is a separately measured strategy with bounded prefix and typo expansion, not a label placed on any ordinary search. Token prefixes and whole-value prefixes use separate configuration and tests. If the lexical adapter has not implemented fuzzy/proximity/synonyms/suggestions, these requests return an explicit unsupported-capability error. Empty input and very short prefixes have defined rejection or suggestion policies, not an unbounded whole-corpus scan.

Plain-text mode treats punctuation predictably; advanced syntax is opt-in and covers field names, quoting, escapes, Boolean grouping, and boosts through a controlled parser. Exact identifier protection is also opt-in and evaluated separately from ordinary keyword relevance. Short queries, zero-result behavior, Chinese normalization, mixed-language names, misspellings, and phrase/field boundaries are fixtures in the held-out quality set.

## 6. Reconfigure models/analyzers and switch generations

```sql
SELECT qdrant.configure_index('knowledge', '{
  "schema_version":1,
  "query_defaults":{"candidate_budget":{"per_branch":150,"total":450}}
}'::jsonb) AS config_task \gset

SELECT qdrant.task_status(:'config_task'::uuid);

SELECT qdrant.rebuild_index('knowledge') AS rebuild_task \gset
SELECT qdrant.task_status(:'rebuild_task'::uuid);
SELECT qdrant.index_status('knowledge');
SELECT qdrant.await_task(:'rebuild_task'::uuid, timeout_ms => 30000);
```

Every configuration change is classified as `query-only`, `reindex-required`, or `reencode-required` before it takes effect. Query budget changes may be query-only. Tokenization, phrase positions, prefix indexes, and many storage changes need rebuilding. A different model, vocabulary, dimensions, or encoding source needs an explicit representation contract and matching new outputs; model replacement is not solved by reopening existing bytes.

Rebuild constructs a new generation with capture, backfill, and catch-up. The current generation continues serving pinned queries. Cutover occurs only after the new generation satisfies its required representations, persistence, and source-version checks. Old generations are removed after query references drain and rollback retention permits removal. The extension never deletes the only serving generation before building its replacement.

Each generation reports the SQL/config schema, engine build/features, analysis and dictionary hashes, model contracts, and disk-format compatibility. A failed or cancelled rebuild leaves the prior usable generation visible and names the new generation's failure. If no generation is safe, status and queries must reflect that fact.

## 7. Observe backlog, failure, restart, and recovery

```sql
SELECT qdrant.index_status('knowledge');
SELECT qdrant.task_status(:'rebuild_task'::uuid);
SELECT qdrant.cancel_task(:'rebuild_task'::uuid);
SELECT qdrant.capabilities('knowledge');

SELECT qdrant.rebuild_index('knowledge') AS recovery_task \gset
SELECT qdrant.await_task(:'recovery_task'::uuid, timeout_ms => 30000);
```

Index states are `registered`, `building`, `catching_up`, `ready`, `rebuilding`, `degraded`, `failed`, and `dropping`. Slot states are independently `ready`, `stale`, `missing`, and `failed`; mixed row states require counts as well as the summary. Status includes serving/building generations, last successful durable apply, pending/retrying events, safe failure details, per-slot coverage, maintenance activity, and query eligibility. A sequence maximum must not be presented as a complete commit watermark.

Worker restart must reacquire exclusive generation ownership and validate the catalog/index relationship before serving queries. An uncertain disk write, corrupt shard, or PostgreSQL timeline mismatch must not become `ready` through optimistic startup. Resource and disk faults can stop ingestion while a verified prior generation remains available, but this must be shown as degraded with freshness limits. Unavailable/corrupt representations cannot be used as silent fallbacks.

Recovery from source tables, retained configuration, and retained BYOV inputs must be executable in a packaged release. A reopened snapshot alone does not prove correspondence with the current PostgreSQL state. Metadata restore with `pg_dump`, base backup, PITR, physical replication, and failover have separate acceptance gates; each remains unsupported until its own test and recovery procedure pass. Outbox ACK and Edge WAL do not form one atomic PostgreSQL WAL record.

Success criteria: backlog and failed jobs are visible; retry and cancellation have defined boundaries; crash-before-ACK replay is idempotent; permanent failure does not loop invisibly; recovery cannot serve stale permissions or resurrect a deleted incarnation.

## 8. Controlled advanced retrieval

Advanced function names below are proposals to be frozen after compiled and SQL input validation. They share the index permission checks, trusted retrieval domain, source rechecks, generation pinning, result schema, and budgets of ordinary search. Example IDs are resolved only within the caller's authorized current sources.

```sql
SELECT qdrant.recommend(
  index_name => 'knowledge',
  positive_keys => '[101]'::jsonb,
  negative_keys => '[301]'::jsonb,
  top_k => 10,
  options => '{"representation":"dense_v1","strategy":"average_vector",
               "candidate_budget":{"total":100}}'::jsonb
);

SELECT qdrant.discover(
  index_name => 'knowledge',
  context => '[{"positive_key":101,"negative_key":301}]'::jsonb,
  top_k => 10,
  options => '{"representation":"dense_v1","candidate_budget":{"total":100}}'::jsonb
);

SELECT qdrant.search_matrix(
  index_name => 'knowledge',
  options => '{"representation":"dense_v1","sample_limit":32,
               "neighbors_per_sample":8,"max_cells":256}'::jsonb
);

-- $2 contains compatible query global/patch vectors for a declared visual model.
PREPARE visual_query(text, jsonb) AS
SELECT qdrant.search_page('knowledge', $1, mode => 'semantic', query_vectors => $2,
  options => '{"plan":"visual","group_by":"document_id",
               "candidate_budget":{"total":100},"fallback":"error"}'::jsonb);
```

An explicitly typed scoring expression can add a declared business/time/geographic signal through a supported Formula adapter. It cannot embed arbitrary SQL or arbitrary engine expressions. MMR reports its actual vector-based diversity/relevance domain; it does not claim to optimize arbitrary fused external scores. Feedback, deduplication, order-by, scroll, sample, retrieve, and facets need their own tested option schemas and bounds. No advanced endpoint bypasses the permissions or freshness contract.

Visual BYOV does not add PDF parsing, image preprocessing, GPU inference, or a model download to the core package. Global vectors and patch matrices require compatible document/query models and explicit dimensional/token limits. Native bit-vector input must be explicitly rejected if only binary quantization of a supported input vector exists.

## Common result and error contract

The proposed `qdrant.hit` composite has `key text`, `rank bigint`, `score double precision`, `group_key text`, `snippets jsonb`, and `match_info jsonb`. Rank is one-based. A score is meaningful only in the reported effective pipeline and is not a probability. Tie-breaking must be deterministic and versioned. Null group keys identify ungrouped results.

`match_info` includes source identity, incarnation/revision, generation, permitted matched fields, and actual matching stages. `snippets` uses offsets into the original returned source field, with a declared `unicode_scalar` unit and half-open `[start,end)` spans. Offset mappings survive normalization; converting them to JavaScript UTF-16 indices is a driver/UI responsibility documented with examples. Semantic-only hits have no invented lexical spans. A real excerpt may have an empty highlight list and an explicit `excerpt` reason. Internal sparse IDs are never presented as source words.

`search_page` returns `hits`, `facets`, `total`, and `meta`. Metadata includes requested/effective mode, interaction and plan, the pinned generation, model contracts, candidate limits and actual use, source freshness, authorized representation coverage, fallback reason, result underfill reason, and statistics scope. A total has an explicit unit (`point` or `document`), domain (`candidates` or a proven full match domain), and exact/estimated flag; otherwise it is `null`. Statistics and diagnostics cannot expose another authorization domain.

`explain_search` accepts the search inputs and reports the safe compiled stages, required/ready capabilities, declared filters, source recheck, budgets, and rejection reasons. It excludes full vectors, secrets, inaccessible source values, and internal filesystem paths. `capabilities()` and the planner use one versioned registry, with separate upstream, adapter, release-test, and per-index readiness states.

| Proposed SQLSTATE | Meaning | Required result/detail |
| --- | --- | --- |
| `22023` | Invalid input or unknown option | Field path, accepted shape, safe correction |
| `0A000` | Unsupported capability/combination or unsafe RLS mode | Capability ID and unsupported reason |
| `42501` | Missing index/source permission | Safe permission boundary; no forbidden source content |
| `54000` | Request exceeds a declared structural/resource bound | Bound name and permitted maximum |
| `57014` | PostgreSQL cancellation or statement timeout | Cancellation propagated to queued/running work |
| `PQ001` | Required representation unavailable | Required slot/model and authorized coverage reason |
| `PQ002` | Stale model output | Expected identity contract, without another row's content |
| `PQ003` | Waiting on an uncommitted ticket | Commit the captured transaction before waiting |
| `PQ004` | Engine/owner unavailable or queue capacity exhausted | Retryability and safe index status |
| `PQ005` | Corrupt or timeline-incompatible generation | Recovery/rebuild action and task linkage |

Custom codes are provisional and must be checked against the final SQL error catalog. A completed management function returns a task ID, not an assertion that asynchronous work already succeeded. Await-function time budgets may return explicit incomplete JSON; PostgreSQL statement cancellation still raises its normal error. All engine/status/task functions are initially conservatively `VOLATILE` and `PARALLEL UNSAFE`; actual signatures, security context, transaction restrictions, and declarations must be verified in SQL before freezing them.

## Changes relative to the original design baseline

This document turns the existing product scope into testable journeys without promoting any capability to implemented status. It adds explicit proposals for representation input identity, sealed post-commit ticket membership, a semantic-only default pipeline, an error catalog, field-level fusion semantics, Unicode result offsets, and the boundary around indexing joined parent data. These are candidate API decisions requiring P0/P1/P2 validation. They do not replace the capability IDs or dependency contracts.

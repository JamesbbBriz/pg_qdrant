# pg_qdrant

Development CI runs locally with `python scripts/local_ci.py` using act and
Docker Linux containers. See the [CI execution policy](docs/ci-temporary-hold.md)
for complete and scoped checks, input hashes, logs and evidence.

**Qdrant-powered search and ranking for PostgreSQL.**

Full-text, vector, and hybrid search over ordinary PostgreSQL tables, with optional local models and learning to rank.

> **Early development:** The development branch installs source capture, managed embedded Edge consumption, flush-before-ACK tickets, local BM25 and declared BYOV dense/sparse/token queries. Native hybrid fusion, bounded MaxSim, generation rebuilds and scoped recovery have executable regressions. The full capability, quality, resource, upgrade and packaging gates remain open; there is no product release. See the [installed scope](docs/p1-integration.md) and [acceptance contract](docs/acceptance.md).

## Why pg_qdrant?

Keep your data in PostgreSQL. Register a table, search through SQL, and let the extension manage indexing, updates, retrieval, fusion, and reranking.

- Keep using your PostgreSQL driver and ordinary `INSERT`, `UPDATE`, `DELETE`, and `COPY` statements.
- Run search locally with an embedded Qdrant engine, without operating a separate search service.
- Start with full-text search, then add your own vectors or compatible local models when needed.
- Use simple SQL or a familiar Qdrant-shaped JSON query interface, and inspect the strategy that ran.

PostgreSQL remains the source of truth. The search index is an asynchronously updated, rebuildable projection.

## Planned capabilities

- **Full-text search:** local BM25, text analysis, phrase and prefix matching.
- **Vector search:** dense, learned sparse, named vectors, and multivectors.
- **Hybrid retrieval:** prefetch, fusion, filtering, document grouping, and quantization.
- **Multi-stage ranking:** native MaxSim and formula scoring, with optional local model stages.
- **Local models:** planned FastEmbed and ONNX adapters for encoding and reranking, subject to validation; a later supervised LTR workflow.
- **Search exploration:** recommendation, diversity, facets, and interactive text-search strategies.

Models are optional. Ranking improvements follow a prepare, compare, activate, and rollback workflow. Detailed coverage and acceptance conditions are in [the capability contract](docs/capabilities.md) and [the design](docs/design.md).

## Development SQL experience

After installing the managed-helper development build, registration starts an
asynchronous build. This example uses an existing table with a bigint primary
key and one non-null text body; execute the search after status reports ready.
The registered owner must retain complete source SELECT, and RLS is rejected.

```sql
-- After the extension binaries are installed:
CREATE EXTENSION pg_qdrant;

-- Register an existing table.
SELECT qdrant.create_index(
    index_name => 'knowledge',
    source     => 'document_chunks'::regclass,
    key_field  => 'id',
    settings   => '{"text":{"fields":["body"]}}'::jsonb
);

-- After committing registration, check build progress.
SELECT qdrant.index_status('knowledge');

-- Once ready, join search results to your source data.
SELECT c.title, h.score
FROM qdrant.search(
    index_name => 'knowledge',
    q          => 'transaction recovery',
    mode       => 'text',
    top_k      => 10
) AS h
JOIN document_chunks AS c ON c.id = h.source_key::bigint
ORDER BY h.rank;
```

See [declared dense models](docs/dense-representations.md),
[native hybrid fusion](docs/hybrid-search.md) and
[candidate-stage lexical matching](docs/lexical-matching.md) for implemented
development contracts. Broader native JSON and model workflows remain in the
[proposed API](docs/design.md#proposed-sql-experience).
See [bounded search pages](docs/search-pages.md) for immutable candidate pages
with current source/version and permission rechecks.
See [capability discovery](docs/capability-discovery.md) for current index and
model-slot readiness, separate from adapter implementation and release support.

See the [P1 integration status](docs/p1-integration.md) and
[local act CI policy](docs/ci-temporary-hold.md). Full CI runs locally against
an immutable source snapshot; opening a PR alone does not establish a tested
installation.

## Development

The implementation uses **Rust + pgrx + Qdrant Edge**. Optional managed Rust model adapters remain a separate design track. The first target is self-hosted PostgreSQL 17 on Linux x86_64, subject to the explicit native-library and CPU baseline in the [build guide](docs/build.md).

The [verified P0 run](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37743504259) built and installed four diagnostic profiles and exercised SQL-to-engine queries, cancellation, process faults, and bounded recovery experiments. The selected implementation route uses a PostgreSQL supervisor and an Edge helper installed in the same package. These private diagnostic paths do not implement source-table indexing or the proposed product API; the [P0 report](docs/p0-report.md) identifies exact tested revisions and limits.

The immediate priority is a working community extension: reliable indexing and core search, followed by optional local ranking. Supported capabilities and deployment environments will be published as they are validated.

- [Product and implementation design](docs/design.md)
- [Capability coverage](docs/capabilities.md)
- [Dependencies and upgrade policy](docs/dependencies.md)
- [Build and diagnostic instructions](docs/build.md)
- [User journeys and proposed SQL contracts](docs/user-journeys.md)
- [P0 evidence and stage acceptance](docs/p0-report.md), [acceptance checklist](docs/acceptance.md)

Share use cases, deployment needs, or reproducible findings through [GitHub Issues](https://github.com/JamesbbBriz/pg_qdrant/issues).

## License

Original project material is licensed under **AGPL-3.0-only**. See [LICENSE](LICENSE). Third-party components and model artifacts retain their respective licenses.

pg_qdrant is an independent community project and is not an official Qdrant product.

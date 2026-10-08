# pg_qdrant

**Qdrant-powered search and ranking for PostgreSQL.**

Full-text, vector, and hybrid search over ordinary PostgreSQL tables, with optional local models and learning to rank.

> **Early development:** design and feasibility stage. There is no installable extension yet. The capabilities and SQL examples below describe the intended product.

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
- **Local models:** validated FastEmbed and ONNX adapters for encoding and reranking; a later supervised LTR workflow.
- **Search exploration:** recommendation, diversity, facets, and interactive text-search strategies.

Models are optional. Ranking improvements follow a prepare, compare, activate, and rollback workflow. Detailed coverage and acceptance conditions are in [the capability contract](docs/capabilities.md) and [the design](docs/design.md).

## Proposed SQL experience

API sketch; registration starts an asynchronous build, and search runs once the index is ready.

```sql
-- After the extension binaries are installed:
CREATE EXTENSION pg_qdrant;

-- Register an existing table.
SELECT qdrant.create_index(
    index_name => 'knowledge',
    source     => 'document_chunks'::regclass,
    key_field  => 'id',
    settings   => '{"text":{"fields":["title","body"]}}'::jsonb
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
JOIN document_chunks AS c ON c.id = h.key::bigint
ORDER BY h.rank;
```

Hybrid queries, native JSON requests, and optional model ranking use the same index and PostgreSQL permissions. See [the proposed API and architecture](docs/design.md#proposed-sql-experience).

## Development

The proposed stack is **Rust + pgrx + Qdrant Edge**, with an optional managed Rust model runtime. The first target is self-hosted PostgreSQL 17 on Linux x86_64.

The immediate priority is a working community extension: reliable indexing and core search, followed by optional local ranking. Supported capabilities and deployment environments will be published as they are validated.

- [Product and implementation design](docs/design.md)
- [Capability coverage](docs/capabilities.md)
- [Dependencies and upgrade policy](docs/dependencies.md)

Share use cases, deployment needs, or reproducible findings through [GitHub Issues](https://github.com/JamesbbBriz/pg_qdrant/issues).

## License

Original project material is licensed under **AGPL-3.0-only**. See [LICENSE](LICENSE). Third-party components and model artifacts retain their respective licenses.

pg_qdrant is an independent community project and is not an official Qdrant product.

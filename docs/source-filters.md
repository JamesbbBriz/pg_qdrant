# Declared scalar source filters

Source query contract 17 accepts an optional `filter` in SQL search options.
Expressions reference aliases declared in the index's `payload` configuration;
they cannot choose native paths, source columns or tenant identities.

```sql
SELECT * FROM qdrant.search(
  'articles', 'recovery', 'text', 10, '{}',
  '{"filter":{"all":[
    {"field":"category","eq":"Engineering"},
    {"field":"quantity","range":{"gte":1,"lt":20}},
    {"not":{"field":"archived","eq":true}}
  ]}}'
);
```

The declared aliases must use keyword, int64, finite float64 or Boolean scalar
columns. `eq` takes the declared type without implicit casts. Keyword `prefix`
matches the whole case-sensitive value, independently of token-prefix text
matching. `is_null:true` matches an explicit null scalar. Null equality is
rejected. Numeric `range` supports `gt`, `gte`, `lt` and `lte`, including combined
bounds. Fixed Edge 0.8.0 exposes float range endpoints: integer endpoints must
lie in the exactly representable inclusive interval [-2^53, 2^53]. Equality
retains the entire int64 domain; indexed integer row values remain int64.

`all`, `any` and `not` compose expressions. Boolean child arrays contain 1–16
expressions; the expression contains at most 32 nodes, depth four below the
root and 8 KiB of JSON. Shapes, types, unknown operators, arbitrary native paths
and undeclared aliases fail with SQLSTATE 22023. No caller-provided identity
creates a PostgreSQL permission domain.

The adapter compiles a checked expression against the owned generation's
immutable scalar contract. It enters each native recall/prefetch branch before
candidate truncation, including BM25, dense, learned sparse, RRF/DBSF and the
recall domain for MaxSim/precision. Source-example recommendation, discovery,
feedback and MMR receive the same filter. The SQL result join additionally
checks current source identity, revision, incarnation and both body and payload
fingerprints, then reevaluates the filter against the authorized source row.
Current payload changes therefore cannot pass using stale native attributes.

Permissions include index-owner domain, source SELECT and SELECT on every
declared scalar source field. Unsupported RLS is refused. The internal
admission/evaluation functions are private. Filtering cannot grant access or
replace the source security contract. Explain returns the checked expression
and its budget/scope. Paging binds the expression to the original request and
rechecks source payload identity on reuse.

Existing combination rules remain explicit: precision accepts one token slot,
one dense slot and one learned sparse slot; hybrid can fuse BM25 with both
model recall kinds. The same scalar filter enters all three prefetch branches.
Formula does not compose with explore strategies in this development implementation.
General nested objects, arrays, geo/datetime
filters, tenant policy integration, grouping, global facets/counts and full
P2-PERMISSION/P3-FILTER acceptance remain open. This is a development contract;
full release support and upgrade compatibility are not established.

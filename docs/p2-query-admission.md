# P2 query admission and fail-closed result contract — incomplete phase

The P2 draft adds a typed search-result shape and PostgreSQL permission/query
validation. It does not execute Edge retrieval and cannot satisfy a core Alpha.

## Implemented in this slice

- Exposes a stable candidate result type with source key, rank, score, document
  key, excerpt and provenance. These are type declarations, not fake hits.
- explain_search validates query text, exact query options, mode/plan, candidate
  limit 1..1000, top_k 1..100, timeout <=30 seconds and explicit fallback.
- Requires the calling login role to possess source SELECT, PK-column SELECT
  and configured text-column SELECT. RLS and changed source metadata fail closed.
- Detects missing/disabled source capture triggers. It refuses semantic/hybrid
  inputs that have no production model-vector adapter yet.
- Reports search_executable=false, edge_generation_ready=false and
  native_predicates_compiled=false. The typed search() function RAISES an
  unsupported-operation error rather than returning an empty successful set.
- SQL fixture: crates/pg_qdrant/tests/p2_admission.sql.

## Phase gates still missing

A real Edge source consumer, BM25 analyzer policy, compiled native predicates
on EVERY retrieval/prefetch branch, text/vector/fusion/MaxSim, post-search
source revision checks, RLS acceptance/rejection coverage under multiple login
roles, result hydration/highlights, stats and relevant-quality benchmarks.
There is no executable source-to-search journey.

A SECURITY DEFINER function uses session_user as its least-authority caller
identity in this restricted preview. Role switching and owner privileges must
be audited before using this as production authorization. PG row security is
always refused, never claimed supported. Unknown options fail explicitly.

The SQL fixture and new code have NOT been compiled or integration-tested in
the available environment. Keep this PR Draft, preserve P1/P0 dependencies,
and restore all tests before merge or any release claim.

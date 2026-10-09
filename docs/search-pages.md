# Bounded development search pages

`qdrant.search_page(index_name, q, mode, page_size, query_vectors, options,
page_cursor)` captures one authorized search result domain and returns stable
pages from it. It uses the existing text, dense, sparse, hybrid or MaxSim query
contract, including candidate-stage matching. It does not enumerate all native
matches or provide an exact global match count.

```sql
SELECT qdrant.search_page('knowledge', 'transaction recovery',
  page_size => 7);
-- Pass the returned next_cursor with identical query arguments:
SELECT qdrant.search_page('knowledge', 'transaction recovery',
  page_size => 7, page_cursor => '<returned next_cursor>'::jsonb);
```

The first call executes the native search, captures at most 100 source-rechecked
hits, and stores their ranks, scores, source excerpts and version provenance.
`candidate_limit` remains bounded at 1000; `captured_limit` is the smaller of 100
and that budget. These values describe different truncation stages. Each page
contains at most 100 hits; `page_size` must be 1..100. Repeating a cursor returns
the same page and never advances server state. Page size may change; ranking
arguments may not. The cursor
contains only a snapshot UUID and nonnegative integer offset.

Every call checks the effective PostgreSQL actor, registered owner access,
complete source/participating-column SELECT, RLS rejection, source binding,
index identity, generation, storage epoch and consumer. Every captured hit is
rechecked against its point ID, incarnation, revision, primary key and current
source-body SHA-256, including hits returned on earlier pages. Cached excerpts
are returned only after these checks. Source mutation, primary-key reuse,
same-name index replacement, generation change or owner replacement invalidates
the cursor instead of mixing different result versions. Other uncaptured rows
can change: the snapshot is a bounded historical candidate domain, not a
transactionally frozen global ranking.

Source-example recommendations also capture the resolved seed digest. Changing
only an excluded example invalidates the cursor with 55000, even when cached
hits are unchanged. See [source recommendations](source-recommendations.md).

Source binding and catalog locks span page admission and rechecking. They use
NOWAIT to avoid unbounded DDL/management waits. Initial snapshot quota admission
uses a transaction advisory lock with immediate refusal. Cancellation or an
error rolls back the entire new snapshot. Native operation ownership follows
the existing managed-helper cancellation contract.

The private per-database cache admits at most 128 live snapshots, each expiring
120 seconds after creation and containing at most 256 KiB of serialized JSON.
Expiry is checked on every cached read; expired rows are deleted during the next
successful first-page admission. This bounds live logical result data, not
PostgreSQL physical table bloat, RSS or the native engine's memory. First-page
requests contend on quota admission until their enclosing transaction ends;
commit promptly. Expiry and rollback release capacity. Upgrades and backup
restoration of development snapshots are not supported.
Dropping an index transactionally deletes its cached snapshots with its catalog;
rollback preserves them. Reusing an index name cannot reuse those cursors.

Responses include `hits`, `next_cursor`, `snapshot`, creation/expiry timestamps,
generation/epoch, `captured_count`, both limits, `statistics_scope`,
`source_rechecked` and `native_query_executed`. `global_match_count` is null and
`global_coverage_verified` is false. A null `next_cursor` means the captured
domain is exhausted. It does not prove global coverage. A zero-result captured
domain is reported as such. Results retain `release_supported=false`.

Error contracts: 22023 for invalid cursor/query arguments; 42501 for permissions
or a different actor; 55000 for stale/expired/absent snapshots, source drift or
unready generations; 55P03 for lock/admission contention; 54000 for live cache or
byte limits; 0A000 for unsupported RLS; 57014 for PostgreSQL cancellation. Restart with a null cursor after
freshness failure. `crates/pg_qdrant/tests/verify_paging.py` exercises installed
SQL, native search, negative permissions, resource admission and replay.

Complete P2-RESULT/P2-STATS, global counts/facets, deep pagination beyond the
captured domain and release support remain separate acceptance gates.

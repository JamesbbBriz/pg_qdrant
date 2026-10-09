# Development capability discovery

`qdrant.capabilities()` returns all 54 retained product requirements and the
installed adapter's bounded query modes. `qdrant.capabilities(index_name)` adds
the current source-generation and declared model-slot readiness. It performs
registered-owner and complete source/participating-column SELECT checks before
reading private index metadata. RLS sources are explicitly unsupported.

```sql
SELECT qdrant.capabilities();
SELECT qdrant.capabilities('knowledge');
SELECT value FROM jsonb_array_elements(
  qdrant.capabilities('knowledge')->'query_modes')
WHERE value->>'mode' = 'precision';
```

The response uses registry schema version 2. Existing requirement IDs, product
status, partial SQL interface flags and `release_supported=false` are retained.
The private mode registry drives both accepted search mode names and discovery;
callers cannot read or change it. It is distinct from arbitrary native request
validation, which does not promise execution of advanced capabilities.

| Mode | Required complete declared slot kind |
| --- | --- |
| text | None; offline native BM25 |
| semantic | Dense |
| sparse | Learned sparse |
| hybrid | At least one dense or learned sparse slot |
| maxsim | Token vectors |
| precision | Token vectors; dense/sparse recall remains optional |
| explore | Dense; query-specific current source examples required |

Each mode reports its capability IDs and bounded implementation scope,
`adapter_available`, `upstream_api_scope`, `release_validation_passed`,
`required_representation_kinds`, `required_slot_ready`, `index_ready` and
`admission_ready`. Global readiness values are null. No mode is marked release
validated or release supported. No native query is executed by discovery.

A slot is complete only when every current live source identity has a ready
representation for its current incarnation and source fingerprint. Summary row
counts alone cannot establish this. Body changes invalidate old vectors until
matching model outputs arrive through ordinary source-table DML. One complete
slot of a required kind is sufficient; query-specific named slots may still be
unavailable. Deleted identities are excluded from this readiness domain.

`admission_ready` combines current engine-index readiness with at least one
complete required slot kind. It does not validate a query vector, model revision,
vocabulary/IDF, candidate/token/work/byte budget or matching input. It does not
wait for durable updates. Use `explain_search` for actual query admission and
`await_changes` for a committed ticket's durability contract. An available mode
can still reject a particular request. Missing slot kinds never imply fallback.

`index_state` includes capture, backfill, pending events, generation/epoch and
representation summaries but omits private native-owner diagnostics. It uses
the same helper lifecycle as `index_status`; an index-specific call may start a
managed owner. Catalog/source locks use NOWAIT, so concurrent management or DDL
can return 55P03. Unknown indexes return 22023, unauthorized access returns
42501 and unsupported RLS/non-managed builds return 0A000.

The installed tests cover global/index discovery, private-table/function denial,
effective-role source privilege revocation, missing and stale model output,
identity/fingerprint mismatches, real same-contract rebuild and owner replay.
General configuration, interaction presets, full advanced capability planning,
quality and release evidence remain open P2-DISCOVERY work.

# P4 bounded advanced request shapes — incomplete product scope

This reviewable P4 slice adds a strict typed parser and a PostgreSQL validator.
It does not add recommendation, scoring, facet, matrix, visual or MMR execution
against Qdrant Edge or any other engine.

## Implemented in this slice

- A pure Rust serde validator requires a named request family, refuses unknown
  keys, rejects duplicate/overlapping example IDs and enforces small bounded
  candidate/limit ranges.
- Families admitted for syntax only: recommend, MMR, Formula (typed arithmetic
  tree with bounded depth/node count), facet, matrix and BYOV-visual shapes.
- Numeric limits, overflow detection, allowlisted formula fields and lexical
  identifier names are mandatory. The PostgreSQL function caps serialized
  validation requests at 16 KiB.
- qdrant.validate_advanced_request returns source_authorization_checked=false,
  search_executable=false and native_implementation_available=false.
- Rust unit tests and a disposable PostgreSQL SQL validation fixture exist;
  these new tests have NOT been run.

## Explicitly not satisfied

Q01–Q14 real server/Edge query semantics, document/chunk grouping, filtered
prefetch, authenticated example reads, generation pinning, scoring formulas,
facet counts, visual vector compatibility, MMR stability and authorized
ordering are not implemented.

F13–F20 rich lexical behavior also remains open: prefix typo correction,
slop/proximity, query grammar, synonyms, Unicode-offset highlights, instant
suggestions, accurate authorized lexical statistics and measured exact-match
ranking. The P0 locked Tantivy experiment is NOT a production second engine.
A future design must either demonstrate a real integrated engine with shared
source identities/authorization/recovery or explicitly reject unsupported
public subfeatures with their consequences. The existing 54 ID ledger must not
be marked release-supported merely because request syntax parses.

This is a draft contract, not a completed phase or release. Its tests need a
real PG17/pgrx build after the temporary CI hold is lifted.

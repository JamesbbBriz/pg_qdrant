# P4 bounded advanced request shapes — incomplete product scope

The original P4 slice adds a strict typed parser and a PostgreSQL validator.
That parser does not execute engine queries. Separate installed development
adapters now provide bounded [recommendations](source-recommendations.md),
[Discover/Context](source-context-discovery.md) and [feedback](source-feedback.md).
The [MMR adapter](source-mmr.md) and [score Formula](source-formula.md) have separate execution and verification scopes.
These partial adapters do not complete P4 acceptance.

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
- The disposable PostgreSQL SQL shape fixture runs in the installed source
  regression; it does not prove the corresponding engine capabilities.

## Explicitly not satisfied

The full Q01–Q14 contracts, document/chunk grouping, general filter/prefetch
combinations, general payload formula execution, facet counts, visual vector compatibility,
quality and complete authorized ordering remain open. Individual installed
adapter tests cover only the explicit scopes in their respective documents.

F13–F20 rich lexical behavior also remains open: prefix typo correction,
slop/proximity, query grammar, synonyms, Unicode-offset highlights, instant
suggestions, accurate authorized lexical statistics and measured exact-match
ranking. The P0 locked Tantivy experiment is NOT a production second engine.
A future design must either demonstrate a real integrated engine with shared
source identities/authorization/recovery or explicitly reject unsupported
public subfeatures with their consequences. The existing 54 ID ledger must not
be marked release-supported merely because request syntax parses.

This remains an incomplete phase and release contract. Automatic CI is
restored; failures or unavailable dependency downloads are not passing evidence.

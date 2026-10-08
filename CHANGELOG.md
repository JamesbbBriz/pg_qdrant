# Changelog

## Unreleased — P0 feasibility and draft P1–P5 foundations

No product extension release, Git tag or distribution artifact is published.

- P0: reproducible locked Rust/PG17 feasibility prototype and process/fault
  experiment with successful recorded CI on its exact prior revision.
- P1 Draft: transactional PostgreSQL source registration, revision identity
  and pending outbox; native durability consumer and wait are absent.
- P2 Draft: strict permission/query admission and typed result shape; search
  deliberately raises unsupported status until real Edge indexing exists.
- P3 Draft: checked request budgets and unbuilt generation reservations;
  no live generation switching, resource isolation or recovered engine state.
- P4 Draft: bounded advanced request parser and SQL admission; no authorized
  native execution for its proposed advanced/lexical families.
- P5 Draft: release readiness checker and test fixtures. A blocked release
  is the expected state until all source/CI/artifact gates pass.

The original project is AGPL-3.0-only. Upstream packages preserve their own
license requirements. Check NOTICE and the actual locked dependency inventory
before distributing any binaries.

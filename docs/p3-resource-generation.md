# P3 resource admission and reserved generations — incomplete phase

This draft implements verified-by-inspection arithmetic guards and a PostgreSQL
transactional reservation/cancel/status path. It deliberately does NOT create,
open, flush or switch any Qdrant Edge generation.

## Contents

- The pure Rust protocol budget rejects top_k, candidate, token, dimension,
  matrix multiplication overflow, oversized response and unbounded timeouts.
  Unit tests cover positive and adversarial/overflow cases; NOT yet run.
- A per-index reservation ledger allocates monotonically increasing generation
  numbers under an index row lock, including cancellation of still-unbuilt
  reservations. It leaves the source index and active generation untouched.
- Reserved states cannot pretend to be ready: only reserved_unbuilt,
  cancelled and failed states exist in the current table.
- SQL status and operation limits explicitly report that native RSS limits,
  native cancellation and generation switching are not implemented.
- A SQL fixture asserts reservation uniqueness, idempotent cancellation and
  no false search readiness.

## Hard blockers

The budget module is still a **pure protocol library**, not wired into every
native Edge call. P2 SQL preflight has narrower limits; final acceptance must
enforce the same budgets end-to-end with bounded queue, native helper memory,
thread pools, resource admission, backpressure, cancellation and actual
measurement. Native generation creation/catch-up, online atomic switch,
reference pins/drain, error/corruption recovery, source/BYOV rebuild, real
upgrade/rollback and supported platform matrix have not been implemented.

The P0 helper protocol remains diagnostic-only. These changes are not
compile-tested or PostgreSQL-tested and the heavy workflow is temporarily
manual-only. Do not merge or mark P3 complete without all its acceptance rows.

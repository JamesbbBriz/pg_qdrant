# Managed embedded engine helper

This executable owns embedded Edge for the installed development source-index
pipeline and also retains the P0 diagnostic profile. The
[feasibility decision](../../docs/evidence/p0-feasibility.json) selects this
same-package process topology. Source operations call the published Edge Rust
library directly; diagnostic operations use the sibling Edge adapter. It has no
PostgreSQL dependency or network-service API. The PostgreSQL supervisor selects
the installed binary and supplies its private engine-owner path.

The development owner applies committed source events, flushes before exact
receipt acknowledgement, and executes bounded native BM25, dense, sparse,
MaxSim, fusion, [source-example recommendations](../../docs/source-recommendations.md)
and [Discover/Context](../../docs/source-context-discovery.md).
These installed paths remain partial product integration; complete reliability,
resource, upgrade and release acceptance is still open.

It holds an OS ownership lock until process exit, uses bounded stdin/stdout
messages with process and request identities, and exits all native work when
the supervisor's stdin writer closes. Private panic/abort operations require
the explicit `p0-fault-injection` build feature. Parent-death cleanup, native
failure containment and production durability are distinct acceptance gates.

The [P0 CI record](../../docs/evidence/p0-current-ci.json) passes both
standalone protocol suites (seven normal and eight private assertion groups),
the two real-child bookkeeping tests, both helper SQL profiles and the separate
helper kernel-OOM experiment. Native-helper abort/SIGKILL/OOM preserve the
supervisor and companion in these fixtures; killing the PostgreSQL supervisor
still triggers PostgreSQL recovery. The selected-victim OOM experiment does not
prove general memory isolation or durable index recovery.

The standalone process test creates only owned temporary files and can run
without a PostgreSQL cluster:

```bash
cargo test --locked -p pg-qdrant-helper --test supervisor_child
cargo build --locked -p pg-qdrant-helper
python3 crates/pg_qdrant-helper/tests/probe.py \
  target/debug/pg_qdrant_p0_helper --out artifacts/helper-pipes.json

cargo build --locked -p pg-qdrant-helper --features p0-fault-injection
python3 crates/pg_qdrant-helper/tests/probe.py \
  target/debug/pg_qdrant_p0_helper --faults --out artifacts/helper-fault-pipes.json
```

The `supervisor_child` target includes the PostgreSQL worker's private
`helper_child.rs` module directly. Its two bounded real-child fixtures check
that an already-finished child is reaped without a cleanup signal and that an
explicit kill retains its actual result and first reason. This avoids a second
implementation of the bookkeeping or a PostgreSQL dependency in this test.
A successful kill call records a termination request, not proof that this
request caused the final exit; natural completion can race any check-and-signal
sequence. The SQL fault suite separately checks the actual supervisor and its
unchanged 125-second process-stop limit.

The standalone controller-SIGKILL check requires a visible, live helper identity
in `/proc`, including its parent, owner and start ticks, before sending the
signal. If that precondition fails in the executing environment, the cleanup
gate fails and the suite is not reported as passed. Missing process entries
alone cannot establish parent-death cleanup; direct stdin-EOF checks remain
separate evidence.

The shared PostgreSQL suite in `../pg_qdrant/tests` tests the actual supervised
profile after installing matching helper and extension binaries. Set
`PG_QDRANT_MANAGED_HELPER=1` so the suite verifies the selected build profile.
The default direct-worker experiment remains separately selectable.

# P0 managed engine helper

This executable is an optional process-isolation comparison for the PostgreSQL
P0 prototype. It calls the sibling Edge adapter directly and contains no
PostgreSQL dependency or network-service API. The PostgreSQL supervisor selects
the installed binary and supplies its private engine-owner path.

It holds an OS ownership lock until process exit, uses bounded stdin/stdout
messages with process and request identities, and exits all native work when
the supervisor's stdin writer closes. Private panic/abort operations require
the explicit `p0-fault-injection` build feature. Parent-death cleanup, native
failure containment and production durability are distinct acceptance gates.

The standalone process test creates only owned temporary files and can run
without a PostgreSQL cluster:

```bash
cargo build --locked -p pg-qdrant-helper
python3 crates/pg_qdrant-helper/tests/probe.py \
  target/debug/pg_qdrant_p0_helper --out artifacts/helper-pipes.json

cargo build --locked -p pg-qdrant-helper --features p0-fault-injection
python3 crates/pg_qdrant-helper/tests/probe.py \
  target/debug/pg_qdrant_p0_helper --faults --out artifacts/helper-fault-pipes.json
```

The shared PostgreSQL suite in `../pg_qdrant/tests` tests the actual supervised
profile after installing matching helper and extension binaries. Set
`PG_QDRANT_MANAGED_HELPER=1` so the suite verifies the selected build profile.
The default direct-worker experiment remains separately selectable.

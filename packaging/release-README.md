# Building an actual release candidate

This directory contains no binary release artifacts and does not publish any.

On a disposable, provisioned Linux/PG17 host:

- Restore full CI and run the locked integration / concurrency / faults.
- Build the pgrx cdylib and native helper with the exact pinned source.
- Collect extension control and versioned SQL install / upgrade scripts.
- Verify the real native dependency and CPU requirements (see docs/build.md).
- Exercise the full ordinary-SQL registration → write → postcommit wait →
  authorized Edge search journey, rebuild, restart, and rollback.
- Capture full revision-bound result evidence and exact artifact checksums.
- Produce a real signed release archive and inspect the third-party notices.

The example in packaging/release-attestation.example.json is deliberately
invalid. The release gate should return nonzero while any required stage is
unfinished. Do not bypass failed/missing checks for convenience.

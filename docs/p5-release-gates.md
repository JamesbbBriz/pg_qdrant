# P5 distribution and release gate — draft, no package published

Status: **release blocked**. Installed source-to-Edge-to-authorized-SQL slices
have regression evidence, but full P1–P5 acceptance, quality, upgrade and
distribution evidence remain incomplete. Historical CI results apply only to
their recorded revisions; a current complete local act run is still required.

## P5 artifact contract

A candidate deployment for Linux x86_64/PostgreSQL 17 must contain all of:

1. A pgrx-built `pg_qdrant.so`, matched `pg_qdrant.control` and
   `pg_qdrant--0.0.1.sql` for exactly the target PG major and build.
2. The `pg_qdrant_p0_helper` managed native engine executable (the P0 name
   will be revised before product release); it must match the source features
   and exact protocol/package version.
3. A locked Cargo build, native library/CPU floor, complete third-party
   licenses and notices, test output and per-file SHA-256 checksums.
4. A documented clean PostgreSQL install, smoke query against a real source,
   uninstall/drop/reinstall, real upgrade from an earlier indexed generation
   or rebuild migration, and tested rollback/downgrade alternative.

A manifest is not a package. No such product artifact, public version tag,
release binary or verified upgrade script has been published by this draft.

## Local act verification

See the [local CI policy](ci-temporary-hold.md). Hosted workflow execution is
disabled. Run `python scripts/local_ci.py` against the actual final source
commit; `ci/steps.json` retains the engine, installed PostgreSQL, transaction,
crash, disk, strict OOM and static gates. Full CI requires every registered
step to pass. Subset, missing, skipped, cancelled and failed results cannot
make a Draft ready or satisfy a release gate.
Mandatory P1–P5 acceptance spans:

- P1: source locks, concurrent commit inversion, rollback/COPY/savepoints,
  DDL, durable native flush/ACK, crash replay, fixed-member wait tickets
- P2: real BM25/dense/fusion/MaxSim, security and source recheck, honest
  snippets/stats, Chinese/English quality, executable clean-database journeys
- P3: actual resource/cancellation enforcement, generation switch/drain,
  disk/OOM and dirty-state recovery, upgrade and rollback
- P4: authorized real advanced/visual/lexical behavior or explicit reviewed
  exclusions, all 54 capability IDs with tested outcomes
- P5: native distro package, source notices, install/uninstall, CI/provenance,
  versioned docs, support/CPU/platform matrix and final artifact inspection

## Release candidate attestation (fail closed)

Run only on a **provisioned, disposable build/release runner** where all
required PG17, Rust 1.96, pgrx 0.19.3, Qdrant Edge 0.8.0 and native system
dependencies are available. Protect secrets; never put them in evidence files.

```bash
python3 -m unittest discover -s scripts/tests -p test_release_gate.py
python3 scripts/check_contracts.py
python3 scripts/check_release_gate.py \
  --attestation dist/release-attestation.json \
  --artifact-root dist/candidate
```

`check_release_gate.py` refuses a release when any of the following is
missing: the local act workflow and step registry; current Git SHA; full
source/capability ledger; baseline license; checksum-verified successful full
act receipts from a clean exact-commit snapshot; all 45 evidence-bound P0–P5
acceptance rows, discovered from the authoritative acceptance table; reviewed dispositions
for all 54 capability IDs; four exact candidate files with SHA-256.
Attested files cannot escape the repository by `..` or symlink paths.
The output is JSON with `ready=false` and specific blockers until complete.

Each required `gates` entry must be of the form:

```json
{
  "P1-DURABILITY": {
    "status": "passed",
    "checkout_sha": "<actual 40-character commit SHA>",
    "evidence": [
      {"path": "artifacts/a-real-existing-result.json",
       "sha256": "<actual lowercase SHA-256>"}
    ]
  }
}
```

The `ci` key contains `checkout_sha`, `runner: act-local`,
`conclusion: success`, and `run_report`/`verify_report` objects with existing
repository-relative `path` and lowercase `sha256` fields. The checker verifies
matching run/snapshot identities, clean source and file hashes, every full
pipeline step and its log checksum. See the deliberately non-passing
[attestation example](../packaging/release-attestation.example.json).
Each `capabilities.F01` etc. disposition
must be `supported` or `explicitly_excluded` plus a nonempty
rationale; exclusions still need an honest public scope and documented
consequences. The `artifacts` key holds exact SHA256 strings for each
four required package filenames under `dist/candidate`.

This tool checks **presence, structure, source identity and local hashes**;
it cannot prove that a claimed result really ran. Reviewers must inspect full
act logs, source snapshot, test code, native results and archive hashes. Signed
artifacts, SBOM and reproducible binary comparison remain release tasks.

## Stacked PR handling

`main` remains untouched. The original draft chain was:

`main ← P0 #1 ← P1 #2 ← P2 #3 ← P3 #4 ← P4 #5 ← P5 #6`.

The canonical P1 integration consolidates the overlapping #2/#7 contracts
under [ADR 0005](adr/0005-p1-ledger-integration.md). Subsequent integration PRs
are stacked separately; inspect each current base/head and full dependency
diff rather than treating the original draft chain as current runtime evidence.

After each prerequisite merges, rebase/retarget the next PR to the
corresponding tested branch (and review the final diff and merge tree). Do not
merge a successor into an unreviewed/preliminary dependency, and do not tag
a release until the candidate artifact and all declared P5 gates pass.

**This document does not change the status of the 54-capability contract.**

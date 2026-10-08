# P5 distribution and release gate — draft, no package published

Status: **release blocked**. The upstream P1–P4 drafts do not yet supply
a completed source-to-Edge-to-authorized-SQL journey. P0 is the only phase
with a recorded successful integrated CI revision, and that revision is not
the source of the P1–P5 changes.

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
release binary or upgrade script has been built by this draft.

## Reversible CI hold

See [temporary CI hold](ci-temporary-hold.md). The P0 workflow is stored
in the repo, with its test steps intact, and is manually dispatchable.
Restore automatic `push` and `pull_request` triggers and add phase
CI before merging. Re-run against the **actual final merge commit**.

Do not report GitHub missing/skipped checks as passing or mark a Draft ready.
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
missing: automatic CI restoration; current Git SHA; full source/capability
ledger; baseline license; a current successful CI run attestation; 44
specific evidence-bound P0–P5 acceptance rows; reviewed dispositions
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

The `ci` key contains `checkout_sha`, `conclusion: success` and the
real GitHub Actions run URL. Each `capabilities.F01` etc. disposition
must be `supported` or `explicitly_excluded` plus a nonempty
rationale; exclusions still need an honest public scope and documented
consequences. The `artifacts` key holds exact SHA256 strings for each
four required package filenames under `dist/candidate`.

This tool checks **presence, structure, source identity and local hashes**;
it cannot prove that a claimed result really ran or that the real upstream
GitHub CI says success. Reviewers must inspect full logs, merge commit,
test code, archive hashes and first-party CI responses separately. Signed
artifacts, SBOM and reproducible binary comparison remain release tasks.

## Stacked PR handling

`main` remains untouched. PRs are stacked:

`main ← P0 #1 ← P1 #2 ← P2 #3 ← P3 #4 ← P4 #5 ← P5 #6`.

After each prerequisite merges, rebase/retarget the next PR to the
corresponding tested branch (and review the final diff and merge tree). Do not
merge a successor into an unreviewed/preliminary dependency, and do not tag
a release until the candidate artifact and all declared P5 gates pass.

**This document does not change the status of the 54-capability contract.**

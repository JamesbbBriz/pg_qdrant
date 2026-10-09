# Local PG17 installation preview

Status: local act run `preview-a044eef4fe310bef` passed the actual archive and
package-only clean-install path for source `acfc88539bb852ea2620a72ce551cd3b17ff86b2`.
The [scoped receipt](evidence/local-installation-preview.json) records archive,
installed-object, SQL-result and log hashes. Twelve archive regressions and four
SQL groups passed, including two completed physical drops after restart and
reinstallation. This is a local development preview, with no release, tag,
published binary or completed P5 acceptance.

The preview builds an archive from an existing normal managed-helper Linux
x86_64/PGDG PostgreSQL 17 image. It requires that image's clean exact-commit
source manifest, verifies every source file, and checks the installed extension
and helper against their native build report. Fault images and mismatched or
dirty source manifests are refused. No dependency pin or product requirement
changes for packaging.

The archive contains four matched install objects, the original source snapshot
including Cargo.lock and license declarations, and native/CPU build evidence.
Upstream license texts remain unchanged. Complete transitive/native notices and
distribution review remain open; this archive is for local installation tests.
It is not a release attestation or a complete distribution license inventory.

```sh
# Compile this clean checkout, then build and exercise its package:
python3 scripts/local_preview.py --build

# Or reuse a matching previously compiled normal image:
python3 scripts/local_preview.py \
  --image pg-qdrant-p0-helper-normal-<full-run-id> \
  --source-snapshot artifacts/local-ci/<full-run-id>/snapshot/ci-input.json
```

The command uses local act only. It freezes packaging inputs and resolves
existing image IDs before execution. It builds the archive twice from the same
installed binaries, requiring identical bytes. Archive metadata is normalized;
this checks archive reproducibility, not independent binary-build reproducibility.
Source, compiler, native ABI and CPU support remain subject to the
[build contract](build.md).

The installation test starts from the matching PG17 build-tools environment
with no extension objects or original source tree. It copies only checksum
verified install objects into that environment, creates a fresh durable
PostgreSQL cluster, and exercises ordinary source registration/DML, durable
tickets, actual BM25 with a source JOIN, writer permission refusal, generation
rebuild, PostgreSQL restart/replay, public drop, uninstall and reinstall.
The test environment retains the build-tools system dependencies; it does not
establish a minimal runtime dependency closure or support for other distributions.

Each product test container is limited to two CPUs, 2 GiB with no extra swap,
128 PIDs and no network. Failed containers and disposable clusters are retained
for inspection. Evidence includes the archive, manifest, installed hashes,
actual SQL outcomes, source and image identities, frozen packaging inputs and
complete act log. Successful local installation does not satisfy the entire
[P1-P5 acceptance contract](acceptance.md).

## Inspect and stage a generated archive

Use the frozen packaging script from the run whose receipt you are inspecting.
Verification checks exact member membership, sizes, modes and SHA-256 values;
links, special files, duplicate names, traversal and budget overflow are refused.
Staging writes only the four fixed PG17 install paths below a newly created
directory and never overwrites an existing staging tree.

```sh
python3 scripts/preview_package.py verify --archive preview.tar.gz
python3 scripts/preview_package.py stage --archive preview.tar.gz --destination preview-stage
```

The stage command does not modify PostgreSQL system directories or a running
database. The archive retains the current helper filename
`pg_qdrant_p0_helper`; SQL and helper protocol versions must remain matched.
Real upgrades, downgrade/rollback, complete notices, minimal deployment images,
signatures and release provenance remain required by
[the release gate](p5-release-gates.md).

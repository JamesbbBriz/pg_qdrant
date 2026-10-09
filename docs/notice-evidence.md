# Unchanged notice evidence

The local installation preview exports a `notices/` directory beside its
archive. It contains the project's original `LICENSE` and `NOTICE`, discovered
Rust package notices and installed native package copyright files. Every copied
file retains its original bytes and has a SHA-256 digest in `manifest.json`.
These materials do not change the project's AGPL-3.0-only declaration or any
upstream license.

`scripts/collect_notices.py` runs in the compiled product's Linux build image:

```sh
python3 scripts/collect_notices.py --root /src --output /tmp/notice-evidence
```

The output must be new and its parent must already exist. Collection may require
the build user that owns cached dependency materials; it does not change their
permissions. The preview build performs collection before switching to its
unprivileged package builder. The installed PostgreSQL tests run as UID 10001.

The collector uses frozen Cargo metadata for the Linux x86_64 normal managed
helper profile. Package identity includes name, version and source. Cached crate
archive hashes must match `Cargo.lock`; discovered notice bytes are compared
with the exact original archive member. Optional extracted-file checksums are
checked when present. An absent original archive remains explicitly unverified;
if neither archive nor extracted-file checksum evidence is available, collection
fails. Workspace notices remain separate from upstream notices.

Discovery covers top-level conventional `LICENSE`, `LICENCE`, `COPYING`,
`COPYRIGHT`, `NOTICE` and `AUTHORS` names and explicit relative `license_file`
paths. Cargo's [license metadata contract](https://doc.rust-lang.org/cargo/reference/manifest.html#the-license-and-license-file-fields)
distinguishes a license expression from a file containing the license text.
An expression is preserved as metadata and never substituted for missing text.
Unreviewed path dependencies, ambiguous paths, source symlinks, changed bytes,
duplicate outputs and exceeded byte budgets are refused.

Native copyright files are taken from the actual installed documentation tree.
Shared documentation symlinks are accepted only when their resolved target stays
inside that tree. Installed package names and versions must match the native
build inventory before and after collection. Each notice is limited to 4 MiB,
and copied material is limited to 128 MiB in total.

Local act diagnostic `notices-093eca72dd8ce726` collected 1,014 files containing
8,003,072 bytes from the normal product image compiled at
`acfc88539bb852ea2620a72ce551cd3b17ff86b2`. All exported file hashes were checked.
The resolved graph had 482 Rust packages and the installed inventory had 246
native packages. All registry crate archives were verified against the lockfile,
and all native package copyright files were found. Thirteen refusal and
preservation tests passed. The full log SHA-256 is
`2a140b3b07d4c31fb1721add0b2720411e6248b3c4df849841a87e650daeff7d`.

That diagnostic found 44 Rust package entries without discovered notice files,
including four workspace crates and upstream `qdrant-edge`, `pgrx` and
`charabia`. The preview integration additionally copies the project root license
and notice. Upstream missing texts still require provenance-bound collection
and review. Nested bundled libraries, dictionaries and model materials need
separate coverage; the resolved workspace graph is not binary linkage evidence.
The exported manifest keeps `license_review_complete` and `release_supported`
false. Neither collection nor its successful tests satisfy P5-LICENSE or permit
a release with incomplete acceptance evidence.

Local act integration `preview-af8e293ae3f4992b` then passed 12 archive tests,
15 notice tests and all four installed SQL groups, including two exact physical
drops and ordered results with identical numeric scores after reinstall. It
exported 1,016 original notice files (8,037,695 bytes), including the project
root texts. All 287 frozen inputs and 1,022 exported artifacts were hash checked.
The native product remains the exact clean `acfc885` build above; packaging
inputs were frozen separately as changes on `e77ead4`, with source manifest
`36475b3ba44b4d34fee44e6f92966b72f0421f5244dcaffa4ec2108175590f0c`.
The notices manifest SHA-256 is
`d4f4f93392168f3bfa35bb36f646d797af048e126c78b274c20d4b740bdec169`;
the act log SHA-256 is
`5b937f2f592f5078b35f94e41666d88be0dfdddcd66e1d7282fc8dfaa6734f5b`.
The same 44 missing Rust notice entries remain explicit. No native recompilation,
complete regression, license clearance or release support is claimed by this
packaging integration.

## Supplemental upstream texts

The checked-in `packaging/licenses/upstream/` bundle retains original texts from
fixed repository commits associated with 34 locked crate packages. The original
crate archive checksum, normalized Cargo manifest and `.cargo_vcs_info.json`
hashes bind each association. Collection verifies the declared repository,
commit, component path and clean VCS metadata against the original archive.
Text SHA-256 and Git blob hashes must match the checked-in record. Raw source
URLs include the full commit, and only notices in component ancestor directories
are accepted. Collection works offline and performs no network lookup.

The supplemental rows remain separate from notices present in published crates.
They preserve repository-level license and attribution texts without asserting
that the published source is reproducible or that nested materials are covered.
The six upstream entries without this additional evidence remain explicit:
`crc32c`, `enum-map`, `enum-map-derive`, `htmlescape`, `qdrant-edge` and `seahash`.
The four workspace packages have the separately collected project root texts.
This association does not complete license review or P5-LICENSE.

Local act run `upstream-f8f3b75dc3468066` passed 27 refusal and byte-preservation
tests and collected the actual frozen dependency inventory offline. The output
contains 1,051 original texts totaling 8,192,707 bytes; all 39 frozen inputs,
1,052 exported artifacts and the act log were hash checked. The collected
manifest SHA-256 is
`aa5fdc6ebaf55b93c1827003f4904e11115e5a361369b2946337a12f1014d2a5`;
the act log SHA-256 is
`45e1a5f8d33d6c1f8fdd70eb593c663f158972691a0a34b9bdc298473fe47c25`.
This collection uses the unchanged clean `acfc885` native build above and a
separately frozen supplemental collector and bundle on `119bbea`. It does not
establish a new native build, package installation or full regression result.

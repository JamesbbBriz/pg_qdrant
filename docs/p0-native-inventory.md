# P0 native build and license metadata artifact

`scripts/native_build_report.py` is a read-only collector for the frozen native
build. Its clean-image execution is pending. It does not install packages,
execute the extension/helper, run `ldd`, or grant license clearance. The only
write is the requested JSON artifact. The [native package freeze](p0-native-build.md)
and the Cargo license inventory remain separate inputs.

The collector first compares every installed package/version entry against the
246-entry expected inventory. Missing, additional, changed, duplicate, or
not-installed entries fail collection. Each entry also records architecture.
This includes inherited packages and build tools; it is not a claim that every
package is a runtime dependency of the extension.

The artifact records the resolved `cc`, `ld`, `objdump`, and `pg_config`
executables and their hashes/version output, compiler target/full version, and
`pg_config --configure`. These are observations of the selected tools in the
completed image, not a retrospective proof of every compiler/linker invocation.
Machine-local executable paths and configure strings belong in the CI artifact;
do not copy generated reports into the public source tree without sanitization.

For the installed extension and, when required by the profile, helper, it records
the actual ELF file hash and `objdump -p` direct `NEEDED`, `SONAME`, `RPATH`, and
`RUNPATH` entries. It deliberately leaves the resolved transitive shared-library
closure null: loader search, lazy loads, and `dlopen` dependencies are not measured.
See the primary [GNU objdump documentation](https://sourceware.org/binutils/docs/binutils/objdump.html).

In pinned pgrx 0.19.3, `run_bindgen` reads the preferred `CLANG` from `pg_config`;
neither that setting nor `LIBCLANG_PATH` proves which shared library was loaded.
The [include detector](https://docs.rs/crate/pgrx-bindgen/0.19.3/source/src/build/clang.rs)
prints bindgen's loaded-library version, and its fallback branch also prints the
path obtained from `clang_sys::get_library()`. Optional explicit
`--pgrx-build-stderr` arguments retain trace hashes and those PG17 observations.
Only a unique actually reported path sets `selected_libclang`; absent or conflicting
paths leave it null with an explicit status. A matching preferred compiler can
take the branch which reports a version without a library path. The report does
not force that branch or infer a path from the version.

Installed libclang package names and the configured path remain separate fields.
Optional explicit `--bindings-artifact` arguments hash binding outputs. Supplied
trace/output files are not automatically attributed to a Cargo profile: the CI
hook must select relevant build outputs and preserve its build invocation evidence.
The collector does not claim to observe an already completed build's process map.

For each package, the collector records the requested and actual resolved
`/usr/share/doc/<package>/copyright` filename, byte size and SHA-256. Aliases within
the documentation tree are followed. Files outside that tree are refused.
Machine-readable [Debian copyright format 1.0](https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/)
files yield declared `License` synopsis labels together with header, files-pattern,
or license-definition stanza scope. Labels and expressions remain verbatim
declarations; they are not normalized to SPDX or selected as the only applicable
license. Full copyright/license documents are not copied into the report.

Legacy prose, missing files, malformed metadata, invalid UTF-8 and the 8 MiB file
budget each receive an explicit status and null labels. A hash is retained whenever
the file was read, including non-machine-readable prose. Status counts accompany
all package entries. `upstream_declared_license_inventory=true` means installed
package declarations have been inventoried with these gaps exposed; it does not
mean all licenses were parsed or reviewed. `distribution_review_complete` and
`p5_notices_complete` remain false. Source correspondence, license compatibility,
required notices, source-offer obligations and redistributability remain release
review work. Binary names never determine a license.

## Image hook

`Dockerfile.p0` collects after the final installed-package check and extension/helper
installation. `P0_HELPER` and `P0_FAULTS` are the actual Docker build arguments;
the equivalent command below makes profile selection explicit. The output is uploaded with the
existing `/src/artifacts` directory. Do not replace a failing report with a newly
generated expected package list.

```sh
set -- --expected-packages /opt/pg_qdrant_native/expected-installed.tsv \
  --pg-config "$PGRX_PG_CONFIG_PATH" \
  --extension "$("$PGRX_PG_CONFIG_PATH" --pkglibdir)/pg_qdrant.so" \
  --out /src/artifacts/native-build-report.json
profile=direct
if [ "$P0_HELPER" = 1 ]; then
  profile=helper
  set -- "$@" --helper "$("$PGRX_PG_CONFIG_PATH" --bindir)/pg_qdrant_p0_helper"
fi
if [ "$P0_FAULTS" = 1 ]; then profile="$profile-fault"; fi
python3 scripts/native_build_report.py "$@" --profile "$profile"
```

The Docker hook additionally supplies the actual available Cargo PG17 binding
files and `pgrx-pg-sys` build stderr files from that image. Each is individually
hashed; no unique profile or loaded-library path is inferred if the evidence is
ambiguous. A successful clean-image execution has not yet been recorded.
The helper argument is mandatory for helper profiles and forbidden for direct
profiles, so absence cannot be silently treated as a complete helper inventory.
Every required tool, ELF artifact and the full package inventory must be readable;
command or collection errors return a nonzero process exit status. License parsing
gaps are reported as data rather than concealed by a false clearance result.

Safe parser and temporary-fixture verification:

```sh
python3 -m unittest discover -s scripts/tests -p test_native_build_report.py -v
```

The tests exercise scoped DEP5 labels, legacy/ambiguous metadata, documentation
aliases and boundary refusal, invalid encoding/file size, exact package entries,
and ELF direct-entry parsing. They do not establish clean CI collection, selected
libclang, a full runtime link closure or completed third-party notices.

# P0 native build and license metadata artifact

`scripts/native_build_report.py` is a read-only collector for the frozen native
build. [CI8](evidence/p0-native-ci.json) verifies its clean-image execution in
all four diagnostic profiles at implementation `57c58fcee51efb0067f04b03ffb44e300f76ce72`,
tree `8b1d6d863be52229f8b15b49ad0b58b96b28608f`, run
[37743504259](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37743504259).
It does not install packages,
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
ambiguous. CI8 produced all four reports with matching inventories and tool
metadata. Bindgen reported `Ubuntu clang version 19.1.1 (1ubuntu1~24.04.2)`;
its exact loaded-library path was not reported, so `selected_libclang` remains
null with `selection_status=not_observed`. All four reported PG17 binding
outputs have SHA-256 `031a40c8c67b67dc28e3d965958e06f646e41ca1c011d7ac040ff51282a17791`.
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
and ELF direct-entry parsing. The actual clean-CI collection is separate
evidence; neither those tests nor collection establishes an unobserved
libclang path, full runtime link closure or completed third-party notices.

## CI8 inventory observations

Every profile matches all 246 installed package/version entries and records
architecture, actual copyright-file names and reported file hashes. No copyright
file is missing. The identical parsing counts are 125 `parsed_dep5`, 75
`unparsed_non_dep5`, 44 `unparsed_malformed_dep5` and 2
`unparsed_empty_license_synopsis`. The 121 parsing gaps describe this collector
and do not mean those packages lack licenses. Their hashes and package names
remain available for distribution review.

Compiler, linker/disassembler and PG configuration are identical across profiles:
GCC 13.3.0, GNU binutils 2.42 and PostgreSQL 17.11. Installed extension/helper ELF
reports name the direct dependencies `libgcc_s.so.1`, `libm.so.6`, `libc.so.6`
and `ld-linux-x86-64.so.2`. They do not resolve the loader's transitive closure.
The sanitized public evidence preserves report hashes and recorded measurements.
Original copyright, archive, binary, binding and build-trace bytes were not
separately uploaded for an independent second hash calculation. Distribution
review and P5 notices remain false.

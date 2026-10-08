# P0 native package freeze

Status: proposed build gate, with signed repository metadata and the historical
CI inventory verified. A clean build using this configuration has **not** run.
This does not yet close P0-BUILD, CPU compatibility, or the release gate.

The image retains its Ubuntu 24.04 digest. Its Ubuntu sources now use the fixed
`20261008T061600Z` snapshot for noble, noble-updates, noble-security and
noble-backports. The existing main, restricted, universe and multiverse
components remain configured. Phased-update selection is explicit. APT still
checks repository signatures and transport certificates; neither is disabled.
Canonical documents the timestamped archive and a retention policy of at least
two years, rather than perpetual availability: [Ubuntu snapshot service](https://snapshot.ubuntu.com/).

The [source evidence](evidence/p0-native-source.json) records verified signatures
for all four Ubuntu release files and the historical PGDG release file. The
three used Ubuntu package indexes and PGDG index match their signed SHA-256
entries. Every one of the 155 package/version/architecture tuples downloaded
by CI7 is present in those indexes. This is metadata verification, distinct
from reinstalling those packages.

## PostgreSQL package closure

PGDG's [historical archive](https://apt-archive.postgresql.org/) publishes all
released versions in a moving index. It is not a timestamped repository.
The configuration therefore fixes all seven PGDG packages actually installed
by CI7, blocks other PGDG candidates, and checks downloaded archive bytes
before installation. The server, client and development headers remain 17.11;
CI7's libpq packages were 18.6, a separately recorded package choice.

| Packages | Exact package version |
| --- | --- |
| `postgresql-17`, `postgresql-client-17`, `postgresql-server-dev-17` | `17.11-1.pgdg24.04+2` |
| `postgresql-common`, `postgresql-client-common` | `293.pgdg24.04+1` |
| `libpq5`, `libpq-dev` | `18.6-1.pgdg24.04+2` |

[pgdg.pref](../packaging/native/pgdg.pref) gives these exact versions precedence
and rejects other packages from the PGDG archive. The seven explicit install
arguments are in [pgdg-packages.txt](../packaging/native/pgdg-packages.txt);
[pgdg-packages.json](../packaging/native/pgdg-packages.json) records their
architecture, byte size and signed-index archive hashes. The vendored public
PGDG signing key has fingerprint
`B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8` and a recorded file hash. There is no
build-time download of an unpinned signing key.

The PGDG stage first downloads the dependency set. The
[native verifier](../scripts/verify_native.py) requires each of the seven
archives exactly once, checks its control-field identity, size and SHA-256,
and refuses unexpected PGDG archives. APT then installs with `--no-download`.
Other downloaded dependencies come from the authenticated fixed Ubuntu
snapshot. A missing archive or changed byte/hash fails the build; the script
does not quietly accept a replacement release.

## TLS bootstrap and final inventory

The minimal base image does not yet contain the `ca-certificates` package.
`Dockerfile.p0` uses BuildKit's [checksum-checked HTTPS ADD](https://docs.docker.com/reference/dockerfile/#add---checksum)
for the exact Ubuntu `ca-certificates_20260601~24.04.1_all.deb`. Its SHA-256 is
`6bac2a01979e210d9eac1d4d56747ec709ea60654744d66705dc3c36e7629e50`, matched
against the signed snapshot index. It extracts only the package's public
Mozilla roots to bootstrap the CA bundle. The ordinary authenticated APT
installation then installs/configures the package. No unpinned APT bootstrap,
insecure TLS switch, private CA or credential is introduced.

A read-only local check downloaded and verified this archive, parsed its real
package identity and extracted its roots. A handshake using only that bundle
failed certificate verification in the review network; no private root was
added and no TLS check was disabled to make it pass. This observation is
preserved in the source evidence. The proposed clean-container TLS bootstrap
remains an explicit next-CI requirement.

The eight CI7 artifact inventories were byte-identical: 246 package/version
entries, SHA-256
`df4b121fd4610bbe95ef7d31bf267b8ef117e933cc3bcb69cc4d6e455f77d872`.
The [expected inventory](../packaging/native/expected-installed.tsv) retains
all entries, including 91 without a download in the recorded build; the
unchanged base-image digest anchors those inherited inputs. A final image
check compares the entire inventory, ignoring order only. Missing, extra or
changed packages fail. This avoids a 246-package installation command while
checking the complete observed result in every image profile. The image emits
archive and inventory verification JSON alongside the existing package TSV.

The expected inventory is a deliberate upgrade boundary. Dependency or base
updates require new signed metadata, explicit package changes and a clean
build/test record. Do not regenerate the expectation from a failing image to
make it pass. Ubuntu snapshot retention and the historical PGDG service remain
availability dependencies; this patch does not introduce a private mirror.

## CPU boundary remains separate

The published Edge 0.8.0 `build_quantization.rs` builds **both**
`cpp/quantization/sse.c` and `avx2.c` with `-march=haswell -O3 -mpopcnt` on the
Linux x86_64/GCC path. The default Rust target does not override that native
code generation. In `encoded_vectors_u8.rs`, one fallback selects the SSE C
entry point after testing only SSE4.1; binary-vector code has SSE4.2 dispatch.
An existing local object inspection also observes AVX VEX instructions inside
`impl_score_dot_sse`. That observation is not a CI7 binary/disassembly audit.

Consequently, this build must not advertise generic pre-AVX x86_64 support.
GCC's [Haswell target](https://gcc.gnu.org/onlinedocs/gcc-13.3.0/gcc/x86-Options.html)
permits features beyond the x86-64-v3 level; the labels are not interchangeable.
The integrated [CPU admission](p0-cpu-baseline.md) separately defines 20
required features and checks usable AVX state before engine entry, with a
generated-object audit for the explicit HLE exclusion. Pure policy tests cover
missing-feature refusal; no unsupported physical CPU or OS-state emulation
has run. The revised clean image and its actual host report remain pending.
For a narrower x86-64-v3 promise, first audit the actual pinned-compiler native
objects for instructions outside that level. Upstream dispatch names alone
are insufficient evidence.

## Validation before marking the native gate complete

The safe script tests cover complete/missing/duplicate archives, changed bytes
of the same length, changed version, unexpected PGDG input, symlink refusal,
and full-inventory drift/duplicate rejection:

```sh
python3 -m unittest discover -s scripts/tests -p test_verify_native.py -v
```

Next CI must prove the checksum/TLS bootstrap, frozen APT resolution,
seven-archive verification and exact 246-entry inventory from an uncached
image build, then execute the existing Rust/PostgreSQL profiles. Capture
`cc --version`, `ld --version`, `pg_config --configure`, observed libclang diagnostics and
direct ELF dependencies alongside that evidence. The [native metadata collector](p0-native-inventory.md)
is wired into each image profile; unobserved libclang paths, unparsed package
licenses and transitive loader closure remain explicit gaps. Native license/notices and
CPU checks remain explicit requirements. No bit-identical compiler output,
new platform support, or successful clean reinstall is claimed here.

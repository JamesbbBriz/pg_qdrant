# Native build reuse and capacity

The native Docker build copies Cargo manifests, the lockfile, the pinned Rust
toolchain and crate inputs before compilation. Repository documentation and
Python verification tools are copied afterwards. A documentation-only change
therefore reuses the compiled native layer. Cargo's target and registry caches
survive source changes through locked BuildKit cache mounts. Cargo still checks
compiler version, flags, features, profiles and dependency fingerprints.

The mutable target cache never enters a running PostgreSQL process. Existing
native CPU/fault probes use immutable artifacts copied into their test image.
Only the runtime binaries and all Edge C-object/pgrx audit directories are
exported, with a two-GiB admission limit and per-file checksums. Cargo dependency
archives and completed test executables stay in the bounded target cache.
The target path links to that immutable copy; moving it between image layers
would duplicate large debug artifacts in the exported image.
Post-build dependency auditing uses the same registry mount. An immutable copy
of registry sources and archives supports offline license notice collection.
Dependencies and the PostgreSQL exception boundary retain their existing pins
and panic semantics until a candidate completes compatibility qualification.

All supported local launchers use one OS-held run lock and the same local cache
root. `PGQ_CACHE_ROOT` can select another dedicated local directory. Do not place
the cache inside a source checkout. Its mounted runner path is `/pgq-cache`.

| Resource | Default budget | Retention and refusal |
| --- | --- | --- |
| Dedicated BuildKit cache | 24 GiB; 2 GiB reserved; 40 GiB free-space target | Pinned BuildKit 0.34.0, project builder only; GC plus explicit pre/post-build prune and measurement |
| Saved local OCI inputs | 16 GiB, 16 entries | Exact image/manifest identities, every blob checksum, LRU; active input contexts protected |
| Compressed source snapshots | 2 GiB, 32 entries | SHA-256 manifests and archive receipts; dirty/failed inputs pinned; verified completed source views retired |
| Registered images and explicit image pins | 80 GiB conservative full-image sum | Prefer two passed families and one failed family; older eligible families can retire to satisfy bytes; latest family, pins and container references protected |
| Local evidence | 8 GiB admission threshold | Preserve failures/logs/receipts; refuse another invocation when full |
| Failed-cluster diagnostic archive | 512 MiB compressed, 8 GiB uncompressed, 90 seconds | Gzip/raw-stream hashes and all tar bodies verified before exact-container retirement; incomplete exports retain the original |

BuildKit GC is periodic, not a filesystem quota. Busy cache records can exceed
its target during a build. Pre/post-build checks reject unresolved excess.
The host launcher also samples free space every second and cancels its owned
workload when the host reserve is reached. Heavy jobs retain 10 GiB on evidence
and cache filesystems and 40 GiB on the detected Docker/repository backing drive.
`PGQ_DOCKER_STORAGE_PATH` identifies a relocated Docker backing directory when
automatic discovery does not cover it. Periodic cancellation cannot guarantee
zero overshoot or protect against an unrelated process filling the disk.

Legacy unregistered images, the shared default builder, business containers,
database volumes and historical evidence are not automatic deletion targets.
The image budget counts shared layers conservatively more than once; it does
not report physically reclaimed bytes. A pinned or referenced resource that
prevents admission is retained and the next build fails visibly.

Inspect or trim a cache under the run lock:

```sh
python scripts/ci_cache.py status --bucket snapshots
python scripts/ci_cache.py status --bucket oci
python scripts/ci_cache.py trim --bucket oci
python scripts/ci_images.py status
```

After reviewing a protected snapshot, release only its exact key with
`python scripts/ci_cache.py unpin --key <sha256>`. Restore a retained snapshot
into an empty directory with
`python scripts/ci_cache.py restore --manifest <ci-input.json> --destination <empty-directory>`.
Unknown files, interrupted writes, changed hashes and links stop automated
retirement for review. No global Docker prune or force removal is used.

For an exact failed run whose raw cluster exceeds the default diagnostic limit,
review its retained resources, then retry `scripts/ci_resources.py` with
`--failed-cluster-gib 16` or `32`. Compressed output remains limited to 512 MiB;
the explicit retry allows at most 32 GiB of raw data and 180 seconds. It does
not change normal launcher limits or turn the failed test into a pass.

## Compiler measurement

`python scripts/local_ci.py --profile compiler_benchmark --compiler <candidate>`
runs the independent measurement profile through local act. Candidates are
locked in `ci/compiler-candidates.json`: LLVM 1.96, LLVM 1.99, a dated nightly
parallel frontend, and the same dated nightly with Cranelift. The existing
`pg-qdrant-build-tools:1.96` image supplies the native baseline; its exact image
ID is recorded, and derived compiler inputs are saved and hash-verified locally.

Each candidate runs two empty-target builds, two unchanged builds, a README
change and one declared protocol-source change, then installed P0/P1 SQL.
Timings include cache checks; complete Cargo/BuildKit logs separately record the
native build layer and distinguish compiler work from layer reuse. Intermediate
measurements use cache-only output; only the final image is loaded for SQL tests.
Empty-target repetitions share registry downloads.
Candidate compiler installation is recorded separately from native timings.
The final extension-install timestamp separates compilation and installation
from copying immutable artifacts into the image. Compare native recipe and crate
input hashes before comparing compiler timings; later audit/export changes can
change total wall time without changing compiler work.
The compiler probe requires panic unwinding across a callback, destructor cleanup
and native SIMD before the full Edge build. Unsupported candidates fail; no
abort-only workaround is permitted. Measurements are diagnostic evidence and
cannot replace the complete 37-gate product regression or formal acceptance.

Select a default only after measuring compatible candidates on the same source,
CPU/memory budget and lockfile. Compiler feedback (`cargo check`), native code
generation, linking and runtime test time are different workloads; a faster
code generator need not be the fastest end-to-end configuration.

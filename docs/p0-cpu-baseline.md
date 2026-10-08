# P0 native CPU admission

Status: conservative admission and its scoped object audit are verified in all
four clean CI8 image profiles at implementation
`57c58fcee51efb0067f04b03ffb44e300f76ce72`, tree
`8b1d6d863be52229f8b15b49ad0b58b96b28608f`, run
[37743504259](https://github.com/JamesbbBriz/pg_qdrant/actions/runs/37743504259).
The [native reports](evidence/p0-native-ci.json) record actual host observations;
[current CI evidence](evidence/p0-current-ci.json) separately records SQL/helper
execution. This is a restricted build baseline, not a processor benchmark or a
release platform matrix.

## Why the default Rust target is insufficient

Published Edge 0.8.0's
[build_quantization.rs](https://docs.rs/crate/qdrant-edge/0.8.0/source/build_quantization.rs)
compiles both `cpp/quantization/sse.c` and `avx2.c` with
`-march=haswell -O3 -mpopcnt` on the selected GCC/Linux x86_64 path. An upstream
SSE dispatch label does not constrain the C compiler to pre-AVX instructions.
The local generated `sse.o` actually contains VEX vector instructions.

[GCC 13.3's target documentation](https://gcc.gnu.org/onlinedocs/gcc-13.3.0/gcc/x86-Options.html)
distinguishes processor targets from the psABI x86-64 levels. Inspection of the
selected compiler's options also finds Haswell features beyond x86-64-v3.
This project does not equate the two labels or advertise generic pre-AVX support.
The upstream crate, its native source and its compiler flags are unchanged.

## Shared admission contract

The [CPU module](../crates/edge-probe/src/cpu.rs) defines baseline
`edge-0.8.0-native-haswell-conservative-v1`. It requires x86_64 and the following
usable features:

| Detection | Required features |
| --- | --- |
| Rust runtime feature detection | AVX, AVX2, FMA, F16C, BMI1, BMI2, LZCNT, MOVBE, POPCNT, SSE3, SSSE3, SSE4.1, SSE4.2, CMPXCHG16B, PCLMULQDQ, RDRAND, XSAVE, XSAVEOPT |
| Explicit CPUID, after checking maximum supported leaves | LAHF/SAHF and FSGSBASE |

Rust's AVX-family detection includes operating-system state. The two explicit
CPUID observations are hardware bits only, as the report states. No caller can
supply or override observations. Unsupported architecture, missing observation
or missing required feature refuses admission before the relevant engine entry.

The standalone `--cpu-check` prints the shared report without calling Edge and
returns zero when admitted or 78 when refused. Extra arguments return 2.
`qdrant.build_info()` includes the same `cpu_admission` object. A managed helper
checks admission before creating its owner file and reports `cpu_unsupported`
on refusal. Engine smoke, persistence/BM25 and test fixture entry points use
the same check; the compile-only method inventory remains unexecuted.

```sh
cargo run --locked -p pg-qdrant-edge-probe -- --cpu-check
```

The guarded baseline uses the declared generic Rust target. Global
`target-cpu=native` or manually enabled Rust target features are outside it:
standard feature detection can be constant-folded for compile-time features,
and unrelated generated code can run before a runtime check. The fixed build
must not gain those overrides implicitly.

## HLE disposition and native object audit

HLE is observed but not required. The selected C source contains no atomic/HLE
operation, and the audited generated objects contain no HLE/RTM instructions.
The omission is specific to those objects; it is not a statement that Haswell
and x86-64-v3 have the same features. Recheck this decision whenever upstream,
native compiler or flags change.

```sh
python3 scripts/check_native_cpu.py
```

The [object audit](../scripts/check_native_cpu.py) requires the actual generated
SSE and AVX2 objects in each selected build directory. It records their hashes,
normalized decoded-instruction hashes, compiler/disassembler versions and
vector mnemonics. Unexpected HLE/RTM instructions or AVX-512 register operands
fail the audit. A missing object or missing decoded instruction list also fails.
The report explicitly does not claim a complete ISA proof for every dependency.

Each clean P0 image runs CPU admission before its engine tests and repeats
admission plus the object audit after the selected extension/helper build.
It retains `cpu-admission.json` and `native-cpu-objects.json` with the other
verification artifacts. The actual host feature report is evidence for that
host; it does not certify every CPU bearing a marketing name.

## Verification limits

The two pure policy tests cover rejection when a required feature is absent or
unusable and acceptance independent of HLE's observed value. They do not fake
CPUID, run an emulator, execute guarded instructions on an unsupported host,
or demonstrate an operating system with disabled AVX state. The local CPU
preflight is an actual host observation. The [integrated local record](evidence/p0-cpu-native-local.json)
passes 27 ordinary engine tests including those two policy tests, all four
PostgreSQL compile profiles, both helper compile profiles and the actual
local object audit. CI8 subsequently passed the current integrated native,
engine, SQL and helper checks. Its four identical CPU reports admit all 20
required features. The same GCC 13.3.0/objdump 2.42 build reports 1,152 decoded
SSE-object instructions and 296 AVX2-object instructions, with no audited
HLE/RTM instructions or AVX-512 register operands. The reported object hashes
are identical across the four profiles:

- SSE: `a7f199042280bc49f9776355f8e704de2b2986761c08c5b2b7ff57d5f614d7d4`.
- AVX2: `3b4405cafff5cb03cc34c5ad92c1bae47b3347fe2e984e4cf1a85945b5ea6a4b`.

These are build-tool measurements retained in [native evidence](evidence/p0-native-ci.json);
the uploaded JSON does not include the original object bytes for independent
rehashing. Unsupported-hardware execution, OS-disabled AVX, every dependency
ISA and broader platform support remain outside the observed scope. The
[native package freeze](p0-native-build.md) records the separate input closure.

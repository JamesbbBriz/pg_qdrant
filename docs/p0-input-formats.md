# P0 PostgreSQL format and source-recheck experiments

Status: candidate diagnostic contracts. These experiments address the type/shape
and source-recheck portions of P0-PG, particularly V01–V03 and L08–L09. They do not
freeze a public SQL API, implement P1 source capture, or change product capability
status. The dependency and capability contracts remain authoritative.

## Implemented candidate-format probe

The private, superuser-only diagnostic is:

```sql
SELECT qdrant_internal.p0_vector_formats(
    ARRAY[1,-0.0,0.25]::real[],
    ARRAY[0,4294967295]::bigint[],
    ARRAY[0.5,-1]::real[],
    ARRAY[[1,2],[3,4]]::real[]
);
```

Its four required arguments are dense values, sparse indices, sparse weights,
and a rectangular token matrix. PostgreSQL uses the same array type/OID for
`real[]` and `real[][]`; the function inspects actual dimension metadata. It is
`VOLATILE`, `PARALLEL UNSAFE`, security invoker and called on null input. Runtime
superuser checks precede validation even if schema/function ACLs are granted to
another role. SQL NULL therefore cannot silently bypass the function through a
STRICT declaration.

The probe copies bounded values into owned Rust vectors, encodes and decodes a
bounded serde JSON message, compares every finite float32 bit pattern and shape,
and returns the decoded values plus measured wire bytes and declared limits.
Signed zero, finite subnormals and finite large float32 values are included in
the SQL assertions through an explicit prefix of at most eight decoded float32
bit patterns, represented as unsigned integers. This bitwise guarantee applies
to the owned JSON roundtrip before conversion to PostgreSQL JSONB. Returned
JSONB numeric values do not preserve IEEE 754 signed-zero representation. Metadata calls and pgrx array access run only on the calling
backend thread. This experiment does not submit an IPC or Edge request, validate
a model, or prove a PostgreSQL binary driver protocol.

| Input | Accepted candidate contract | P0 budget |
| --- | --- | --- |
| Dense `real[]` | One nonempty dimension, lower bound one, finite non-null values | 4,096 dimensions |
| Sparse `bigint[]` / `real[]` | Two nonempty one-dimensional arrays with lower bound one and equal length; indices strictly increasing, unique and within unsigned 32-bit range; weights finite/non-null | 4,096 stored entries; zero weights allowed; no sorting, deduplication or score normalization |
| Tokens `real[][]` | Exactly two nonempty dimensions, both lower bounds one; finite non-null values; row boundaries retained | 128 rows, width 1,024, at most 16,384 values |
| Owned scalar storage | Dense/weights/tokens float32 plus sparse unsigned indices | 128 KiB of scalar contents; excludes allocator/container overhead and serialization copies |
| Encoded message / result | Owned JSON wire representation / complete result JSON | 256 KiB / 512 KiB; bounded writer rejects excess before appending |

PostgreSQL may allocate/detoast an input array before this Rust function runs.
These budgets bound the probe's copied values and serialized bytes, not arbitrary
SQL expression construction, PostgreSQL input memory, or process RSS. The small
prototype does not establish a complete resource-governance contract.

| Error | SQLSTATE | Required assertion |
| --- | --- | --- |
| Null argument or element | `22004` | Explicit error with field and DETAIL |
| Empty input, wrong dimensions/lower bounds, non-finite value, invalid/duplicate/unsorted sparse index, mismatched sparse lengths | `22023` | No truncation, flattening or implicit normalization |
| Cardinality, token shape or byte budget exceeded | `53400` | Explicit rejection; a subsequent valid call still works |
| Non-superuser call | `42501` | Both ACL and runtime checks, including all-null arguments after explicit grants |
| Ragged matrix | `2202E` | PostgreSQL rejects the array constructor before extension execution |
| Owned message loses shape or float32 bits | `XX000` | Probe fails; never a passing roundtrip report |

`crates/pg_qdrant/tests/verify_formats.py` adds three named check groups to the
existing disposable-cluster runner: valid/boundary/prepared inputs, negative
cases and budgets, and independent ACL/runtime authorization. They run in every
normal/private direct/helper profile through the existing runner. Code, compile,
SQL-generation and actual SQL-runtime evidence are separate; an authored CI
assertion is not a passing runtime result. No PostgreSQL server is started as
root to obtain a local result.

## Compile and SQL-generation checkpoint

The source based on `77f9ecca11ede76e42ba197e294b177ea865d830` passed the
following locked local checks with Rust 1.96.0, pgrx/cargo-pgrx 0.19.3 and
PostgreSQL 17.11 headers on Linux x86_64:

```sh
cargo check --locked -p pg_qdrant --no-default-features --features pg17
cargo pgrx schema --manifest-path crates/pg_qdrant/Cargo.toml \
  --pg-config "$PGRX_PG_CONFIG_PATH" --no-default-features --features pg17 \
  --cargo=--locked --out "$PG_QDRANT_SCHEMA_OUT"
cargo check --locked -p pg_qdrant --no-default-features \
  --features pg17,p0-managed-helper,p0-fault-injection
```

All three commands exited 0. Normal schema generation linked an actual x86_64
ELF shared object and found two schemas, seven functions and the final SQL
privilege block. The new signature is `(real[], bigint[], real[], real[]) ->
jsonb`, with VOLATILE/PARALLEL UNSAFE and no STRICT or SECURITY DEFINER clause.
The final block revokes public access to the internal schema and functions.
Python syntax checks and Rust formatting also pass. The three new SQL check
groups have **not run**: actual array metadata calls, errors, prepared inputs,
float32 roundtrip and runtime authorization require the next disposable non-root
PostgreSQL CI. Linking and SQL generation do not close that gate.

## Fixed-version API basis

The implementation uses public pgrx 0.19.3 `Array::len`, `Array::iter`,
`Array::into_datum` and `direct_function_call`, plus the PostgreSQL 17 public
`array_ndims`, `array_lower` and `array_length` functions through `pg_sys`.
It does not read private `Array` fields or reproduce PostgreSQL's array-header
layout. The array owner remains live while elements are copied; consuming it
with `into_datum` retains the detoasted allocation in the current PostgreSQL
memory context for metadata calls. No Datum or PostgreSQL pointer enters the
owned serialized message.

Fixed registry source references:

- [pgrx 0.19.3 array conversion](https://docs.rs/crate/pgrx/0.19.3/source/src/datum/array.rs)
- [pgrx 0.19.3 direct function calls](https://docs.rs/crate/pgrx/0.19.3/source/src/fcinfo.rs)
- [pgrx-pg-sys 0.19.3 PostgreSQL 17 bindings](https://docs.rs/crate/pgrx-pg-sys/0.19.3/source/src/include/pg17.rs)

PostgreSQL 17.11's [jsonb scalar parser](https://github.com/postgres/postgres/blob/REL_17_11/src/backend/utils/adt/jsonb.c#L379)
passes numeric tokens to `numeric_in`. Its [numeric result constructor](https://github.com/postgres/postgres/blob/REL_17_11/src/backend/utils/adt/numeric.c#L7838)
normalizes a zero result to a positive sign. pgrx 0.19.3 `JsonB::into_datum`
uses that `jsonb_in` conversion. Consequently, SQL tests inspect the explicit
integer bit evidence for negative zero; they do not require the output JSONB
number to retain its sign. The original compile/link checkpoint above preceded
this diagnostic-evidence correction. The corrected source has since passed
`cargo check --locked -p pg_qdrant --no-default-features --features pg17`.
The [source-bound local record](evidence/p0-input-formats-local.json) separates
that check from the earlier linked iteration. Subsequently, all three format
assertion groups passed in all four PostgreSQL profiles in
[CI6 at `d843706`](evidence/p0-capacity-and-sql-ci.json). That runtime evidence
covers this candidate-format source; it does not certify later identity/recheck
additions or freeze a public API.

## Separate identity and visible-result recheck experiment

The additive [source-recheck prototype](p0-source-recheck.md) now has private
source, a linked normal PostgreSQL 17 schema, and authored disposable-cluster
assertions. Its SQL runtime remains unverified. It accepts tagged bigint/uuid/text
keys and checks candidate fixture versions against an explicitly named source
snapshot; it does not implement durable point-ID allocation, source capture,
incarnation generation, production permissions or MVCC top-k.

The recheck diagnostic's `PgRelation` SQL conversion acquires a source relation
lock before its function-body superuser guard. Its P0-only namespace guard also
uses a broad conditional catalog lock. These limitations do not apply to the
vector-format probe, which performs no source reads. Neither diagnostic freezes
a public product SQL API.

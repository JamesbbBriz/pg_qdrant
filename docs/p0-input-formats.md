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
that check from the earlier linked iteration. Actual SQL execution remains pending.

## Proposed identity and visible-result recheck experiment

The following is a separate, **unimplemented** next experiment. It needs review
before introducing source relation lookup or dynamic SQL:

```text
qdrant_internal.p0_identity_roundtrip(
    bigint_key bigint, uuid_key uuid, text_key text
) -> jsonb

qdrant_internal.p0_recheck_candidates(
    source regclass, key_field name, candidates jsonb
) -> jsonb
```

Both would remain superuser-only, security invoker, called-on-null, VOLATILE and
PARALLEL UNSAFE. The identity probe should encode a tagged representation:
signed 64-bit integer as a decimal string, UUID as its exact 16 bytes/canonical
string, and UTF-8 text without case or Unicode normalization. All three required
keys would reject SQL NULL. Text may be empty but is capped at 1,024 UTF-8 bytes.
The cases include bigint minimum/maximum and values above JavaScript's exact
integer range; UUID zero/nonzero/case-equivalent inputs; empty text, multibyte
text, and composed/decomposed Unicode remaining distinct. Type tags must keep
bigint `1` and text `1` distinct. No ctid/xmin identity, stable point-ID mapping,
primary-key reuse or incarnation claim follows from encoding alone.

The recheck prototype would accept at most 32 synthetic candidates, each with a
tagged source key and explicit fixture `revision bigint`, `incarnation uuid`
and content fingerprint. These are columns supplied by an isolated test fixture,
not inferred reliable production metadata. It would require a local ordinary
table with one declared, non-null primary key of exactly bigint/uuid/text and
reject RLS-enabled/forced-RLS tables, unsupported relation kinds, unknown or
extra candidate fields, mismatched key tags and malformed versions. A table
lock must cover catalog validation through the SELECT. Identifiers must come
from catalog-validated attributes and proper identifier quoting; all key/version
values must be bound parameters. Arbitrary SQL or caller-supplied predicates
must never be accepted.

Proposed bounded CI scenarios, using two ordinary PostgreSQL sessions:

1. Exact key/revision/incarnation/fingerprint accepts a currently visible row;
   missing/deleted rows and any mismatched version component are excluded.
2. Session B's uncommitted update/delete is invisible to session A. After B
   commits, a fresh READ COMMITTED statement in A observes the change and rejects
   the stale candidate. REPEATABLE READ is tested separately against its fixed
   snapshot; the result must name that visibility scope.
3. Delete/reinsert of the same key with a different explicit fixture incarnation
   rejects the old candidate, including a late candidate delivered afterward.
   This demonstrates comparison only, not durable incarnation allocation.
4. Bigint/uuid/text fixtures use their native key types and binary-safe bound
   values; identifiers containing quotes cannot escape quoting. A schema-qualified
   regclass prevents search-path substitution. Concurrent DDL has a measured
   lock/rejection outcome.
5. RLS tables and unsupported key definitions are rejected explicitly. An
   unauthorized diagnostic call is rejected independently of ACLs. Because this
   remains a superuser diagnostic, it cannot establish production source SELECT,
   tenant authorization, RLS semantics, snippet safety or statistics safety.

No current candidate-format code performs these rechecks. Even a passing future
recheck experiment cannot recall relevant rows absent from the candidate set,
prove arbitrary historical MVCC top-k, or implement transactional outbox/replay.
Those remain separate P1/P2/P3 acceptance requirements.

# P0 tagged identity and source visibility probe

Status: private feasibility code, normal PostgreSQL 17 compile/link verified,
SQL execution pending. This experiment contributes evidence to L05, L08 and L09;
none of those product capabilities is complete. It adds two diagnostic functions
without implementing source capture, a point-ID registry, an asynchronous index,
production authorization or a public SQL API.

```sql
SELECT qdrant_internal.p0_identity_roundtrip(
    9007199254740993::bigint,
    '12345678-1234-5678-9abc-123456789abc'::uuid,
    '知识库'::text
);

CREATE SCHEMA p0_fixture;
CREATE TABLE p0_fixture.chunks (
    id bigint PRIMARY KEY,
    revision bigint NOT NULL,
    incarnation uuid NOT NULL,
    fingerprint text NOT NULL
);
INSERT INTO p0_fixture.chunks VALUES (
    1, 1, '12345678-1234-5678-9abc-123456789abc', repeat('a', 64)
);
SELECT qdrant_internal.p0_recheck_candidates(
    'p0_fixture.chunks'::regclass,
    'id'::text,
    jsonb_build_array(jsonb_build_object(
        'key', jsonb_build_object('type', 'bigint', 'value', '1'),
        'revision', '1',
        'incarnation', '12345678-1234-5678-9abc-123456789abc',
        'fingerprint', repeat('a', 64)
    ))
);
DROP SCHEMA p0_fixture CASCADE;
```

These examples target the additive private prototype and require its installation.
They are authored runtime scenarios, not evidence of a successful SQL execution.
All source data and versions above are fixture values supplied by the caller.

## Identity representation

`p0_identity_roundtrip(bigint, uuid, text) -> jsonb` requires all three arguments.
It copies native values into tagged owned Rust values and verifies serialization
and deserialization equality. A bigint becomes a canonical decimal string so it
cannot lose precision in a JSON consumer limited to JavaScript numbers. A UUID
returns a canonical lowercase hyphenated string and its 16 bytes. A text value
is copied unchanged; empty text is allowed, and composed/decomposed Unicode
remains distinct. Type tags distinguish bigint `1` from text `1`.

Text is limited to 1,024 UTF-8 bytes, and the complete encoded identity message
is limited to 8 KiB. The response reports actual encoded bytes and text bytes.
It explicitly reports that no stable point-ID mapping, incarnation allocation
or source lookup has occurred. Encoding a key does not prevent deletion/reuse
races by itself.

## Candidate and source contract

`p0_recheck_candidates(regclass, text, jsonb) -> jsonb` accepts between one and
32 candidates. Each candidate has exactly these fields:

| Field | Required representation |
| --- | --- |
| `key` | Object containing exactly `type` and `value`; tag is `bigint`, `uuid` or `text`; value is a string |
| `revision` | Canonical positive signed-int64 decimal string |
| `incarnation` | Canonical lowercase hyphenated UUID string |
| `fingerprint` | Exactly 64 lowercase hexadecimal characters |

Bigint key strings must be canonical signed-int64 decimal strings. All key tags
must match the exact source key type. Duplicate candidate keys, unknown fields,
null values, implicit casts, normalization and silent deduplication are rejected.
Text keys have the same 1,024-byte budget as identity encoding. Encoded candidate
JSON is limited to 64 KiB after bounded field validation; the result is limited
to 8 KiB. PostgreSQL/JSONB input allocation happens before these checks, so these
are copied-data budgets, not complete backend memory limits.

The source must be a persistent ordinary table outside system and extension
schemas. Partitioned tables, partitions, inheritance, views, foreign tables,
temporary/unlogged tables and RLS-enabled or forced-RLS tables are rejected.
The named key must be exactly `bigint`, `uuid` or `text`, not a domain. Its primary
key must be valid, immediate, nondeferrable, single-column built-in btree, with
no included columns and the corresponding default `pg_catalog` operator class.
All four required columns must be non-null and nongenerated. The fixture columns
are exactly `revision bigint`, `incarnation uuid` and `fingerprint text`.
Nondeterministic text key or fingerprint collations are rejected.

The result contains one ordinal/verdict per input candidate. It returns no
source text, snippets, vectors or arbitrary payload:

| Verdict | Meaning under the selected PostgreSQL snapshot |
| --- | --- |
| `matched` | A visible row has the same key, revision, incarnation and fingerprint |
| `missing` | No source row with that key is visible |
| `stale` | A row is visible, but at least one version component differs |
| `invalid_fixture` | A visible row has a nonpositive revision or malformed fingerprint |

Fingerprints are compared, not calculated from content. Incarnations are
compared, not allocated. No inference about the authenticity of a model,
fingerprint or version follows from equality of supplied fixture fields.

## Locks, permissions and snapshot scope

Both functions are security invoker, called on null input, `VOLATILE` and
`PARALLEL UNSAFE`. Default SQL revokes public access to the internal schema and
functions. Both function bodies require a superuser even if execution is
explicitly granted. The recheck function does not establish source `SELECT`,
tenant, RLS, snippet or statistics authorization for a future product API.

There is a specific P0 conversion limitation: pgrx 0.19.3 converts the recheck
function's `regclass` into `PgRelation` by opening the relation and acquiring
`AccessShareLock` **before** the function-body superuser check runs. With explicit
schema/function grants, a non-superuser can consequently encounter relation
lookup/lock behavior before the diagnostic rejects execution. SQL argument
expressions and regclass name resolution also occur before the function body.
The prototype does not claim that its runtime guard precedes all relation access.
No source candidate SELECT runs before the body guard. A production API must
resolve this conversion and authorization boundary separately.

The owned `PgRelation` keeps the source relation reference and lock through
catalog validation, source SELECT and copying its result. Current relcache
checks reject RLS and unsupported relation/column definitions after acquiring
that lock. The primary index receives its own relation guard during validation.
Source identifiers come from the locked relation and validated attribute name,
are schema-qualified and quoted, and are never accepted as SQL fragments. All
key/version values use typed bound parameters. Built-in comparison operators
and JSON functions are explicitly `pg_catalog` qualified.

A relation lock alone does not block schema rename. This P0 prototype therefore
conditionally takes a broad `ShareLock` on `pg_catalog.pg_namespace` before
extracting the qualified name, holds it through the source SELECT, and returns
`55P03` immediately if it conflicts. It does not wait for that catalog lock
while holding the source lock. This is an experimental guard, **not a production
locking recommendation**: it can conflict with unrelated schema DDL. Its Rust
guard releases it on normal return/unwind; PostgreSQL transaction/subtransaction
error cleanup provides a further ownership boundary. Actual error/cancellation
cleanup assertions remain part of the pending SQL tests. PostgreSQL's ordinary
source SELECT can retain an additional relation lock until transaction end;
that is distinct from the scoped catalog guard.

Source reading uses one generated SELECT through PostgreSQL
`SPI_execute_with_args(..., read_only=true)`, with an existing active statement
snapshot required. It decodes its single bounded JSONB aggregate while the SPI
memory context remains live, returning only owned Rust values. The public pgrx
`SpiClient::select` automatically switches its read-only flag once the caller has
an XID, so it is deliberately not used for this contract. The intended semantics
are the outer statement snapshot under READ COMMITTED and the transaction's
established snapshot under REPEATABLE READ. The authored test assigns an XID
before the READ COMMITTED statement to exercise this exact distinction. These
snapshot outcomes have not yet been demonstrated by this prototype's SQL tests.

Even a passing recheck cannot recover a correct row that the engine did not
recall. It does not prove arbitrary historical MVCC top-k, transactionally
synchronized Edge state or production permission enforcement.

## Error contract and authored runtime gates

| Failure | SQLSTATE |
| --- | --- |
| Required SQL argument is null | `22004` |
| Malformed candidate, mismatched key tag, duplicate, unknown field or invalid key-field name | `22023` |
| Count, key, encoded-request or result budget exceeded | `53400` |
| Unsupported source definition or missing required fixture column | `0A000` |
| Conditional namespace guard conflicts | `55P03` |
| Function-body non-superuser check | `42501` |
| No active statement snapshot | `55000` |
| Unexpected SPI result or roundtrip mismatch | `XX000` |

PostgreSQL argument conversion, relation lookup, lock timeout and cancellation
can independently raise PostgreSQL errors. In particular, the pre-body
`PgRelation` conversion caveat above affects error ordering.

`crates/pg_qdrant/tests/verify_source.py` adds six named check groups to the
existing disposable-cluster harness:

1. Exact tagged bigint/uuid/text roundtrip, int64 extremes and values above
   JavaScript's exact range, UTF-8 and normalization boundaries, null and size
   errors, and SQL function attributes.
2. Visible/missing/stale candidates, explicit delete/reinsert incarnation
   differences, malformed fixture values, all three native key types, quoted
   identifiers, apostrophes in bound values and a changed search path.
3. Strict candidate fields, key/type/version errors, missing/deferrable primary
   key and wrong fixture definitions, RLS and view rejection, and scoped catalog
   lock cleanup after a caught extension error and a normal call.
4. READ COMMITTED active-statement visibility after the caller has an XID;
   REPEATABLE READ visibility across statements; separately committed changes
   and another transaction's uncommitted/rolled-back update.
5. Source DDL blocking, conditional namespace-lock conflict, and a PostgreSQL
   cancellation while awaiting a primary-index lock. The cancellation test
   first requires both the exact ungranted index lock and a granted namespace
   guard; it then verifies guard release and continued use of the same backend.
   This cancellation occurs **before SPI**, so it is not evidence of cleanup
   after a PostgreSQL error during the generated source SELECT.
6. Default ACL denial and independent function-body superuser checks after
   explicit grants, including null input; no production authorization claim.

These tests are authored and syntax-checked, not runtime-verified. Explicit
SQL cases for nondeterministic collations, custom operator classes, partition
and inheritance rejection, malicious operator search paths, source-schema
rename races and a PostgreSQL error during SPI execution remain open.

## Current local checkpoint

On the source derived from commit
`77f9ecca11ede76e42ba197e294b177ea865d830`, the following commands passed with
Rust 1.96.0, pgrx/cargo-pgrx 0.19.3 and PostgreSQL 17.11 headers:

```sh
cargo check --locked -p pg_qdrant --no-default-features --features pg17
cargo pgrx schema --manifest-path crates/pg_qdrant/Cargo.toml \
  --pg-config "$PGRX_PG_CONFIG_PATH" --no-default-features --features pg17 \
  --cargo=--locked --out "$PG_QDRANT_SCHEMA_OUT"
```

Both exited zero. Normal schema generation linked the extension and found
12 entities: two schemas, nine functions and the final privilege block.
The two new signatures are `(bigint, uuid, text) -> jsonb` and
`(regclass, text, jsonb) -> jsonb`, without STRICT or SECURITY DEFINER.
The new SQL tests have not run in a PostgreSQL cluster. Neither compile success,
source review nor schema generation closes the P0 visibility, cancellation,
permissions or error-cleanup runtime gates.

[The local evidence record](evidence/p0-source-recheck-local.json) binds this
checkpoint to its source, manifest, lockfile and observed generated-SQL hashes.
The bounded native-cancellation assertion was added to the Python suite after
linking and received syntax validation only. All six new SQL groups remain
unrun; builds of later combined source require their own evidence.

Fixed-version source basis:

- [pgrx 0.19.3 relation conversion and guard](https://docs.rs/crate/pgrx/0.19.3/source/src/rel.rs)
- [pgrx 0.19.3 SPI client](https://docs.rs/crate/pgrx/0.19.3/source/src/spi/client.rs)
- [PostgreSQL 17.11 SPI execution and snapshot behavior](https://github.com/postgres/postgres/blob/REL_17_11/src/backend/executor/spi.c)
- [PostgreSQL 17.11 schema rename locking](https://github.com/postgres/postgres/blob/REL_17_11/src/backend/commands/schemacmds.c)

The [integrated local validation](evidence/p0-integrated-local.json) subsequently
checks all four extension profiles together with the OOM and capacity changes.
That record is compilation evidence only; the earlier normal linked schema and
the six still-pending SQL groups retain their separate scope.

//! P0 fixture-only source visibility. No capture, point-ID registry, or RLS support.

use crate::identity::{TaggedKey, bounded_text, reject, required};
use pgrx::datum::DatumWithOid;
use pgrx::{FromDatum, JsonB, PgRelation, PgSqlErrorCode as Code, Spi, Uuid, pg_sys};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashSet;
use std::ffi::CString;

const MAX_CANDIDATES: usize = 32;
const MAX_REQUEST_BYTES: usize = 64 * 1024;
const MAX_RESULT_BYTES: usize = 8 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Candidate {
    key: TaggedKey,
    revision: String,
    incarnation: String,
    fingerprint: String,
}

enum BoundKey {
    Bigint(i64),
    Uuid(Uuid),
    Text(String),
}

impl BoundKey {
    fn oid(&self) -> pg_sys::Oid {
        match self {
            Self::Bigint(_) => pg_sys::INT8OID,
            Self::Uuid(_) => pg_sys::UUIDOID,
            Self::Text(_) => pg_sys::TEXTOID,
        }
    }

    fn datum(&self) -> DatumWithOid<'_> {
        match self {
            Self::Bigint(v) => (*v).into(),
            Self::Uuid(v) => (*v).into(),
            Self::Text(v) => v.as_str().into(),
        }
    }
}

struct ValidatedCandidate {
    key: BoundKey,
    revision: i64,
    incarnation: Uuid,
    fingerprint: String,
}

fn invalid(detail: &str) -> ! {
    reject(
        Code::ERRCODE_INVALID_PARAMETER_VALUE,
        "P0 candidate input rejected",
        detail,
    )
}

fn unsupported(detail: &str) -> ! {
    reject(
        Code::ERRCODE_FEATURE_NOT_SUPPORTED,
        "P0 source definition is unsupported",
        detail,
    )
}

fn canonical_integer(value: &str, positive: bool) -> i64 {
    let parsed: i64 = value.parse().unwrap_or_else(|_| {
        invalid("Integer identities and revisions must be canonical decimal int64 strings.")
    });
    if parsed.to_string() != value || (positive && parsed < 1) {
        invalid("Integer strings must be canonical; revisions must be positive.");
    }
    parsed
}

fn canonical_uuid(value: &str) -> Uuid {
    let value = value.as_bytes();
    if value.len() != 36 {
        invalid("UUID strings must use canonical lowercase hyphenated form.");
    }
    let mut bytes = [0u8; 16];
    let mut digit = 0usize;
    for (position, byte) in value.iter().copied().enumerate() {
        if [8, 13, 18, 23].contains(&position) {
            if byte != b'-' {
                invalid("UUID separators are invalid.");
            }
            continue;
        }
        let nibble = match byte {
            b'0'..=b'9' => byte - b'0',
            b'a'..=b'f' => byte - b'a' + 10,
            _ => invalid("UUID strings must use canonical lowercase hexadecimal digits."),
        };
        bytes[digit / 2] = (bytes[digit / 2] << 4) | nibble;
        digit += 1;
    }
    Uuid::from_bytes(bytes)
}

fn validate_candidates(input: Value) -> Vec<ValidatedCandidate> {
    let array = input
        .as_array()
        .unwrap_or_else(|| invalid("Candidates must be an array."));
    if array.is_empty() || array.len() > MAX_CANDIDATES {
        reject(
            Code::ERRCODE_CONFIGURATION_LIMIT_EXCEEDED,
            "P0 candidate count is outside the budget",
            "Supply between 1 and 32 candidates.",
        );
    }
    let candidates: Vec<Candidate> = serde_json::from_value(input)
        .unwrap_or_else(|_| invalid("Each candidate requires exactly key, revision, incarnation and fingerprint with declared string/tagged types."));
    let mut seen = HashSet::new();
    let mut result = Vec::with_capacity(candidates.len());
    for candidate in &candidates {
        let key = match &candidate.key {
            TaggedKey::Bigint(value) => BoundKey::Bigint(canonical_integer(value, false)),
            TaggedKey::Uuid(value) => BoundKey::Uuid(canonical_uuid(value)),
            TaggedKey::Text(value) => {
                bounded_text(value);
                BoundKey::Text(value.clone())
            }
        };
        let identity = serde_json::to_string(&candidate.key).unwrap();
        if !seen.insert(identity) {
            invalid("Duplicate candidate keys are rejected; no deduplication is performed.");
        }
        if candidate.fingerprint.len() != 64
            || !candidate
                .fingerprint
                .bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        {
            invalid("Fingerprint must be exactly 64 lowercase hexadecimal characters.");
        }
        result.push(ValidatedCandidate {
            key,
            revision: canonical_integer(&candidate.revision, true),
            incarnation: canonical_uuid(&candidate.incarnation),
            fingerprint: candidate.fingerprint.clone(),
        });
    }
    // Validation above bounds every string and array before encoding. Actual
    // escaped size is checked as well; PostgreSQL input decoding precedes us.
    let wire = serde_json::to_vec(
        &candidates
            .iter()
            .map(|c| {
                json!({
                    "key": c.key, "revision": c.revision,
                    "incarnation": c.incarnation, "fingerprint": c.fingerprint
                })
            })
            .collect::<Vec<_>>(),
    )
    .unwrap();
    if wire.len() > MAX_REQUEST_BYTES {
        reject(
            Code::ERRCODE_CONFIGURATION_LIMIT_EXCEEDED,
            "P0 candidate byte budget exceeded",
            "Encoded candidates are limited to 64 KiB.",
        );
    }
    result
}

/// P0-only broad catalog lock: prevents schema rename between name extraction
/// and parsing a qualified source name. Never a production locking proposal.
struct NamespaceLock;

impl NamespaceLock {
    fn acquire() -> Self {
        // SAFETY: fixed catalog OID and valid lock mode; only backend calls here.
        if !unsafe {
            pg_sys::ConditionalLockRelationOid(pg_sys::NamespaceRelationId, pg_sys::ShareLock as _)
        } {
            reject(
                Code::ERRCODE_LOCK_NOT_AVAILABLE,
                "P0 namespace guard is busy",
                "Retry the diagnostic after schema DDL finishes. It never waits while holding the source lock.",
            );
        }
        Self
    }
}

impl Drop for NamespaceLock {
    fn drop(&mut self) {
        // SAFETY: this guard exists only after successful acquisition. PostgreSQL
        // also releases transaction/subtransaction-owned locks on ERROR cleanup.
        unsafe { pg_sys::UnlockRelationOid(pg_sys::NamespaceRelationId, pg_sys::ShareLock as _) };
    }
}

/// Explicit read_only=true is essential. pgrx SpiClient::select can switch to
/// read_only=false after a transaction obtains an XID, taking a newer snapshot.
fn readonly_json(query: &str, args: &[DatumWithOid<'_>]) -> Value {
    Spi::connect(|_| {
        let sql =
            CString::new(query).unwrap_or_else(|_| invalid("SQL identifier contained a NUL byte."));
        let mut types: Vec<_> = args.iter().map(DatumWithOid::oid).collect();
        let mut values: Vec<_> = args
            .iter()
            .map(|arg| {
                arg.datum()
                    .map(|d| d.sans_lifetime())
                    .unwrap_or_else(|| pg_sys::Datum::from(0usize))
            })
            .collect();
        let nulls: Vec<std::ffi::c_char> = args
            .iter()
            .map(|arg| {
                if arg.datum().is_some() {
                    b' ' as _
                } else {
                    b'n' as _
                }
            })
            .collect();
        // SAFETY: SPI connection is live; all declared-type parameter allocations
        // outlive this call; query is one generated SELECT. Decode its one JSONB
        // column while the SPI tuple table and its memory context remain alive.
        unsafe {
            if !pg_sys::ActiveSnapshotSet() {
                reject(
                    Code::ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE,
                    "P0 source recheck needs an active statement snapshot",
                    "Call the diagnostic from an ordinary PostgreSQL SELECT.",
                );
            }
            let status = pg_sys::SPI_execute_with_args(
                sql.as_ptr(),
                args.len() as i32,
                types.as_mut_ptr(),
                values.as_mut_ptr(),
                nulls.as_ptr(),
                true,
                1,
            );
            if status != pg_sys::SPI_OK_SELECT as i32
                || pg_sys::SPI_processed != 1
                || pg_sys::SPI_tuptable.is_null()
            {
                reject(
                    Code::ERRCODE_INTERNAL_ERROR,
                    "P0 source SELECT returned an unexpected SPI result",
                    "Expected exactly one bounded JSONB aggregate row.",
                );
            }
            let table = &*pg_sys::SPI_tuptable;
            if table.vals.is_null() || table.tupdesc.is_null() {
                reject(
                    Code::ERRCODE_INTERNAL_ERROR,
                    "P0 SPI tuple table is invalid",
                    "No candidate result is returned.",
                );
            }
            let mut is_null = false;
            let datum = pg_sys::SPI_getbinval(*table.vals, table.tupdesc, 1, &mut is_null);
            JsonB::from_datum(datum, is_null)
                .unwrap_or_else(|| {
                    reject(
                        Code::ERRCODE_INTERNAL_ERROR,
                        "P0 source SELECT returned NULL",
                        "No candidate result is returned.",
                    )
                })
                .0
        }
    })
}

fn source_key_type(source: &PgRelation, key_field: &str) -> pg_sys::Oid {
    // SAFETY: PgRelation owns a valid relcache reference and AccessShareLock.
    let class = unsafe { source.rd_rel.as_ref() }.unwrap();
    if !source.is_table()
        || class.relam != pg_sys::HEAP_TABLE_AM_OID
        || class.relrowsecurity
        || class.relforcerowsecurity
        || class.relispartition
        || class.relhassubclass
        || class.relpersistence != b'p' as std::ffi::c_char
    {
        unsupported(
            "Only persistent ordinary tables using the built-in heap access method, without RLS, partitions or inheritance, are accepted. Custom table access methods are unverified.",
        );
    }
    let descriptor = source.tuple_desc();
    if descriptor.iter().any(|a| a.attinhcount != 0) {
        unsupported("Inherited columns are outside this fixture probe.");
    }
    let attribute = |name: &str, oid: Option<pg_sys::Oid>| {
        let attr = descriptor
            .iter()
            .find(|a| !a.attisdropped && pgrx::name_data_to_str(&a.attname) == name)
            .unwrap_or_else(|| unsupported("The key or a required fixture column is missing."));
        if !attr.attnotnull || attr.attgenerated != 0 || oid.is_some_and(|t| attr.atttypid != t) {
            unsupported(
                "Fixture columns require exact built-in types, NOT NULL, and no generated expression.",
            );
        }
        attr
    };
    let key = attribute(key_field, None);
    if ![pg_sys::INT8OID, pg_sys::UUIDOID, pg_sys::TEXTOID].contains(&key.atttypid) {
        unsupported(
            "Source key must have exactly bigint, uuid or text type; domains are excluded.",
        );
    }
    attribute("revision", Some(pg_sys::INT8OID));
    attribute("incarnation", Some(pg_sys::UUIDOID));
    let fingerprint = attribute("fingerprint", Some(pg_sys::TEXTOID));
    // Fingerprints are compared as literal hexadecimal text, not with a
    // nondeterministic equivalence relation supplied by a fixture collation.
    if !unsafe { pg_sys::get_collation_isdeterministic(fingerprint.attcollation) } {
        unsupported("Nondeterministic fingerprint collations are excluded.");
    }
    if key.atttypid == pg_sys::TEXTOID
        && !unsafe { pg_sys::get_collation_isdeterministic(key.attcollation) }
    {
        unsupported("Nondeterministic text-key collations are excluded.");
    }
    // SAFETY: source and the subsequently opened primary index remain locked
    // through these relcache reads. Dynamic source selection follows below while
    // the source guard remains live; SQL also obtains its ordinary relation lock.
    let index_oid = unsafe { pg_sys::RelationGetPrimaryKeyIndex(source.as_ptr()) };
    if index_oid == pg_sys::InvalidOid {
        unsupported("Source needs an immediate single-column primary key.");
    }
    let primary = unsafe { PgRelation::with_lock(index_oid, pg_sys::AccessShareLock as _) };
    let index = unsafe { primary.rd_index.as_ref() }.unwrap();
    let index_class = unsafe { primary.rd_rel.as_ref() }.unwrap();
    if index.indnatts != 1
        || index.indnkeyatts != 1
        || !index.indisprimary
        || !index.indisunique
        || !index.indimmediate
        || !index.indisvalid
        || !index.indisready
        || !index.indislive
        || index_class.relam != pg_sys::BTREE_AM_OID
    {
        unsupported(
            "Primary key must be valid, immediate, single-column built-in btree without INCLUDE columns.",
        );
    }
    // SAFETY: the validated one-column index has one indkey entry.
    if unsafe { index.indkey.values.as_slice(1) }[0] != key.attnum {
        unsupported("key_field is not the primary-key column.");
    }
    let opclass = unsafe { pg_sys::get_index_column_opclass(index_oid, 1) };
    let expected = match key.atttypid {
        pg_sys::INT8OID => "int8_ops",
        pg_sys::UUIDOID => "uuid_ops",
        pg_sys::TEXTOID => "text_ops",
        _ => unreachable!(),
    };
    let check = readonly_json(
        "SELECT pg_catalog.jsonb_build_object('valid', EXISTS(SELECT 1 FROM pg_catalog.pg_opclass c JOIN pg_catalog.pg_namespace n ON n.oid OPERATOR(pg_catalog.=) c.opcnamespace WHERE c.oid OPERATOR(pg_catalog.=) $1 AND n.nspname OPERATOR(pg_catalog.=) 'pg_catalog' AND c.opcname OPERATOR(pg_catalog.=) $2 AND c.opcdefault AND c.opcintype OPERATOR(pg_catalog.=) $3 AND c.opcmethod OPERATOR(pg_catalog.=) $4))",
        &[
            opclass.into(),
            expected.into(),
            key.atttypid.into(),
            pg_sys::BTREE_AM_OID.into(),
        ],
    );
    if check["valid"] != true {
        unsupported("Custom or non-default primary-key operator classes are excluded.");
    }
    key.atttypid
}

pub fn recheck(source: Option<PgRelation>, key_field: Option<&str>, input: Option<JsonB>) -> JsonB {
    let source = required(source, "source");
    let key_field = required(key_field, "key_field");
    if key_field.is_empty()
        || key_field.len() > 63
        || ["revision", "incarnation", "fingerprint"].contains(&key_field)
    {
        invalid("key_field must be a distinct fixture key-column name of 1..63 UTF-8 bytes.");
    }
    let candidates = validate_candidates(required(input, "candidates").0);
    let _namespace = NamespaceLock::acquire();
    let namespace = source.namespace().to_owned();
    if namespace.starts_with("pg_")
        || ["information_schema", "qdrant", "qdrant_internal"].contains(&namespace.as_str())
    {
        unsupported("System and extension schemas are excluded from source fixtures.");
    }
    let key_type = source_key_type(&source, key_field);
    if candidates.iter().any(|c| c.key.oid() != key_type) {
        invalid("Every candidate key tag must match the exact source key type.");
    }
    let source_name = pgrx::spi::quote_qualified_identifier(namespace.as_str(), source.name());
    let key_name = pgrx::spi::quote_identifier(key_field);
    let mut parameters = Vec::with_capacity(candidates.len() * 4);
    let mut rows = Vec::with_capacity(candidates.len());
    for (ordinal, candidate) in candidates.iter().enumerate() {
        let position = parameters.len() + 1;
        rows.push(format!(
            "({ordinal},${position},${},${},${})",
            position + 1,
            position + 2,
            position + 3
        ));
        parameters.extend([
            candidate.key.datum(),
            candidate.revision.into(),
            candidate.incarnation.into(),
            candidate.fingerprint.as_str().into(),
        ]);
    }
    let query = format!(
        "WITH candidate(ordinal,key,revision,incarnation,fingerprint) AS (VALUES {}) \
         SELECT pg_catalog.jsonb_build_object('isolation',pg_catalog.current_setting('transaction_isolation'), \
         'rows',pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('ordinal',c.ordinal,'status', \
         CASE WHEN t.{key_name} IS NULL THEN 'missing' \
         WHEN t.revision OPERATOR(pg_catalog.<) 1 OR pg_catalog.octet_length(t.fingerprint) OPERATOR(pg_catalog.<>) 64 \
           OR NOT (t.fingerprint OPERATOR(pg_catalog.~) '^[0-9a-f]{{64}}$') THEN 'invalid_fixture' \
         WHEN t.revision OPERATOR(pg_catalog.=) c.revision AND t.incarnation OPERATOR(pg_catalog.=) c.incarnation \
           AND t.fingerprint OPERATOR(pg_catalog.=) c.fingerprint THEN 'matched' ELSE 'stale' END) ORDER BY c.ordinal)) \
         FROM candidate c LEFT JOIN ONLY {source_name} t ON t.{key_name} OPERATOR(pg_catalog.=) c.key",
        rows.join(",")
    );
    let selected = readonly_json(&query, &parameters);
    if selected["rows"].as_array().map(Vec::len) != Some(candidates.len()) {
        reject(
            Code::ERRCODE_INTERNAL_ERROR,
            "P0 source key did not yield one verdict per candidate",
            "No partial candidate result is returned.",
        );
    }
    let result = json!({
        "schema_version": 1, "kind": "p0_source_candidate_recheck", "source_oid": source.oid().to_u32(),
        "candidate_count": candidates.len(), "rows": selected["rows"],
        "isolation": selected["isolation"], "snapshot_scope": "active_statement_snapshot",
        "spi_read_only": true, "source_select_count": 1,
        "namespace_guard": "conditional_pg_namespace_ShareLock_P0_only",
        "source_identity_allocation": false, "production_authorization": false,
        "rls_supported": false, "outbox_implemented": false, "engine_mvcc_top_k": false,
        "release_supported": false
    });
    if serde_json::to_vec(&result).unwrap().len() > MAX_RESULT_BYTES {
        reject(
            Code::ERRCODE_CONFIGURATION_LIMIT_EXCEEDED,
            "P0 source result budget exceeded",
            "Result JSON is limited to 8 KiB.",
        );
    }
    // Both guards remain live until after the final source SELECT and result copy.
    JsonB(result)
}

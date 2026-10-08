//! P0 tagged source-key encoding only; no source lookup or point-ID allocation.

use pgrx::{JsonB, PgSqlErrorCode, Uuid};
use serde::{Deserialize, Serialize};
use serde_json::json;

pub const MAX_KEY_BYTES: usize = 1024;
const MAX_IDENTITY_WIRE_BYTES: usize = 8192;

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(
    tag = "type",
    content = "value",
    rename_all = "lowercase",
    deny_unknown_fields
)]
pub enum TaggedKey {
    Bigint(String),
    Uuid(String),
    Text(String),
}

pub fn reject(code: PgSqlErrorCode, message: &str, detail: &str) -> ! {
    pgrx::ereport!(ERROR, code, message.to_owned(), detail.to_owned());
}

pub fn required<T>(value: Option<T>, field: &str) -> T {
    value.unwrap_or_else(|| {
        reject(
            PgSqlErrorCode::ERRCODE_NULL_VALUE_NOT_ALLOWED,
            "P0 identity input must not be NULL",
            field,
        )
    })
}

pub fn bounded_text(value: &str) {
    if value.len() > MAX_KEY_BYTES {
        reject(
            PgSqlErrorCode::ERRCODE_CONFIGURATION_LIMIT_EXCEEDED,
            "P0 source key exceeds the byte budget",
            "Text keys are limited to 1024 UTF-8 bytes; no normalization or truncation is performed.",
        );
    }
}

pub fn roundtrip(bigint_key: Option<i64>, uuid_key: Option<Uuid>, text_key: Option<&str>) -> JsonB {
    let integer = required(bigint_key, "bigint_key");
    let uuid = required(uuid_key, "uuid_key");
    let text = required(text_key, "text_key");
    bounded_text(text);
    let original = vec![
        TaggedKey::Bigint(integer.to_string()),
        TaggedKey::Uuid(uuid.to_string()),
        TaggedKey::Text(text.to_owned()),
    ];
    // Input sizes bound even worst-case JSON escaping below 8 KiB; verify the
    // actual encoding too. This does not bound prior PostgreSQL input allocation.
    let wire = serde_json::to_vec(&original).unwrap_or_else(|_| {
        reject(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "P0 key serialization failed",
            "No source operation was performed.",
        )
    });
    if wire.len() > MAX_IDENTITY_WIRE_BYTES {
        reject(
            PgSqlErrorCode::ERRCODE_CONFIGURATION_LIMIT_EXCEEDED,
            "P0 key wire budget exceeded",
            "No source operation was performed.",
        );
    }
    let decoded: Vec<TaggedKey> = serde_json::from_slice(&wire).unwrap_or_else(|_| {
        reject(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "P0 key decoding failed",
            "No source operation was performed.",
        )
    });
    if decoded != original {
        reject(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "P0 tagged key roundtrip changed values",
            "No source operation was performed.",
        );
    }
    JsonB(json!({
        "schema_version": 1, "kind": "p0_tagged_identity_roundtrip",
        "keys": decoded, "uuid_bytes": uuid.as_bytes(),
        "text_utf8_bytes": text.len(), "wire_bytes": wire.len(),
        "limits": {"text_utf8_bytes": MAX_KEY_BYTES, "wire_bytes": MAX_IDENTITY_WIRE_BYTES},
        "roundtrip_verified": true, "normalization_performed": false,
        "point_id_mapping": false, "incarnation_allocation": false,
        "source_recheck": false, "release_supported": false
    }))
}

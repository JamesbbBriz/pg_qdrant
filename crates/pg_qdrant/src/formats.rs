//! Bounded P0 SQL-array conversion. Every PostgreSQL call stays on the backend.
//! This is not a model contract, engine request, or public input API.

use pgrx::{Array, IntoDatum, JsonB, PgSqlErrorCode, direct_function_call, pg_sys};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::io::{self, Write};

const MAX_DENSE_DIM: usize = 4096;
const MAX_SPARSE_NNZ: usize = 4096;
const MAX_TOKEN_ROWS: usize = 128;
const MAX_TOKEN_WIDTH: usize = 1024;
const MAX_TOKEN_VALUES: usize = 16384;
const MAX_OWNED_VALUE_BYTES: usize = 128 * 1024;
const MAX_WIRE_BYTES: usize = 256 * 1024;
const MAX_RESULT_BYTES: usize = 512 * 1024;

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OwnedVectors {
    dense: Vec<f32>,
    sparse_indices: Vec<u32>,
    sparse_weights: Vec<f32>,
    tokens: Vec<Vec<f32>>,
}

fn fail(code: PgSqlErrorCode, field: &str, reason: &str) -> ! {
    pgrx::ereport!(
        ERROR,
        code,
        format!("P0 vector format rejected: {field}"),
        format!("{reason}; see the internal P0 vector-format contract.")
    );
}

fn invalid(field: &str, reason: &str) -> ! {
    fail(
        PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
        field,
        reason,
    )
}

fn budget(field: &str, reason: &str) -> ! {
    fail(
        PgSqlErrorCode::ERRCODE_CONFIGURATION_LIMIT_EXCEEDED,
        field,
        reason,
    )
}

fn required<T>(value: Option<T>, field: &str) -> T {
    value.unwrap_or_else(|| {
        fail(
            PgSqlErrorCode::ERRCODE_NULL_VALUE_NOT_ALLOWED,
            field,
            "SQL NULL is not an input representation",
        )
    })
}

fn cardinality(len: usize, max: usize, field: &str) {
    if len == 0 {
        invalid(field, "empty representations are rejected by this probe");
    }
    if len > max {
        budget(field, "array cardinality exceeds the probe limit");
    }
}

// Called only after bounded elements have been copied while Array is alive.
// into_datum retains its detoasted allocation in the current PG memory context.
// The built-in metadata functions receive the correctly typed, live Datum; no
// PostgreSQL pointer is put into OwnedVectors or sent to another thread/process.
fn shape<T: IntoDatum>(array: Array<'_, T>, dimensions: usize, field: &str) -> Vec<usize> {
    let datum = array.into_datum();
    // SAFETY: `datum` came from a live typed Array. These are public PostgreSQL
    // built-ins with exactly their declared arguments and int4 result type.
    let actual: Option<i32> = unsafe { direct_function_call(pg_sys::array_ndims, &[datum]) };
    if actual != Some(dimensions as i32) {
        invalid(field, "unexpected number of PostgreSQL array dimensions");
    }
    (1..=dimensions)
        .map(|axis| {
            let args = [datum, (axis as i32).into_datum()];
            // SAFETY: same live Array Datum; axis is a validated positive int4.
            let lower: Option<i32> = unsafe { direct_function_call(pg_sys::array_lower, &args) };
            let length: Option<i32> = unsafe { direct_function_call(pg_sys::array_length, &args) };
            if lower != Some(1) {
                invalid(field, "every array lower bound must be one");
            }
            match length {
                Some(length) if length > 0 => length as usize,
                _ => invalid(field, "array dimensions must be nonempty"),
            }
        })
        .collect()
}

fn floats(array: &Array<'_, f32>, max: usize, field: &str) -> Vec<f32> {
    cardinality(array.len(), max, field);
    array
        .iter()
        .map(|value| {
            let value = required(value, field);
            if !value.is_finite() {
                invalid(field, "NaN and positive/negative infinity are rejected");
            }
            value
        })
        .collect()
}

struct BoundedJson {
    bytes: Vec<u8>,
    limit: usize,
    exceeded: bool,
}

impl Write for BoundedJson {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.bytes.len()) {
            self.exceeded = true;
            return Err(io::Error::other("P0 encoded byte limit exceeded"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn encode<T: Serialize>(value: &T, limit: usize, field: &str) -> Vec<u8> {
    let mut output = BoundedJson {
        bytes: Vec::new(),
        limit,
        exceeded: false,
    };
    if serde_json::to_writer(&mut output, value).is_err() {
        if output.exceeded {
            budget(field, "encoded JSON exceeds the declared byte budget");
        }
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            field,
            "serialization failed",
        );
    }
    output.bytes
}

fn equal_bits(left: &OwnedVectors, right: &OwnedVectors) -> bool {
    left.sparse_indices == right.sparse_indices
        && left.dense.len() == right.dense.len()
        && left.sparse_weights.len() == right.sparse_weights.len()
        && left.tokens.len() == right.tokens.len()
        && left
            .tokens
            .iter()
            .zip(&right.tokens)
            .all(|(a, b)| a.len() == b.len())
        && left
            .dense
            .iter()
            .chain(&left.sparse_weights)
            .chain(left.tokens.iter().flatten())
            .zip(
                right
                    .dense
                    .iter()
                    .chain(&right.sparse_weights)
                    .chain(right.tokens.iter().flatten()),
            )
            .all(|(a, b)| a.to_bits() == b.to_bits())
}

pub fn roundtrip(
    dense: Option<Array<'_, f32>>,
    sparse_indices: Option<Array<'_, i64>>,
    sparse_weights: Option<Array<'_, f32>>,
    tokens: Option<Array<'_, f32>>,
) -> JsonB {
    let dense = required(dense, "dense");
    let indices = required(sparse_indices, "sparse_indices");
    let weights = required(sparse_weights, "sparse_weights");
    let tokens = required(tokens, "tokens");

    // Array unboxing/detoasting and SQL expression construction precede this
    // function. These limits bound our owned values, not PostgreSQL input memory.
    let dense_values = floats(&dense, MAX_DENSE_DIM, "dense");
    let dense_shape = shape(dense, 1, "dense");
    cardinality(indices.len(), MAX_SPARSE_NNZ, "sparse_indices");
    let mut sparse_indices = Vec::with_capacity(indices.len());
    for index in indices.iter() {
        let index = required(index, "sparse_indices");
        let index = u32::try_from(index)
            .unwrap_or_else(|_| invalid("sparse_indices", "indices must be within 0..4294967295"));
        if sparse_indices.last().is_some_and(|last| *last >= index) {
            invalid(
                "sparse_indices",
                "indices must be strictly increasing and unique; no sorting is performed",
            );
        }
        sparse_indices.push(index);
    }
    shape(indices, 1, "sparse_indices");
    let sparse_weights = floats(&weights, MAX_SPARSE_NNZ, "sparse_weights");
    shape(weights, 1, "sparse_weights");
    if sparse_indices.len() != sparse_weights.len() {
        invalid("sparse_weights", "index and weight lengths must match");
    }
    let token_values = floats(&tokens, MAX_TOKEN_VALUES, "tokens");
    let token_shape = shape(tokens, 2, "tokens");
    if token_shape[0] > MAX_TOKEN_ROWS || token_shape[1] > MAX_TOKEN_WIDTH {
        budget("tokens", "token row count or width exceeds the probe limit");
    }
    if token_shape[0].checked_mul(token_shape[1]) != Some(token_values.len()) {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "tokens",
            "array shape and copied value count disagree",
        );
    }
    let owned_value_bytes = (dense_values.len() + sparse_weights.len() + token_values.len())
        * size_of::<f32>()
        + sparse_indices.len() * size_of::<u32>();
    if owned_value_bytes > MAX_OWNED_VALUE_BYTES {
        budget("vectors", "owned scalar bytes exceed the probe limit");
    }
    let owned = OwnedVectors {
        dense: dense_values,
        sparse_indices,
        sparse_weights,
        tokens: token_values
            .chunks_exact(token_shape[1])
            .map(<[f32]>::to_vec)
            .collect(),
    };
    let encoded = encode(&owned, MAX_WIRE_BYTES, "wire");
    let decoded: OwnedVectors = serde_json::from_slice(&encoded).unwrap_or_else(|_| {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "wire",
            "owned payload decode failed",
        )
    });
    if !equal_bits(&owned, &decoded) {
        fail(
            PgSqlErrorCode::ERRCODE_INTERNAL_ERROR,
            "wire",
            "float32 bits or array shape changed during roundtrip",
        );
    }
    // Numeric JSONB normalizes negative zero. Emit a small explicit bit sample
    // from the owned float32 decode; never infer those bits from JSONB numbers.
    let dense_bits_prefix: Vec<u32> = decoded.dense.iter().take(8).map(|v| v.to_bits()).collect();
    let result = json!({
        "schema_version": 1, "kind": "p0_candidate_vector_formats",
        "shape": {"dense": dense_shape, "sparse_nnz": decoded.sparse_indices.len(), "tokens": token_shape},
        "values": decoded, "float32_bitwise_roundtrip": true,
        "float32_bitwise_roundtrip_scope": "owned_json_before_postgresql_jsonb",
        "internal_dense_f32_bits_prefix": dense_bits_prefix,
        "owned_scalar_bytes": owned_value_bytes, "wire_bytes": encoded.len(),
        "limits": {"dense_dimensions": MAX_DENSE_DIM, "sparse_nnz": MAX_SPARSE_NNZ,
            "token_rows": MAX_TOKEN_ROWS, "token_width": MAX_TOKEN_WIDTH,
            "token_values": MAX_TOKEN_VALUES, "owned_scalar_bytes": MAX_OWNED_VALUE_BYTES,
            "wire_bytes": MAX_WIRE_BYTES, "result_json_bytes": MAX_RESULT_BYTES},
        "engine_executed": false, "ipc_executed": false,
        "model_contract_verified": false, "public_api_frozen": false,
        "postgres_input_memory_bounded": false, "release_supported": false
    });
    encode(&result, MAX_RESULT_BYTES, "result");
    JsonB(result)
}

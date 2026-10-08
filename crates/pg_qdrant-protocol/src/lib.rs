//! Pure owned values shared across the P0 PostgreSQL and exec boundaries.
//! This crate has no PostgreSQL dependency and performs no PostgreSQL calls.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::io;

pub const VERSION: u32 = 1;
pub const REQUEST_BYTES: usize = 16 * 1024;
pub const RESPONSE_BYTES: usize = 1024 * 1024;
pub const QUEUE_LIMIT: usize = 8;
pub const CONNECTION_LIMIT: usize = 16;
pub const MAX_TIMEOUT_MS: i32 = 120_000;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum Operation {
    Ping,
    Engine,
    Delay { delay_ms: u64 },
    Panic,
    Abort,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub protocol_version: u32,
    pub timeout_ms: u64,
    pub operation: Operation,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProbeError {
    pub code: String,
    pub message: String,
    pub detail: String,
}

impl ProbeError {
    pub fn new(code: &str, message: impl Into<String>, detail: &str) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            detail: detail.into(),
        }
    }

    pub fn invalid(message: &str) -> Self {
        Self::new(
            "invalid_parameter",
            message,
            "Use the documented P0 diagnostic parameter bounds.",
        )
    }

    pub fn timeout() -> Self {
        Self::new(
            "timeout",
            "pg_qdrant P0 request deadline exceeded",
            "The caller stops waiting. A native Edge call already running retains its owner and may continue until completion.",
        )
    }

    pub fn io(error: io::Error) -> Self {
        Self::new(
            "worker_unavailable",
            format!("pg_qdrant P0 worker transport: {error}"),
            "Check PostgreSQL worker logs and call qdrant_internal.p0_start_worker in this database.",
        )
    }
}

pub fn encode_response(result: Result<Value, ProbeError>) -> Vec<u8> {
    let envelope = match result {
        Ok(value) => json!({"protocol_version": VERSION, "result": value}),
        Err(error) => json!({"protocol_version": VERSION, "error": error}),
    };
    let mut bytes = serde_json::to_vec(&envelope).expect("JSON values serialize");
    if bytes.len() + 1 > RESPONSE_BYTES {
        bytes = serde_json::to_vec(&json!({"protocol_version": VERSION,
            "error": ProbeError::new("response_limit", "P0 response exceeded its byte budget", "Reduce the probe result payload.")})).expect("JSON values serialize");
    }
    bytes.push(b'\n');
    bytes
}

/// The helper control stream has its own monotonically increasing identity.
#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HelperRequest {
    pub protocol_version: u32,
    pub request_id: u64,
    pub operation: Operation,
}

pub fn encode_helper_response(
    request_id: u64,
    process_id: u32,
    result: Result<Value, ProbeError>,
) -> Vec<u8> {
    let mut envelope: Value =
        serde_json::from_slice(&encode_response(result)).expect("valid envelope");
    envelope["request_id"] = json!(request_id);
    envelope["engine_pid"] = json!(process_id);
    let mut bytes = serde_json::to_vec(&envelope).expect("JSON values serialize");
    if bytes.len() + 1 > RESPONSE_BYTES {
        return encode_helper_response(
            request_id,
            process_id,
            Err(ProbeError::invalid("helper response exceeds byte budget")),
        );
    }
    bytes.push(b'\n');
    bytes
}

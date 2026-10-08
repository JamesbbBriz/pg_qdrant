//! Pure owned values shared across the P0 PostgreSQL and exec boundaries.
//! This crate has no PostgreSQL dependency and performs no PostgreSQL calls.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::io;

pub mod advanced;
pub mod budgets;

pub const VERSION: u32 = 1;
pub const REQUEST_BYTES: usize = 16 * 1024;
pub const RESPONSE_BYTES: usize = 1024 * 1024;
pub const QUEUE_LIMIT: usize = 8;
pub const CONNECTION_LIMIT: usize = 16;
pub const MAX_TIMEOUT_MS: i32 = 120_000;
pub const CONSUMER_REQUEST_BYTES: usize = 512 * 1024;
pub const SEARCH_REQUEST_BYTES: usize = 128 * 1024;
pub const SOURCE_CONTRACT_VERSION: u32 = 4;

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceFusion {
    Rrf,
    Dbsf,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct DenseContract {
    pub kind: String,
    pub model_id: String,
    pub model_version: String,
    pub tokenizer: String,
    pub dimensions: usize,
    pub distance: String,
    pub normalization: String,
    pub storage_precision: String,
    pub vector_field: String,
    pub fingerprint_field: String,
    pub incarnation_field: String,
    pub model_id_field: String,
    pub model_version_field: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DenseQuery {
    pub representation: String,
    pub model_id: String,
    pub model_version: String,
    pub vector: Vec<f32>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceEvent {
    pub event_id: u64,
    pub point_id: u64,
    pub revision: u64,
    pub incarnation: String,
    pub fingerprint: Option<String>,
    pub key: Value,
    pub body: Option<String>,
    pub vectors: BTreeMap<String, Vec<f32>>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceBatch {
    pub source_contract_version: u32,
    pub index_id: u64,
    pub generation: String,
    pub storage_epoch: String,
    pub consumer_id: String,
    #[serde(default)]
    pub task_id: Option<String>,
    #[serde(default)]
    pub retire: bool,
    pub events: Vec<SourceEvent>,
    pub representations: BTreeMap<String, DenseContract>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
pub enum Operation {
    Ping,
    Engine,
    Delay {
        delay_ms: u64,
    },
    Panic,
    Abort,
    Oom,
    SourceApply {
        batch: SourceBatch,
    },
    SourceSearch {
        index_id: u64,
        generation: String,
        storage_epoch: String,
        q: String,
        top_k: usize,
        #[serde(default)]
        dense_query: Option<DenseQuery>,
        #[serde(default)]
        fusion: Option<SourceFusion>,
    },
}

impl Operation {
    pub fn request_byte_limit(&self) -> usize {
        match self {
            Self::SourceApply { .. } => CONSUMER_REQUEST_BYTES,
            Self::SourceSearch { .. } => SEARCH_REQUEST_BYTES,
            _ => REQUEST_BYTES,
        }
    }
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

#[cfg(test)]
mod search_contract_tests {
    use super::*;

    #[test]
    fn old_text_request_remains_text_and_named_fusion_is_typed() {
        let mut value = json!({"operation":"source_search","index_id":1,
            "generation":"g","storage_epoch":"e","q":"word","top_k":10});
        let operation: Operation = serde_json::from_value(value.clone()).unwrap();
        assert!(matches!(
            operation,
            Operation::SourceSearch {
                dense_query: None,
                fusion: None,
                ..
            }
        ));
        value["fusion"] = json!("rrf");
        let operation: Operation = serde_json::from_value(value.clone()).unwrap();
        assert!(matches!(
            operation,
            Operation::SourceSearch {
                fusion: Some(SourceFusion::Rrf),
                ..
            }
        ));
        value["fusion"] = json!("dbsf");
        assert!(matches!(
            serde_json::from_value::<Operation>(value).unwrap(),
            Operation::SourceSearch {
                fusion: Some(SourceFusion::Dbsf),
                ..
            }
        ));
    }

    #[test]
    fn unknown_fusion_cannot_be_silently_ignored() {
        let value = json!({"operation":"source_search","index_id":1,
            "generation":"g","storage_epoch":"e","q":"word","top_k":10,"fusion":"sum"});
        assert!(serde_json::from_value::<Operation>(value).is_err());
    }
}

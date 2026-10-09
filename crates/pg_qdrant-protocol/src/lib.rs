//! Pure owned values shared across the P0 PostgreSQL and exec boundaries.
//! This crate has no PostgreSQL dependency and performs no PostgreSQL calls.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::io;

pub mod advanced;
pub mod budgets;
pub mod formula;
pub mod payload_filter;

pub const VERSION: u32 = 1;
pub const REQUEST_BYTES: usize = 16 * 1024;
pub const RESPONSE_BYTES: usize = 1024 * 1024;
pub const QUEUE_LIMIT: usize = 8;
pub const CONNECTION_LIMIT: usize = 16;
pub const MAX_TIMEOUT_MS: i32 = 120_000;
pub const CONSUMER_REQUEST_BYTES: usize = 512 * 1024;
pub const SEARCH_REQUEST_BYTES: usize = 128 * 1024;
pub const SOURCE_CONTRACT_VERSION: u32 = 21;
/// Linux virtual address space, including mmap. This is not an RSS quota.
pub const HELPER_ADDRESS_SPACE_BYTES: u64 = 8 * 1024 * 1024 * 1024;
pub const HELPER_MIN_ADDRESS_SPACE_BYTES: u64 = 512 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceFusion {
    Rrf,
    Dbsf,
}

/// Fixed owned payload paths; callers cannot supply arbitrary native filters.
#[derive(Clone, Debug, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourcePredicates {
    pub all: Option<String>,
    pub any: Option<String>,
    pub exclude: Option<String>,
    pub phrase: Option<String>,
    pub token_prefix: Option<String>,
    pub key_exact: Option<String>,
    pub key_prefix: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub payload_filter: Option<payload_filter::PayloadFilter>,
}

impl SourcePredicates {
    pub fn validate(&self) -> Result<(), ProbeError> {
        let mut total = 0;
        for value in [
            &self.all,
            &self.any,
            &self.exclude,
            &self.phrase,
            &self.token_prefix,
            &self.key_exact,
            &self.key_prefix,
        ]
        .into_iter()
        .flatten()
        {
            total += value.len();
            if value.is_empty() || value.len() > 2048 {
                return Err(ProbeError::invalid(
                    "matching strings require 1..2048 bytes",
                ));
            }
        }
        if total > 8192
            || [&self.key_exact, &self.key_prefix]
                .into_iter()
                .flatten()
                .any(|s| s.len() > 1024)
        {
            return Err(ProbeError::invalid("matching/key byte budget exceeded"));
        }
        if self.token_prefix.as_ref().is_some_and(|s| {
            !(2..=32).contains(&s.chars().count()) || !s.chars().all(char::is_alphanumeric)
        }) {
            return Err(ProbeError::invalid(
                "token_prefix requires one alphanumeric token of 2..32 characters",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceMatrix {
    pub representation: String,
    pub sample_size: usize,
    pub neighbors: usize,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceGroups {
    pub field: String,
    pub q: String,
    pub groups: usize,
    pub group_size: usize,
    pub representation_query: Option<RepresentationQuery>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RepresentationContract {
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub vocabulary: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub idf_policy: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub idf_revision: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_tokens: Option<usize>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comparator: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SparseValues {
    pub indices: Vec<u32>,
    pub values: Vec<f32>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(untagged)]
pub enum RepresentationVector {
    Dense(Vec<f32>),
    Sparse(SparseValues),
    Tokens(Vec<Vec<f32>>),
}

impl<'de> Deserialize<'de> for RepresentationVector {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;
        // Struct deserialization also accepts sequences. With an untagged enum,
        // a two-row integer matrix could become SparseValues(indices, values).
        // Choose the representation by JSON shape before decoding its values.
        if value.is_object() {
            return serde_json::from_value(value)
                .map(Self::Sparse)
                .map_err(serde::de::Error::custom);
        }
        if let Some(array) = value.as_array() {
            if array.first().is_some_and(Value::is_array) {
                return serde_json::from_value(value)
                    .map(Self::Tokens)
                    .map_err(serde::de::Error::custom);
            }
            return serde_json::from_value(value)
                .map(Self::Dense)
                .map_err(serde::de::Error::custom);
        }
        Err(serde::de::Error::custom(
            "vector must be an array, matrix or sparse object",
        ))
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RepresentationQuery {
    pub representation: String,
    pub model_id: String,
    pub model_version: String,
    pub vector: RepresentationVector,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecommendationStrategy {
    BestScore,
    SumScores,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceRecommendation {
    pub representation: String,
    pub model_id: String,
    pub model_version: String,
    pub strategy: RecommendationStrategy,
    pub positive: Vec<Vec<f32>>,
    pub negative: Vec<Vec<f32>>,
    pub exclude_ids: Vec<u64>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DiscoveryStrategy {
    Discover,
    Context,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceContextPair {
    pub positive: Vec<f32>,
    pub negative: Vec<f32>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceDiscovery {
    pub representation: String,
    pub model_id: String,
    pub model_version: String,
    pub strategy: DiscoveryStrategy,
    pub target: Option<Vec<f32>>,
    pub context: Vec<SourceContextPair>,
    pub exclude_ids: Vec<u64>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FeedbackCoefficients {
    pub a: f32,
    pub b: f32,
    pub c: f32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceFeedbackItem {
    pub vector: Vec<f32>,
    pub score: f32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceFeedback {
    pub representation: String,
    pub model_id: String,
    pub model_version: String,
    pub target: Vec<f32>,
    pub feedback: Vec<SourceFeedbackItem>,
    pub coefficients: FeedbackCoefficients,
    pub exclude_ids: Vec<u64>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceMmr {
    pub representation: String,
    pub model_id: String,
    pub model_version: String,
    pub target: Vec<f32>,
    pub lambda: f32,
    pub exclude_id: u64,
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
    pub payload: Option<BTreeMap<String, Value>>,
    pub payload_fingerprint: Option<String>,
    pub vectors: BTreeMap<String, RepresentationVector>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceLexical {
    pub q: String,
    pub kind: String,
    pub slop: u32,
    pub top_k: usize,
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
    pub representations: BTreeMap<String, RepresentationContract>,
    pub payload_contract: BTreeMap<String, PayloadKind>,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PayloadKind {
    Keyword,
    Integer,
    Float,
    Bool,
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
    AddressSpaceProbe,
    SourceApply {
        batch: SourceBatch,
    },
    SourceRetrieve {
        index_id: u64,
        generation: String,
        storage_epoch: String,
        point_ids: Vec<u64>,
    },
    SourceStatistics {
        index_id: u64,
        generation: String,
        storage_epoch: String,
        point_ids: Vec<u64>,
        facet: Option<String>,
        facet_limit: usize,
        #[serde(default)]
        matrix: Option<SourceMatrix>,
        #[serde(default)]
        groups: Option<SourceGroups>,
        #[serde(default)]
        lexical: Option<SourceLexical>,
        #[serde(default)]
        predicates: SourcePredicates,
    },
    SourceSearch {
        index_id: u64,
        generation: String,
        storage_epoch: String,
        q: String,
        top_k: usize,
        #[serde(default)]
        representation_query: Option<RepresentationQuery>,
        #[serde(default)]
        additional_recall: Vec<RepresentationQuery>,
        #[serde(default)]
        recommendation_query: Option<SourceRecommendation>,
        #[serde(default)]
        discovery_query: Option<SourceDiscovery>,
        #[serde(default)]
        feedback_query: Option<SourceFeedback>,
        #[serde(default)]
        mmr_query: Option<SourceMmr>,
        #[serde(default)]
        formula: Option<formula::ScoreFormula>,
        #[serde(default)]
        rerank_query: Option<RepresentationQuery>,
        #[serde(default)]
        fusion: Option<SourceFusion>,
        #[serde(default)]
        predicates: SourcePredicates,
    },
}

impl Operation {
    pub fn request_byte_limit(&self) -> usize {
        match self {
            Self::SourceApply { .. } => CONSUMER_REQUEST_BYTES,
            Self::SourceSearch { .. }
            | Self::SourceRetrieve { .. }
            | Self::SourceStatistics { .. } => SEARCH_REQUEST_BYTES,
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
    fn retrieve_preserves_integer_ids_and_refuses_unowned_options() {
        let value = json!({"operation":"source_retrieve","index_id":1,"generation":"g","storage_epoch":"e","point_ids":[7,3,1]});
        let operation: Operation = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(operation.request_byte_limit(), SEARCH_REQUEST_BYTES);
        assert!(
            matches!(operation, Operation::SourceRetrieve { point_ids, .. } if point_ids == vec![7,3,1])
        );
        for replacement in [json!([-1]), json!([1.5]), json!(["1"]), json!([null])] {
            let mut invalid = value.clone();
            invalid["point_ids"] = replacement;
            assert!(serde_json::from_value::<Operation>(invalid).is_err());
        }
        for name in ["with_vectors", "with_payload", "filter", "path", "q"] {
            let mut invalid = value.clone();
            invalid[name] = json!(true);
            assert!(serde_json::from_value::<Operation>(invalid).is_err());
        }
    }

    #[test]
    fn mmr_source_target_and_lambda_refuse_unknown_fields() {
        let input = json!({"representation":"dense","model_id":"fixture","model_version":"r1","target":[1,0],"lambda":0.25,"exclude_id":3});
        assert!(serde_json::from_value::<SourceMmr>(input.clone()).is_ok());
        for (field, value) in [
            ("target", json!("raw-id")),
            ("lambda", json!("NaN")),
            ("tenant_id", json!("caller")),
        ] {
            let mut invalid = input.clone();
            invalid[field] = value;
            assert!(serde_json::from_value::<SourceMmr>(invalid).is_err());
        }
    }

    #[test]
    fn feedback_vectors_scores_and_coefficients_refuse_unknown_fields() {
        let input = json!({"representation":"dense","model_id":"fixture","model_version":"r1",
            "target":[1,0],"feedback":[{"vector":[0,1],"score":2}],
            "coefficients":{"a":1,"b":2,"c":0.25},"exclude_ids":[1,2]});
        assert!(serde_json::from_value::<SourceFeedback>(input.clone()).is_ok());
        for (field, value) in [
            ("target", json!("raw-id")),
            (
                "feedback",
                json!([{"vector":[0,1],"score":2,"tenant_id":"caller"}]),
            ),
            (
                "coefficients",
                json!({"a":1,"b":2,"c":0.25,"strategy":"caller"}),
            ),
            ("tenant_id", json!("caller")),
        ] {
            let mut invalid = input.clone();
            invalid[field] = value;
            assert!(serde_json::from_value::<SourceFeedback>(invalid).is_err());
        }
    }

    #[test]
    fn discovery_context_pairs_are_typed_and_refuse_unknown_fields() {
        let input = json!({"representation":"dense","model_id":"fixture","model_version":"r1",
            "strategy":"discover","target":[1,0],"context":[{"positive":[1,0],"negative":[0,1]}],"exclude_ids":[1,2,3]});
        let value = json!({"operation":"source_search","index_id":1,"generation":"g","storage_epoch":"e",
            "q":"","top_k":1,"discovery_query":input});
        assert!(matches!(
            serde_json::from_value::<Operation>(value.clone()).unwrap(),
            Operation::SourceSearch {
                discovery_query: Some(SourceDiscovery {
                    strategy: DiscoveryStrategy::Discover,
                    ..
                }),
                ..
            }
        ));
        for (field, contents) in [
            ("strategy", json!("caller_strategy")),
            ("target", json!("caller-point-id")),
            (
                "context",
                json!([{"positive":[1,0],"negative":[0,1],"tenant_id":"caller"}]),
            ),
            ("tenant_id", json!("caller-domain")),
        ] {
            let mut invalid = value.clone();
            invalid["discovery_query"][field] = contents;
            assert!(serde_json::from_value::<Operation>(invalid).is_err());
        }
    }

    #[test]
    fn recommendation_model_strategy_and_resolved_examples_are_typed() {
        let seed = json!({"representation":"dense","model_id":"fixture","model_version":"r1",
            "strategy":"sum_scores","positive":[[1,0]],"negative":[],"exclude_ids":[1]});
        let value = json!({"operation":"source_search","index_id":1,"generation":"g",
            "storage_epoch":"e","q":"","top_k":1,"recommendation_query":seed});
        assert!(matches!(
            serde_json::from_value::<Operation>(value.clone()).unwrap(),
            Operation::SourceSearch {
                recommendation_query: Some(SourceRecommendation {
                    strategy: RecommendationStrategy::SumScores,
                    ..
                }),
                ..
            }
        ));
        let mut invalid = value.clone();
        invalid["recommendation_query"]["strategy"] = json!("average_vector");
        assert!(serde_json::from_value::<Operation>(invalid).is_err());
        let mut invalid = value.clone();
        invalid["recommendation_query"]["positive"] = json!(["caller-point-id"]);
        assert!(serde_json::from_value::<Operation>(invalid).is_err());
        let mut invalid = value;
        invalid["recommendation_query"]["tenant_id"] = json!("caller-domain");
        assert!(serde_json::from_value::<Operation>(invalid).is_err());
    }

    #[test]
    fn token_matrix_and_reranking_preserve_distinct_shapes() {
        let matrix = json!([[1, 0], [0, 1]]);
        assert!(
            matches!(serde_json::from_value::<RepresentationVector>(matrix.clone()).unwrap(),
            RepresentationVector::Tokens(v) if v.len()==2 && v[0].len()==2)
        );
        let value = json!({"operation":"source_search","index_id":1,"generation":"g",
            "storage_epoch":"e","q":"word","top_k":2,"rerank_query":{
                "representation":"tokens","model_id":"fixture","model_version":"r1","vector":matrix}});
        assert!(matches!(
            serde_json::from_value::<Operation>(value).unwrap(),
            Operation::SourceSearch {
                rerank_query: Some(RepresentationQuery {
                    vector: RepresentationVector::Tokens(_),
                    ..
                }),
                ..
            }
        ));
        assert!(serde_json::from_value::<RepresentationVector>(json!([[1, 0], "wrong"])).is_err());
    }

    #[test]
    fn old_text_request_remains_text_and_named_fusion_is_typed() {
        let mut value = json!({"operation":"source_search","index_id":1,
            "generation":"g","storage_epoch":"e","q":"word","top_k":10});
        let operation: Operation = serde_json::from_value(value.clone()).unwrap();
        assert!(matches!(
            operation,
            Operation::SourceSearch {
                representation_query: None,
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

    #[test]
    fn source_payload_contract_is_typed_and_cannot_be_omitted() {
        let value = json!({"source_contract_version":SOURCE_CONTRACT_VERSION,"index_id":1,
            "generation":"g","storage_epoch":"e","consumer_id":"c","events":[],
            "representations":{},"payload_contract":{"class":"keyword","qty":"integer","price":"float","active":"bool"}});
        let parsed: SourceBatch = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(parsed.payload_contract["qty"], PayloadKind::Integer);
        for kind in ["array", "geo", "text", "uuid", "datetime"] {
            let mut bad = value.clone();
            bad["payload_contract"]["class"] = json!(kind);
            assert!(serde_json::from_value::<SourceBatch>(bad).is_err());
        }
        let mut missing = value.clone();
        missing.as_object_mut().unwrap().remove("payload_contract");
        assert!(serde_json::from_value::<SourceBatch>(missing).is_err());
        let mut extra = value;
        extra["path"] = json!("/tmp");
        assert!(serde_json::from_value::<SourceBatch>(extra).is_err());
    }

    #[test]
    fn predicates_preserve_key_case_and_reject_arbitrary_paths_and_unbounded_prefixes() {
        let good: SourcePredicates = serde_json::from_value(json!({"phrase":"alpha beta",
            "key_exact":"PG-001","token_prefix":"事务"}))
        .unwrap();
        assert!(good.validate().is_ok());
        assert_eq!(good.key_exact.as_deref(), Some("PG-001"));
        assert!(serde_json::from_value::<SourcePredicates>(json!({"body.path":"x"})).is_err());
        for value in [
            json!({"all":""}),
            json!({"key_prefix":"x".repeat(1025)}),
            json!({"token_prefix":"a"}),
            json!({"token_prefix":"a b"}),
            json!({"token_prefix":"a".repeat(33)}),
        ] {
            let parsed: SourcePredicates = serde_json::from_value(value).unwrap();
            assert!(parsed.validate().is_err());
        }
    }

    #[test]
    fn sparse_shape_preserves_numeric_ids_and_rejects_unknown_or_missing_fields() {
        let value = json!({"indices":[0,4294967295u32],"values":[1.0,-2.0]});
        let RepresentationVector::Sparse(parsed) =
            serde_json::from_value::<RepresentationVector>(value.clone()).unwrap()
        else {
            panic!("sparse became dense")
        };
        assert_eq!(parsed.indices, vec![0, u32::MAX]);
        for wrong in [
            json!({"indices":[0],"values":[1],"unknown":true}),
            json!({"values":[1]}),
            json!({"indices":[-1],"values":[1]}),
            json!({"indices":[4294967296u64],"values":[1]}),
        ] {
            assert!(serde_json::from_value::<RepresentationVector>(wrong).is_err());
        }
    }
}

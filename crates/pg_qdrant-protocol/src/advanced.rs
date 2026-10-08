//! Pure, bounded shape admission for advanced request families.
//! Admission does NOT grant source authorization or execute Qdrant queries.

use serde::Deserialize;
use serde_json::Value;
use std::collections::HashSet;

const MAX_EXAMPLES: usize = 32;
const MAX_CANDIDATES: usize = 1_000;
const MAX_MATRIX_CELLS: usize = 100_000;
const MAX_FORMULA_NODES: usize = 64;
const MAX_FORMULA_DEPTH: usize = 8;

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum AdvancedRequest {
    Recommend {
        positive: Vec<u64>,
        negative: Vec<u64>,
        limit: usize,
    },
    Mmr {
        candidates: Vec<u64>,
        lambda: f64,
        limit: usize,
    },
    Formula {
        expression: Formula,
        limit: usize,
    },
    Facet {
        field: String,
        limit: usize,
    },
    Matrix {
        source_ids: Vec<u64>,
        target_ids: Vec<u64>,
    },
    Visual {
        model_name: String,
        vector_dimensions: usize,
        patch_count: usize,
        limit: usize,
    },
}

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Formula {
    Constant { value: f64 },
    Field { name: String },
    Add { args: Vec<Formula> },
    Multiply { args: Vec<Formula> },
}

#[derive(Debug, PartialEq, Eq)]
pub struct Admission {
    pub family: &'static str,
    pub candidate_limit: usize,
    pub needs_source_authorization: bool,
}

fn valid_limit(limit: usize) -> Result<(), String> {
    if !(1..=100).contains(&limit) {
        return Err("result limit must be between 1 and 100".into());
    }
    Ok(())
}

fn unique_ids(ids: &[u64], maximum: usize) -> Result<(), String> {
    if ids.len() > maximum {
        return Err("too many candidate/example IDs".into());
    }
    let unique: HashSet<u64> = ids.iter().copied().collect();
    if unique.len() != ids.len() {
        return Err("duplicate candidate/example IDs".into());
    }
    Ok(())
}

fn validate_formula(node: &Formula, depth: usize, total: &mut usize) -> Result<(), String> {
    *total = total.saturating_add(1);
    if depth > MAX_FORMULA_DEPTH || *total > MAX_FORMULA_NODES {
        return Err("formula depth or node budget exceeded".into());
    }
    match node {
        Formula::Constant { value } if value.is_finite() && value.abs() <= 1_000_000.0 => Ok(()),
        Formula::Constant { .. } => Err("nonfinite or out-of-range constant".into()),
        Formula::Field { name } if ["popularity", "freshness", "distance"].contains(&name.as_str()) => Ok(()),
        Formula::Field { .. } => Err("unknown or unauthorized formula field".into()),
        Formula::Add { args } | Formula::Multiply { args } => {
            if !(2..=8).contains(&args.len()) {
                return Err("formula operator needs 2..8 arguments".into());
            }
            for child in args {
                validate_formula(child, depth + 1, total)?;
            }
            Ok(())
        }
    }
}

pub fn validate(raw: Value) -> Result<Admission, String> {
    // serde rejects fields not named in the tagged request or typed formula.
    let request: AdvancedRequest = serde_json::from_value(raw)
        .map_err(|e| format!("Invalid advanced request: {e}"))?;
    match request {
        AdvancedRequest::Recommend { positive, negative, limit } => {
            valid_limit(limit)?;
            if positive.is_empty() {
                return Err("recommendation needs positive examples".into());
            }
            unique_ids(&positive, MAX_EXAMPLES)?;
            unique_ids(&negative, MAX_EXAMPLES)?;
            let positives: HashSet<u64> = positive.iter().copied().collect();
            if negative.iter().any(|id| positives.contains(id)) {
                return Err("positive/negative example overlap".into());
            }
            Ok(Admission { family: "recommend", candidate_limit: limit,
                needs_source_authorization: true })
        }
        AdvancedRequest::Mmr { candidates, lambda, limit } => {
            valid_limit(limit)?;
            unique_ids(&candidates, MAX_CANDIDATES)?;
            if candidates.len() < limit || !lambda.is_finite() || !(0.0..=1.0).contains(&lambda) {
                return Err("MMR candidate domain/lambda invalid".into());
            }
            Ok(Admission { family: "mmr", candidate_limit: candidates.len(),
                needs_source_authorization: true })
        }
        AdvancedRequest::Formula { expression, limit } => {
            valid_limit(limit)?;
            let mut visited_nodes = 0;
            validate_formula(&expression, 0, &mut visited_nodes)?;
            Ok(Admission { family: "formula", candidate_limit: limit,
                needs_source_authorization: true })
        }
        AdvancedRequest::Facet { field, limit } => {
            valid_limit(limit)?;
            if field.len() > 63 || field.is_empty()
                || !field.as_bytes().iter().all(|b| b.is_ascii_alphanumeric() || *b == b'_') {
                return Err("facet field must be simple ASCII identifier".into());
            }
            Ok(Admission { family: "facet", candidate_limit: limit,
                needs_source_authorization: true })
        }
        AdvancedRequest::Matrix { source_ids, target_ids } => {
            unique_ids(&source_ids, MAX_CANDIDATES)?;
            unique_ids(&target_ids, MAX_CANDIDATES)?;
            let cells = source_ids.len().checked_mul(target_ids.len())
                .ok_or("matrix cell count overflow")?;
            if source_ids.is_empty() || target_ids.is_empty() || cells > MAX_MATRIX_CELLS {
                return Err("matrix cell budget exceeded".into());
            }
            Ok(Admission { family: "matrix", candidate_limit: cells,
                needs_source_authorization: true })
        }
        AdvancedRequest::Visual {
            model_name, vector_dimensions, patch_count, limit,
        } => {
            valid_limit(limit)?;
            if model_name.is_empty() || model_name.len() > 64 ||
                !model_name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') {
                return Err("visual model identifier is invalid".into());
            }
            let cells = vector_dimensions.checked_mul(patch_count)
                .ok_or("visual token matrix overflow")?;
            if !(1..=4096).contains(&vector_dimensions) ||
               !(1..=256).contains(&patch_count) || cells > 1_000_000 {
                return Err("visual token matrix budget exceeded".into());
            }
            Ok(Admission { family: "visual", candidate_limit: limit,
                needs_source_authorization: true })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn recommendation_requires_distinct_authorized_ids() {
        assert!(validate(json!({"kind":"recommend","positive":[1],"negative":[1],
            "limit":10})).is_err());
        assert_eq!(validate(json!({"kind":"recommend","positive":[1],"negative":[2],
            "limit":10})).unwrap().family,"recommend");
    }
    #[test]
    fn unknown_fields_and_unimplemented_stages_are_refused() {
        assert!(validate(json!({"kind":"recommend","positive":[1],"negative":[],
            "limit":5,"silent_override":true})).is_err());
        assert!(validate(json!({"kind":"native_formula","limit":10})).is_err());
    }
    #[test]
    fn formula_is_typed_and_depth_bounded() {
        assert_eq!(validate(json!({"kind":"formula","expression":
           {"op":"add","args":[{"op":"field","name":"popularity"},
           {"op":"constant","value":2.0}]},"limit":10})).unwrap().family,"formula");
        assert!(validate(json!({"kind":"formula","expression":
           {"op":"field","name":"arbitrary_sql"},"limit":10})).is_err());
    }
    #[test]
    fn bounded_facet_matrix_and_visual() {
        assert!(validate(json!({"kind":"facet","field":"unsafe.sql","limit":3})).is_err());
        assert!(validate(json!({"kind":"matrix","source_ids":[],"target_ids":[1]})).is_err());
        assert!(validate(json!({"kind":"visual","model_name":"v","vector_dimensions":4096,
            "patch_count":257,"limit":10})).is_err());
        assert_eq!(validate(json!({"kind":"visual","model_name":"v",
            "vector_dimensions":768,"patch_count":64,"limit":10})).unwrap().family,"visual");
    }
}

//! Bounded Boolean expressions over declared scalar aliases, never native paths.
use crate::{PayloadKind, ProbeError};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(transparent)]
pub struct PayloadFilter(pub Value);

impl PayloadFilter {
    pub fn validate(&self, contract: &BTreeMap<String, PayloadKind>) -> Result<(), ProbeError> {
        if serde_json::to_vec(&self.0)
            .map_err(|_| ProbeError::invalid("filter JSON"))?
            .len()
            > 8192
        {
            return Err(ProbeError::invalid("payload filter exceeds 8 KiB"));
        }
        let mut nodes = 0;
        validate_node(&self.0, contract, 0, &mut nodes)
    }

    /// Compile only validated source expressions into the native filter shape.
    pub fn native(&self, contract: &BTreeMap<String, PayloadKind>) -> Result<Value, ProbeError> {
        self.validate(contract)?;
        Ok(compile(&self.0, contract))
    }

    pub fn matches(&self, projection: &Value) -> bool {
        evaluate(&self.0, projection)
    }
}

fn invalid() -> ProbeError {
    ProbeError::invalid("invalid declared scalar filter")
}

fn validate_node(
    node: &Value,
    contract: &BTreeMap<String, PayloadKind>,
    depth: usize,
    nodes: &mut usize,
) -> Result<(), ProbeError> {
    *nodes += 1;
    if depth > 4 || *nodes > 32 {
        return Err(invalid());
    }
    let map = node.as_object().ok_or_else(invalid)?;
    for op in ["all", "any", "not"] {
        if let Some(value) = map.get(op) {
            if map.len() != 1 {
                return Err(invalid());
            }
            if op == "not" {
                return validate_node(value, contract, depth + 1, nodes);
            }
            let children = value
                .as_array()
                .filter(|v| !v.is_empty() && v.len() <= 16)
                .ok_or_else(invalid)?;
            for child in children {
                validate_node(child, contract, depth + 1, nodes)?;
            }
            return Ok(());
        }
    }
    if map.len() != 2 {
        return Err(invalid());
    }
    let field = map
        .get("field")
        .and_then(Value::as_str)
        .ok_or_else(invalid)?;
    let kind = contract.get(field).ok_or_else(invalid)?;
    if let Some(value) = map.get("eq") {
        if !match kind {
            PayloadKind::Keyword => value.as_str().is_some_and(|s| s.len() <= 1024),
            PayloadKind::Integer => value.as_i64().is_some(),
            PayloadKind::Float => value.as_f64().is_some_and(|n| n.is_finite()),
            PayloadKind::Bool => value.is_boolean(),
        } {
            return Err(invalid());
        }
    } else if let Some(value) = map.get("prefix") {
        if *kind != PayloadKind::Keyword
            || !value
                .as_str()
                .is_some_and(|s| !s.is_empty() && s.len() <= 1024)
        {
            return Err(invalid());
        }
    } else if let Some(value) = map.get("is_null") {
        if value != &json!(true) {
            return Err(invalid());
        }
    } else if let Some(value) = map.get("range") {
        if !matches!(kind, PayloadKind::Float | PayloadKind::Integer) {
            return Err(invalid());
        }
        let bounds = value
            .as_object()
            .filter(|v| !v.is_empty() && v.len() <= 4)
            .ok_or_else(invalid)?;
        for (op, value) in bounds {
            if !["lt", "lte", "gt", "gte"].contains(&op.as_str())
                || !value.as_f64().is_some_and(|v| v.is_finite())
            {
                return Err(invalid());
            }
            // Edge 0.8 exposes float range bounds. Admit integer bounds only
            // when conversion is exact; equality retains the whole int64 domain.
            if *kind == PayloadKind::Integer
                && !value
                    .as_i64()
                    .is_some_and(|v| (-9007199254740992..=9007199254740992).contains(&v))
            {
                return Err(invalid());
            }
        }
    } else {
        return Err(invalid());
    }
    Ok(())
}

fn compile(node: &Value, contract: &BTreeMap<String, PayloadKind>) -> Value {
    for (op, native) in [("all", "must"), ("any", "should")] {
        if let Some(children) = node[op].as_array() {
            let conditions: Vec<_> = children.iter().map(|n| compile(n, contract)).collect();
            return json!({native: conditions});
        }
    }
    if let Some(child) = node.get("not") {
        return json!({"must_not":[compile(child, contract)]});
    }
    let field = node["field"].as_str().expect("validated alias");
    let key = format!("attributes.{field}");
    if let Some(value) = node.get("is_null") {
        return json!({"key":key,"is_null":value});
    }
    if let Some(value) = node.get("range") {
        return json!({"key":key,"range":value});
    }
    if let Some(value) = node.get("prefix") {
        return json!({"key":key,"match":{"prefix":value}});
    }
    let value = &node["eq"];
    if contract[field] == PayloadKind::Float {
        json!({"key":key,"range":{"gte":value,"lte":value}})
    } else {
        json!({"key":key,"match":{"value":value}})
    }
}

fn evaluate(node: &Value, projection: &Value) -> bool {
    if let Some(children) = node["all"].as_array() {
        return children.iter().all(|n| evaluate(n, projection));
    }
    if let Some(children) = node["any"].as_array() {
        return children.iter().any(|n| evaluate(n, projection));
    }
    if let Some(child) = node.get("not") {
        return !evaluate(child, projection);
    }
    let value = &projection[node["field"].as_str().expect("validated alias")];
    if node.get("is_null").is_some() {
        return value.is_null();
    }
    if let Some(expected) = node.get("eq") {
        if let (Some(value), Some(expected)) = (value.as_i64(), expected.as_i64()) {
            return value == expected;
        }
        return value == expected
            || (value.is_number() && expected.is_number() && value.as_f64() == expected.as_f64());
    }
    if let Some(prefix) = node["prefix"].as_str() {
        return value.as_str().is_some_and(|s| s.starts_with(prefix));
    }
    let Some(number) = value.as_f64() else {
        return false;
    };
    node["range"]
        .as_object()
        .expect("validated range")
        .iter()
        .all(|(op, bound)| {
            // Integer comparisons use i128, avoiding float loss near large row values.
            if let (Some(number), Some(bound)) = (value.as_i64(), bound.as_i64()) {
                return compare(number as i128, bound as i128, op);
            }
            compare(number, bound.as_f64().expect("validated bound"), op)
        })
}

fn compare<T: PartialOrd>(value: T, bound: T, op: &str) -> bool {
    match op {
        "lt" => value < bound,
        "lte" => value <= bound,
        "gt" => value > bound,
        "gte" => value >= bound,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn contract() -> BTreeMap<String, PayloadKind> {
        BTreeMap::from([
            ("category".into(), PayloadKind::Keyword),
            ("quantity".into(), PayloadKind::Integer),
            ("price".into(), PayloadKind::Float),
            ("available".into(), PayloadKind::Bool),
        ])
    }
    #[test]
    fn bounded_alias_boolean_filter_has_exact_scalar_semantics() {
        let c = contract();
        let f = PayloadFilter(
            json!({"all":[{"field":"category","prefix":"Ab"},{"any":[{"field":"quantity","eq":i64::MAX},{"field":"price","range":{"gte":1.25,"lt":2}}]},{"not":{"field":"available","eq":false}}]}),
        );
        f.validate(&c).unwrap();
        assert!(
            f.matches(&json!({"category":"Ab-C","quantity":i64::MAX,"price":0,"available":true}))
        );
        assert!(
            !f.matches(&json!({"category":"ab-C","quantity":i64::MAX,"price":0,"available":true}))
        );
        for value in [
            json!({}),
            json!({"all":[]}),
            json!({"field":"private.path","eq":1}),
            json!({"field":"quantity","eq":u64::MAX}),
            json!({"field":"quantity","eq":null}),
            json!({"field":"quantity","range":{"gte":9007199254740993i64}}),
            json!({"field":"price","range":{"other":1}}),
            json!({"field":"category","eq":"x","path":"/tmp"}),
            json!({"field":"available","range":{"gte":0}}),
            json!({"field":"quantity","is_null":false}),
        ] {
            assert!(PayloadFilter(value).validate(&c).is_err());
        }
        let at = PayloadFilter(json!({"field":"quantity","range":{"lte":9007199254740992i64}}));
        at.validate(&c).unwrap();
        assert!(!at.matches(&json!({"quantity":9007199254740993i64})));
        let mut deep = json!({"field":"category","eq":"x"});
        for _ in 0..5 {
            deep = json!({"not":deep});
        }
        assert!(PayloadFilter(deep).validate(&c).is_err());
        let leaf = json!({"field":"category","eq":"x"});
        assert!(
            PayloadFilter(json!({"all":vec![json!({"any":vec![leaf;16]});3]}))
                .validate(&c)
                .is_err()
        );
    }
}

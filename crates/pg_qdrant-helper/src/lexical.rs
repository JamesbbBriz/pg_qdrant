//! One fixed analyzer policy compiled separately for ranking and matching.
use pg_qdrant_protocol::{ProbeError, SourcePredicates};
use qdrant_edge::bm25_embed::{EdgeBm25, EdgeBm25Config};
use qdrant_edge::{
    Condition, CreateIndex, EdgeShard, FieldCondition, FieldIndexOperations, Filter,
    KeywordIndexParams, Match, MatchPhrase, MatchTextAny, PayloadFieldSchema, PayloadSchemaParams,
    TextIndexParams, UpdateOperation, ValueVariants,
};
use serde_json::json;

fn error(e: impl std::fmt::Display) -> ProbeError {
    ProbeError::new(
        "source_analyzer_error",
        e.to_string(),
        "Do not ACK; rebuild the dirty generation.",
    )
}

fn policy() -> serde_json::Value {
    json!({"tokenizer":"multilingual","stemmer":{"type":"none"},"stopwords":{},
        "lowercase":true,"ascii_folding":false})
}

pub fn encoder() -> Result<EdgeBm25, ProbeError> {
    let mut policy = policy();
    policy["avg_len"] = json!(16.0);
    let config: EdgeBm25Config = serde_json::from_value(policy).map_err(error)?;
    EdgeBm25::new(config).map_err(error)
}

pub fn install(shard: &EdgeShard, _fail_after_first_index: bool) -> Result<(), ProbeError> {
    let mut text = policy();
    text["type"] = json!("text");
    text["phrase_matching"] = json!(true);
    let body: TextIndexParams = serde_json::from_value(text.clone()).map_err(error)?;
    text["tokenizer"] = json!("prefix");
    text["phrase_matching"] = json!(false);
    text["min_token_len"] = json!(2);
    text["max_token_len"] = json!(32);
    let prefix: TextIndexParams = serde_json::from_value(text).map_err(error)?;
    let schemas = [
        ("body", PayloadSchemaParams::Text(body)),
        ("body_prefix", PayloadSchemaParams::Text(prefix)),
        (
            "source_key.value",
            PayloadSchemaParams::Keyword(KeywordIndexParams {
                prefix: Some(true),
                ..Default::default()
            }),
        ),
    ];
    for (field, schema) in schemas {
        shard
            .update(UpdateOperation::FieldIndexOperation(
                FieldIndexOperations::CreateIndex(CreateIndex {
                    field_name: field
                        .parse()
                        .map_err(|_| error("invalid constant field path"))?,
                    field_schema: Some(PayloadFieldSchema::FieldParams(schema)),
                }),
            ))
            .map_err(error)?;
        #[cfg(feature = "p0-fault-injection")]
        if field == "body" && _fail_after_first_index {
            return Err(error(
                "injected lexical schema error after body index creation",
            ));
        }
    }
    Ok(())
}

fn condition(field: &str, value: Match) -> Result<Condition, ProbeError> {
    Ok(Condition::Field(FieldCondition::new_match(
        field
            .parse()
            .map_err(|_| error("invalid constant field path"))?,
        value,
    )))
}

pub fn compile(p: &SourcePredicates, encoder: &EdgeBm25) -> Result<Option<Filter>, ProbeError> {
    p.validate()?;
    // Native analyzers may remove punctuation. Reject empty analyzed clauses
    // instead of silently weakening a required condition.
    for value in [&p.all, &p.any, &p.exclude, &p.phrase]
        .into_iter()
        .flatten()
    {
        let tokens = encoder.embed_query(value);
        if tokens.indices.is_empty() || tokens.indices.len() > 128 {
            return Err(ProbeError::invalid(
                "matching clause requires 1..128 analyzed terms",
            ));
        }
    }
    let mut must = Vec::new();
    let mut must_not = Vec::new();
    if let Some(s) = &p.all {
        must.push(condition("body", Match::new_text(s))?);
    }
    if let Some(s) = &p.any {
        must.push(condition(
            "body",
            Match::TextAny(MatchTextAny {
                text_any: s.clone(),
            }),
        )?);
    }
    if let Some(s) = &p.exclude {
        must_not.push(condition(
            "body",
            Match::TextAny(MatchTextAny {
                text_any: s.clone(),
            }),
        )?);
    }
    if let Some(s) = &p.phrase {
        must.push(condition(
            "body",
            Match::Phrase(MatchPhrase { phrase: s.clone() }),
        )?);
    }
    if let Some(s) = &p.token_prefix {
        must.push(condition("body_prefix", Match::new_text(s))?);
    }
    if let Some(s) = &p.key_exact {
        must.push(condition(
            "source_key.value",
            Match::new_value(ValueVariants::String(s.clone())),
        )?);
    }
    if let Some(s) = &p.key_prefix {
        must.push(condition("source_key.value", Match::new_prefix(s))?);
    }
    if must.is_empty() && must_not.is_empty() {
        return Ok(None);
    }
    Ok(Some(Filter {
        must: (!must.is_empty()).then_some(must),
        must_not: (!must_not.is_empty()).then_some(must_not),
        ..Default::default()
    }))
}

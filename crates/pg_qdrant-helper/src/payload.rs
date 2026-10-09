//! Declared scalar attributes and native typed indexes share one owned generation.
use pg_qdrant_protocol::{PayloadKind, ProbeError, SourceEvent};
use qdrant_edge::{
    CreateIndex, EdgeShard, FieldIndexOperations, PayloadFieldSchema, PayloadSchemaType,
    UpdateOperation,
};
use std::collections::BTreeMap;

pub fn validate_contract(contract: &BTreeMap<String, PayloadKind>) -> Result<(), ProbeError> {
    if contract.len() > 8
        || contract.keys().any(|name| {
            name.is_empty()
                || name.len() > 32
                || !name.as_bytes()[0].is_ascii_lowercase()
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
        })
    {
        return Err(ProbeError::invalid("invalid declared payload contract"));
    }
    Ok(())
}

pub fn validate_event(
    event: &SourceEvent,
    contract: &BTreeMap<String, PayloadKind>,
) -> Result<(), ProbeError> {
    if event.body.is_none() {
        if event.payload.is_some() || event.payload_fingerprint.is_some() {
            return Err(ProbeError::invalid("tombstone contains payload"));
        }
        return Ok(());
    }
    let payload = event
        .payload
        .as_ref()
        .ok_or_else(|| ProbeError::invalid("missing source payload"))?;
    if !event.payload_fingerprint.as_ref().is_some_and(|fp| {
        fp.len() == 64
            && fp
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    }) || payload.len() != contract.len()
        || serde_json::to_vec(payload)
            .map_err(|_| ProbeError::invalid("invalid payload JSON"))?
            .len()
            > 8192
    {
        return Err(ProbeError::invalid(
            "invalid payload fingerprint or projection size",
        ));
    }
    for (name, kind) in contract {
        let value = payload
            .get(name)
            .ok_or_else(|| ProbeError::invalid("missing declared payload alias"))?;
        if !value.is_null()
            && !match kind {
                PayloadKind::Keyword => value.as_str().is_some_and(|s| s.len() <= 1024),
                PayloadKind::Integer => value.as_i64().is_some(),
                PayloadKind::Float => value.as_f64().is_some_and(|n| n.is_finite()),
                PayloadKind::Bool => value.as_bool().is_some(),
            }
        {
            return Err(ProbeError::invalid(
                "payload scalar does not match declared kind",
            ));
        }
    }
    Ok(())
}

pub fn install(
    shard: &EdgeShard,
    contract: &BTreeMap<String, PayloadKind>,
) -> Result<(), ProbeError> {
    for (name, kind) in contract {
        let schema = match kind {
            PayloadKind::Keyword => PayloadSchemaType::Keyword,
            PayloadKind::Integer => PayloadSchemaType::Integer,
            PayloadKind::Float => PayloadSchemaType::Float,
            PayloadKind::Bool => PayloadSchemaType::Bool,
        };
        shard
            .update(UpdateOperation::FieldIndexOperation(
                FieldIndexOperations::CreateIndex(CreateIndex {
                    field_name: format!("attributes.{name}")
                        .parse()
                        .map_err(|_| ProbeError::invalid("invalid attribute path"))?,
                    field_schema: Some(PayloadFieldSchema::FieldType(schema)),
                }),
            ))
            .map_err(|e| {
                ProbeError::new(
                    "source_payload_error",
                    e.to_string(),
                    "Do not ACK; preserve the owned dirty generation for retirement or rebuild.",
                )
            })?;
    }
    Ok(())
}

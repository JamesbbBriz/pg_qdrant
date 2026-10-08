//! Embedded Edge mutation owner. No SQL values select a filesystem path.
use pg_qdrant_protocol::{ProbeError, SourceBatch};
use qdrant_edge::bm25_embed::{EdgeBm25, EdgeBm25Config};
use qdrant_edge::{
    EdgeConfig, EdgeShard, EdgeSparseVectorParams, Modifier, NamedQuery, PointId,
    PointInsertOperations, PointOperations, PointStruct, QueryEnum, QueryRequest, ScoringQuery,
    SearchParams, UpdateOperation, Vector, VectorInternal, Vectors, WithPayloadInterface,
};
use serde_json::{Value, json};
use std::{collections::HashMap, path::PathBuf};

pub struct SourceOwner {
    root: PathBuf,
    shards: HashMap<String, EdgeShard>,
    encoder: EdgeBm25,
}

fn error(e: impl std::fmt::Display) -> ProbeError {
    ProbeError::new(
        "source_engine_error",
        e.to_string(),
        "No PostgreSQL ACK is permitted; preserve dirty storage and rebuild.",
    )
}

fn identity(index: u64, generation: &str, epoch: &str) -> Result<String, ProbeError> {
    if index == 0
        || [generation, epoch]
            .iter()
            .any(|s| s.len() != 36 || !s.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-'))
    {
        return Err(ProbeError::invalid("invalid source generation identity"));
    }
    Ok(format!("{index}-{generation}-{epoch}"))
}

impl SourceOwner {
    pub fn new(root: PathBuf) -> Result<Self, ProbeError> {
        let config: EdgeBm25Config = serde_json::from_value(json!({
            "tokenizer":"multilingual","stemmer":{"type":"none"},"stopwords":{},
            "lowercase":true,"ascii_folding":false,"avg_len":16.0
        }))
        .map_err(error)?;
        Ok(Self {
            root,
            shards: HashMap::new(),
            encoder: EdgeBm25::new(config).map_err(error)?,
        })
    }

    pub fn apply(&mut self, batch: SourceBatch) -> Result<Value, ProbeError> {
        let key = identity(batch.index_id, &batch.generation, &batch.storage_epoch)?;
        if batch.events.len() > 16
            || batch.events.iter().any(|e| {
                e.event_id == 0
                    || e.point_id == 0
                    || e.revision == 0
                    || e.body.as_ref().is_some_and(|b| b.len() > 65536)
            })
        {
            return Err(ProbeError::invalid("source batch exceeds bounds"));
        }
        if !self.shards.contains_key(&key) {
            if self.shards.len() >= 32 {
                return Err(ProbeError::invalid("open shard limit reached"));
            }
            let path = self.root.join(&key);
            // Never load an existing path as a clean generation after owner loss.
            if path.exists() {
                return Err(error("storage epoch already exists; rebuild required"));
            }
            std::fs::create_dir_all(&path).map_err(error)?;
            let config = EdgeConfig {
                sparse_vectors: HashMap::from([(
                    "bm25".into(),
                    EdgeSparseVectorParams {
                        modifier: Some(Modifier::Idf),
                        ..Default::default()
                    },
                )]),
                max_search_threads: Some(2),
                ..Default::default()
            };
            self.shards
                .insert(key.clone(), EdgeShard::new(&path, config).map_err(error)?);
        }
        let shard = &self.shards[&key];
        #[cfg(feature = "p0-fault-injection")]
        fault(&self.root, "before_apply");
        for event in &batch.events {
            let operation = if let Some(body) = &event.body {
                let point = PointStruct::new(
                    event.point_id,
                    Vectors::new_named([("bm25", Vector::from(self.encoder.embed_document(body)))]),
                    json!({"source_key":event.key,"revision":event.revision,
                        "incarnation":event.incarnation,"fingerprint":event.fingerprint,"body":body}),
                );
                PointOperations::UpsertPoints(PointInsertOperations::PointsList(vec![point.into()]))
            } else {
                PointOperations::DeletePoints {
                    ids: vec![PointId::NumId(event.point_id)],
                }
            };
            shard
                .update(UpdateOperation::PointOperation(operation))
                .map_err(error)?;
        }
        #[cfg(feature = "p0-fault-injection")]
        fault(&self.root, "before_flush");
        shard.flush().map_err(error)?;
        #[cfg(feature = "p0-fault-injection")]
        fault(&self.root, "after_flush");
        Ok(
            json!({"generation":batch.generation,"storage_epoch":batch.storage_epoch,
            "consumer_id":batch.consumer_id,"event_ids":batch.events.iter().map(|e|e.event_id).collect::<Vec<_>>(),
            "flushed":true}),
        )
    }

    pub fn search(
        &self,
        index_id: u64,
        generation: &str,
        epoch: &str,
        q: &str,
        top_k: usize,
    ) -> Result<Value, ProbeError> {
        if q.is_empty() || q.len() > 8192 || !(1..=1000).contains(&top_k) {
            return Err(ProbeError::invalid("search outside bounds"));
        }
        let key = identity(index_id, generation, epoch)?;
        let shard = self
            .shards
            .get(&key)
            .ok_or_else(|| error("generation not owned by this helper"))?;
        let mut request = QueryRequest::new(top_k);
        request.query = Some(ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
            using: Some("bm25".into()),
            query: VectorInternal::Sparse(self.encoder.embed_query(q)),
        })));
        request.params = Some(SearchParams {
            exact: true,
            indexed_only: false,
            ..Default::default()
        });
        // Source text is returned only through the authorized PostgreSQL JOIN.
        // Fetch identity/version metadata without copying every candidate body
        // into the bounded IPC response (one source row can contain 64 KiB).
        request.with_payload = WithPayloadInterface::Fields(
            ["source_key", "revision", "incarnation", "fingerprint"]
                .map(|field| field.parse().expect("constant payload selector"))
                .to_vec(),
        );
        let hits = shard.query(request).map_err(error)?;
        let mut result = Vec::with_capacity(hits.len());
        for hit in hits {
            let PointId::NumId(id) = hit.id else {
                return Err(error("unexpected point identity"));
            };
            result.push(json!({"id":id,"score":hit.score,"payload":hit.payload.map(|p|p.0)}));
        }
        Ok(Value::Array(result))
    }
}

#[cfg(feature = "p0-fault-injection")]
fn fault(root: &std::path::Path, cut: &str) {
    let marker = root.with_extension("fault");
    if std::fs::read_to_string(&marker).ok().as_deref() == Some(cut) {
        let _ = std::fs::remove_file(marker);
        std::process::abort();
    }
}

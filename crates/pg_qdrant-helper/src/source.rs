//! Embedded Edge mutation owner. No SQL values select a filesystem path.
use pg_qdrant_protocol::{
    DenseContract, DenseQuery, ProbeError, SOURCE_CONTRACT_VERSION, SourceBatch,
};
use qdrant_edge::bm25_embed::{EdgeBm25, EdgeBm25Config};
use qdrant_edge::{
    Distance, EdgeConfig, EdgeShard, EdgeSparseVectorParams, EdgeVectorParams, Modifier,
    NamedQuery, PointId, PointInsertOperations, PointOperations, PointStruct, QueryEnum,
    QueryRequest, ScoringQuery, SearchParams, UpdateOperation, Vector, VectorInternal, Vectors,
    WithPayloadInterface,
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap},
    path::PathBuf,
};

struct SourceShard {
    shard: EdgeShard,
    representations: BTreeMap<String, DenseContract>,
}

pub struct SourceOwner {
    root: PathBuf,
    shards: HashMap<String, SourceShard>,
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
        if batch.source_contract_version != SOURCE_CONTRACT_VERSION
            || batch.representations.len() > 4
            || batch.events.len() > 16
            || batch.events.iter().any(|e| {
                e.event_id == 0
                    || e.point_id == 0
                    || e.revision == 0
                    || e.body.as_ref().is_some_and(|b| b.len() > 65536)
            })
        {
            return Err(ProbeError::invalid("source batch exceeds bounds"));
        }
        let mut dense_config = HashMap::new();
        for (name, contract) in &batch.representations {
            if name == "bm25"
                || name.is_empty()
                || name.len() > 32
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
                || contract.kind != "dense"
                || !(1..=4096).contains(&contract.dimensions)
                || contract.storage_precision != "float32"
                || !["none", "unit"].contains(&contract.normalization.as_str())
            {
                return Err(ProbeError::invalid("unsupported owned dense contract"));
            }
            let distance = match contract.distance.as_str() {
                "dot" => Distance::Dot,
                "cosine" => Distance::Cosine,
                "euclid" => Distance::Euclid,
                "manhattan" => Distance::Manhattan,
                _ => return Err(ProbeError::invalid("unsupported owned dense distance")),
            };
            dense_config.insert(
                name.clone(),
                EdgeVectorParams::builder(contract.dimensions, distance).build(),
            );
        }
        for event in &batch.events {
            for (name, vector) in &event.vectors {
                let contract = batch
                    .representations
                    .get(name)
                    .ok_or_else(|| ProbeError::invalid("undeclared named vector"))?;
                validate_dense(vector, contract)?;
            }
            if event.body.is_none() && !event.vectors.is_empty() {
                return Err(ProbeError::invalid("tombstone contains vectors"));
            }
        }
        if self
            .shards
            .get(&key)
            .is_some_and(|s| s.representations != batch.representations)
        {
            return Err(error("representation contract changed within a generation"));
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
                vectors: dense_config,
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
            self.shards.insert(
                key.clone(),
                SourceShard {
                    shard: EdgeShard::new(&path, config).map_err(error)?,
                    representations: batch.representations.clone(),
                },
            );
        }
        let shard = &self.shards[&key].shard;
        #[cfg(feature = "p0-fault-injection")]
        fault(&self.root, "before_apply");
        for event in &batch.events {
            let operation = if let Some(body) = &event.body {
                // Replace the complete owned vector set. A partial named-vector
                // upsert must never leave a stale representation searchable.
                shard
                    .update(UpdateOperation::PointOperation(
                        PointOperations::DeletePoints {
                            ids: vec![PointId::NumId(event.point_id)],
                        },
                    ))
                    .map_err(error)?;
                #[cfg(feature = "p0-fault-injection")]
                fault(&self.root, "after_point_delete");
                let mut vectors = vec![(
                    "bm25".to_owned(),
                    Vector::from(self.encoder.embed_document(body)),
                )];
                vectors.extend(
                    event
                        .vectors
                        .iter()
                        .map(|(name, vector)| (name.clone(), Vector::new_dense(vector.clone()))),
                );
                let point = PointStruct::new(
                    event.point_id,
                    Vectors::new_named(vectors),
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
            json!({"source_contract_version":SOURCE_CONTRACT_VERSION,"generation":batch.generation,"storage_epoch":batch.storage_epoch,
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
        dense_query: Option<DenseQuery>,
    ) -> Result<Value, ProbeError> {
        if q.is_empty() || q.len() > 8192 || !(1..=1000).contains(&top_k) {
            return Err(ProbeError::invalid("search outside bounds"));
        }
        let key = identity(index_id, generation, epoch)?;
        let owned = self
            .shards
            .get(&key)
            .ok_or_else(|| error("generation not owned by this helper"))?;
        let shard = &owned.shard;
        let mut request = QueryRequest::new(top_k);
        let (using, query) = if let Some(dense) = dense_query {
            let contract = owned
                .representations
                .get(&dense.representation)
                .ok_or_else(|| ProbeError::invalid("dense slot is not owned by this generation"))?;
            if dense.model_id != contract.model_id || dense.model_version != contract.model_version
            {
                return Err(ProbeError::invalid(
                    "query model does not match the generation",
                ));
            }
            validate_dense(&dense.vector, contract)?;
            (dense.representation, VectorInternal::Dense(dense.vector))
        } else {
            (
                "bm25".to_owned(),
                VectorInternal::Sparse(self.encoder.embed_query(q)),
            )
        };
        request.query = Some(ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
            using: Some(using),
            query,
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

fn validate_dense(vector: &[f32], contract: &DenseContract) -> Result<(), ProbeError> {
    if vector.len() != contract.dimensions || vector.iter().any(|value| !value.is_finite()) {
        return Err(ProbeError::invalid(
            "dense vector violates dimensions or finite-value contract",
        ));
    }
    let norm: f64 = vector.iter().map(|value| f64::from(*value).powi(2)).sum();
    if (contract.normalization == "unit" && (norm - 1.0).abs() > 0.0001)
        || (contract.distance == "cosine" && norm == 0.0)
    {
        return Err(ProbeError::invalid(
            "dense vector violates normalization contract",
        ));
    }
    Ok(())
}

#[cfg(feature = "p0-fault-injection")]
fn fault(root: &std::path::Path, cut: &str) {
    let marker = root.with_extension("fault");
    if std::fs::read_to_string(&marker).ok().as_deref() == Some(cut) {
        let _ = std::fs::remove_file(marker);
        std::process::abort();
    }
}

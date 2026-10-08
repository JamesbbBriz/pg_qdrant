//! Embedded Edge mutation owner. No SQL values select a filesystem path.
use pg_qdrant_protocol::{
    ProbeError, RepresentationContract, RepresentationQuery, RepresentationVector,
    SOURCE_CONTRACT_VERSION, SourceBatch, SourceFusion, SourcePredicates,
};
use qdrant_edge::bm25_embed::EdgeBm25;
use qdrant_edge::{
    Distance, EdgeConfig, EdgeShard, EdgeSparseVectorParams, EdgeVectorParams, Filter, Fusion,
    IdfCorpusParams, IdfParams, Modifier, MultiVectorComparator, MultiVectorConfig, NamedQuery,
    PointId, PointInsertOperations, PointOperations, PointStruct, Prefetch, QueryEnum,
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
    representations: BTreeMap<String, RepresentationContract>,
    lexical_ready: bool,
}

pub struct SourceOwner {
    root: PathBuf,
    shards: HashMap<String, SourceShard>,
    encoder: EdgeBm25,
    retired: HashMap<String, (String, Option<String>)>,
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
        Ok(Self {
            root,
            shards: HashMap::new(),
            encoder: crate::lexical::encoder()?,
            retired: HashMap::new(),
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
        if batch.retire {
            if !batch.events.is_empty()
                || !batch.representations.is_empty()
                || batch.task_id.is_none()
            {
                return Err(ProbeError::invalid("retirement contains mutation data"));
            }
            if let Some(receipt) = self.retired.get(&key) {
                if receipt != &(batch.consumer_id.clone(), batch.task_id.clone()) {
                    return Err(error("retirement receipt identity mismatch"));
                }
                return Ok(
                    json!({"source_contract_version":SOURCE_CONTRACT_VERSION,"generation":batch.generation,
                    "storage_epoch":batch.storage_epoch,"consumer_id":batch.consumer_id,"task_id":batch.task_id,
                    "event_ids":[],"flushed":true,"retired":true}),
                );
            }
            if self.retired.len() >= 256 {
                return Err(error("retirement receipt cache is full; restart required"));
            }
            let owned = self
                .shards
                .get(&key)
                .ok_or_else(|| error("retired generation is not owned"))?;
            owned.shard.flush().map_err(error)?;
            let path = self.root.join(&key);
            let metadata = std::fs::symlink_metadata(&path).map_err(error)?;
            if metadata.file_type().is_symlink()
                || !metadata.is_dir()
                || path.canonicalize().map_err(error)?.parent()
                    != Some(self.root.canonicalize().map_err(error)?.as_path())
            {
                return Err(error("retirement path is outside the owned storage root"));
            }
            // A single native owner serializes this after all admitted native
            // queries. PostgreSQL switches only after source binding pins end.
            drop(self.shards.remove(&key));
            std::fs::remove_dir_all(&path).map_err(error)?;
            self.retired
                .insert(key, (batch.consumer_id.clone(), batch.task_id.clone()));
            return Ok(
                json!({"source_contract_version":SOURCE_CONTRACT_VERSION,"generation":batch.generation,
                "storage_epoch":batch.storage_epoch,"consumer_id":batch.consumer_id,"task_id":batch.task_id,
                "event_ids":[],"flushed":true,"retired":true}),
            );
        }
        if self.retired.contains_key(&key) {
            return Err(error("retired storage epoch cannot receive mutations"));
        }
        let mut dense_config = HashMap::new();
        let mut sparse_config = HashMap::from([(
            "bm25".to_owned(),
            EdgeSparseVectorParams {
                modifier: Some(Modifier::Idf),
                ..Default::default()
            },
        )]);
        for (name, contract) in &batch.representations {
            if name == "bm25"
                || name.is_empty()
                || name.len() > 32
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
                || contract.storage_precision != "float32"
                || !["none", "unit"].contains(&contract.normalization.as_str())
            {
                return Err(ProbeError::invalid(
                    "unsupported owned representation contract",
                ));
            }
            if contract.kind == "learned_sparse" {
                if contract.dimensions == 0
                    || contract.max_tokens.is_some()
                    || contract.comparator.is_some()
                    || contract.dimensions > u32::MAX as usize + 1
                    || contract.distance != "dot"
                    || contract.normalization != "none"
                    || contract
                        .vocabulary
                        .as_ref()
                        .is_none_or(|v| v.is_empty() || v.len() > 256)
                    || contract
                        .idf_revision
                        .as_ref()
                        .is_none_or(|v| v.is_empty() || v.len() > 256)
                    || !matches!(
                        contract.idf_policy.as_deref(),
                        Some("none" | "external" | "engine")
                    )
                    || (contract.idf_policy.as_deref() == Some("engine")
                        && contract.idf_revision.as_deref() != Some("qdrant-edge:0.8.0"))
                {
                    return Err(ProbeError::invalid("unsupported learned sparse contract"));
                }
                sparse_config.insert(
                    name.clone(),
                    EdgeSparseVectorParams {
                        modifier: (contract.idf_policy.as_deref() == Some("engine"))
                            .then_some(Modifier::Idf),
                        ..Default::default()
                    },
                );
                continue;
            }
            if !["dense", "token_vectors"].contains(&contract.kind.as_str())
                || !(1..=4096).contains(&contract.dimensions)
                || contract.vocabulary.is_some()
                || contract.idf_policy.is_some()
                || contract.idf_revision.is_some()
            {
                return Err(ProbeError::invalid("unsupported dense contract"));
            }
            if contract.kind == "token_vectors" {
                if !contract.max_tokens.is_some_and(|n| (1..=128).contains(&n))
                    || contract.comparator.as_deref() != Some("maxsim")
                    || contract.distance != "dot"
                {
                    return Err(ProbeError::invalid("unsupported token-vector contract"));
                }
            } else if contract.max_tokens.is_some() || contract.comparator.is_some() {
                return Err(ProbeError::invalid("token options on a dense slot"));
            }
            let distance = match contract.distance.as_str() {
                "dot" => Distance::Dot,
                "cosine" => Distance::Cosine,
                "euclid" => Distance::Euclid,
                "manhattan" => Distance::Manhattan,
                _ => return Err(ProbeError::invalid("unsupported owned dense distance")),
            };
            let mut params = EdgeVectorParams::builder(contract.dimensions, distance).build();
            if contract.kind == "token_vectors" {
                params.multivector_config = Some(MultiVectorConfig {
                    comparator: MultiVectorComparator::MaxSim,
                });
            }
            dense_config.insert(name.clone(), params);
        }
        for event in &batch.events {
            for (name, vector) in &event.vectors {
                let contract = batch
                    .representations
                    .get(name)
                    .ok_or_else(|| ProbeError::invalid("undeclared named vector"))?;
                validate_representation(vector, contract)?;
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
                sparse_vectors: sparse_config,
                max_search_threads: Some(2),
                ..Default::default()
            };
            self.shards.insert(
                key.clone(),
                SourceShard {
                    shard: EdgeShard::new(&path, config).map_err(error)?,
                    representations: batch.representations.clone(),
                    lexical_ready: false,
                },
            );
            // Register native ownership before fallible schema creation so a
            // failed build remains eligible for exact same-owner retirement.
        }
        if !self.shards[&key].lexical_ready {
            #[cfg(not(feature = "p0-fault-injection"))]
            let fail_schema = false;
            #[cfg(feature = "p0-fault-injection")]
            let fail_schema = batch.task_id.is_some()
                && take_fault_marker(&self.root, "lexical_schema_error", batch.index_id, true);
            crate::lexical::install(&self.shards[&key].shard, fail_schema)?;
            self.shards
                .get_mut(&key)
                .expect("owned shard")
                .lexical_ready = true;
        }
        let shard = &self.shards[&key].shard;
        #[cfg(feature = "p0-fault-injection")]
        fault(
            &self.root,
            "before_apply",
            batch.index_id,
            batch.task_id.is_some(),
        );
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
                fault(
                    &self.root,
                    "after_point_delete",
                    batch.index_id,
                    batch.task_id.is_some(),
                );
                let mut vectors = vec![(
                    "bm25".to_owned(),
                    Vector::from(self.encoder.embed_document(body)),
                )];
                for (name, vector) in &event.vectors {
                    vectors.push((name.clone(), native_vector(vector)?));
                }
                let point = PointStruct::new(
                    event.point_id,
                    Vectors::new_named(vectors),
                    json!({"source_key":event.key,"revision":event.revision,
                        "incarnation":event.incarnation,"fingerprint":event.fingerprint,"body":body,"body_prefix":body}),
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
        if batch.task_id.is_some()
            && take_fault_marker(&self.root, "shadow_apply_error", batch.index_id, true)
        {
            return Err(error(
                "injected shadow error after native apply before flush",
            ));
        }
        #[cfg(feature = "p0-fault-injection")]
        fault(
            &self.root,
            "before_flush",
            batch.index_id,
            batch.task_id.is_some(),
        );
        shard.flush().map_err(error)?;
        #[cfg(feature = "p0-fault-injection")]
        fault(
            &self.root,
            "after_flush",
            batch.index_id,
            batch.task_id.is_some(),
        );
        Ok(
            json!({"source_contract_version":SOURCE_CONTRACT_VERSION,"generation":batch.generation,"storage_epoch":batch.storage_epoch,
            "consumer_id":batch.consumer_id,"task_id":batch.task_id,"event_ids":batch.events.iter().map(|e|e.event_id).collect::<Vec<_>>(),
            "flushed":true,"retired":false}),
        )
    }

    pub fn search(
        &self,
        index_id: u64,
        generation: &str,
        epoch: &str,
        q: &str,
        top_k: usize,
        representation_query: Option<RepresentationQuery>,
        rerank_query: Option<RepresentationQuery>,
        fusion: Option<SourceFusion>,
        predicates: SourcePredicates,
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
        if !owned.lexical_ready {
            return Err(error("owned lexical indexes are not ready"));
        }
        if fusion.is_some() && representation_query.is_none() {
            return Err(ProbeError::invalid(
                "hybrid fusion requires a named representation query",
            ));
        }
        let mut request = QueryRequest::new(top_k);
        let filter = crate::lexical::compile(&predicates, &self.encoder)?;
        request.filter = filter.clone();
        let (using, query) = if let Some(dense) = representation_query {
            let contract = owned
                .representations
                .get(&dense.representation)
                .ok_or_else(|| {
                    ProbeError::invalid("representation slot is not owned by this generation")
                })?;
            if dense.model_id != contract.model_id || dense.model_version != contract.model_version
            {
                return Err(ProbeError::invalid(
                    "query model does not match the generation",
                ));
            }
            validate_representation(&dense.vector, contract)?;
            if let RepresentationVector::Tokens(tokens) = &dense.vector {
                if fusion.is_some() || rerank_query.is_some() {
                    return Err(ProbeError::invalid(
                        "token recall cannot be fused or reranked again",
                    ));
                }
                admit_maxsim(
                    tokens.len(),
                    contract,
                    shard.info().map_err(error)?.points_count,
                )?;
            }
            (dense.representation, native_vector(&dense.vector)?.into())
        } else {
            (
                "bm25".to_owned(),
                VectorInternal::Sparse(self.encoder.embed_query(q)),
            )
        };
        let uses_idf = using == "bm25"
            || owned.representations.get(&using).is_some_and(|c| {
                c.kind == "learned_sparse" && c.idf_policy.as_deref() == Some("engine")
            });
        let scoring = ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
            using: Some(using),
            query,
        }));
        if let Some(fusion) = fusion {
            let bm25 = ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
                using: Some("bm25".to_owned()),
                query: VectorInternal::Sparse(self.encoder.embed_query(q)),
            }));
            request.prefetches = [bm25, scoring]
                .into_iter()
                .enumerate()
                .map(|(branch, query)| {
                    let mut stage = Prefetch::new(top_k);
                    stage.query = Some(query);
                    stage.filter = filter.clone();
                    stage.params = Some(SearchParams {
                        exact: true,
                        indexed_only: false,
                        idf: (branch == 0 || uses_idf).then(|| {
                            IdfParams::Corpus(IdfCorpusParams {
                                corpus: Filter::default(),
                            })
                        }),
                        ..Default::default()
                    });
                    stage
                })
                .collect();
            request.query = Some(ScoringQuery::Fusion(match fusion {
                SourceFusion::Rrf => Fusion::Rrf {
                    k: 2,
                    weights: None,
                },
                SourceFusion::Dbsf => Fusion::Dbsf,
            }));
        } else {
            request.query = Some(scoring);
        }
        request.params = Some(SearchParams {
            exact: true,
            indexed_only: false,
            // Global posting counts include deleted offsets until optimize.
            // Corpus statistics resolve live points, so source replacement,
            // replay and rebuild do not change IDF through tombstone history.
            idf: (fusion.is_none() && uses_idf).then(|| {
                IdfParams::Corpus(IdfCorpusParams {
                    corpus: Filter::default(),
                })
            }),
            ..Default::default()
        });
        if let Some(rerank) = rerank_query {
            let contract = owned
                .representations
                .get(&rerank.representation)
                .ok_or_else(|| ProbeError::invalid("reranking slot is not owned"))?;
            if contract.kind != "token_vectors"
                || rerank.model_id != contract.model_id
                || rerank.model_version != contract.model_version
            {
                return Err(ProbeError::invalid(
                    "reranking requires the declared token model",
                ));
            }
            validate_representation(&rerank.vector, contract)?;
            let RepresentationVector::Tokens(tokens) = &rerank.vector else {
                return Err(ProbeError::invalid(
                    "reranking query must contain token vectors",
                ));
            };
            admit_maxsim(tokens.len(), contract, top_k)?;
            let mut candidates = Prefetch::new(top_k);
            candidates.query = request.query.take();
            candidates.prefetches = std::mem::take(&mut request.prefetches);
            candidates.params = request.params.take();
            candidates.filter = filter;
            request.prefetches = vec![candidates];
            request.query = Some(ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
                using: Some(rerank.representation),
                query: native_vector(&rerank.vector)?.into(),
            })));
            request.params = Some(SearchParams {
                exact: true,
                indexed_only: false,
                ..Default::default()
            });
        }
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

// Bound the dot-product scalar work before entering a native exact MaxSim query.
// Standalone searches use the owner's live point count, not PostgreSQL's
// potentially lagging source count. Prefetch reranking uses its candidate cap.
fn admit_maxsim(
    query_tokens: usize,
    contract: &RepresentationContract,
    candidates: usize,
) -> Result<(), ProbeError> {
    let work = query_tokens
        .checked_mul(contract.max_tokens.unwrap_or(0))
        .and_then(|n| n.checked_mul(contract.dimensions))
        .and_then(|n| n.checked_mul(candidates));
    if work.is_none_or(|n| n > 20_000_000) {
        return Err(ProbeError::invalid(
            "MaxSim scalar-work budget exceeds 20000000",
        ));
    }
    Ok(())
}

fn native_vector(vector: &RepresentationVector) -> Result<Vector, ProbeError> {
    match vector {
        RepresentationVector::Dense(values) => Ok(Vector::new_dense(values.clone())),
        RepresentationVector::Sparse(values) => {
            Vector::new_sparse(values.indices.clone(), values.values.clone()).map_err(error)
        }
        RepresentationVector::Tokens(values) => Vector::new_multi(values.clone()).map_err(error),
    }
}

fn validate_representation(
    vector: &RepresentationVector,
    contract: &RepresentationContract,
) -> Result<(), ProbeError> {
    if let RepresentationVector::Tokens(tokens) = vector {
        if contract.kind != "token_vectors"
            || tokens.is_empty()
            || tokens.len() > contract.max_tokens.unwrap_or(0)
        {
            return Err(ProbeError::invalid(
                "token count or representation kind invalid",
            ));
        }
        for token in tokens {
            validate_dense_values(token, contract)?;
        }
        return Ok(());
    }
    let RepresentationVector::Dense(vector) = vector else {
        let RepresentationVector::Sparse(sparse) = vector else {
            unreachable!()
        };
        if contract.kind != "learned_sparse"
            || sparse.indices.len() > 2048
            || sparse.indices.len() != sparse.values.len()
            || sparse.indices.windows(2).any(|w| w[0] >= w[1])
            || sparse
                .indices
                .iter()
                .any(|i| *i as usize >= contract.dimensions)
            || sparse.values.iter().any(|v| !v.is_finite() || *v == 0.0)
        {
            return Err(ProbeError::invalid(
                "sparse vocabulary, ordering, finite nonzero weights or nonzero count invalid",
            ));
        }
        return Ok(());
    };
    if contract.kind != "dense" {
        return Err(ProbeError::invalid(
            "vector kind differs from model contract",
        ));
    }
    validate_dense_values(vector, contract)
}

fn validate_dense_values(
    vector: &[f32],
    contract: &RepresentationContract,
) -> Result<(), ProbeError> {
    if vector.len() != contract.dimensions || vector.iter().any(|value| !value.is_finite()) {
        return Err(ProbeError::invalid(
            "dense vector violates dimensions or finite-value contract",
        ));
    }
    let norm: f64 = vector.iter().map(|value| f64::from(*value).powi(2)).sum();
    if contract.kind == "token_vectors"
        && norm > f64::from(f32::MAX) / (2.0 * contract.max_tokens.unwrap_or(0) as f64)
    {
        return Err(ProbeError::invalid(
            "token norm can overflow finite MaxSim scoring",
        ));
    }
    if (contract.normalization == "unit" && (norm - 1.0).abs() > 0.0001)
        || (contract.distance == "cosine" && norm == 0.0)
    {
        return Err(ProbeError::invalid(
            "dense vector violates normalization contract",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use pg_qdrant_protocol::SourceEvent;

    #[test]
    fn maxsim_work_cap_rejects_overflow_and_large_exact_corpora() {
        let contract: RepresentationContract = serde_json::from_value(json!({
            "kind":"token_vectors","model_id":"fixture","model_version":"r1","tokenizer":"fixture",
            "dimensions":512,"distance":"dot","normalization":"none","storage_precision":"float32",
            "vector_field":"v","fingerprint_field":"f","incarnation_field":"i",
            "model_id_field":"m","model_version_field":"r","max_tokens":128,"comparator":"maxsim"
        }))
        .unwrap();
        assert!(admit_maxsim(128, &contract, 1).is_ok());
        assert!(admit_maxsim(128, &contract, 3).is_err());
        assert!(admit_maxsim(usize::MAX, &contract, usize::MAX).is_err());
    }

    #[test]
    fn retirement_receipt_replays_without_reopening_or_mutating_the_epoch() {
        let root = std::env::temp_dir().join(format!(
            "pgq-retirement-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&root).unwrap();
        let mut owner = SourceOwner::new(root.clone()).unwrap();
        let generation = "00000000-0000-0000-0000-000000000001".to_owned();
        let epoch = "00000000-0000-0000-0000-000000000002".to_owned();
        let batch = SourceBatch {
            source_contract_version: SOURCE_CONTRACT_VERSION,
            index_id: 1,
            generation: generation.clone(),
            storage_epoch: epoch.clone(),
            consumer_id: epoch.clone(),
            task_id: None,
            retire: false,
            representations: BTreeMap::new(),
            events: vec![SourceEvent {
                event_id: 1,
                point_id: 1,
                revision: 1,
                incarnation: generation.clone(),
                fingerprint: Some("fixture".into()),
                key: json!({"type":"bigint","value":"1"}),
                body: Some("retired searchable point".into()),
                vectors: BTreeMap::new(),
            }],
        };
        #[cfg(feature = "p0-fault-injection")]
        {
            let failed = SourceBatch {
                storage_epoch: "00000000-0000-0000-0000-000000000003".into(),
                task_id: Some(generation.clone()),
                ..batch.clone()
            };
            std::fs::write(
                root.with_extension("fault"),
                "shadow:1:lexical_schema_error",
            )
            .unwrap();
            assert!(owner.apply(failed.clone()).is_err());
            let key = identity(1, &generation, &failed.storage_epoch).unwrap();
            assert!(!owner.shards[&key].lexical_ready);
            assert!(
                owner
                    .search(
                        1,
                        &generation,
                        &failed.storage_epoch,
                        "retired",
                        10,
                        None,
                        None,
                        None,
                        SourcePredicates::default()
                    )
                    .is_err()
            );
            let retirement = SourceBatch {
                retire: true,
                events: vec![],
                ..failed
            };
            assert_eq!(owner.apply(retirement).unwrap()["retired"], true);
            assert!(!root.join(key).exists());
        }
        owner.apply(batch.clone()).unwrap();
        let path = root.join(identity(1, &generation, &epoch).unwrap());
        assert!(path.is_dir());
        let retirement = SourceBatch {
            events: vec![],
            retire: true,
            task_id: Some(generation.clone()),
            ..batch.clone()
        };
        for _ in 0..2 {
            assert_eq!(owner.apply(retirement.clone()).unwrap()["retired"], true);
            assert!(!path.exists());
        }
        for wrong in [
            SourceBatch {
                consumer_id: generation.clone(),
                ..retirement.clone()
            },
            SourceBatch {
                task_id: Some(epoch.clone()),
                ..retirement.clone()
            },
        ] {
            assert!(owner.apply(wrong).is_err());
            assert!(!path.exists());
        }
        assert!(owner.apply(batch).is_err());
        assert!(
            owner
                .search(
                    1,
                    &generation,
                    &epoch,
                    "retired",
                    10,
                    None,
                    None,
                    None,
                    SourcePredicates::default()
                )
                .is_err()
        );
        drop(owner);
        std::fs::remove_dir(&root).unwrap();
    }
}

#[cfg(feature = "p0-fault-injection")]
fn fault(root: &std::path::Path, cut: &str, index: u64, shadow: bool) {
    if take_fault_marker(root, cut, index, shadow) {
        std::process::abort();
    }
}

#[cfg(feature = "p0-fault-injection")]
fn take_fault_marker(root: &std::path::Path, cut: &str, index: u64, shadow: bool) -> bool {
    let marker = root.with_extension("fault");
    let armed = std::fs::read_to_string(&marker).ok();
    if armed.as_deref() == Some(cut)
        || (shadow && armed.as_deref() == Some(format!("shadow:{index}:{cut}").as_str()))
    {
        let _ = std::fs::remove_file(marker);
        return true;
    }
    false
}

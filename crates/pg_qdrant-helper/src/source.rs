//! Embedded Edge mutation owner. No SQL values select a filesystem path.
use pg_qdrant_protocol::{
    DiscoveryStrategy, ProbeError, RecommendationStrategy, RepresentationContract,
    RepresentationQuery, RepresentationVector, SOURCE_CONTRACT_VERSION, SourceBatch,
    SourceDiscovery, SourceFusion, SourcePredicates, SourceRecommendation,
};
use qdrant_edge::bm25_embed::EdgeBm25;
use qdrant_edge::{
    ContextPair, ContextQuery, DiscoverQuery, Distance, EdgeConfig, EdgeShard,
    EdgeSparseVectorParams, EdgeVectorParams, Filter, Fusion, IdfCorpusParams, IdfParams, Modifier,
    MultiVectorComparator, MultiVectorConfig, NamedQuery, PointId, PointInsertOperations,
    PointOperations, PointStruct, Prefetch, QueryEnum, QueryRequest, RecommendQuery, ScoringQuery,
    SearchParams, UpdateOperation, Vector, VectorInternal, Vectors, WithPayloadInterface,
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
        recommendation_query: Option<SourceRecommendation>,
        discovery_query: Option<SourceDiscovery>,
        rerank_query: Option<RepresentationQuery>,
        fusion: Option<SourceFusion>,
        predicates: SourcePredicates,
    ) -> Result<Value, ProbeError> {
        if (recommendation_query.is_none() && discovery_query.is_none() && q.is_empty())
            || q.len() > 8192
            || !(1..=1000).contains(&top_k)
        {
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
        let mut filter = crate::lexical::compile(&predicates, &self.encoder)?;
        request.filter = filter.clone();
        let (scoring, uses_idf) = if let Some(discovery) = discovery_query {
            if recommendation_query.is_some()
                || representation_query.is_some()
                || rerank_query.is_some()
                || fusion.is_some()
            {
                return Err(ProbeError::invalid(
                    "discovery cannot be combined, fused or reranked",
                ));
            }
            let contract = owned
                .representations
                .get(&discovery.representation)
                .ok_or_else(|| ProbeError::invalid("discovery slot is not owned"))?;
            let target_required = matches!(discovery.strategy, DiscoveryStrategy::Discover);
            let count = discovery
                .context
                .len()
                .saturating_mul(2)
                .saturating_add(usize::from(discovery.target.is_some()));
            if contract.kind != "dense"
                || discovery.model_id != contract.model_id
                || discovery.model_version != contract.model_version
                || discovery.target.is_some() != target_required
                || discovery.context.is_empty()
                || count > 32
                || discovery.exclude_ids.len() != count
                || discovery.exclude_ids.iter().any(|id| *id == 0)
                || discovery
                    .exclude_ids
                    .iter()
                    .collect::<std::collections::BTreeSet<_>>()
                    .len()
                    != count
            {
                return Err(ProbeError::invalid(
                    "invalid discovery target, context or model contract",
                ));
            }
            admit_example_work(
                count,
                contract.dimensions,
                shard.info().map_err(error)?.points_count,
            )?;
            for vector in discovery.target.iter().chain(
                discovery
                    .context
                    .iter()
                    .flat_map(|pair| [&pair.positive, &pair.negative]),
            ) {
                validate_representation(&RepresentationVector::Dense(vector.clone()), contract)?;
            }
            let exclusion: Filter =
                serde_json::from_value(json!({"must_not":[{"has_id":discovery.exclude_ids}]}))
                    .map_err(error)?;
            filter
                .get_or_insert_with(Filter::default)
                .must_not
                .get_or_insert_with(Vec::new)
                .extend(exclusion.must_not.unwrap());
            request.filter = filter.clone();
            let pairs = discovery
                .context
                .into_iter()
                .map(|pair| ContextPair {
                    positive: VectorInternal::Dense(pair.positive),
                    negative: VectorInternal::Dense(pair.negative),
                })
                .collect();
            let using = Some(discovery.representation);
            let query = match discovery.strategy {
                DiscoveryStrategy::Discover => QueryEnum::Discover(NamedQuery {
                    using,
                    query: DiscoverQuery::new(
                        VectorInternal::Dense(discovery.target.unwrap()),
                        pairs,
                    ),
                }),
                DiscoveryStrategy::Context => QueryEnum::Context(NamedQuery {
                    using,
                    query: ContextQuery::new(pairs),
                }),
            };
            (ScoringQuery::Vector(query), false)
        } else if let Some(recommendation) = recommendation_query {
            if representation_query.is_some() || rerank_query.is_some() || fusion.is_some() {
                return Err(ProbeError::invalid(
                    "recommendation cannot be fused or reranked",
                ));
            }
            let contract = owned
                .representations
                .get(&recommendation.representation)
                .ok_or_else(|| ProbeError::invalid("recommendation slot is not owned"))?;
            let count = recommendation
                .positive
                .len()
                .saturating_add(recommendation.negative.len());
            if contract.kind != "dense"
                || recommendation.model_id != contract.model_id
                || recommendation.model_version != contract.model_version
                || recommendation.positive.is_empty()
                || count > 32
                || recommendation.exclude_ids.len() != count
                || recommendation.exclude_ids.iter().any(|id| *id == 0)
                || recommendation
                    .exclude_ids
                    .iter()
                    .collect::<std::collections::BTreeSet<_>>()
                    .len()
                    != count
            {
                return Err(ProbeError::invalid(
                    "invalid recommendation examples or model contract",
                ));
            }
            admit_example_work(
                count,
                contract.dimensions,
                shard.info().map_err(error)?.points_count,
            )?;
            for vector in recommendation
                .positive
                .iter()
                .chain(&recommendation.negative)
            {
                validate_representation(&RepresentationVector::Dense(vector.clone()), contract)?;
            }
            let exclusion: Filter =
                serde_json::from_value(json!({"must_not":[{"has_id":recommendation.exclude_ids}]}))
                    .map_err(error)?;
            let combined = filter.get_or_insert_with(Filter::default);
            combined
                .must_not
                .get_or_insert_with(Vec::new)
                .extend(exclusion.must_not.unwrap());
            request.filter = filter.clone();
            let query = NamedQuery {
                using: Some(recommendation.representation),
                query: RecommendQuery::new(
                    recommendation
                        .positive
                        .into_iter()
                        .map(VectorInternal::Dense)
                        .collect(),
                    recommendation
                        .negative
                        .into_iter()
                        .map(VectorInternal::Dense)
                        .collect(),
                ),
            };
            let query = match recommendation.strategy {
                RecommendationStrategy::BestScore => QueryEnum::RecommendBestScore(query),
                RecommendationStrategy::SumScores => QueryEnum::RecommendSumScores(query),
            };
            (ScoringQuery::Vector(query), false)
        } else {
            let (using, query) = if let Some(dense) = representation_query {
                let contract = owned
                    .representations
                    .get(&dense.representation)
                    .ok_or_else(|| {
                        ProbeError::invalid("representation slot is not owned by this generation")
                    })?;
                if dense.model_id != contract.model_id
                    || dense.model_version != contract.model_version
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
            (scoring, uses_idf)
        };
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
            if !hit.score.is_finite() {
                return Err(ProbeError::new(
                    "source_engine_error",
                    "native query produced a non-finite score",
                    "No search results are returned; inspect the model contract and rebuild uncertain storage.",
                ));
            }
            let PointId::NumId(id) = hit.id else {
                return Err(error("unexpected point identity"));
            };
            result.push(json!({"id":id,"score":hit.score,"payload":hit.payload.map(|p|p.0)}));
        }
        Ok(Value::Array(result))
    }
}

// Recommendation exact work uses the owned corpus before applying filters.
fn admit_example_work(
    examples: usize,
    dimensions: usize,
    owned_points: usize,
) -> Result<(), ProbeError> {
    let work = examples
        .saturating_mul(dimensions)
        .saturating_mul(owned_points);
    if work > 20_000_000 {
        return Err(ProbeError::invalid(
            "source-example scalar-work budget exceeds 20000000",
        ));
    }
    Ok(())
}

// Standalone MaxSim uses the owner's live corpus; prefetch uses its candidate cap.
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
        let norm: f64 = sparse
            .values
            .iter()
            .map(|value| f64::from(*value).powi(2))
            .sum();
        if norm > 1e14 {
            return Err(ProbeError::invalid(
                "sparse squared norm exceeds native score budget 100000000000000",
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
    // Leave headroom for squared differences and the pinned DBSF f32 variance
    // over at most 1000 candidates. Finite input components alone are insufficient.
    if contract.kind == "dense" && norm > 1e16 {
        return Err(ProbeError::invalid(
            "dense squared norm exceeds native score budget 10000000000000000",
        ));
    }
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

    #[test]
    fn recommendation_work_uses_owned_corpus_and_cannot_overflow() {
        assert!(admit_example_work(32, 125, 5000).is_ok());
        assert!(admit_example_work(32, 125, 5001).is_err());
        assert!(admit_example_work(32, 4096, 153).is_err());
        assert!(admit_example_work(32, 4096, usize::MAX).is_err());
    }
    use pg_qdrant_protocol::{SourceEvent, SparseValues};

    fn numeric_contract() -> RepresentationContract {
        serde_json::from_value(json!({
            "kind":"dense","model_id":"fixture","model_version":"r1","tokenizer":"fixture",
            "dimensions":2,"distance":"dot","normalization":"none","storage_precision":"float32",
            "vector_field":"v","fingerprint_field":"f","incarnation_field":"i",
            "model_id_field":"m","model_version_field":"r"
        }))
        .unwrap()
    }

    #[test]
    fn score_budgets_cover_joint_component_norms_and_all_dense_distances() {
        let mut contract = numeric_contract();
        for distance in ["dot", "cosine", "euclid", "manhattan"] {
            contract.distance = distance.into();
            assert!(validate_dense_values(&[1e8, 0.0], &contract).is_ok());
            assert!(validate_dense_values(&[1e8, 1e8], &contract).is_err());
            assert!(validate_dense_values(&[1e30, 0.0], &contract).is_err());
        }
        contract.kind = "learned_sparse".into();
        contract.dimensions = 100;
        for values in [vec![1e7], vec![-1e7]] {
            assert!(
                validate_representation(
                    &RepresentationVector::Sparse(SparseValues {
                        indices: vec![7],
                        values
                    }),
                    &contract
                )
                .is_ok()
            );
        }
        for values in [vec![1e7, 1e7], vec![1e30, 1.0]] {
            assert!(
                validate_representation(
                    &RepresentationVector::Sparse(SparseValues {
                        indices: vec![7, 8],
                        values
                    }),
                    &contract
                )
                .is_err()
            );
        }
    }

    #[test]
    fn out_of_contract_native_storage_cannot_serialize_infinite_scores_as_null() {
        let root = std::env::temp_dir().join(format!(
            "pgq-numeric-{}-{}",
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
        let contract = numeric_contract();
        let batch = SourceBatch {
            source_contract_version: SOURCE_CONTRACT_VERSION,
            index_id: 1,
            generation: generation.clone(),
            storage_epoch: epoch.clone(),
            consumer_id: epoch.clone(),
            task_id: None,
            retire: false,
            representations: BTreeMap::from([("dense".into(), contract.clone())]),
            events: vec![SourceEvent {
                event_id: 1,
                point_id: 1,
                revision: 1,
                incarnation: generation.clone(),
                fingerprint: Some("fixture".into()),
                key: json!({"type":"bigint","value":"1"}),
                body: Some("anchor".into()),
                vectors: BTreeMap::from([(
                    "dense".into(),
                    RepresentationVector::Dense(vec![1.0, 0.0]),
                )]),
            }],
        };
        owner.apply(batch.clone()).unwrap();
        let key = identity(1, &generation, &epoch).unwrap();
        // Bypass the adapter deliberately to represent corrupted/out-of-contract
        // persisted vectors. The query itself remains within its admitted budget.
        let point = PointStruct::new(
            1,
            Vectors::new_named(vec![("dense", Vector::new_dense(vec![1e31, 0.0]))]),
            json!({"source_key":{"type":"bigint","value":"1"},"revision":1,"incarnation":generation,"fingerprint":"fixture","body":"anchor","body_prefix":"anchor"}),
        );
        owner.shards[&key]
            .shard
            .update(UpdateOperation::PointOperation(
                PointOperations::UpsertPoints(PointInsertOperations::PointsList(vec![
                    point.into(),
                ])),
            ))
            .unwrap();
        let query = RepresentationQuery {
            representation: "dense".into(),
            model_id: contract.model_id,
            model_version: contract.model_version,
            vector: RepresentationVector::Dense(vec![1e8, 0.0]),
        };
        let failure = owner
            .search(
                1,
                &generation,
                &epoch,
                "anchor",
                10,
                Some(query.clone()),
                None,
                None,
                None,
                None,
                SourcePredicates::default(),
            )
            .unwrap_err();
        assert_eq!(failure.code, "source_engine_error");
        assert!(failure.message.contains("non-finite score"));
        owner.apply(batch).unwrap();
        let hits = owner
            .search(
                1,
                &generation,
                &epoch,
                "anchor",
                10,
                Some(query.clone()),
                None,
                None,
                None,
                None,
                SourcePredicates::default(),
            )
            .unwrap();
        assert_eq!(hits[0]["score"], 1e8);
        let points = [(1, vec![1e15, 0.0]), (2, vec![0.0, 1.0])].map(|(id, values)| {
            PointStruct::new(id, Vectors::new_named(vec![
                ("dense", Vector::new_dense(values)),
                ("bm25", Vector::from(owner.encoder.embed_document("anchor"))),
            ]), json!({"source_key":{"type":"bigint","value":id.to_string()},"revision":1,
                "incarnation":generation,"fingerprint":"fixture","body":"anchor","body_prefix":"anchor"})).into()
        });
        owner.shards[&key]
            .shard
            .update(UpdateOperation::PointOperation(
                PointOperations::UpsertPoints(PointInsertOperations::PointsList(points.into())),
            ))
            .unwrap();
        let nearest = owner
            .search(
                1,
                &generation,
                &epoch,
                "anchor",
                10,
                Some(query.clone()),
                None,
                None,
                None,
                None,
                SourcePredicates::default(),
            )
            .unwrap();
        assert_eq!(nearest.as_array().unwrap().len(), 2);
        assert!(
            nearest
                .as_array()
                .unwrap()
                .iter()
                .all(|hit| hit["score"].as_f64().unwrap().is_finite())
        );
        let failure = owner
            .search(
                1,
                &generation,
                &epoch,
                "anchor",
                10,
                Some(query),
                None,
                None,
                None,
                Some(SourceFusion::Dbsf),
                SourcePredicates::default(),
            )
            .unwrap_err();
        assert_eq!(failure.code, "source_engine_error");
        assert!(failure.message.contains("non-finite score"));
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn discovery_context_adapter_pinned_scores_and_exclusions() {
        use pg_qdrant_protocol::SourceContextPair;
        let (owner, root, generation, epoch, values) = dense_example_fixture();
        for distance in ["dot", "cosine", "euclid", "manhattan"] {
            let vector = |raw: [f32; 2]| -> Vec<f32> {
                if distance != "cosine" {
                    return raw.to_vec();
                }
                let norm = (raw[0] * raw[0] + raw[1] * raw[1]).sqrt();
                if norm == 0.0 {
                    vec![0.5f32.sqrt(), 0.5f32.sqrt()]
                } else {
                    vec![raw[0] / norm, raw[1] / norm]
                }
            };
            // Independent f64 goldens: native context rank plus bounded target,
            // or the sum of bounded negative-margin losses. Ties have no order.
            let similarity = |a: &[f32], b: &[f32]| -> f64 {
                match distance {
                    "dot" | "cosine" => a
                        .iter()
                        .zip(b)
                        .map(|(x, y)| f64::from(*x) * f64::from(*y))
                        .sum(),
                    "euclid" => -a
                        .iter()
                        .zip(b)
                        .map(|(x, y)| (f64::from(*x) - f64::from(*y)).powi(2))
                        .sum::<f64>(),
                    "manhattan" => -a
                        .iter()
                        .zip(b)
                        .map(|(x, y)| (f64::from(*x) - f64::from(*y)).abs())
                        .sum::<f64>(),
                    _ => unreachable!(),
                }
            };
            for discover in [false, true] {
                let input = SourceDiscovery {
                    representation: distance.into(),
                    model_id: "fixture".into(),
                    model_version: "r1".into(),
                    strategy: if discover {
                        DiscoveryStrategy::Discover
                    } else {
                        DiscoveryStrategy::Context
                    },
                    target: discover.then(|| vector(values[2])),
                    context: vec![SourceContextPair {
                        positive: vector(values[0]),
                        negative: vector(values[1]),
                    }],
                    exclude_ids: if discover { vec![1, 2, 3] } else { vec![1, 2] },
                };
                let hits = owner
                    .search(
                        1,
                        &generation,
                        &epoch,
                        "",
                        100,
                        None,
                        None,
                        Some(input.clone()),
                        None,
                        None,
                        SourcePredicates::default(),
                    )
                    .unwrap();
                let hits = hits.as_array().unwrap();
                assert_eq!(hits.len(), if discover { 4 } else { 5 });
                for hit in hits {
                    let id = hit["id"].as_u64().unwrap();
                    assert!(!input.exclude_ids.contains(&id));
                    let v = vector(values[id as usize - 1]);
                    let p = similarity(&v, &input.context[0].positive);
                    let n = similarity(&v, &input.context[0].negative);
                    let expected = if discover {
                        let rank = if p > n {
                            1.0
                        } else if p < n {
                            -1.0
                        } else {
                            0.0
                        };
                        let target = similarity(&v, input.target.as_ref().unwrap());
                        rank + 0.5 * (target / (1.0 + target.abs()) + 1.0)
                    } else {
                        let loss = (p - n - f64::from(f32::EPSILON)).min(0.0);
                        loss / (1.0 + loss.abs())
                    };
                    assert!(
                        (hit["score"].as_f64().unwrap() - expected).abs() < 5e-6,
                        "{distance} discover={discover} id={id}: {hit} != {expected}"
                    );
                }
                let constrained = owner
                    .search(
                        1,
                        &generation,
                        &epoch,
                        "",
                        1,
                        None,
                        None,
                        Some(input.clone()),
                        None,
                        None,
                        SourcePredicates {
                            key_exact: Some("4".into()),
                            ..Default::default()
                        },
                    )
                    .unwrap();
                assert_eq!(constrained[0]["id"], 4);
                let excluded = owner
                    .search(
                        1,
                        &generation,
                        &epoch,
                        "",
                        1,
                        None,
                        None,
                        Some(input.clone()),
                        None,
                        None,
                        SourcePredicates {
                            key_exact: Some("1".into()),
                            ..Default::default()
                        },
                    )
                    .unwrap();
                assert_eq!(excluded, json!([]));
                for invalid in [
                    SourceDiscovery {
                        target: if discover { None } else { Some(vec![1., 0.]) },
                        ..input.clone()
                    },
                    SourceDiscovery {
                        context: vec![],
                        ..input.clone()
                    },
                    SourceDiscovery {
                        model_version: "wrong".into(),
                        ..input.clone()
                    },
                    SourceDiscovery {
                        exclude_ids: vec![1, 1],
                        ..input.clone()
                    },
                ] {
                    assert!(
                        owner
                            .search(
                                1,
                                &generation,
                                &epoch,
                                "",
                                10,
                                None,
                                None,
                                Some(invalid),
                                None,
                                None,
                                SourcePredicates::default()
                            )
                            .is_err()
                    );
                }
            }
        }
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

    fn dense_example_fixture() -> (SourceOwner, PathBuf, String, String, [[f32; 2]; 7]) {
        let root = std::env::temp_dir().join(format!(
            "pgq-recommend-{}-{}",
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
        let values: [[f32; 2]; 7] = [
            [1., 0.],
            [0., 1.],
            [0.9, 0.1],
            [0.1, 0.9],
            [0.5, 0.5],
            [-1., 0.],
            [0., 0.],
        ];
        let batch = SourceBatch {
            source_contract_version: SOURCE_CONTRACT_VERSION,
            index_id: 1,
            generation: generation.clone(),
            storage_epoch: epoch.clone(),
            consumer_id: epoch.clone(),
            task_id: None,
            retire: false,
            representations: ["dot", "cosine", "euclid", "manhattan"]
                .into_iter()
                .map(|distance| {
                    let mut contract = numeric_contract();
                    contract.distance = distance.into();
                    if distance == "cosine" {
                        contract.normalization = "unit".into();
                    }
                    (distance.to_owned(), contract)
                })
                .collect(),
            events: values
                .into_iter()
                .enumerate()
                .map(|(offset, v)| {
                    let id = offset as u64 + 1;
                    SourceEvent {
                        event_id: id,
                        point_id: id,
                        revision: 1,
                        incarnation: generation.clone(),
                        fingerprint: Some("fixture".into()),
                        key: json!({"type":"bigint","value":id.to_string()}),
                        body: Some("anchor".into()),
                        vectors: ["dot", "cosine", "euclid", "manhattan"]
                            .into_iter()
                            .map(|distance| {
                                (
                                    distance.to_owned(),
                                    RepresentationVector::Dense(if distance == "cosine" {
                                        let norm = (v[0] * v[0] + v[1] * v[1]).sqrt();
                                        if norm == 0.0 {
                                            vec![0.5f32.sqrt(), 0.5f32.sqrt()]
                                        } else {
                                            vec![v[0] / norm, v[1] / norm]
                                        }
                                    } else {
                                        v.to_vec()
                                    }),
                                )
                            })
                            .collect(),
                    }
                })
                .collect(),
        };
        owner.apply(batch).unwrap();
        (owner, root, generation, epoch, values)
    }

    #[test]
    fn recommendation_adapter_pinned_best_and_sum() {
        let (owner, root, generation, epoch, values) = dense_example_fixture();
        for distance in ["dot", "cosine", "euclid", "manhattan"] {
            for best in [false, true] {
                let recommendation = SourceRecommendation {
                    representation: distance.into(),
                    model_id: "fixture".into(),
                    model_version: "r1".into(),
                    strategy: if best {
                        RecommendationStrategy::BestScore
                    } else {
                        RecommendationStrategy::SumScores
                    },
                    positive: vec![vec![1., 0.]],
                    negative: vec![vec![0., 1.]],
                    exclude_ids: vec![1, 2],
                };
                let hits = owner
                    .search(
                        1,
                        &generation,
                        &epoch,
                        "anchor",
                        100,
                        None,
                        Some(recommendation.clone()),
                        None,
                        None,
                        None,
                        SourcePredicates::default(),
                    )
                    .unwrap();
                let hits = hits.as_array().unwrap();
                assert_eq!(hits.len(), 5);
                let constrained = owner
                    .search(
                        1,
                        &generation,
                        &epoch,
                        "anchor",
                        1,
                        None,
                        Some(recommendation.clone()),
                        None,
                        None,
                        None,
                        SourcePredicates {
                            key_exact: Some("4".into()),
                            ..Default::default()
                        },
                    )
                    .unwrap();
                assert_eq!(constrained[0]["id"], 4);
                let excluded = owner
                    .search(
                        1,
                        &generation,
                        &epoch,
                        "anchor",
                        1,
                        None,
                        Some(recommendation.clone()),
                        None,
                        None,
                        None,
                        SourcePredicates {
                            key_exact: Some("1".into()),
                            ..Default::default()
                        },
                    )
                    .unwrap();
                assert_eq!(excluded.as_array().unwrap().len(), 0);
                for invalid in [
                    SourceRecommendation {
                        positive: vec![],
                        ..recommendation.clone()
                    },
                    SourceRecommendation {
                        exclude_ids: vec![1, 1],
                        ..recommendation.clone()
                    },
                    SourceRecommendation {
                        model_version: "r2".into(),
                        ..recommendation.clone()
                    },
                    SourceRecommendation {
                        positive: vec![vec![1e30, 0.]],
                        ..recommendation.clone()
                    },
                ] {
                    assert!(
                        owner
                            .search(
                                1,
                                &generation,
                                &epoch,
                                "anchor",
                                100,
                                None,
                                Some(invalid),
                                None,
                                None,
                                None,
                                SourcePredicates::default()
                            )
                            .is_err()
                    );
                }
                for (offset, value) in values.iter().enumerate().skip(2) {
                    let id = offset as u64 + 1;
                    let [x, y] = value.map(f64::from);
                    let (positive, negative) = match distance {
                        "dot" => (x, y),
                        "cosine" => {
                            let norm = (x * x + y * y).sqrt();
                            if norm == 0.0 {
                                (0.5f64.sqrt(), 0.5f64.sqrt())
                            } else {
                                (x / norm, y / norm)
                            }
                        }
                        "euclid" => (-((x - 1.0).powi(2) + y * y), -(x * x + (y - 1.0).powi(2))),
                        "manhattan" => (-((x - 1.0).abs() + y.abs()), -(x.abs() + (y - 1.0).abs())),
                        _ => unreachable!(),
                    };
                    let sigmoid = |v: f64| 0.5 * (1.0 + v / (1.0 + v.abs()));
                    let score = if !best {
                        positive - negative
                    } else if positive > negative {
                        sigmoid(positive)
                    } else {
                        -sigmoid(negative)
                    };
                    let hit = hits.iter().find(|h| h["id"] == id).unwrap();
                    assert!(
                        (hit["score"].as_f64().unwrap() - score).abs() < 1e-6,
                        "distance={distance} best={best} id={id} native={} expected={score}",
                        hit["score"]
                    );
                }
                assert_eq!(hits[0]["id"], 3);
            }
        }
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

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

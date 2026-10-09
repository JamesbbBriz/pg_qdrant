//! Embedded Edge mutation owner. No SQL values select a filesystem path.
use pg_qdrant_protocol::formula::ScoreFormula;
use pg_qdrant_protocol::{
    DiscoveryStrategy, PayloadKind, ProbeError, RecommendationStrategy, RepresentationContract,
    RepresentationQuery, RepresentationVector, SOURCE_CONTRACT_VERSION, SourceBatch,
    SourceDiscovery, SourceFeedback, SourceFusion, SourceGroups, SourceMatrix, SourceMmr,
    SourcePredicates, SourceRecommendation,
};
use qdrant_edge::EdgeShardRead;
use qdrant_edge::bm25_embed::EdgeBm25;
use qdrant_edge::{
    ContextPair, ContextQuery, DiscoverQuery, Distance, EdgeConfig, EdgeShard,
    EdgeSparseVectorParams, EdgeVectorParams, Expression, FeedbackItem, FeedbackNaiveQuery, Filter,
    Formula, Fusion, IdfCorpusParams, IdfParams, Mmr, Modifier, MultiVectorComparator,
    MultiVectorConfig, NaiveFeedbackStrategy, NamedQuery, PointId, PointInsertOperations,
    PointOperations, PointStruct, Prefetch, QueryEnum, QueryRequest, RecommendQuery,
    RetrieveRequest, ScoringQuery, SearchParams, UpdateOperation, Vector, VectorInternal, Vectors,
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
    payload_contract: BTreeMap<String, PayloadKind>,
    payload_ready: bool,
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
                || !batch.payload_contract.is_empty()
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
        crate::payload::validate_contract(&batch.payload_contract)?;
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
            crate::payload::validate_event(event, &batch.payload_contract)?;
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
        if self.shards.get(&key).is_some_and(|s| {
            s.representations != batch.representations
                || s.payload_contract != batch.payload_contract
        }) {
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
                    payload_contract: batch.payload_contract.clone(),
                    payload_ready: false,
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
        if !self.shards[&key].payload_ready {
            crate::payload::install(&self.shards[&key].shard, &batch.payload_contract)?;
            self.shards
                .get_mut(&key)
                .expect("owned shard")
                .payload_ready = true;
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
                          "incarnation":event.incarnation,"fingerprint":event.fingerprint,"body":body,"body_prefix":body,
                          "attributes":event.payload,"payload_fingerprint":event.payload_fingerprint}),
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

    #[cfg(test)]
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
        feedback_query: Option<SourceFeedback>,
        mmr_query: Option<SourceMmr>,
        formula: Option<ScoreFormula>,
        rerank_query: Option<RepresentationQuery>,
        fusion: Option<SourceFusion>,
        predicates: SourcePredicates,
    ) -> Result<Value, ProbeError> {
        self.search_multiple(
            index_id,
            generation,
            epoch,
            q,
            top_k,
            representation_query,
            Vec::new(),
            recommendation_query,
            discovery_query,
            feedback_query,
            mmr_query,
            formula,
            rerank_query,
            fusion,
            predicates,
        )
    }

    pub fn search_multiple(
        &self,
        index_id: u64,
        generation: &str,
        epoch: &str,
        q: &str,
        top_k: usize,
        representation_query: Option<RepresentationQuery>,
        additional_recall: Vec<RepresentationQuery>,
        recommendation_query: Option<SourceRecommendation>,
        discovery_query: Option<SourceDiscovery>,
        feedback_query: Option<SourceFeedback>,
        mmr_query: Option<SourceMmr>,
        formula: Option<ScoreFormula>,
        rerank_query: Option<RepresentationQuery>,
        fusion: Option<SourceFusion>,
        predicates: SourcePredicates,
    ) -> Result<Value, ProbeError> {
        if (recommendation_query.is_none()
            && discovery_query.is_none()
            && feedback_query.is_none()
            && mmr_query.is_none()
            && q.is_empty())
            || q.len() > 8192
            || !(1..=1000).contains(&top_k)
        {
            return Err(ProbeError::invalid("search outside bounds"));
        }
        if let Some(expression) = &formula {
            expression
                .validate()
                .map_err(|message| ProbeError::invalid(&message))?;
            if recommendation_query.is_some()
                || discovery_query.is_some()
                || feedback_query.is_some()
                || mmr_query.is_some()
            {
                return Err(ProbeError::invalid(
                    "score formula cannot wrap explore scoring",
                ));
            }
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
        if additional_recall.len() > 1
            || (!additional_recall.is_empty()
                && (fusion.is_none()
                    || recommendation_query.is_some()
                    || discovery_query.is_some()
                    || feedback_query.is_some()
                    || mmr_query.is_some()))
        {
            return Err(ProbeError::invalid(
                "additional recall requires bounded dense/sparse fusion",
            ));
        }
        let mut extra = None;
        if let Some(input) = additional_recall.first() {
            let primary = representation_query
                .as_ref()
                .and_then(|query| owned.representations.get(&query.representation))
                .ok_or_else(|| ProbeError::invalid("primary recall slot absent"))?;
            let contract = owned
                .representations
                .get(&input.representation)
                .ok_or_else(|| ProbeError::invalid("additional recall slot absent"))?;
            if primary.kind != "dense"
                || contract.kind != "learned_sparse"
                || input.model_id != contract.model_id
                || input.model_version != contract.model_version
            {
                return Err(ProbeError::invalid(
                    "additional recall requires a distinct declared learned sparse model after dense",
                ));
            }
            validate_representation(&input.vector, contract)?;
            extra = Some((
                ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
                    using: Some(input.representation.clone()),
                    query: native_vector(&input.vector)?.into(),
                })),
                contract.idf_policy.as_deref() == Some("engine"),
            ));
        }
        let mut request = QueryRequest::new(top_k);
        let mut filter = crate::lexical::compile(&predicates, &self.encoder)?;
        if let Some(expression) = &predicates.payload_filter {
            if !owned.payload_ready {
                return Err(error("owned payload indexes are not ready"));
            }
            let condition: qdrant_edge::Condition =
                serde_json::from_value(expression.native(&owned.payload_contract)?)
                    .map_err(|_| ProbeError::invalid("invalid compiled scalar filter"))?;
            filter
                .get_or_insert_with(Filter::default)
                .must
                .get_or_insert_with(Vec::new)
                .push(condition);
        }
        request.filter = filter.clone();
        let (scoring, uses_idf) = if let Some(mmr) = mmr_query {
            if recommendation_query.is_some()
                || discovery_query.is_some()
                || feedback_query.is_some()
                || representation_query.is_some()
                || rerank_query.is_some()
                || fusion.is_some()
            {
                return Err(ProbeError::invalid(
                    "MMR cannot be combined, fused or reranked",
                ));
            }
            let contract = owned
                .representations
                .get(&mmr.representation)
                .ok_or_else(|| ProbeError::invalid("MMR slot is not owned"))?;
            if contract.kind != "dense"
                || mmr.model_id != contract.model_id
                || mmr.model_version != contract.model_version
                || mmr.exclude_id == 0
                || !mmr.lambda.is_finite()
                || !(0.0..=1.0).contains(&mmr.lambda)
            {
                return Err(ProbeError::invalid("invalid MMR target, lambda or model"));
            }
            let points = shard.info().map_err(error)?.points_count;
            admit_mmr_work(contract.dimensions, points, top_k)?;
            validate_representation(&RepresentationVector::Dense(mmr.target.clone()), contract)?;
            let exclusion: Filter =
                serde_json::from_value(json!({"must_not":[{"has_id":[mmr.exclude_id]}]}))
                    .map_err(error)?;
            filter
                .get_or_insert_with(Filter::default)
                .must_not
                .get_or_insert_with(Vec::new)
                .extend(exclusion.must_not.unwrap());
            request.filter = filter.clone();
            use qdrant_edge::external::ordered_float::OrderedFloat;
            (
                ScoringQuery::Mmr(Mmr {
                    vector: VectorInternal::Dense(mmr.target),
                    using: mmr.representation,
                    lambda: OrderedFloat(mmr.lambda),
                    candidates_limit: top_k,
                }),
                false,
            )
        } else if let Some(feedback) = feedback_query {
            if recommendation_query.is_some()
                || discovery_query.is_some()
                || representation_query.is_some()
                || rerank_query.is_some()
                || fusion.is_some()
            {
                return Err(ProbeError::invalid(
                    "feedback cannot be combined, fused or reranked",
                ));
            }
            let contract = owned
                .representations
                .get(&feedback.representation)
                .ok_or_else(|| ProbeError::invalid("feedback slot is not owned"))?;
            let count = feedback.feedback.len().saturating_add(1);
            let c = &feedback.coefficients;
            if contract.kind != "dense"
                || feedback.model_id != contract.model_id
                || feedback.model_version != contract.model_version
                || feedback.feedback.is_empty()
                || count > 32
                || feedback.exclude_ids.len() != count
                || feedback.exclude_ids.iter().any(|id| *id == 0)
                || feedback
                    .exclude_ids
                    .iter()
                    .collect::<std::collections::BTreeSet<_>>()
                    .len()
                    != count
                || !feedback_coefficients_valid(c.a, c.b, c.c)
                || feedback
                    .feedback
                    .iter()
                    .any(|item| !item.score.is_finite() || item.score.abs() > 16.0)
            {
                return Err(ProbeError::invalid(
                    "invalid feedback examples, scores, coefficients or model",
                ));
            }
            // Native extraction can form n*(n-1)/2 pairs and score both vectors
            // in each pair. Admit the worst case even for equal feedback scores.
            let n = feedback.feedback.len();
            let work_vectors = n.saturating_mul(n.saturating_sub(1)).saturating_add(1);
            admit_example_work(
                work_vectors,
                contract.dimensions,
                shard.info().map_err(error)?.points_count,
            )?;
            for vector in std::iter::once(&feedback.target)
                .chain(feedback.feedback.iter().map(|item| &item.vector))
            {
                validate_representation(&RepresentationVector::Dense(vector.clone()), contract)?;
            }
            let exclusion: Filter =
                serde_json::from_value(json!({"must_not":[{"has_id":feedback.exclude_ids}]}))
                    .map_err(error)?;
            filter
                .get_or_insert_with(Filter::default)
                .must_not
                .get_or_insert_with(Vec::new)
                .extend(exclusion.must_not.unwrap());
            request.filter = filter.clone();
            use qdrant_edge::external::ordered_float::OrderedFloat;
            let query = FeedbackNaiveQuery {
                target: VectorInternal::Dense(feedback.target),
                feedback: feedback
                    .feedback
                    .into_iter()
                    .map(|item| FeedbackItem {
                        vector: VectorInternal::Dense(item.vector),
                        score: OrderedFloat(item.score),
                    })
                    .collect(),
                coefficients: NaiveFeedbackStrategy {
                    a: OrderedFloat(c.a),
                    b: OrderedFloat(c.b),
                    c: OrderedFloat(c.c),
                },
            };
            (
                ScoringQuery::Vector(QueryEnum::FeedbackNaive(NamedQuery {
                    using: Some(feedback.representation),
                    query,
                })),
                false,
            )
        } else if let Some(discovery) = discovery_query {
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
            let mut branches = vec![(bm25, true), (scoring, uses_idf)];
            branches.extend(extra);
            request.prefetches = branches
                .into_iter()
                .map(|(query, branch_idf)| {
                    let mut stage = Prefetch::new(top_k);
                    stage.query = Some(query);
                    stage.filter = filter.clone();
                    stage.params = Some(SearchParams {
                        exact: true,
                        indexed_only: false,
                        idf: branch_idf.then(|| {
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
            candidates.filter = filter.clone();
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
        if let Some(expression) = formula {
            let mut candidates = Prefetch::new(top_k);
            candidates.query = request.query.take();
            candidates.prefetches = std::mem::take(&mut request.prefetches);
            candidates.params = request.params.take();
            candidates.filter = filter.clone();
            request.prefetches = vec![candidates];
            let formula = Formula {
                formula: native_formula(expression),
                defaults: HashMap::new(),
            }
            .try_into()
            .map_err(error)?;
            request.query = Some(ScoringQuery::Formula(formula));
            request.params = None;
        }
        // Source text is returned only through the authorized PostgreSQL JOIN.
        // Fetch identity/version metadata without copying every candidate body
        // into the bounded IPC response (one source row can contain 64 KiB).
        request.with_payload = WithPayloadInterface::Fields(
            [
                "source_key",
                "revision",
                "incarnation",
                "fingerprint",
                "payload_fingerprint",
            ]
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

    pub fn statistics(
        &self,
        index_id: u64,
        generation: &str,
        epoch: &str,
        point_ids: Vec<u64>,
        facet: Option<String>,
        facet_limit: usize,
        matrix: Option<SourceMatrix>,
        groups: Option<SourceGroups>,
        predicates: SourcePredicates,
    ) -> Result<Value, ProbeError> {
        if point_ids.len() > 1000
            || !(1..=100).contains(&facet_limit)
            || point_ids.iter().any(|&id| id == 0 || id > i64::MAX as u64)
            || point_ids
                .iter()
                .collect::<std::collections::HashSet<_>>()
                .len()
                != point_ids.len()
        {
            return Err(ProbeError::invalid(
                "statistics identity/facet budget exceeded",
            ));
        }
        let key = identity(index_id, generation, epoch)?;
        let owned = self
            .shards
            .get(&key)
            .ok_or_else(|| error("generation not owned by this helper"))?;
        if !owned.lexical_ready || !owned.payload_ready {
            return Err(error("owned lexical/payload indexes are not ready"));
        }
        // Validate the entire owned domain, including points excluded by a filter.
        // This makes stale or omitted source identities a complete-query refusal.
        let mut all = qdrant_edge::CountRequest::new();
        all.exact = true;
        if owned.shard.count(all).map_err(error)? != point_ids.len() {
            return Err(error(
                "statistics owned point domain differs from source domain",
            ));
        }
        let mut proof = Vec::with_capacity(point_ids.len());
        for batch in point_ids.chunks(100) {
            let records = self.retrieve(index_id, generation, epoch, batch.to_vec())?;
            let records = records
                .as_array()
                .ok_or_else(|| error("invalid statistics identity proof"))?;
            if records.len() != batch.len() {
                return Err(error("incomplete statistics identity proof"));
            }
            proof.extend(records.iter().cloned());
        }
        let mut filter = crate::lexical::compile(&predicates, &self.encoder)?.unwrap_or_default();
        if let Some(expression) = &predicates.payload_filter {
            let condition: qdrant_edge::Condition =
                serde_json::from_value(expression.native(&owned.payload_contract)?)
                    .map_err(|_| ProbeError::invalid("invalid compiled statistics filter"))?;
            filter.must.get_or_insert_with(Vec::new).push(condition);
        }
        filter = filter.with_point_ids(point_ids.iter().copied().map(PointId::NumId));
        let mut request = qdrant_edge::CountRequest::new();
        request.exact = true;
        request.filter = Some(filter.clone());
        let points = owned.shard.count(request).map_err(error)?;
        let mut group_result = Value::Null;
        if let Some(groups) = groups {
            if facet.is_some()
                || matrix.is_some()
                || !(1..=32).contains(&groups.groups)
                || !(1..=16).contains(&groups.group_size)
                || groups.groups * groups.group_size > 256
                || groups.q.trim().is_empty()
                || groups.q.len() > 8192
                || !matches!(
                    owned.payload_contract.get(&groups.field),
                    Some(PayloadKind::Keyword | PayloadKind::Integer)
                )
            {
                return Err(ProbeError::invalid(
                    "groups require keyword/integer alias, query and bounded group/hit counts",
                ));
            }
            let (using, vector, idf): (String, VectorInternal, bool) =
                if let Some(query) = &groups.representation_query {
                    let contract = owned
                        .representations
                        .get(&query.representation)
                        .filter(|c| {
                            c.kind == "dense"
                                && c.model_id == query.model_id
                                && c.model_version == query.model_version
                        })
                        .ok_or_else(|| {
                            ProbeError::invalid("group query requires the declared dense model")
                        })?;
                    let work = point_ids
                        .len()
                        .checked_mul(contract.dimensions)
                        .and_then(|n| n.checked_mul(10));
                    if work.is_none_or(|n| n > 20_000_000) {
                        return Err(ProbeError::invalid(
                            "group dense scalar work exceeds 20000000",
                        ));
                    }
                    validate_representation(&query.vector, contract)?;
                    let mut complete = qdrant_edge::CountRequest::new();
                    complete.exact = true;
                    complete.filter = Some(Filter::new_must(
                        serde_json::from_value(json!({"has_vector":query.representation}))
                            .map_err(error)?,
                    ));
                    if owned.shard.count(complete).map_err(error)? != point_ids.len() {
                        return Err(error(
                            "group representation is incomplete in the owned generation",
                        ));
                    }
                    (
                        query.representation.clone(),
                        native_vector(&query.vector)?.into(),
                        false,
                    )
                } else {
                    (
                        "bm25".to_owned(),
                        VectorInternal::Sparse(self.encoder.embed_query(&groups.q)),
                        true,
                    )
                };
            let mut query = QueryRequest::new(groups.groups * groups.group_size);
            query.query = Some(ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
                using: Some(using),
                query: vector.into(),
            })));
            query.filter = Some(filter.clone());
            query.params = Some(SearchParams {
                exact: true,
                indexed_only: false,
                idf: idf.then(|| {
                    IdfParams::Corpus(IdfCorpusParams {
                        corpus: Filter::default(),
                    })
                }),
                ..Default::default()
            });
            let response = owned
                .shard
                .query_groups(qdrant_edge::GroupRequest::new(
                    query,
                    format!("attributes.{}", groups.field)
                        .parse()
                        .map_err(|_| ProbeError::invalid("invalid grouping alias"))?,
                    groups.groups,
                    groups.group_size,
                ))
                .map_err(error)?;
            if response.len() > groups.groups {
                return Err(error("native group count exceeds budget"));
            }
            let mut rows = Vec::with_capacity(response.len());
            let mut keys = std::collections::HashSet::new();
            for group in response {
                let value = serde_json::to_value(group.key).map_err(error)?;
                if !(value.is_string() || value.as_i64().is_some())
                    || !keys.insert(value.clone())
                    || group.hits.len() > groups.group_size
                {
                    return Err(error("invalid native group key or hit budget"));
                }
                let mut ids = std::collections::HashSet::new();
                let mut hits = Vec::with_capacity(group.hits.len());
                for hit in group.hits {
                    let PointId::NumId(id) = hit.id else {
                        return Err(error("unexpected grouped point identity"));
                    };
                    if !point_ids.contains(&id) || !ids.insert(id) || !hit.score.is_finite() {
                        return Err(error("invalid grouped point or non-finite score"));
                    }
                    hits.push(json!({"id":id,"score":hit.score}));
                }
                rows.push(json!({"value":value,"hits":hits}));
            }
            group_result = json!({"field":groups.field,"groups":rows,"group_limit":groups.groups,"group_size":groups.group_size,
                "mode":if idf {"text"}else{"semantic"},"exact_scores":true,"groups_complete":false,
                "request_budget":{"collect":5,"fill":5},"nulls":"omitted","ordering":"native best-hit order",
                "scope":"bounded native group discovery and filling; no total-group or per-group count claim"});
        }
        let mut matrix_result = Value::Null;
        if let Some(matrix) = matrix {
            let contract = owned
                .representations
                .get(&matrix.representation)
                .filter(|c| c.kind == "dense")
                .ok_or_else(|| {
                    ProbeError::invalid("matrix requires a declared dense representation")
                })?;
            let work = point_ids
                .len()
                .checked_mul(matrix.sample_size)
                .and_then(|n| n.checked_mul(contract.dimensions));
            if !(1..=64).contains(&matrix.sample_size)
                || !(1..=32).contains(&matrix.neighbors)
                || work.is_none_or(|n| n > 20_000_000)
                || facet.is_some()
            {
                return Err(ProbeError::invalid(
                    "matrix sample/neighbor/scalar budget exceeded",
                ));
            }
            let mut complete = qdrant_edge::CountRequest::new();
            complete.exact = true;
            complete.filter = Some(Filter::new_must(
                serde_json::from_value(json!({
                    "has_vector": matrix.representation
                }))
                .map_err(error)?,
            ));
            if owned.shard.count(complete).map_err(error)? != point_ids.len() {
                return Err(error(
                    "matrix representation is incomplete in the owned generation",
                ));
            }
            let mut request = qdrant_edge::SearchMatrixRequest::new(
                matrix.sample_size,
                matrix.neighbors,
                matrix.representation.clone(),
            );
            request.filter = Some(filter.clone());
            let response = owned.shard.search_matrix(request).map_err(error)?;
            let sample_ids: Vec<u64> = response
                .sample_ids
                .into_iter()
                .map(|id| match id {
                    PointId::NumId(id) if point_ids.contains(&id) => Ok(id),
                    _ => Err(error("matrix sampled an unexpected identity")),
                })
                .collect::<Result<_, _>>()?;
            if sample_ids.len() > matrix.sample_size
                || response.nearests.len() != sample_ids.len()
                || sample_ids.windows(2).any(|ids| ids[0] >= ids[1])
            {
                return Err(error("invalid native matrix sample domain"));
            }
            let mut rows = Vec::with_capacity(sample_ids.len());
            for (&from, nearests) in sample_ids.iter().zip(response.nearests) {
                let mut seen = std::collections::HashSet::new();
                if nearests.len() > matrix.neighbors {
                    return Err(error("matrix neighbor bound exceeded"));
                }
                let mut neighbors = Vec::with_capacity(nearests.len());
                for hit in nearests {
                    let PointId::NumId(to) = hit.id else {
                        return Err(error("unexpected matrix neighbor identity"));
                    };
                    if from == to
                        || !sample_ids.contains(&to)
                        || !seen.insert(to)
                        || !hit.score.is_finite()
                    {
                        return Err(error("invalid native matrix neighbor or non-finite score"));
                    }
                    neighbors.push(json!({"id":to,"score":hit.score}));
                }
                rows.push(json!({"id":from,"neighbors":neighbors}));
            }
            matrix_result = json!({"representation":matrix.representation,"model_id":contract.model_id,
                "model_version":contract.model_version,"distance":contract.distance,"sample_size":matrix.sample_size,
                "neighbor_limit":matrix.neighbors,"sample_ids":sample_ids,"rows":rows,
                "sampling":"native random; fewer than two matches returns an empty matrix",
                "neighbor_domain":"sampled set only","exact":false,"search_params":"native defaults",
                "scalar_work_limit":20_000_000});
        }
        let mut facet_result = Value::Null;
        if let Some(name) = facet {
            match owned.payload_contract.get(&name) {
                Some(PayloadKind::Keyword | PayloadKind::Integer | PayloadKind::Bool) => {}
                _ => {
                    return Err(ProbeError::invalid(
                        "facet requires a declared keyword/integer/bool alias",
                    ));
                }
            }
            let mut request = qdrant_edge::FacetRequest::new(
                format!("attributes.{name}")
                    .parse()
                    .map_err(|_| ProbeError::invalid("invalid facet alias"))?,
            );
            request.filter = Some(filter);
            request.exact = true;
            // At most 1000 scalar rows => at most 1000 distinct non-null values.
            request.limit = 1000;
            let response = owned.shard.facet(request).map_err(error)?;
            let complete = response.hits.len() <= facet_limit;
            let hits: Vec<Value> = response
                .hits
                .into_iter()
                .take(facet_limit)
                .map(|hit| {
                    let value = match hit.value {
                        qdrant_edge::FacetValue::Keyword(value) => json!(value),
                        qdrant_edge::FacetValue::Int(value) => json!(value),
                        qdrant_edge::FacetValue::Bool(value) => json!(value),
                        qdrant_edge::FacetValue::Uuid(_) => Value::Null,
                    };
                    json!({"value":value,"count":hit.count})
                })
                .collect();
            if hits.iter().any(|hit| hit["value"].is_null()) {
                return Err(error("unexpected facet value kind"));
            }
            facet_result = json!({"field":name,"hits":hits,"values_complete":complete,"nulls":"omitted","ordering":"count descending; typed value ascending for ties"});
        }
        Ok(
            json!({"points":points,"proof":proof,"facet":facet_result,"matrix":matrix_result,"grouped":group_result}),
        )
    }

    pub fn retrieve(
        &self,
        index_id: u64,
        generation: &str,
        epoch: &str,
        point_ids: Vec<u64>,
    ) -> Result<Value, ProbeError> {
        if point_ids.len() > 100
            || point_ids.iter().any(|&id| id == 0 || id > i64::MAX as u64)
            || point_ids
                .iter()
                .collect::<std::collections::HashSet<_>>()
                .len()
                != point_ids.len()
        {
            return Err(ProbeError::invalid(
                "retrieve requires at most 100 distinct positive bigint point IDs",
            ));
        }
        let key = identity(index_id, generation, epoch)?;
        let owned = self
            .shards
            .get(&key)
            .ok_or_else(|| error("generation not owned by this helper"))?;
        let mut request =
            RetrieveRequest::new(point_ids.iter().copied().map(PointId::NumId).collect());
        request.with_payload = Some(WithPayloadInterface::Fields(
            [
                "source_key",
                "revision",
                "incarnation",
                "fingerprint",
                "payload_fingerprint",
            ]
            .map(|field| field.parse().expect("constant payload selector"))
            .to_vec(),
        ));
        // RetrieveRequest defaults vectors to false. Explicitly strip all native
        // fields beyond identity/version metadata from the owned IPC result.
        let records = owned.shard.retrieve(request).map_err(error)?;
        let mut result = Vec::with_capacity(records.len());
        let mut previous = None;
        for record in records {
            let PointId::NumId(id) = record.id else {
                return Err(error("unexpected point identity"));
            };
            let position = point_ids
                .iter()
                .position(|&requested| requested == id)
                .ok_or_else(|| error("unexpected native retrieve point"))?;
            if previous.is_some_and(|last| position <= last) {
                return Err(error("native retrieve order or duplicate mismatch"));
            }
            previous = Some(position);
            result.push(json!({"id":id,"payload":record.payload.map(|payload| payload.0)}));
        }
        Ok(Value::Array(result))
    }
}

fn feedback_coefficients_valid(a: f32, b: f32, c: f32) -> bool {
    a.is_finite()
        && a.abs() <= 16.0
        && b.is_finite()
        && (0.0..=4.0).contains(&b)
        && c.is_finite()
        && c.abs() <= 16.0
}

fn native_formula(expression: ScoreFormula) -> Expression {
    match expression {
        ScoreFormula::Constant { value } => Expression::Constant(value as f32),
        ScoreFormula::Score {} => Expression::Variable("$score[0]".into()),
        ScoreFormula::Add { args } => {
            Expression::Sum(args.into_iter().map(native_formula).collect())
        }
        ScoreFormula::Multiply { args } => {
            Expression::Mult(args.into_iter().map(native_formula).collect())
        }
        ScoreFormula::Negate { arg } => Expression::Neg(Box::new(native_formula(*arg))),
        ScoreFormula::Abs { arg } => Expression::Abs(Box::new(native_formula(*arg))),
        ScoreFormula::Sqrt { arg } => Expression::Sqrt(Box::new(native_formula(*arg))),
        ScoreFormula::Divide {
            left,
            right,
            by_zero_default,
        } => Expression::Div {
            left: Box::new(native_formula(*left)),
            right: Box::new(native_formula(*right)),
            by_zero_default: by_zero_default.map(|v| v as f32),
        },
    }
}

fn admit_mmr_work(
    dimensions: usize,
    owned_points: usize,
    candidate_limit: usize,
) -> Result<(), ProbeError> {
    // Exact nearest recall plus conservative pairwise diversity work, before filters.
    let candidates = owned_points.min(candidate_limit);
    let work = owned_points
        .saturating_add(candidates.saturating_mul(candidates))
        .saturating_mul(dimensions);
    if work > 20_000_000 {
        return Err(ProbeError::invalid(
            "MMR scalar-work budget exceeds 20000000",
        ));
    }
    Ok(())
}

// Exact source-example work uses the owned corpus before applying filters.
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
        assert!(admit_example_work(931, 125, 171).is_ok());
        assert!(admit_example_work(931, 125, 172).is_err());
        assert!(admit_mmr_work(100, 10000, 100).is_ok());
        assert!(admit_mmr_work(100, 190000, 100).is_ok());
        assert!(admit_mmr_work(100, 190001, 100).is_err());
        assert!(admit_mmr_work(4096, usize::MAX, 1000).is_err());
    }

    use pg_qdrant_protocol::{SourceEvent, SparseValues};

    #[test]
    fn typed_payload_is_indexed_replaced_and_guarded_by_generation() {
        use qdrant_edge::{Condition, CountRequest, FieldCondition, Match, PayloadSchemaType};
        let (mut owner, root, generation, epoch, _) = dense_example_fixture();
        let contract = BTreeMap::from([
            ("category".into(), PayloadKind::Keyword),
            ("quantity".into(), PayloadKind::Integer),
            ("price".into(), PayloadKind::Float),
            ("available".into(), PayloadKind::Bool),
        ]);
        let mut batch = SourceBatch {
            source_contract_version: SOURCE_CONTRACT_VERSION,
            index_id: 2,
            generation: generation.clone(),
            storage_epoch: epoch.clone(),
            consumer_id: epoch.clone(),
            task_id: None,
            retire: false,
            representations: BTreeMap::new(),
            payload_contract: contract,
            events: vec![SourceEvent {
                event_id: 1,
                point_id: 100,
                revision: 1,
                incarnation: generation.clone(),
                fingerprint: Some("fixture".into()),
                key: json!({"type":"bigint","value":"100"}),
                body: Some("payload anchor".into()),
                vectors: BTreeMap::new(),
                payload: Some(BTreeMap::from([
                    ("category".into(), json!("original")),
                    ("quantity".into(), json!(i64::MAX)),
                    ("price".into(), json!(-12.5)),
                    ("available".into(), json!(true)),
                ])),
                payload_fingerprint: Some("0".repeat(64)),
            }],
        };
        assert_eq!(owner.apply(batch.clone()).unwrap()["flushed"], true);
        let key = identity(2, &generation, &epoch).unwrap();
        let info = owner.shards[&key].shard.info().unwrap();
        for (field, schema) in [
            ("category", PayloadSchemaType::Keyword),
            ("quantity", PayloadSchemaType::Integer),
            ("price", PayloadSchemaType::Float),
            ("available", PayloadSchemaType::Bool),
        ] {
            assert_eq!(
                info.payload_schema[&format!("attributes.{field}").parse().unwrap()].data_type,
                schema
            );
        }
        let count = |owner: &SourceOwner, category: &str| {
            owner.shards[&key]
                .shard
                .count(CountRequest {
                    exact: true,
                    filter: Some(Filter {
                        must: Some(vec![Condition::Field(FieldCondition::new_match(
                            "attributes.category".parse().unwrap(),
                            Match::new_value(qdrant_edge::ValueVariants::String(
                                category.to_owned(),
                            )),
                        ))]),
                        ..Default::default()
                    }),
                })
                .unwrap()
        };
        assert_eq!(count(&owner, "original"), 1);
        for value in [json!([]), json!({}), json!(1), json!("x".repeat(1025))] {
            let mut bad = batch.clone();
            bad.events[0]
                .payload
                .as_mut()
                .unwrap()
                .insert("category".into(), value);
            assert_eq!(owner.apply(bad).unwrap_err().code, "invalid_parameter");
        }
        let mut bad = batch.clone();
        bad.events[0]
            .payload
            .as_mut()
            .unwrap()
            .insert("quantity".into(), json!(u64::MAX));
        assert!(owner.apply(bad).is_err());
        let mut bad = batch.clone();
        bad.events[0].payload_fingerprint = None;
        assert!(owner.apply(bad).is_err());
        let mut bad = batch.clone();
        bad.events[0]
            .payload
            .as_mut()
            .unwrap()
            .insert("undeclared".into(), json!(true));
        assert!(owner.apply(bad).is_err());
        let mut bad = batch.clone();
        bad.payload_contract
            .insert("alias.path".into(), PayloadKind::Bool);
        assert!(owner.apply(bad).is_err());
        let mut bad = batch.clone();
        bad.payload_contract
            .insert("quantity".into(), PayloadKind::Float);
        assert_eq!(owner.apply(bad).unwrap_err().code, "source_engine_error");
        batch.events[0].revision = 2;
        batch.events[0]
            .payload
            .as_mut()
            .unwrap()
            .insert("category".into(), json!("new"));
        batch.events[0]
            .payload
            .as_mut()
            .unwrap()
            .insert("quantity".into(), json!(i64::MIN));
        owner.apply(batch.clone()).unwrap();
        assert_eq!(count(&owner, "original"), 0);
        assert_eq!(count(&owner, "new"), 1);
        let native = owner.retrieve(2, &generation, &epoch, vec![100]).unwrap();
        assert!(native[0]["payload"].get("attributes").is_none());
        assert_eq!(native[0]["payload"]["revision"], 2);
        batch.events[0].body = None;
        batch.events[0].payload = None;
        batch.events[0].payload_fingerprint = None;
        owner.apply(batch).unwrap();
        assert_eq!(count(&owner, "new"), 0);
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

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
            payload_contract: BTreeMap::new(),
            representations: BTreeMap::from([("dense".into(), contract.clone())]),
            events: vec![SourceEvent {
                event_id: 1,
                point_id: 1,
                revision: 1,
                incarnation: generation.clone(),
                fingerprint: Some("fixture".into()),
                key: json!({"type":"bigint","value":"1"}),
                body: Some("anchor".into()),
                payload: Some(BTreeMap::new()),
                payload_fingerprint: Some("0".repeat(64)),
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
                        None,
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
                        "",
                        1,
                        None,
                        None,
                        Some(input.clone()),
                        None,
                        None,
                        None,
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
                                None,
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

    #[test]
    fn source_formula_native_scores_scope_and_arithmetic_errors() {
        let (owner, root, generation, epoch, values) = dense_example_fixture();
        for distance in ["dot", "cosine", "euclid", "manhattan"] {
            let query = RepresentationQuery {
                representation: distance.into(),
                model_id: "fixture".into(),
                model_version: "r1".into(),
                vector: RepresentationVector::Dense(vec![1.0, 0.0]),
            };
            let run = |expression, cap, predicates| {
                owner.search(
                    1,
                    &generation,
                    &epoch,
                    "anchor",
                    cap,
                    Some(query.clone()),
                    None,
                    None,
                    None,
                    None,
                    Some(expression),
                    None,
                    None,
                    predicates,
                )
            };
            let hits = run(ScoreFormula::Score {}, 7, SourcePredicates::default()).unwrap();
            assert_eq!(hits.as_array().unwrap().len(), 7);
            for hit in hits.as_array().unwrap() {
                let n = hit["id"].as_u64().unwrap() as usize - 1;
                let v = values[n];
                let expected = match distance {
                    "dot" => f64::from(v[0]),
                    "cosine" => {
                        let norm = f64::from(v[0]).hypot(f64::from(v[1]));
                        if norm == 0.0 {
                            0.5f64.sqrt()
                        } else {
                            f64::from(v[0]) / norm
                        }
                    }
                    "euclid" => (f64::from(v[0]) - 1.0).hypot(f64::from(v[1])),
                    "manhattan" => (f64::from(v[0]) - 1.0).abs() + f64::from(v[1]).abs(),
                    _ => unreachable!(),
                };
                assert!(
                    (hit["score"].as_f64().unwrap() - expected).abs() < 1e-5,
                    "{distance}: {hit} != {expected}"
                );
                assert!(hit.get("vector").is_none());
            }
            let neg = ScoreFormula::Negate {
                arg: Box::new(ScoreFormula::Score {}),
            };
            let bounded = run(neg.clone(), 3, SourcePredicates::default()).unwrap();
            let nearest = run(ScoreFormula::Score {}, 3, SourcePredicates::default()).unwrap();
            let ids = |hits: &Value| {
                hits.as_array()
                    .unwrap()
                    .iter()
                    .map(|h| h["id"].as_u64().unwrap())
                    .collect::<std::collections::BTreeSet<_>>()
            };
            assert_eq!(ids(&bounded), ids(&nearest));
            assert!(
                bounded
                    .as_array()
                    .unwrap()
                    .windows(2)
                    .all(|w| w[0]["score"].as_f64() >= w[1]["score"].as_f64())
            );
            let constrained = run(
                neg,
                1,
                SourcePredicates {
                    key_exact: Some("6".into()),
                    ..Default::default()
                },
            )
            .unwrap();
            assert_eq!(constrained[0]["id"], 6);
            let division = |default| ScoreFormula::Divide {
                left: Box::new(ScoreFormula::Constant { value: 1.0 }),
                right: Box::new(ScoreFormula::Constant { value: 0.0 }),
                by_zero_default: default,
            };
            assert!(run(division(None), 7, SourcePredicates::default()).is_err());
            let fallback = run(division(Some(-7.0)), 7, SourcePredicates::default()).unwrap();
            assert!(
                fallback
                    .as_array()
                    .unwrap()
                    .iter()
                    .all(|h| h["score"] == -7.0)
            );
            assert!(
                run(
                    ScoreFormula::Sqrt {
                        arg: Box::new(ScoreFormula::Constant { value: -1.0 })
                    },
                    7,
                    SourcePredicates::default()
                )
                .is_err()
            );
            let overflow = ScoreFormula::Multiply {
                args: vec![ScoreFormula::Constant { value: 1_000_000.0 }; 8],
            };
            assert!(run(overflow, 7, SourcePredicates::default()).is_err());
            assert!(
                run(
                    ScoreFormula::Constant {
                        value: f64::INFINITY
                    },
                    7,
                    SourcePredicates::default()
                )
                .is_err()
            );
            assert_eq!(
                run(ScoreFormula::Score {}, 7, SourcePredicates::default())
                    .unwrap()
                    .as_array()
                    .unwrap()
                    .len(),
                7
            );
        }
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn source_mmr_native_selection_scores_projection_and_admission() {
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
            for lambda in [0.0, 0.25, 1.0] {
                let input = SourceMmr {
                    representation: distance.into(),
                    model_id: "fixture".into(),
                    model_version: "r1".into(),
                    target: vector(values[2]),
                    lambda,
                    exclude_id: 3,
                };
                let hits = owner
                    .search(
                        1,
                        &generation,
                        &epoch,
                        "",
                        7,
                        None,
                        None,
                        None,
                        None,
                        Some(input.clone()),
                        None,
                        None,
                        None,
                        SourcePredicates::default(),
                    )
                    .unwrap();
                assert_eq!(hits.as_array().unwrap().len(), 6);
                let mut remaining: Vec<usize> = (0..7).filter(|n| *n != 2).collect();
                let mut selected: Vec<usize> = vec![];
                for hit in hits.as_array().unwrap() {
                    let id = hit["id"].as_u64().unwrap() as usize - 1;
                    assert!(remaining.contains(&id));
                    let objective = |n: usize| {
                        let relevance = similarity(&vector(values[n]), &input.target);
                        if selected.is_empty() {
                            relevance
                        } else {
                            f64::from(lambda) * relevance
                                - (1.0 - f64::from(lambda))
                                    * selected
                                        .iter()
                                        .map(|s| {
                                            similarity(&vector(values[n]), &vector(values[*s]))
                                        })
                                        .fold(f64::NEG_INFINITY, f64::max)
                        }
                    };
                    let best = remaining
                        .iter()
                        .map(|n| objective(*n))
                        .fold(f64::NEG_INFINITY, f64::max);
                    assert!(
                        (objective(id) - best).abs() < 1e-5,
                        "{distance}/{lambda}: {hit} not maximal objective {best}"
                    );
                    let raw = similarity(&vector(values[id]), &input.target);
                    let score = match distance {
                        "euclid" => (-raw).sqrt(),
                        "manhattan" => -raw,
                        _ => raw,
                    };
                    assert!(
                        (hit["score"].as_f64().unwrap() - score).abs() < 1e-5,
                        "{distance}: {hit} != {score}"
                    );
                    assert!(hit.get("vector").is_none());
                    remaining.retain(|n| *n != id);
                    selected.push(id);
                }
                let hits = owner
                    .search(
                        1,
                        &generation,
                        &epoch,
                        "",
                        1,
                        None,
                        None,
                        None,
                        None,
                        Some(input.clone()),
                        None,
                        None,
                        None,
                        SourcePredicates {
                            key_exact: Some("4".into()),
                            ..Default::default()
                        },
                    )
                    .unwrap();
                assert_eq!(hits[0]["id"], 4);
                assert_eq!(
                    owner
                        .search(
                            1,
                            &generation,
                            &epoch,
                            "",
                            1,
                            None,
                            None,
                            None,
                            None,
                            Some(input.clone()),
                            None,
                            None,
                            None,
                            SourcePredicates {
                                key_exact: Some("3".into()),
                                ..Default::default()
                            }
                        )
                        .unwrap(),
                    json!([])
                );
                for bad in [f32::NAN, f32::INFINITY, -0.01, 1.01] {
                    let mut invalid = input.clone();
                    invalid.lambda = bad;
                    assert!(
                        owner
                            .search(
                                1,
                                &generation,
                                &epoch,
                                "",
                                1,
                                None,
                                None,
                                None,
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
            }
        }
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn source_feedback_native_scores_equal_boundary_and_numeric_admission() {
        use pg_qdrant_protocol::{FeedbackCoefficients, SourceFeedbackItem};
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
            for scores in [[2.0, 0.0, -1.0], [1.0, 1.0, 1.0]] {
                let input = SourceFeedback {
                    representation: distance.into(),
                    model_id: "fixture".into(),
                    model_version: "r1".into(),
                    target: vector(values[2]),
                    feedback: [0, 1, 4]
                        .into_iter()
                        .zip(scores)
                        .map(|(id, score)| SourceFeedbackItem {
                            vector: vector(values[id]),
                            score,
                        })
                        .collect(),
                    coefficients: FeedbackCoefficients {
                        a: 1.0,
                        b: 2.0,
                        c: 0.25,
                    },
                    exclude_ids: vec![3, 1, 2, 5],
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
                        None,
                        Some(input.clone()),
                        None,
                        None,
                        None,
                        None,
                        SourcePredicates::default(),
                    )
                    .unwrap();
                assert_eq!(hits.as_array().unwrap().len(), 3);
                for hit in hits.as_array().unwrap() {
                    let id = hit["id"].as_u64().unwrap();
                    assert!(!input.exclude_ids.contains(&id));
                    let v = vector(values[id as usize - 1]);
                    let mut expected = similarity(&v, &input.target);
                    for p in &input.feedback {
                        for n in &input.feedback {
                            let confidence = f64::from(p.score) - f64::from(n.score);
                            if confidence > 0.0 {
                                expected += confidence.powi(2)
                                    * 0.25
                                    * (similarity(&v, &p.vector) - similarity(&v, &n.vector));
                            }
                        }
                    }
                    assert!(
                        (hit["score"].as_f64().unwrap() - expected).abs() < 1e-5,
                        "{distance} id={id}: {hit} != {expected}"
                    );
                }
                let hits = owner
                    .search(
                        1,
                        &generation,
                        &epoch,
                        "",
                        1,
                        None,
                        None,
                        None,
                        Some(input.clone()),
                        None,
                        None,
                        None,
                        None,
                        SourcePredicates {
                            key_exact: Some("4".into()),
                            ..Default::default()
                        },
                    )
                    .unwrap();
                assert_eq!(hits[0]["id"], 4);
                assert_eq!(
                    owner
                        .search(
                            1,
                            &generation,
                            &epoch,
                            "",
                            1,
                            None,
                            None,
                            None,
                            Some(input.clone()),
                            None,
                            None,
                            None,
                            None,
                            SourcePredicates {
                                key_exact: Some("1".into()),
                                ..Default::default()
                            }
                        )
                        .unwrap(),
                    json!([])
                );
                for (a, b, c) in [
                    (f32::NAN, 1.0, 1.0),
                    (1.0, f32::INFINITY, 1.0),
                    (1.0, -1.0, 1.0),
                    (1.0, 4.01, 1.0),
                    (16.01, 1.0, 1.0),
                    (1.0, 1.0, -16.01),
                ] {
                    let mut invalid = input.clone();
                    invalid.coefficients = FeedbackCoefficients { a, b, c };
                    assert!(
                        owner
                            .search(
                                1,
                                &generation,
                                &epoch,
                                "",
                                1,
                                None,
                                None,
                                None,
                                Some(invalid),
                                None,
                                None,
                                None,
                                None,
                                SourcePredicates::default()
                            )
                            .is_err()
                    );
                }
                for score in [f32::NAN, f32::INFINITY, 16.01, -16.01] {
                    let mut invalid = input.clone();
                    invalid.feedback[0].score = score;
                    assert!(
                        owner
                            .search(
                                1,
                                &generation,
                                &epoch,
                                "",
                                1,
                                None,
                                None,
                                None,
                                Some(invalid),
                                None,
                                None,
                                None,
                                None,
                                SourcePredicates::default()
                            )
                            .is_err()
                    );
                }
            }
        }
        assert!(feedback_coefficients_valid(-16.0, 0.0, 16.0));
        assert!(feedback_coefficients_valid(16.0, 4.0, -16.0));
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn scalar_filters_enter_dense_lexical_and_fusion_before_candidate_limits() {
        use pg_qdrant_protocol::payload_filter::PayloadFilter;
        let (owner, root, generation, epoch, _) = dense_payload_fixture(true);
        let dense = RepresentationQuery {
            representation: "dot".into(),
            model_id: "fixture".into(),
            model_version: "r1".into(),
            vector: RepresentationVector::Dense(vec![1., 0.]),
        };
        let search = |expression: Value, fusion: Option<SourceFusion>, lexical: bool| {
            let predicates = SourcePredicates {
                payload_filter: Some(PayloadFilter(expression)),
                ..Default::default()
            };
            owner
                .search(
                    1,
                    &generation,
                    &epoch,
                    "anchor",
                    1,
                    if lexical { None } else { Some(dense.clone()) },
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    fusion,
                    predicates,
                )
                .unwrap()
        };
        // The unfiltered nearest point is 1; the best admissible point is 4.
        // A post-top-k predicate would return zero rather than this filled result.
        let result = search(json!({"field":"category","eq":"Allow"}), None, false);
        assert_eq!(result[0]["id"], 4);
        for expression in [
            json!({"field":"quantity","eq":4}),
            json!({"field":"price","eq":0.4}),
            json!({"all":[{"field":"category","prefix":"All"},{"field":"quantity","range":{"gte":4,"lt":5}}]}),
            json!({"any":[{"field":"quantity","eq":4},{"all":[{"field":"available","eq":true},{"not":{"field":"quantity","eq":2}}]}]}),
        ] {
            for fusion in [None, Some(SourceFusion::Rrf), Some(SourceFusion::Dbsf)] {
                assert_eq!(search(expression.clone(), fusion, false)[0]["id"], 4);
            }
            assert_eq!(search(expression, None, true)[0]["id"], 4);
        }
        assert_eq!(
            search(json!({"field":"available","eq":true}), None, false)[0]["id"],
            2
        );
        assert_eq!(
            search(json!({"field":"quantity","is_null":true}), None, false),
            json!([])
        );
        assert_eq!(
            search(json!({"field":"category","prefix":"all"}), None, false),
            json!([])
        );
        assert!(
            owner
                .search(
                    1,
                    &generation,
                    &epoch,
                    "anchor",
                    1,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    SourcePredicates {
                        payload_filter: Some(PayloadFilter(
                            json!({"field":"fingerprint","eq":"fixture"})
                        )),
                        ..Default::default()
                    }
                )
                .is_err()
        );
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

    fn dense_example_fixture() -> (SourceOwner, PathBuf, String, String, [[f32; 2]; 7]) {
        dense_payload_fixture(false)
    }

    fn dense_payload_fixture(
        payload: bool,
    ) -> (SourceOwner, PathBuf, String, String, [[f32; 2]; 7]) {
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
            payload_contract: if payload {
                BTreeMap::from([
                    ("category".into(), PayloadKind::Keyword),
                    ("quantity".into(), PayloadKind::Integer),
                    ("price".into(), PayloadKind::Float),
                    ("available".into(), PayloadKind::Bool),
                ])
            } else {
                BTreeMap::new()
            },
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
                        payload: Some(if payload {
                            BTreeMap::from([
                                (
                                    "category".into(),
                                    json!(if id % 2 == 0 { "Allow" } else { "Denied" }),
                                ),
                                ("quantity".into(), json!(id as i64)),
                                ("price".into(), json!(id as f64 / 10.0)),
                                ("available".into(), json!(id == 2)),
                            ])
                        } else {
                            BTreeMap::new()
                        }),
                        payload_fingerprint: Some("0".repeat(64)),
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
    fn source_statistics_native_domain_facets_and_admission() {
        let (owner, root, generation, epoch, _) = dense_payload_fixture(true);
        let result = owner
            .statistics(
                1,
                &generation,
                &epoch,
                (1..=7).collect(),
                Some("category".into()),
                1,
                None,
                None,
                SourcePredicates::default(),
            )
            .unwrap();
        assert_eq!(result["points"], 7);
        assert_eq!(result["proof"].as_array().unwrap().len(), 7);
        assert_eq!(
            result["facet"]["hits"],
            json!([{"value":"Denied","count":4}])
        );
        assert_eq!(result["facet"]["values_complete"], false);
        let predicates: SourcePredicates = serde_json::from_value(json!({
            "payload_filter":{"field":"category","eq":"Allow"}}))
        .unwrap();
        let result = owner
            .statistics(
                1,
                &generation,
                &epoch,
                (1..=7).collect(),
                Some("available".into()),
                100,
                None,
                None,
                predicates,
            )
            .unwrap();
        assert_eq!(result["points"], 3);
        assert_eq!(
            result["facet"]["hits"],
            json!([
            {"value":false,"count":2},{"value":true,"count":1}])
        );
        assert_eq!(result["facet"]["values_complete"], true);
        assert_eq!(result["proof"].as_array().unwrap().len(), 7);
        for ids in [
            vec![1, 1],
            vec![0],
            (1..=1001).collect(),
            (1..=6).collect(),
            vec![99],
        ] {
            assert!(
                owner
                    .statistics(
                        1,
                        &generation,
                        &epoch,
                        ids,
                        None,
                        1,
                        None,
                        None,
                        SourcePredicates::default()
                    )
                    .is_err()
            );
        }
        for alias in ["price", "unknown"] {
            assert!(
                owner
                    .statistics(
                        1,
                        &generation,
                        &epoch,
                        (1..=7).collect(),
                        Some(alias.into()),
                        100,
                        None,
                        None,
                        SourcePredicates::default()
                    )
                    .is_err()
            );
        }
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn source_groups_native_fill_and_independent_dense_scores() {
        let (owner,root,generation,epoch,values)=dense_payload_fixture(true);
        for alias in ["dot","euclid"] {
            for field in ["category","quantity"] {
                let groups=SourceGroups {field:field.into(),q:"anchor".into(),groups:2,group_size:2,
                    representation_query:Some(RepresentationQuery {representation:alias.into(),model_id:"fixture".into(),
                        model_version:"r1".into(),vector:RepresentationVector::Dense(vec![1.,0.])})};
                let result=owner.statistics(1,&generation,&epoch,(1..=7).collect(),None,1,None,Some(groups.clone()),SourcePredicates::default()).unwrap();
                let rows=result["grouped"]["groups"].as_array().unwrap();
                assert_eq!(rows.len(),2);
                assert_eq!(result["grouped"]["groups_complete"],false);
                let mut best=Vec::new();
                for group in rows {
                    let actual=group["hits"].as_array().unwrap();
                    let score=|n:usize|if alias=="dot" {values[n][0] as f64} else {(1.-values[n][0] as f64).hypot(values[n][1] as f64)};
                    let mut expected:Vec<_>=(0..7).filter(|n|if field=="category" {
                        group["value"]==json!(if (n+1)%2==0 {"Allow"}else{"Denied"})
                    }else {group["value"]==json!(n+1)}).map(score).collect();
                    expected.sort_by(|a,b|if alias=="dot" {b.total_cmp(a)}else{a.total_cmp(b)});
                    expected.truncate(2);
                    assert_eq!(actual.len(),expected.len());
                    for (hit,expected) in actual.iter().zip(expected) {
                        let n=hit["id"].as_u64().unwrap() as usize-1;
                        assert!((hit["score"].as_f64().unwrap()-expected).abs()<1e-6);
                        assert!((score(n)-expected).abs()<1e-6);
                    }
                    best.push(actual[0]["score"].as_f64().unwrap());
                }
                assert!(if alias=="dot" {best[0]>=best[1]}else{best[0]<=best[1]});
                let predicates=serde_json::from_value(json!({"payload_filter":{"field":"category","eq":"Allow"}})).unwrap();
                let filtered=owner.statistics(1,&generation,&epoch,(1..=7).collect(),None,1,None,Some(groups),predicates).unwrap();
                for group in filtered["grouped"]["groups"].as_array().unwrap() {
                    for hit in group["hits"].as_array().unwrap() {assert_eq!(hit["id"].as_u64().unwrap()%2,0);}
                }
            }
        }
        drop(owner);std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn source_matrix_sample_domain_and_independent_dense_scores() {
        let (owner, root, generation, epoch, values) = dense_payload_fixture(true);
        let request = || {
            Some(SourceMatrix {
                representation: "dot".into(),
                sample_size: 7,
                neighbors: 6,
            })
        };
        let result = owner
            .statistics(
                1,
                &generation,
                &epoch,
                (1..=7).collect(),
                None,
                1,
                request(),
                None,
                SourcePredicates::default(),
            )
            .unwrap();
        let rows = result["matrix"]["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 7);
        assert_eq!(result["matrix"]["exact"], false);
        for row in rows {
            let from = row["id"].as_u64().unwrap() as usize - 1;
            assert_eq!(row["neighbors"].as_array().unwrap().len(), 6);
            for hit in row["neighbors"].as_array().unwrap() {
                let to = hit["id"].as_u64().unwrap() as usize - 1;
                assert_ne!(from, to);
                let expected: f64 = values[from]
                    .iter()
                    .zip(&values[to])
                    .map(|(&a, &b)| a as f64 * b as f64)
                    .sum();
                assert!((hit["score"].as_f64().unwrap() - expected).abs() < 1e-6);
            }
        }
        for distance in ["cosine", "euclid", "manhattan"] {
            let result = owner
                .statistics(
                    1,
                    &generation,
                    &epoch,
                    (1..=7).collect(),
                    None,
                    1,
                    Some(SourceMatrix {
                        representation: distance.into(),
                        sample_size: 7,
                        neighbors: 6,
                    }),
                    None,
                    SourcePredicates::default(),
                )
                .unwrap();
            assert_eq!(result["matrix"]["distance"], distance);
            for row in result["matrix"]["rows"].as_array().unwrap() {
                let from = row["id"].as_u64().unwrap() as usize - 1;
                for hit in row["neighbors"].as_array().unwrap() {
                    let to = hit["id"].as_u64().unwrap() as usize - 1;
                    let a = values[from].map(f64::from);
                    let b = values[to].map(f64::from);
                    let expected = match distance {
                        "euclid" => (a[0] - b[0]).hypot(a[1] - b[1]),
                        "manhattan" => (a[0] - b[0]).abs() + (a[1] - b[1]).abs(),
                        _ => {
                            let normalize = |v: [f64; 2]| {
                                let norm = v[0].hypot(v[1]);
                                if norm == 0.0 {
                                    [0.5f64.sqrt(); 2]
                                } else {
                                    v.map(|x| x / norm)
                                }
                            };
                            let a = normalize(a);
                            let b = normalize(b);
                            a[0] * b[0] + a[1] * b[1]
                        }
                    };
                    assert!(
                        (hit["score"].as_f64().unwrap() - expected).abs() < 1e-6,
                        "{distance}: {hit} expected {expected}"
                    );
                }
            }
        }
        let predicates =
            serde_json::from_value(json!({"payload_filter":{"field":"category","eq":"Allow"}}))
                .unwrap();
        let filtered = owner
            .statistics(
                1,
                &generation,
                &epoch,
                (1..=7).collect(),
                None,
                1,
                request(),
                None,
                predicates,
            )
            .unwrap();
        assert_eq!(filtered["matrix"]["sample_ids"], json!([2, 4, 6]));
        for bad in [
            SourceMatrix {
                representation: "dot".into(),
                sample_size: 0,
                neighbors: 1,
            },
            SourceMatrix {
                representation: "dot".into(),
                sample_size: 65,
                neighbors: 1,
            },
            SourceMatrix {
                representation: "dot".into(),
                sample_size: 7,
                neighbors: 33,
            },
            SourceMatrix {
                representation: "unknown".into(),
                sample_size: 7,
                neighbors: 1,
            },
        ] {
            assert!(
                owner
                    .statistics(
                        1,
                        &generation,
                        &epoch,
                        (1..=7).collect(),
                        None,
                        1,
                        Some(bad),
                        None,
                        SourcePredicates::default()
                    )
                    .is_err()
            );
        }
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn source_retrieve_uses_native_order_missing_deletion_and_metadata_projection() {
        let (mut owner, root, generation, epoch, _) = dense_example_fixture();
        let result = owner
            .retrieve(1, &generation, &epoch, vec![7, 999, 3, 1])
            .unwrap();
        assert_eq!(
            result
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v["id"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            vec![7, 3, 1]
        );
        for record in result.as_array().unwrap() {
            assert_eq!(record.as_object().unwrap().len(), 2);
            assert_eq!(record["payload"].as_object().unwrap().len(), 5);
            assert_eq!(record["payload"]["revision"], 1);
            assert_eq!(record["payload"]["fingerprint"], "fixture");
            assert!(record.get("score").is_none());
            assert!(record.get("vectors").is_none());
            assert!(record["payload"].get("body").is_none());
            assert!(record["payload"].get("attributes").is_none());
        }
        assert_eq!(
            owner.retrieve(1, &generation, &epoch, vec![]).unwrap(),
            json!([])
        );
        for ids in [
            vec![0],
            vec![1, 1],
            vec![i64::MAX as u64 + 1],
            (1..=101).collect(),
        ] {
            assert_eq!(
                owner
                    .retrieve(1, &generation, &epoch, ids)
                    .unwrap_err()
                    .code,
                "invalid_parameter"
            );
        }
        assert!(owner.retrieve(2, &generation, &epoch, vec![1]).is_err());
        assert!(
            owner
                .retrieve(1, &generation, &generation, vec![1])
                .is_err()
        );
        let representations = owner.shards[&identity(1, &generation, &epoch).unwrap()]
            .representations
            .clone();
        owner
            .apply(SourceBatch {
                source_contract_version: SOURCE_CONTRACT_VERSION,
                index_id: 1,
                generation: generation.clone(),
                storage_epoch: epoch.clone(),
                consumer_id: epoch.clone(),
                task_id: None,
                retire: false,
                payload_contract: BTreeMap::new(),
                representations,
                events: vec![SourceEvent {
                    event_id: 8,
                    point_id: 3,
                    revision: 2,
                    incarnation: generation.clone(),
                    fingerprint: Some("fixture".into()),
                    key: json!({"type":"bigint","value":"3"}),
                    body: None,
                    payload: None,
                    payload_fingerprint: None,
                    vectors: BTreeMap::new(),
                }],
            })
            .unwrap();
        let after = owner.retrieve(1, &generation, &epoch, vec![3, 7]).unwrap();
        assert_eq!(after.as_array().unwrap().len(), 1);
        assert_eq!(after[0]["id"], 7);
        drop(owner);
        std::fs::remove_dir_all(root).unwrap();
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
            payload_contract: BTreeMap::new(),
            representations: BTreeMap::new(),
            events: vec![SourceEvent {
                event_id: 1,
                point_id: 1,
                revision: 1,
                incarnation: generation.clone(),
                fingerprint: Some("fixture".into()),
                key: json!({"type":"bigint","value":"1"}),
                body: Some("retired searchable point".into()),
                payload: Some(BTreeMap::new()),
                payload_fingerprint: Some("0".repeat(64)),
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

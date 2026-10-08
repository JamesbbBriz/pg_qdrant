//! Executable P0 checks against the published Qdrant Edge API.
//!
//! This is an engine feasibility harness, not the extension's SQL contract.
//! Fixtures are synthetic and do not constitute a retrieval quality benchmark.
//! All shard ownership, threads, and temporary storage are local to this call.

pub mod api_inventory;
#[cfg(feature = "p0-fault-injection")]
pub mod oom_probe;

use std::collections::{BTreeSet, HashMap};
use std::time::Instant;

use qdrant_edge::bm25_embed::{EdgeBm25, EdgeBm25Config};
use qdrant_edge::external::ordered_float::OrderedFloat;
use qdrant_edge::{
    Condition, CountRequest, CreateIndex, Distance, EdgeConfig, EdgeShard, EdgeShardRead,
    EdgeSparseVectorParams, EdgeVectorParams, FieldCondition, FieldIndexOperations, Filter, Fusion,
    GroupRequest, IdfCorpusParams, IdfParams, JsonPath, KeywordIndexParams, Match, MatchPhrase,
    MatchTextAny, Modifier, MultiVectorComparator, MultiVectorConfig, NamedQuery,
    PayloadFieldSchema, PayloadSchemaParams, PointId, PointInsertOperations, PointOperations,
    PointStruct, PointStructPersisted, Prefetch, QueryEnum, QueryRequest, ScoredPoint,
    ScoringQuery, SearchParams, TextIndexParams, TokenizerType, UpdateOperation, ValueVariants,
    Vector, VectorInternal, Vectors, WithPayloadInterface,
};
use serde_json::{Value, json};

type ProbeResult<T> = Result<T, String>;

const MAX_RESULTS: usize = 16;

struct Fixture {
    id: u64,
    tenant: &'static str,
    document: &'static str,
    text: &'static str,
    sku: &'static str,
    dense: [f32; 3],
    tokens: &'static [[f32; 2]],
    sparse_indices: &'static [u32],
    sparse_values: &'static [f32],
}

const FIXTURES: &[Fixture] = &[
    Fixture {
        id: 1,
        tenant: "a",
        document: "doc-a1",
        text: "transaction recovery restores committed records",
        sku: "PG-001",
        dense: [1.0, 0.0, 0.0],
        tokens: &[[1.0, 0.0], [0.0, 1.0]],
        sparse_indices: &[7, 11],
        sparse_values: &[1.0, 0.2],
    },
    Fixture {
        id: 2,
        tenant: "a",
        document: "doc-a1",
        text: "transaction logs support reliable recovery",
        sku: "PG-002",
        dense: [0.9, 0.1, 0.0],
        tokens: &[[0.8, 0.0], [0.0, 0.8]],
        sparse_indices: &[7],
        sparse_values: &[0.8],
    },
    Fixture {
        id: 3,
        tenant: "a",
        document: "doc-a2",
        text: "backup recovery guide",
        sku: "DOC-001",
        dense: [0.8, 0.2, 0.0],
        tokens: &[[0.6, 0.2]],
        sparse_indices: &[11],
        sparse_values: &[1.0],
    },
    Fixture {
        id: 4,
        tenant: "b",
        document: "doc-b1",
        text: "transaction recovery private",
        sku: "PG-secret",
        dense: [1.0, 0.0, 0.0],
        tokens: &[[1.0, 0.0], [0.0, 1.0]],
        sparse_indices: &[7],
        sparse_values: &[5.0],
    },
    Fixture {
        id: 5,
        tenant: "a",
        document: "doc-a3",
        text: "catalog products available",
        sku: "QA-002",
        dense: [0.0, 0.0, 1.0],
        tokens: &[[0.0, 0.1]],
        sparse_indices: &[99],
        sparse_values: &[1.0],
    },
    Fixture {
        id: 6,
        tenant: "a",
        document: "doc-a4",
        text: "事务回滚不应进入检索索引",
        sku: "ZH-001",
        dense: [0.0, 1.0, 0.0],
        tokens: &[[0.1, 0.1]],
        sparse_indices: &[50],
        sparse_values: &[1.0],
    },
    Fixture {
        id: 7,
        tenant: "a",
        document: "doc-a5",
        text: "primary key reuse prevents stale deletion",
        sku: "KEY-001",
        dense: [0.1, 1.0, 0.0],
        tokens: &[[0.3, 0.1]],
        sparse_indices: &[66],
        sparse_values: &[1.0],
    },
    Fixture {
        id: 8,
        tenant: "a",
        document: "doc-a6",
        text: "事务恢复与混合查询",
        sku: "ZH-002",
        dense: [0.2, 0.9, 0.0],
        tokens: &[[0.2, 0.2]],
        sparse_indices: &[50, 66],
        sparse_values: &[0.5, 0.5],
    },
];

fn context<T>(result: Result<T, impl std::fmt::Display>, label: &str) -> ProbeResult<T> {
    result.map_err(|e| format!("{label}: {e}"))
}

fn require(condition: bool, message: &str) -> ProbeResult<()> {
    if condition {
        Ok(())
    } else {
        Err(message.to_owned())
    }
}

fn key(name: &str) -> ProbeResult<JsonPath> {
    name.parse()
        .map_err(|()| format!("invalid fixture payload key: {name}"))
}

fn neutral_bm25() -> ProbeResult<EdgeBm25> {
    // StemmingAlgorithm/StopwordsInterface are fields of the public config but
    // not re-exported names in 0.8.0. Deserialize this project-owned constant
    // into the public config. Never expose this serde layout as SQL options.
    let config: EdgeBm25Config = context(
        serde_json::from_value(json!({
            "tokenizer": "multilingual",
            "stemmer": {"type": "none"},
            "stopwords": {},
            "lowercase": true,
            "ascii_folding": false,
            "avg_len": 16.0
        })),
        "language-neutral BM25 configuration",
    )?;
    context(EdgeBm25::new(config), "create BM25")
}

fn shard_config() -> EdgeConfig {
    let dense = EdgeVectorParams::builder(3, Distance::Dot).build();
    let tokens = EdgeVectorParams::builder(2, Distance::Dot)
        .multivector_config(MultiVectorConfig {
            comparator: MultiVectorComparator::MaxSim,
        })
        .build();
    EdgeConfig {
        vectors: HashMap::from([("dense".to_owned(), dense), ("tokens".to_owned(), tokens)]),
        sparse_vectors: HashMap::from([
            (
                "bm25".to_owned(),
                EdgeSparseVectorParams {
                    modifier: Some(Modifier::Idf),
                    ..Default::default()
                },
            ),
            ("learned".to_owned(), EdgeSparseVectorParams::default()),
        ]),
        max_search_threads: Some(2),
        on_disk_payload: Some(false),
        ..Default::default()
    }
}

fn create_payload_index(
    shard: &EdgeShard,
    field: &str,
    params: PayloadSchemaParams,
) -> ProbeResult<()> {
    context(
        shard.update(UpdateOperation::FieldIndexOperation(
            FieldIndexOperations::CreateIndex(CreateIndex {
                field_name: key(field)?,
                field_schema: Some(PayloadFieldSchema::FieldParams(params)),
            }),
        )),
        "create payload index",
    )
}

fn prepare_keyword_indexes(shard: &EdgeShard) -> ProbeResult<()> {
    // Authorization and matching structures precede insertion/optimization.
    create_payload_index(
        shard,
        "tenant",
        PayloadSchemaParams::Keyword(KeywordIndexParams {
            is_tenant: Some(true),
            ..Default::default()
        }),
    )?;
    create_payload_index(
        shard,
        "document",
        PayloadSchemaParams::Keyword(Default::default()),
    )?;
    create_payload_index(
        shard,
        "sku",
        PayloadSchemaParams::Keyword(KeywordIndexParams {
            prefix: Some(true),
            ..Default::default()
        }),
    )
}

fn prepare_payload_indexes(shard: &EdgeShard) -> ProbeResult<()> {
    prepare_keyword_indexes(shard)?;
    create_payload_index(
        shard,
        "body",
        PayloadSchemaParams::Text(TextIndexParams {
            tokenizer: TokenizerType::Multilingual,
            lowercase: Some(true),
            phrase_matching: Some(true),
            ..Default::default()
        }),
    )?;
    create_payload_index(
        shard,
        "body_prefix",
        PayloadSchemaParams::Text(TextIndexParams {
            tokenizer: TokenizerType::Prefix,
            lowercase: Some(true),
            min_token_len: Some(2),
            max_token_len: Some(32),
            ..Default::default()
        }),
    )
}

fn upsert_fixtures(shard: &EdgeShard, bm25: &EdgeBm25) -> ProbeResult<()> {
    let mut points: Vec<PointStructPersisted> = Vec::new();
    for f in FIXTURES {
        let tokens = context(
            Vector::new_multi(f.tokens.iter().map(|v| v.to_vec()).collect::<Vec<_>>()),
            "fixture tokens",
        )?;
        let learned = context(
            Vector::new_sparse(f.sparse_indices, f.sparse_values),
            "fixture sparse",
        )?;
        let vectors = Vectors::new_named([
            ("dense", Vector::new_dense(f.dense.to_vec())),
            ("bm25", Vector::from(bm25.embed_document(f.text))),
            ("learned", learned),
            ("tokens", tokens),
        ]);
        points.push(
            PointStruct::new(
                f.id,
                vectors,
                json!({
                    "tenant": f.tenant, "document": f.document, "body": f.text,
                    "body_prefix": f.text, "sku": f.sku, "revision": 1
                }),
            )
            .into(),
        );
    }
    context(
        shard.update(UpdateOperation::PointOperation(
            PointOperations::UpsertPoints(PointInsertOperations::PointsList(points)),
        )),
        "upsert fixture points",
    )
}

/// Prepare a disposable fixture in a process that a test supervisor will kill.
/// Keeping the returned shard alive prevents a graceful Drop from substituting
/// for the explicit persistence boundary being tested.
pub fn persistence_fixture(path: &std::path::Path, flush: bool) -> ProbeResult<EdgeShard> {
    persistence_fixture_config(path, flush, shard_config(), PersistenceProfile::Full)
}

fn small_wal_config() -> EdgeConfig {
    let mut config = shard_config();
    config.wal_options = Some(qdrant_edge::WalOptions {
        segment_capacity: 65_536,
        segment_queue_len: 0,
        retain_closed: std::num::NonZeroUsize::MIN,
    });
    config
}

/// The complete eight-row fixture with a small WAL for owned corruption tests.
/// Payload/text placement and the normal smoke fixture remain unchanged.
pub fn bounded_persistence_fixture(path: &std::path::Path) -> ProbeResult<EdgeShard> {
    persistence_fixture_config(path, true, small_wal_config(), PersistenceProfile::Full)
}

/// Eight source identities and all four representations for configuration-save
/// ENOSPC only. Cold payload/vector storage avoids eager backing-page population.
/// The two mutable text indexes are deliberately absent: their Edge 0.8 writable
/// loader populates at least 64 MiB regardless of on_disk/memory settings.
pub fn disk_persistence_fixture(path: &std::path::Path) -> ProbeResult<EdgeShard> {
    let mut config = small_wal_config();
    config.on_disk_payload = Some(true);
    for vector in config.vectors.values_mut() {
        vector.on_disk = Some(true);
    }
    persistence_fixture_config(path, true, config, PersistenceProfile::DiskConfig)
}

#[derive(PartialEq)]
enum PersistenceProfile {
    Full,
    DiskConfig,
}

fn persistence_fixture_config(
    path: &std::path::Path,
    flush: bool,
    config: EdgeConfig,
    profile: PersistenceProfile,
) -> ProbeResult<EdgeShard> {
    let report_progress = profile == PersistenceProfile::DiskConfig;
    let bm25 = neutral_bm25()?;
    if report_progress {
        eprintln!("pg_qdrant_p0_stage=fixture_create");
    }
    let shard = context(EdgeShard::new(path, config), "create persistence fixture")?;
    if report_progress {
        eprintln!("pg_qdrant_p0_stage=fixture_indexes");
    }
    match profile {
        PersistenceProfile::Full => prepare_payload_indexes(&shard)?,
        PersistenceProfile::DiskConfig => prepare_keyword_indexes(&shard)?,
    }
    if report_progress {
        eprintln!("pg_qdrant_p0_stage=fixture_upsert");
    }
    upsert_fixtures(&shard, &bm25)?;
    if flush {
        if report_progress {
            eprintln!("pg_qdrant_p0_stage=fixture_flush");
        }
        context(shard.flush(), "persistence fixture explicit flush")?;
    }
    if report_progress {
        eprintln!("pg_qdrant_p0_stage=fixture_ready");
    }
    Ok(shard)
}

fn tenant_filter() -> ProbeResult<Filter> {
    Ok(Filter::new_must(Condition::Field(
        FieldCondition::new_match(
            key("tenant")?,
            Match::new_value(ValueVariants::String("a".to_owned())),
        ),
    )))
}

fn with_match(field: &str, predicate: Match) -> ProbeResult<Filter> {
    Ok(tenant_filter()?.merge(&Filter::new_must(Condition::Field(
        FieldCondition::new_match(key(field)?, predicate),
    ))))
}

fn nearest(name: &str, vector: impl Into<VectorInternal>) -> ScoringQuery {
    ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
        query: vector.into(),
        using: Some(name.to_owned()),
    }))
}

fn request(scoring: Option<ScoringQuery>, filter: Filter) -> QueryRequest {
    let mut request = QueryRequest::new(MAX_RESULTS);
    let idf = scoring
        .as_ref()
        .and_then(ScoringQuery::get_vector_name)
        .filter(|name| *name == "bm25")
        .map(|_| {
            IdfParams::Corpus(IdfCorpusParams {
                corpus: filter.clone(),
            })
        });
    request.query = scoring;
    request.filter = Some(filter.clone());
    request.with_payload = WithPayloadInterface::Bool(true);
    request.params = Some(SearchParams {
        exact: true,
        indexed_only: false,
        idf,
        ..Default::default()
    });
    request
}

fn stage(scoring: ScoringQuery, filter: Filter) -> Prefetch {
    let mut stage = Prefetch::new(MAX_RESULTS);
    let idf = scoring
        .get_vector_name()
        .filter(|name| *name == "bm25")
        .map(|_| {
            IdfParams::Corpus(IdfCorpusParams {
                corpus: filter.clone(),
            })
        });
    stage.query = Some(scoring);
    stage.filter = Some(filter.clone());
    stage.params = Some(SearchParams {
        exact: true,
        indexed_only: false,
        idf,
        ..Default::default()
    });
    stage
}

fn assert_authorized(hits: &[ScoredPoint]) -> ProbeResult<()> {
    for hit in hits {
        require(
            hit.id != PointId::NumId(4),
            "private point escaped retrieval filter",
        )?;
        let payload = hit.payload.as_ref().ok_or("probe expected payload")?;
        require(
            payload.0.get("tenant").and_then(Value::as_str) == Some("a"),
            "wrong tenant payload returned",
        )?;
        require(hit.score.is_finite(), "non-finite search score")?;
    }
    Ok(())
}

fn ids(hits: &[ScoredPoint]) -> ProbeResult<BTreeSet<u64>> {
    hits.iter()
        .map(|h| match h.id {
            PointId::NumId(n) => Ok(n),
            PointId::Uuid(_) => Err("unexpected UUID fixture ID".to_owned()),
        })
        .collect()
}

fn check_ids(
    shard: &EdgeShard,
    request: QueryRequest,
    expected: &[u64],
    label: &str,
) -> ProbeResult<Vec<ScoredPoint>> {
    let hits = context(shard.query(request), label)?;
    assert_authorized(&hits)?;
    require(
        ids(&hits)? == expected.iter().copied().collect(),
        &format!("{label}: unexpected matching IDs {:?}", ids(&hits)?),
    )?;
    Ok(hits)
}

fn record(checks: &mut Vec<Value>, name: &str, capabilities: &[&str], detail: Value) {
    checks.push(
        json!({"name": name, "capabilities": capabilities, "status": "passed", "detail": detail}),
    );
}

/// Run actual public-API engine checks; success is not a P0 phase exit or a
/// release support claim. In particular, PostgreSQL safety is not tested here.
pub fn run_smoke() -> ProbeResult<Value> {
    let started = Instant::now();
    let mut checks = Vec::new();
    let dir = context(
        tempfile::Builder::new()
            .prefix("pg-qdrant-edge-probe-")
            .tempdir(),
        "temporary shard directory",
    )?;
    let bm25 = neutral_bm25()?;
    let shard = context(EdgeShard::new(dir.path(), shard_config()), "create shard")?;
    prepare_payload_indexes(&shard)?;
    upsert_fixtures(&shard, &bm25)?;

    let mut count = CountRequest::new();
    count.filter = Some(tenant_filter()?);
    count.exact = true;
    require(
        context(shard.count(count), "authorized count")? == 7,
        "authorized count must count 7 points, not documents",
    )?;
    record(
        &mut checks,
        "payload_index_and_scoped_count",
        &["Q11", "Q12", "L01", "L02"],
        json!({"points":7,"global_document_total":null}),
    );

    let bm25_query = bm25.embed_query("transaction recovery");
    require(
        !bm25_query.indices.is_empty(),
        "BM25 query unexpectedly empty",
    )?;
    require(
        bm25_query.values.iter().all(|v| *v == 1.0),
        "BM25 query weights should be unit weights",
    )?;
    let text = context(
        shard.query(request(
            Some(nearest("bm25", VectorInternal::Sparse(bm25_query.clone()))),
            tenant_filter()?,
        )),
        "BM25 query",
    )?;
    assert_authorized(&text)?;
    require(
        ids(&text)?.is_superset(&BTreeSet::from([1, 2, 3])),
        "BM25 expected lexical matches missing",
    )?;
    record(
        &mut checks,
        "offline_bm25",
        &["F01", "V02", "Q14"],
        json!({"hits":text.len(),"idf_corpus":"tenant a","encoder":"EdgeBm25","tokenizer":"multilingual","stemming":"disabled","stopwords":"empty"}),
    );

    let dense = context(
        shard.query(request(
            Some(nearest("dense", vec![1.0, 0.0, 0.0])),
            tenant_filter()?,
        )),
        "dense nearest",
    )?;
    assert_authorized(&dense)?;
    require(
        dense
            .first()
            .is_some_and(|h| h.id == PointId::NumId(1) && (h.score - 1.0).abs() < 1e-5),
        "dense nearest score or leader mismatch",
    )?;
    record(
        &mut checks,
        "named_dense_exact",
        &["V01", "Q01"],
        json!({"leader":1,"score":dense[0].score,"distance":"dot","exact_domain":"authorized fixture points"}),
    );

    let learned = context(Vector::new_sparse(vec![7], vec![1.0]), "learned query")?;
    let sparse = check_ids(
        &shard,
        request(Some(nearest("learned", learned.clone())), tenant_filter()?),
        &[1, 2],
        "learned sparse",
    )?;
    require(
        (sparse[0].score - 1.0).abs() < 1e-5,
        "learned sparse score mismatch",
    )?;
    record(
        &mut checks,
        "supplied_sparse",
        &["F02", "V02"],
        json!({"hits":2,"model":"synthetic test values","inference":false,"idf":"disabled"}),
    );

    let mut fusion = request(
        Some(ScoringQuery::Fusion(Fusion::Rrf {
            k: 2,
            weights: Some(vec![OrderedFloat(2.0), OrderedFloat(1.0)]),
        })),
        tenant_filter()?,
    );
    fusion.prefetches = vec![
        stage(nearest("dense", vec![1.0, 0.0, 0.0]), tenant_filter()?),
        stage(nearest("learned", learned.clone()), tenant_filter()?),
    ];
    let fused = context(shard.query(fusion.clone()), "weighted RRF")?;
    assert_authorized(&fused)?;
    // 0-based position 0 contributes 1/(1/2+2-1) + 1/(1/1+2-1).
    require(
        fused.first().is_some_and(|h| {
            h.id == PointId::NumId(1) && (h.score - (2.0 / 3.0 + 0.5)).abs() < 1e-5
        }),
        "weighted RRF semantics differ from pinned source",
    )?;
    record(
        &mut checks,
        "weighted_rrf",
        &["Q02", "Q03"],
        json!({"leader":1,"score":fused[0].score,"formula":"sum(1 / ((zero_based_position + 1) / weight + k - 1))","k":2}),
    );

    fusion.query = Some(ScoringQuery::Fusion(Fusion::Dbsf));
    let dbsf = context(shard.query(fusion.clone()), "DBSF")?;
    assert_authorized(&dbsf)?;
    require(
        dbsf.first().is_some_and(|h| h.id == PointId::NumId(1)),
        "DBSF leader mismatch",
    )?;
    // Independent fixture goldens: the authorized dense scores are
    // [1, .9, .8, 0, 0, .1, .2], so mean=3/7 and sample variance=17/84.
    // The sparse branch scores [1, .8] have mean=.9 and variance=.02.
    // Use the closed-form moments in f64, not the upstream normalization code.
    require(
        ids(&dbsf)? == BTreeSet::from([1, 2, 3, 5, 6, 7, 8]),
        "DBSF must return the full authorized prefetch union",
    )?;
    let dense_scale = 6.0 * (17.0_f64 / 84.0).sqrt();
    let sparse_delta = 0.1 / (6.0 * 0.02_f64.sqrt());
    for (id, dense_score) in [
        (1, 1.0),
        (2, 0.9),
        (3, 0.8),
        (5, 0.0),
        (6, 0.0),
        (7, 0.1),
        (8, 0.2),
    ] {
        let dense_normalized = 0.5 + (dense_score - 3.0 / 7.0) / dense_scale;
        let sparse_normalized = match id {
            1 => 0.5 + sparse_delta,
            2 => 0.5 - sparse_delta,
            _ => 0.0,
        };
        let actual = dbsf
            .iter()
            .find(|hit| hit.id == PointId::NumId(id))
            .ok_or("missing DBSF fixture point")?
            .score as f64;
        require(
            (actual - dense_normalized - sparse_normalized).abs() < 1e-5,
            &format!("DBSF score golden mismatch for point {id}"),
        )?;
    }
    record(
        &mut checks,
        "dbsf",
        &["Q03"],
        json!({"leader":1,"score":dbsf[0].score,"scope":"prefetch distributions","golden_scores_checked":7,"variance":"sample (n-1)"}),
    );

    let mut degenerate = request(Some(ScoringQuery::Fusion(Fusion::Dbsf)), tenant_filter()?);
    degenerate.prefetches = vec![
        stage(
            nearest("dense", vec![1.0, 0.0, 0.0]),
            tenant_filter()?.with_point_ids([PointId::NumId(1)]),
        ),
        stage(
            nearest(
                "learned",
                Vector::new_sparse(vec![7], vec![1.0]).map_err(|e| e.to_string())?,
            ),
            tenant_filter()?.with_point_ids([PointId::NumId(2)]),
        ),
    ];
    let singleton = check_ids(
        &shard,
        degenerate.clone(),
        &[1, 2],
        "DBSF disjoint singleton branches",
    )?;
    require(
        singleton.iter().all(|hit| (hit.score - 0.5).abs() < 1e-5),
        "DBSF singleton branches must each normalize to 0.5",
    )?;

    // A zero query creates a genuine constant-score dense distribution. An
    // absent sparse coordinate supplies an empty second branch.
    degenerate.prefetches = vec![
        stage(nearest("dense", vec![0.0, 0.0, 0.0]), tenant_filter()?),
        stage(
            nearest(
                "learned",
                Vector::new_sparse(vec![1_000_000], vec![1.0]).map_err(|e| e.to_string())?,
            ),
            tenant_filter()?,
        ),
    ];
    let constant = check_ids(
        &shard,
        degenerate.clone(),
        &[1, 2, 3, 5, 6, 7, 8],
        "DBSF constant and empty branches",
    )?;
    require(
        constant.iter().all(|hit| (hit.score - 0.5).abs() < 1e-5),
        "DBSF zero-variance branch must normalize every score to 0.5",
    )?;
    for prefetch in &mut degenerate.prefetches {
        prefetch.filter = Some(tenant_filter()?.with_point_ids([PointId::NumId(999)]));
    }
    check_ids(&shard, degenerate, &[], "DBSF all empty branches")?;
    record(
        &mut checks,
        "dbsf_degenerate_distributions",
        &["Q03"],
        json!({"singleton_score":0.5,"constant_score":0.5,"empty_branch":"no contribution","all_empty":"no hits","tie_order_asserted":false}),
    );

    let tokens = context(
        Vector::new_multi(vec![vec![1.0, 0.0], vec![0.0, 1.0]]),
        "token query",
    )?;
    let mut rerank = request(Some(nearest("tokens", tokens.clone())), tenant_filter()?);
    let mut candidates = stage(
        ScoringQuery::Fusion(Fusion::Rrf {
            k: 2,
            weights: None,
        }),
        tenant_filter()?,
    );
    candidates.prefetches = fusion.prefetches.clone();
    candidates.limit = 3;
    rerank.prefetches = vec![candidates];
    rerank.limit = 2;
    let precise = context(shard.query(rerank), "nested MaxSim reranking")?;
    assert_authorized(&precise)?;
    require(
        precise
            .first()
            .is_some_and(|h| h.id == PointId::NumId(1) && (h.score - 2.0).abs() < 1e-5),
        "MaxSim exact candidate score mismatch",
    )?;
    record(
        &mut checks,
        "maxsim_nested_prefetch",
        &["V03", "Q01", "Q02"],
        json!({"leader":1,"score":precise[0].score,"candidate_budget":3,"global_exact_top_k":false}),
    );

    check_ids(
        &shard,
        request(
            None,
            with_match("body", Match::new_text("transaction recovery"))?,
        ),
        &[1, 2],
        "text AND",
    )?;
    check_ids(
        &shard,
        request(
            None,
            with_match(
                "body",
                Match::TextAny(MatchTextAny {
                    text_any: "transaction catalog".into(),
                }),
            )?,
        ),
        &[1, 2, 5],
        "text OR",
    )?;
    record(
        &mut checks,
        "text_and_or",
        &["F06"],
        json!({"and":[1,2],"or":[1,2,5]}),
    );

    let boolean = with_match("body", Match::new_text("transaction recovery"))?.merge(
        &Filter::new_must_not(Condition::Field(FieldCondition::new_match(
            key("sku")?,
            Match::new_value(ValueVariants::String("PG-002".into())),
        ))),
    );
    check_ids(&shard, request(None, boolean), &[1], "Boolean exclusion")?;
    record(
        &mut checks,
        "boolean_exclusion",
        &["F07", "Q12"],
        json!({"matches":[1],"policy":"tenant AND terms AND NOT excluded SKU"}),
    );

    let phrase_filter = with_match(
        "body",
        Match::Phrase(MatchPhrase {
            phrase: "transaction recovery".into(),
        }),
    )?;
    check_ids(
        &shard,
        request(None, phrase_filter.clone()),
        &[1],
        "contiguous phrase",
    )?;
    let mut constrained = request(
        Some(ScoringQuery::Fusion(Fusion::Rrf {
            k: 2,
            weights: None,
        })),
        phrase_filter.clone(),
    );
    constrained.prefetches = vec![
        stage(nearest("dense", vec![1.0, 0.0, 0.0]), phrase_filter.clone()),
        stage(
            nearest("bm25", VectorInternal::Sparse(bm25_query)),
            phrase_filter.clone(),
        ),
        stage(nearest("learned", learned), phrase_filter),
    ];
    check_ids(
        &shard,
        constrained,
        &[1],
        "phrase applied to every fusion branch",
    )?;
    record(
        &mut checks,
        "phrase_in_all_retrieval_branches",
        &["F08", "Q02", "Q03", "Q12"],
        json!({"matches":[1],"phrase_matching":true}),
    );

    check_ids(
        &shard,
        request(None, with_match("body_prefix", Match::new_text("tran"))?),
        &[1, 2],
        "token prefix",
    )?;
    check_ids(
        &shard,
        request(None, with_match("sku", Match::new_prefix("PG-"))?),
        &[1, 2],
        "whole value prefix",
    )?;
    check_ids(
        &shard,
        request(None, with_match("sku", Match::new_prefix("pg-"))?),
        &[],
        "keyword prefix case sensitivity",
    )?;
    check_ids(
        &shard,
        request(
            None,
            with_match(
                "sku",
                Match::new_value(ValueVariants::String("PG-001".into())),
            )?,
        ),
        &[1],
        "whole value exact",
    )?;
    record(
        &mut checks,
        "distinct_token_and_keyword_prefix",
        &["F09", "F10", "F11"],
        json!({"token_prefix":"tran","keyword_prefix":"PG-","keyword_case_sensitive":true}),
    );

    let chinese = bm25.embed_query("事务回滚");
    require(
        !chinese.indices.is_empty(),
        "Chinese analyzer emitted no query terms",
    )?;
    let chinese_hits = context(
        shard.query(request(
            Some(nearest("bm25", VectorInternal::Sparse(chinese))),
            tenant_filter()?,
        )),
        "Chinese BM25",
    )?;
    assert_authorized(&chinese_hits)?;
    require(
        ids(&chinese_hits)?.contains(&6),
        "Chinese source document absent",
    )?;
    record(
        &mut checks,
        "chinese_offline_smoke",
        &["F01", "F04", "F05"],
        json!({"required_match":6,"quality_benchmark":false}),
    );

    let group_request = GroupRequest::new(
        request(
            Some(nearest("dense", vec![1.0, 0.0, 0.0])),
            tenant_filter()?,
        ),
        key("document")?,
        3,
        2,
    );
    let mut groups = context(shard.query_groups(group_request), "filtered grouping")?;
    require(
        groups.len() == 3,
        "grouping should fill three distinct document groups",
    )?;
    for group in &mut groups {
        // Edge 0.8.0's grouping driver projects only the group_by payload.
        // Fetch bounded source payloads through another authorized HasId query
        // before attaching provenance; preserve the grouping scores/order.
        let group_filter = tenant_filter()?.with_point_ids(group.hits.iter().map(|hit| hit.id));
        let hydrated = context(
            shard.query(request(None, group_filter)),
            "authorized group source hydration",
        )?;
        assert_authorized(&hydrated)?;
        require(
            ids(&hydrated)? == ids(&group.hits)?,
            "group sources changed during hydration",
        )?;
        for hit in &mut group.hits {
            hit.payload = hydrated
                .iter()
                .find(|row| row.id == hit.id)
                .ok_or("missing group source")?
                .payload
                .clone();
        }
        assert_authorized(&group.hits)?;
    }
    require(
        groups[0].hits.len() == 2,
        "first document should expose two representative source chunks",
    )?;
    record(
        &mut checks,
        "authorized_document_groups",
        &["Q09"],
        json!({"documents":groups.len(),"first_document_chunks":groups[0].hits.len(),"source_hydration":"authorized HasId query"}),
    );

    // A controlled mismatch demonstrates why configuring a payload text index
    // cannot be treated as configuring EdgeBm25's independent analyzer.
    let english = context(
        EdgeBm25::new(EdgeBm25Config::default()),
        "default English BM25",
    )?;
    require(
        english.embed_query("running").indices == english.embed_query("run").indices,
        "expected default English stemming",
    )?;
    require(
        bm25.embed_query("running").indices != bm25.embed_query("run").indices,
        "neutral analyzer must preserve inflections",
    )?;
    record(
        &mut checks,
        "explicit_analyzer_defaults",
        &["F04"],
        json!({"bm25_default_stems":true,"neutral_policy_stems":false,"full_offset_parity_verified":false}),
    );

    // Persist explicitly before ACK-like success. Do not rely on Drop or on
    // update() merely having appended an operation to the Edge WAL.
    context(shard.flush(), "explicit flush")?;
    drop(shard);
    let reopened = context(EdgeShard::load(dir.path(), None), "reopen flushed shard")?;
    check_ids(
        &reopened,
        request(
            None,
            with_match(
                "body",
                Match::Phrase(MatchPhrase {
                    phrase: "transaction recovery".into(),
                }),
            )?,
        ),
        &[1],
        "phrase after reopen",
    )?;
    let after_reopen = context(
        reopened.query(request(Some(nearest("tokens", tokens)), tenant_filter()?)),
        "MaxSim after reopen",
    )?;
    assert_authorized(&after_reopen)?;
    require(
        after_reopen
            .first()
            .is_some_and(|h| h.id == PointId::NumId(1) && (h.score - 2.0).abs() < 1e-5),
        "reopened MaxSim mismatch",
    )?;
    context(reopened.flush(), "final flush")?;
    drop(reopened);
    record(
        &mut checks,
        "flush_close_reopen",
        &["L01", "L04"],
        json!({"explicit_flush":true,"abrupt_crash_test":false,"wal_replay_claim":false}),
    );

    Ok(json!({
        "schema_version":1,"engine":"qdrant-edge","engine_version":"0.8.0",
        "kind":"synthetic_engine_smoke","status":"passed",
        "elapsed_ms":started.elapsed().as_millis(),"fixture_points":FIXTURES.len(),
        "search_threads":2,"checks":checks,
        "sql_integration_verified":false,"release_supported":false,
        "unverified":["P0 crash/kill/OOM/disk-full isolation", "complete analyzer/offset parity",
            "real multilingual relevance evaluation", "F13-F20 product experience",
            "quantization/MRL/visual/explore", "PostgreSQL transactions and permissions",
            "old-version migrations and rollback", "hard in-flight cancellation"]
    }))
}

#[cfg(test)]
mod tests {
    #[test]
    fn actual_edge_smoke() {
        let report = super::run_smoke().expect("published Edge smoke must pass");
        assert_eq!(report["status"], "passed");
    }
}

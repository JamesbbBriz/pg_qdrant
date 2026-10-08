//! Bounded public-API runtime probes for Q04-Q08 and Q10 in Edge 0.8.0.
//!
//! These synthetic inputs test mathematical semantics and explicit filters,
//! not PostgreSQL permissions, retrieval quality, or supported SQL interfaces.
//! Each request and each leaf prefetch carries the same fixed tenant filter.
//! Expected values are calculated from six fixed two-dimensional Dot vectors.
//! No private upstream module, model download, service, or new dependency is used.
//!
//! Fixed published source used to derive the assertions:
//! - https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/vector_storage/query/reco_query.rs
//! - https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/vector_storage/query/discover_query.rs
//! - https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/vector_storage/query/context_query.rs
//! - https://docs.rs/crate/qdrant-edge/0.8.0/source/src/segment/vector_storage/query/feedback_query.rs
//! - https://docs.rs/crate/qdrant-edge/0.8.0/source/src/shard/query/planned_query.rs
//! - https://docs.rs/crate/qdrant-edge/0.8.0/source/src/edge/read_view/ops/query.rs
//!
//! Scope limits: exact dense Dot with one synthetic tenant policy; MMR only at
//! the root; one arithmetic Formula; integer OrderBy; no random-distribution
//! claim. Sparse/multivector/quantized combinations, real PG authorization,
//! expression allowlists, lambda validation, cancellation, and quality remain
//! separate gates. Empty advanced example lists must be checked by the project
//! planner: this Rust API does not consistently reject them.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use qdrant_edge::external::ordered_float::OrderedFloat;
use qdrant_edge::{
    Condition, ContextPair, ContextQuery, CreateIndex, Direction, DiscoverQuery, Distance,
    EdgeConfig, EdgeShard, EdgeVectorParams, Expression, FeedbackItem, FeedbackNaiveQuery,
    FieldCondition, FieldIndexOperations, Filter, Formula, IntegerIndexParams, JsonPath,
    KeywordIndexParams, Match, Mmr, NaiveFeedbackStrategy, NamedQuery, OperationError, OrderBy,
    OrderValue, PayloadFieldSchema, PayloadSchemaParams, PointId, PointInsertOperations,
    PointOperations, PointStruct, Prefetch, QueryEnum, QueryRequest, RecommendQuery, Sample,
    ScoredPoint, ScoringQuery, SearchParams, StartFrom, UpdateOperation, ValueVariants, Vector,
    VectorInternal, Vectors, WalOptions, WithPayloadInterface,
};
use serde_json::json;

const MAX_CANDIDATES: usize = 8;
const AUTHORIZED_POINTS: usize = 5;
const SOURCE: &str = "dense";
const EPSILON: f32 = 1e-5;

// The last point would dominate nearest, recommendation, Formula, and order-by
// queries if the fixed tenant filter were absent. It is never authorized here.
const ROWS: &[(u64, &str, [f32; 2], i64)] = &[
    (1, "a", [1.0, 0.0], 1),
    (2, "a", [0.8, 0.2], 3),
    (3, "a", [0.4, 0.6], 2),
    (4, "a", [0.0, 1.0], 9),
    (5, "a", [-1.0, 0.0], 7),
    (6, "b", [2.0, -1.0], 1000),
];

struct Fixture {
    // Field order keeps the directory alive until the engine is dropped.
    shard: EdgeShard,
    _directory: tempfile::TempDir,
}

impl Fixture {
    fn new() -> Self {
        pg_qdrant_edge_probe::cpu::require().expect("fixed Edge native CPU baseline");
        let directory = tempfile::tempdir().expect("owned advanced-query fixture directory");
        let mut dense = EdgeVectorParams::builder(2, Distance::Dot).build();
        dense.on_disk = Some(true);
        let config = EdgeConfig {
            vectors: HashMap::from([(SOURCE.to_owned(), dense)]),
            max_search_threads: Some(2),
            on_disk_payload: Some(true),
            wal_options: Some(WalOptions {
                segment_capacity: 65_536,
                segment_queue_len: 0,
                retain_closed: std::num::NonZeroUsize::MIN,
            }),
            ..Default::default()
        };
        let shard = EdgeShard::new(directory.path(), config).expect("create bounded dense fixture");
        for (field, params) in [
            (
                "tenant",
                PayloadSchemaParams::Keyword(KeywordIndexParams {
                    is_tenant: Some(true),
                    ..Default::default()
                }),
            ),
            (
                "priority",
                PayloadSchemaParams::Integer(IntegerIndexParams {
                    lookup: Some(true),
                    range: Some(true),
                    ..Default::default()
                }),
            ),
        ] {
            shard
                .update(UpdateOperation::FieldIndexOperation(
                    FieldIndexOperations::CreateIndex(CreateIndex {
                        field_name: key(field),
                        field_schema: Some(PayloadFieldSchema::FieldParams(params)),
                    }),
                ))
                .expect("create tenant and range indexes before inserting vectors");
        }
        let points = ROWS
            .iter()
            .map(|(id, tenant, vector, priority)| {
                PointStruct::new(
                    *id,
                    Vectors::new_named([(SOURCE, Vector::new_dense(vector.to_vec()))]),
                    json!({
                        "tenant": tenant, "priority": priority,
                        "unindexed_priority": priority, "revision": 1
                    }),
                )
                .into()
            })
            .collect();
        shard
            .update(UpdateOperation::PointOperation(
                PointOperations::UpsertPoints(PointInsertOperations::PointsList(points)),
            ))
            .expect("upsert six bounded fixtures");
        Self {
            shard,
            _directory: directory,
        }
    }

    fn query(&self, request: QueryRequest) -> Vec<ScoredPoint> {
        assert!(request.limit > 0 && request.limit <= MAX_CANDIDATES);
        assert!(request.filter.is_some(), "root filter is mandatory");
        for prefetch in &request.prefetches {
            assert!(prefetch.filter.is_some(), "leaf filter is mandatory");
            assert!(prefetch.limit > 0 && prefetch.limit <= MAX_CANDIDATES);
            assert!(
                prefetch.prefetches.is_empty(),
                "the probe has only one prefetch level"
            );
        }
        let budget = request.limit;
        let hits = self
            .shard
            .query(request)
            .expect("execute public advanced query");
        assert!(hits.len() <= budget);
        let mut ids = BTreeSet::new();
        for hit in &hits {
            assert!(
                matches!(hit.id, PointId::NumId(1..=5)),
                "unauthorized fixture ID"
            );
            assert!(ids.insert(hit.id), "duplicate result ID");
            assert_eq!(
                hit.payload.as_ref().expect("requested payload").0["tenant"],
                "a"
            );
            assert_eq!(hit.payload.as_ref().unwrap().0["revision"], 1);
            assert!(hit.score.is_finite());
        }
        hits
    }
}

fn key(field: &str) -> JsonPath {
    field.parse().expect("fixed fixture payload path")
}

fn tenant_filter() -> Filter {
    Filter::new_must(Condition::Field(FieldCondition::new_match(
        key("tenant"),
        Match::new_value(ValueVariants::String("a".into())),
    )))
}

fn exact_params() -> SearchParams {
    SearchParams {
        exact: true,
        indexed_only: false,
        ..Default::default()
    }
}

fn vector(value: [f32; 2]) -> VectorInternal {
    VectorInternal::Dense(value.to_vec())
}

fn named<T>(query: T) -> NamedQuery<T> {
    NamedQuery {
        query,
        using: Some(SOURCE.into()),
    }
}

fn nearest(value: [f32; 2]) -> ScoringQuery {
    ScoringQuery::Vector(QueryEnum::Nearest(named(vector(value))))
}

fn request(query: ScoringQuery, limit: usize) -> QueryRequest {
    assert!(limit > 0 && limit <= MAX_CANDIDATES);
    let mut request = QueryRequest::new(limit);
    request.query = Some(query);
    request.filter = Some(tenant_filter());
    request.params = Some(exact_params());
    request.with_payload = WithPayloadInterface::Bool(true);
    request
}

fn prefetch(value: [f32; 2], limit: usize) -> Prefetch {
    assert!(limit > 0 && limit <= MAX_CANDIDATES);
    let mut prefetch = Prefetch::new(limit);
    prefetch.query = Some(nearest(value));
    prefetch.filter = Some(tenant_filter());
    prefetch.params = Some(exact_params());
    prefetch
}

fn ids(hits: &[ScoredPoint]) -> Vec<u64> {
    hits.iter()
        .map(|hit| match hit.id {
            PointId::NumId(id) => id,
            PointId::Uuid(_) => panic!("fixture uses numeric IDs"),
        })
        .collect()
}

fn assert_scores(hits: &[ScoredPoint], expected: &[(u64, f32)]) {
    assert_eq!(hits.len(), expected.len());
    for (hit, (id, score)) in hits.iter().zip(expected) {
        assert_eq!(hit.id, PointId::NumId(*id));
        assert!(
            (hit.score - score).abs() < EPSILON,
            "point {id}: actual score {}, expected {score}",
            hit.score
        );
    }
}

fn context_pair() -> ContextPair<VectorInternal> {
    ContextPair {
        positive: vector([1.0, 0.0]),
        negative: vector([0.0, 1.0]),
    }
}

fn formula(expression: Expression, defaults: HashMap<String, serde_json::Value>) -> ScoringQuery {
    ScoringQuery::Formula(
        Formula {
            formula: expression,
            defaults,
        }
        .try_into()
        .expect("parse public Formula"),
    )
}

#[test]
fn q04_recommendation_strategies_have_distinct_exact_scores() {
    let fixture = Fixture::new();
    // Fixed source: src/segment/vector_storage/query/reco_query.rs.
    // BestScore compares strongest positive/negative Dot scores, then uses a
    // bounded signed transform. It is not a vector average or raw Dot score.
    let best = RecommendQuery::new(vec![vector([1.0, 0.0])], vec![vector([0.0, 1.0])]);
    let hits = fixture.query(request(
        ScoringQuery::Vector(QueryEnum::RecommendBestScore(named(best))),
        AUTHORIZED_POINTS,
    ));
    assert_scores(
        &hits,
        &[
            (1, 0.75),
            (2, 13.0 / 18.0),
            (5, -0.5),
            (3, -11.0 / 16.0),
            (4, -0.75),
        ],
    );

    // SumScores adds two positive Dot scores and subtracts the negative one.
    // These fixture goldens are 1.5*x - y, all with distinct output scores.
    let sum = RecommendQuery::new(
        vec![vector([1.0, 0.0]), vector([0.5, 0.0])],
        vec![vector([0.0, 1.0])],
    );
    let hits = fixture.query(request(
        ScoringQuery::Vector(QueryEnum::RecommendSumScores(named(sum))),
        AUTHORIZED_POINTS,
    ));
    assert_scores(&hits, &[(1, 1.5), (2, 1.0), (3, 0.0), (4, -1.0), (5, -1.5)]);
}

#[test]
fn q05_discover_and_context_apply_the_published_pair_semantics() {
    let fixture = Fixture::new();
    // Discover combines a context rank with a bounded target score. The
    // negative-ranked points below remain ordered by target similarity.
    let discover = DiscoverQuery::new(vector([1.0, 0.0]), vec![context_pair()]);
    let hits = fixture.query(request(
        ScoringQuery::Vector(QueryEnum::Discover(named(discover))),
        AUTHORIZED_POINTS,
    ));
    assert_scores(
        &hits,
        &[
            (1, 1.75),
            (2, 31.0 / 18.0),
            (3, -5.0 / 14.0),
            (4, -0.5),
            (5, -0.75),
        ],
    );

    // Context-only scoring has deliberate ties once a point satisfies the
    // positive-over-negative relation. Assert scores by identity, not tie order.
    let context = ContextQuery::new(vec![context_pair()]);
    let hits = fixture.query(request(
        ScoringQuery::Vector(QueryEnum::Context(named(context))),
        AUTHORIZED_POINTS,
    ));
    let actual: BTreeMap<_, _> = hits.iter().map(|hit| (hit.id, hit.score)).collect();
    // The loss is normalized by 1+abs(loss); upstream also subtracts f32::EPSILON.
    let expected = [(1, 0.0), (2, 0.0), (3, -1.0 / 6.0), (4, -0.5), (5, -0.5)];
    assert_eq!(actual.len(), expected.len());
    for (id, score) in expected {
        assert!(
            (actual[&PointId::NumId(id)] - score).abs() < EPSILON,
            "context score for {id}"
        );
    }
}

#[test]
fn q06_feedback_changes_the_scores_with_an_explicit_contract() {
    let fixture = Fixture::new();
    let feedback = FeedbackNaiveQuery {
        target: vector([1.0, 0.0]),
        feedback: vec![
            FeedbackItem {
                vector: vector([1.0, 0.0]),
                score: OrderedFloat(2.0),
            },
            FeedbackItem {
                vector: vector([0.0, 1.0]),
                score: OrderedFloat(0.0),
            },
        ],
        coefficients: NaiveFeedbackStrategy {
            a: OrderedFloat(1.0),
            b: OrderedFloat(2.0),
            c: OrderedFloat(0.25),
        },
    };
    let hits = fixture.query(request(
        ScoringQuery::Vector(QueryEnum::FeedbackNaive(named(feedback.clone()))),
        AUTHORIZED_POINTS,
    ));
    // Relative feedback creates a context pair; its coefficient is
    // (2-0)^b*c = 2^2*.25 = 1, giving score x + (x-y) = 2*x-y.
    assert_scores(&hits, &[(1, 2.0), (2, 1.4), (3, 0.2), (4, -1.0), (5, -2.0)]);

    // Equal feedback scores create no pair, recovering the declared target
    // coefficient rather than inventing an implicit preference.
    let mut equal = feedback;
    for item in &mut equal.feedback {
        item.score = OrderedFloat(1.0);
    }
    let hits = fixture.query(request(
        ScoringQuery::Vector(QueryEnum::FeedbackNaive(named(equal))),
        AUTHORIZED_POINTS,
    ));
    assert_scores(&hits, &[(1, 1.0), (2, 0.8), (3, 0.4), (4, 0.0), (5, -1.0)]);
}

#[test]
fn q07_root_mmr_is_bounded_and_preserves_original_similarity_scores() {
    let fixture = Fixture::new();
    let mmr = |lambda, candidates_limit| {
        ScoringQuery::Mmr(Mmr {
            vector: vector([1.0, 0.0]),
            using: SOURCE.into(),
            lambda: OrderedFloat(lambda),
            candidates_limit,
        })
    };
    // MMR objective is lambda*relevance - (1-lambda)*max similarity to selected
    // candidates. Returned scores are the original nearest Dot scores.
    let hits = fixture.query(request(mmr(0.25, 5), 5));
    assert_scores(&hits, &[(1, 1.0), (5, -1.0), (4, 0.0), (3, 0.4), (2, 0.8)]);
    // Compatibility observation, not the desired SQL output contract: the
    // 0.8.0 root planner merges MMR's dense vector into the final projection
    // even when QueryRequest::new requests no vectors. The project adapter
    // must strip unrequested vectors. An upstream correction changes this
    // version-specific assertion and requires an explicit compatibility review.
    assert!(hits.iter().all(|hit| hit.vector.is_some()));
    eprintln!(
        "{}",
        json!({
            "engine_version": "0.8.0", "observation": "root_mmr_projection",
            "requested_vectors": false, "returned_vectors": true,
            "sql_contract_verified": false
        })
    );
    assert_scores(
        &fixture.query(request(mmr(1.0, 5), 3)),
        &[(1, 1.0), (2, 0.8), (3, 0.4)],
    );
    assert_scores(
        &fixture.query(request(mmr(0.25, 4), 3)),
        &[(1, 1.0), (4, 0.0), (3, 0.4)],
    );

    // With an explicit prefetch, MMR only reranks that candidate domain.
    let mut limited = request(mmr(0.25, 5), 3);
    limited.prefetches = vec![prefetch([1.0, 0.0], 3)];
    assert_scores(&fixture.query(limited), &[(1, 1.0), (3, 0.4), (2, 0.8)]);
}

#[test]
fn q08_formula_uses_prefetched_scores_payload_defaults_and_real_errors() {
    let fixture = Fixture::new();
    // A declared fixture policy, not a general calibration of business values
    // and similarity: score = original Dot + 0.25*integer priority.
    let boost = || {
        formula(
            Expression::Sum(vec![
                Expression::Variable("$score[0]".into()),
                Expression::Mult(vec![
                    Expression::Constant(0.25),
                    Expression::Variable("priority".into()),
                ]),
            ]),
            HashMap::new(),
        )
    };
    let mut full = request(boost(), AUTHORIZED_POINTS);
    full.prefetches = vec![prefetch([1.0, 0.0], AUTHORIZED_POINTS)];
    assert_scores(
        &fixture.query(full),
        &[(4, 2.25), (2, 1.55), (1, 1.25), (3, 0.9), (5, 0.75)],
    );

    let mut limited = request(boost(), AUTHORIZED_POINTS);
    limited.prefetches = vec![prefetch([1.0, 0.0], 3)];
    assert_scores(&fixture.query(limited), &[(2, 1.55), (1, 1.25), (3, 0.9)]);

    let no_prefetch = fixture
        .shard
        .query(request(boost(), 3))
        .expect_err("Formula needs prefetch");
    assert!(matches!(
        no_prefetch,
        OperationError::ValidationError { .. }
    ));
    assert!(
        no_prefetch
            .to_string()
            .contains("cannot apply Formula without prefetches")
    );

    let missing = || {
        Expression::Sum(vec![
            Expression::Variable("$score[0]".into()),
            Expression::Variable("missing_boost".into()),
        ])
    };
    let mut absent = request(formula(missing(), HashMap::new()), 3);
    absent.prefetches = vec![prefetch([1.0, 0.0], 3)];
    let error = fixture
        .shard
        .query(absent)
        .expect_err("missing payload variable has no implicit default");
    assert!(
        matches!(error, OperationError::VariableTypeError { .. }),
        "{error}"
    );

    let mut with_default = request(
        formula(
            missing(),
            HashMap::from([("missing_boost".into(), json!(0.5))]),
        ),
        3,
    );
    with_default.prefetches = vec![prefetch([1.0, 0.0], 3)];
    assert_scores(
        &fixture.query(with_default),
        &[(1, 1.5), (2, 1.3), (3, 0.9)],
    );

    let mut zero_divisor = request(
        formula(
            Expression::Div {
                left: Box::new(Expression::Constant(1.0)),
                right: Box::new(Expression::Constant(0.0)),
                by_zero_default: None,
            },
            HashMap::new(),
        ),
        3,
    );
    zero_divisor.prefetches = vec![prefetch([1.0, 0.0], 3)];
    let error = fixture
        .shard
        .query(zero_divisor)
        .expect_err("nonfinite Formula is an error");
    assert!(
        matches!(error, OperationError::NonFiniteNumber { .. }),
        "{error}"
    );
}

#[test]
fn q10_order_by_and_random_sample_obey_filters_and_candidate_domains() {
    let fixture = Fixture::new();
    let ordered = |field, direction, start_from| {
        ScoringQuery::OrderBy(OrderBy {
            key: key(field),
            direction: Some(direction),
            start_from,
        })
    };
    let hits = fixture.query(request(ordered("priority", Direction::Desc, None), 5));
    assert_eq!(ids(&hits), [4, 5, 2, 3, 1]);
    assert_eq!(
        hits.iter()
            .map(|hit| hit.order_value.clone())
            .collect::<Vec<_>>(),
        [9, 7, 3, 2, 1]
            .into_iter()
            .map(|v| Some(OrderValue::Int(v)))
            .collect::<Vec<_>>()
    );
    let hits = fixture.query(request(
        ordered("priority", Direction::Asc, Some(StartFrom::Integer(3))),
        5,
    ));
    assert_eq!(ids(&hits), [2, 5, 4]);
    let mut limited = request(ordered("priority", Direction::Desc, None), 5);
    limited.prefetches = vec![prefetch([1.0, 0.0], 3)];
    assert_eq!(ids(&fixture.query(limited)), [2, 3, 1]);

    let error = fixture
        .shard
        .query(request(
            ordered("unindexed_priority", Direction::Desc, None),
            3,
        ))
        .expect_err("OrderBy requires a range index");
    assert!(
        matches!(error, OperationError::MissingRangeIndexForOrderBy { .. }),
        "{error}"
    );

    // Sampling all five authorized records is deterministic as a set. A smaller
    // sample only promises a unique authorized subset, not a reproducible order
    // or a distributional quality claim from a tiny fixture.
    let all = fixture.query(request(ScoringQuery::Sample(Sample::Random), 5));
    assert_eq!(
        ids(&all).into_iter().collect::<BTreeSet<_>>(),
        BTreeSet::from([1, 2, 3, 4, 5])
    );
    let small = fixture.query(request(ScoringQuery::Sample(Sample::Random), 3));
    assert_eq!(small.len(), 3);
    let mut limited = request(ScoringQuery::Sample(Sample::Random), 5);
    limited.prefetches = vec![prefetch([1.0, 0.0], 3)];
    assert_eq!(
        ids(&fixture.query(limited))
            .into_iter()
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([1, 2, 3])
    );
}

#[test]
fn advanced_vector_queries_reject_wrong_dimensions_without_fallback() {
    let fixture = Fixture::new();
    let wrong = || VectorInternal::Dense(vec![1.0, 0.0, 0.0]);
    let cases = [
        ScoringQuery::Vector(QueryEnum::RecommendBestScore(named(RecommendQuery::new(
            vec![vector([1.0, 0.0])],
            vec![wrong()],
        )))),
        ScoringQuery::Vector(QueryEnum::RecommendSumScores(named(RecommendQuery::new(
            vec![vector([1.0, 0.0])],
            vec![wrong()],
        )))),
        ScoringQuery::Vector(QueryEnum::Discover(named(DiscoverQuery::new(
            vector([1.0, 0.0]),
            vec![ContextPair {
                positive: vector([1.0, 0.0]),
                negative: wrong(),
            }],
        )))),
        ScoringQuery::Vector(QueryEnum::Context(named(ContextQuery::new(vec![
            ContextPair {
                positive: vector([1.0, 0.0]),
                negative: wrong(),
            },
        ])))),
        ScoringQuery::Vector(QueryEnum::FeedbackNaive(named(FeedbackNaiveQuery {
            target: vector([1.0, 0.0]),
            feedback: vec![FeedbackItem {
                vector: wrong(),
                score: OrderedFloat(1.0),
            }],
            coefficients: NaiveFeedbackStrategy {
                a: OrderedFloat(1.0),
                b: OrderedFloat(1.0),
                c: OrderedFloat(1.0),
            },
        }))),
        ScoringQuery::Mmr(Mmr {
            vector: wrong(),
            using: SOURCE.into(),
            lambda: OrderedFloat(0.25),
            candidates_limit: 5,
        }),
    ];
    for (index, query) in cases.into_iter().enumerate() {
        let error = fixture
            .shard
            .query(request(query, 3))
            .expect_err("incompatible advanced vector dimension");
        assert!(
            matches!(
                error,
                OperationError::WrongVectorDimension {
                    expected_dim: 2,
                    received_dim: 3
                }
            ),
            "case {index}: {error}"
        );
    }
}

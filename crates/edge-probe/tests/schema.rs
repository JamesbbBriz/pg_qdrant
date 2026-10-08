//! Dynamic schema probes against the published Edge 0.8.0 public API.
//! Three synthetic rows, two search threads and a 64 KiB WAL keep the fixture
//! bounded. Tenant predicates are engine inputs, not PostgreSQL authorization.
//! Explicit flush/reopen is not unflushed-WAL or cross-version recovery proof.

use std::collections::HashMap;
use std::num::NonZeroUsize;
use std::path::Path;

use qdrant_edge::*;
use serde_json::json;

fn equals(field: &str, value: &str) -> Filter {
    Filter::new_must(Condition::Field(FieldCondition::new_match(
        field.parse().unwrap(),
        Match::new_value(ValueVariants::String(value.into())),
    )))
}

fn create_index(shard: &EdgeShard, field: &str) {
    shard
        .update(UpdateOperation::FieldIndexOperation(
            FieldIndexOperations::CreateIndex(CreateIndex {
                field_name: field.parse().unwrap(),
                field_schema: Some(PayloadFieldSchema::FieldType(PayloadSchemaType::Keyword)),
            }),
        ))
        .unwrap();
}

fn fixture(path: &Path) -> EdgeShard {
    let shard = EdgeShard::new(
        path,
        EdgeConfig {
            vectors: HashMap::from([(
                "base".into(),
                EdgeVectorParams::builder(2, Distance::Dot).build(),
            )]),
            max_search_threads: Some(2),
            on_disk_payload: Some(false),
            wal_options: Some(WalOptions {
                segment_capacity: 65_536,
                segment_queue_len: 0,
                retain_closed: NonZeroUsize::MIN,
            }),
            ..Default::default()
        },
    )
    .unwrap();
    create_index(&shard, "tenant");
    let points = [(1_u64, "a", "news"), (2, "a", "guide"), (3, "b", "news")]
        .into_iter()
        .map(|(id, tenant, category)| {
            PointStruct::new(
                id,
                Vectors::new_named([("base", vec![id as f32, 1.0])]),
                json!({"tenant":tenant,"category":category}),
            )
            .into()
        })
        .collect();
    shard
        .update(UpdateOperation::PointOperation(
            PointOperations::UpsertPoints(PointInsertOperations::PointsList(points)),
        ))
        .unwrap();
    shard
}

fn records(shard: &EdgeShard) -> Vec<Record> {
    let mut request = RetrieveRequest::new(vec![1.into(), 2.into(), 3.into()]);
    request.with_payload = Some(WithPayloadInterface::Bool(true));
    request.with_vector = Some(WithVector::Bool(true));
    shard.retrieve(request).unwrap()
}

fn named(record: &Record) -> &HashMap<String, VectorInternal> {
    let Some(VectorStructInternal::Named(vectors)) = &record.vector else {
        panic!("expected named vector records")
    };
    vectors
}

fn request(name: &str, vector: Vector, filter: Filter) -> QueryRequest {
    let mut request = QueryRequest::new(3);
    request.query = Some(ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
        using: Some(name.into()),
        query: vector.into(),
    })));
    request.filter = Some(filter);
    request.params = Some(SearchParams {
        exact: true,
        indexed_only: false,
        ..Default::default()
    });
    request.with_payload = WithPayloadInterface::Bool(true);
    request
}

fn assert_scores(shard: &EdgeShard, name: &str, vector: Vector, expected: &[(u64, f32)]) {
    let hits = shard
        .query(request(name, vector, equals("tenant", "a")))
        .unwrap();
    assert_eq!(hits.len(), expected.len());
    for (hit, (id, score)) in hits.iter().zip(expected) {
        assert_eq!(hit.id, PointId::NumId(*id));
        assert!(
            (hit.score - score).abs() < 1e-6,
            "unexpected score: {hit:?}"
        );
        assert_eq!(
            hit.payload.as_ref().unwrap().0.get("tenant"),
            Some(&json!("a"))
        );
    }
}

#[test]
fn payload_index_create_delete_preserves_records_and_filtered_queries_after_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let shard = fixture(dir.path());
    let original = records(&shard);
    let key: JsonPath = "category".parse().unwrap();
    assert!(!shard.info().unwrap().payload_schema.contains_key(&key));
    let query = request(
        "base",
        Vector::new_dense(vec![1.0, 0.0]),
        equals("tenant", "a").merge(&equals("category", "news")),
    );
    let expected = shard.query(query.clone()).unwrap();
    assert_eq!(expected.len(), 1);
    assert_eq!(expected[0].id, PointId::NumId(1));
    assert_eq!(expected[0].score, 1.0);

    // Build the new index over already present payloads, then reopen it.
    create_index(&shard, "category");
    let schema = shard.info().unwrap().payload_schema[&key].clone();
    assert_eq!(schema.data_type, PayloadSchemaType::Keyword);
    assert_eq!(schema.points, 3);
    assert_eq!(shard.query(query.clone()).unwrap(), expected);
    assert_eq!(records(&shard), original);
    shard.flush().unwrap();
    drop(shard);
    let shard = EdgeShard::load(dir.path(), None).unwrap();
    assert_eq!(shard.info().unwrap().payload_schema[&key], schema);
    assert_eq!(shard.query(query.clone()).unwrap(), expected);

    shard
        .update(UpdateOperation::FieldIndexOperation(
            FieldIndexOperations::DeleteIndex(key.clone()),
        ))
        .unwrap();
    assert!(!shard.info().unwrap().payload_schema.contains_key(&key));
    assert_eq!(shard.query(query.clone()).unwrap(), expected);
    shard.flush().unwrap();
    drop(shard);
    let shard = EdgeShard::load(dir.path(), None).unwrap();
    assert!(!shard.info().unwrap().payload_schema.contains_key(&key));
    assert!(
        shard
            .info()
            .unwrap()
            .payload_schema
            .contains_key(&"tenant".parse().unwrap())
    );
    assert_eq!(records(&shard), original);
    assert_eq!(shard.query(query).unwrap(), expected);
}

fn dense_config(size: usize) -> VectorNameConfig {
    VectorNameConfig::dense(DenseVectorConfig {
        size,
        distance: Distance::Dot,
        multivector_config: None,
        datatype: None,
    })
}

fn sparse_config(modifier: Option<Modifier>) -> VectorNameConfig {
    VectorNameConfig::sparse(SparseVectorConfig {
        modifier,
        datatype: None,
    })
}

fn create_vector(shard: &EdgeShard, name: &str, config: VectorNameConfig) -> OperationResult<()> {
    shard.update(UpdateOperation::VectorNameOperation(
        VectorNameOperations::CreateVectorName(CreateVectorName {
            vector_name: name.into(),
            config,
        }),
    ))
}

fn query_added(shard: &EdgeShard, after_filtered_delete: bool) {
    let dense: &[(u64, f32)] = if after_filtered_delete {
        &[(2, 1.0)]
    } else {
        &[(1, 2.0), (2, 1.0)]
    };
    let sparse: &[(u64, f32)] = if after_filtered_delete {
        &[(2, 2.0)]
    } else {
        &[(1, 6.0), (2, 2.0)]
    };
    assert_scores(
        shard,
        "extra_dense",
        Vector::new_dense(vec![1.0, 0.0, 0.0]),
        dense,
    );
    assert_scores(
        shard,
        "extra_sparse",
        Vector::new_sparse(vec![9], vec![2.0]).unwrap(),
        sparse,
    );
}

#[test]
fn dynamic_dense_sparse_names_and_filtered_vector_deletion_survive_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let shard = fixture(dir.path());
    let original = records(&shard);
    create_vector(&shard, "extra_dense", dense_config(3)).unwrap();
    create_vector(&shard, "extra_sparse", sparse_config(None)).unwrap();
    let config = shard.config().clone();
    assert_eq!(config.vectors["extra_dense"].size, 3);
    assert_eq!(config.vectors["extra_dense"].distance, Distance::Dot);
    assert_eq!(config.sparse_vectors["extra_sparse"].modifier, None);
    assert_eq!(records(&shard), original);

    // Same-config replay is idempotent; conflicting identity is rejected before
    // it can change either the advertised schema or the original point data.
    create_vector(&shard, "extra_dense", dense_config(3)).unwrap();
    create_vector(&shard, "extra_sparse", sparse_config(None)).unwrap();
    assert!(matches!(
        create_vector(&shard, "extra_dense", dense_config(4)),
        Err(OperationError::ValidationError { .. })
    ));
    assert!(matches!(
        create_vector(&shard, "extra_sparse", sparse_config(Some(Modifier::Idf))),
        Err(OperationError::ValidationError { .. })
    ));
    assert_eq!(*shard.config(), config);
    assert_eq!(records(&shard), original);

    let points = [(1_u64, 2.0, 3.0), (2, 1.0, 1.0), (3, 100.0, 100.0)]
        .into_iter()
        .map(|(id, dense, sparse)| {
            let point: PointStructPersisted = PointStruct::new(
                id,
                Vectors::new_named([
                    ("extra_dense", Vector::new_dense(vec![dense, 0.0, 0.0])),
                    (
                        "extra_sparse",
                        Vector::new_sparse(vec![9, 19], vec![sparse, 0.5]).unwrap(),
                    ),
                ]),
                json!({}),
            )
            .into();
            PointVectorsPersisted {
                id: point.id,
                vector: point.vector,
            }
        })
        .collect();
    shard
        .update(UpdateOperation::VectorOperation(
            VectorOperations::UpdateVectors(UpdateVectorsOp {
                points,
                update_filter: None,
            }),
        ))
        .unwrap();
    let updated = records(&shard);
    for (before, after) in original.iter().zip(&updated) {
        assert_eq!(before.id, after.id);
        assert_eq!(before.payload, after.payload);
        assert_eq!(named(before)["base"], named(after)["base"]);
        assert_eq!(named(after).len(), 3);
    }
    assert_eq!(
        named(&updated[0])["extra_dense"],
        Vector::new_dense(vec![2.0, 0.0, 0.0]).0
    );
    assert_eq!(
        named(&updated[0])["extra_sparse"],
        Vector::new_sparse(vec![9, 19], vec![3.0, 0.5]).unwrap().0
    );
    query_added(&shard, false);
    shard.flush().unwrap();
    drop(shard);
    let shard = EdgeShard::load(dir.path(), None).unwrap();
    assert_eq!(*shard.config(), config);
    assert_eq!(records(&shard), updated);
    query_added(&shard, false);

    // ID 3 is deliberately in the requested set; the tenant predicate must
    // prevent deletion of its higher-scoring vectors while deleting ID 1's.
    let filter = equals("tenant", "a").merge(&Filter::new_must(Condition::HasId(HasIdCondition {
        has_id: [PointId::NumId(1), PointId::NumId(3)].into_iter().collect(),
    })));
    shard
        .update(UpdateOperation::VectorOperation(
            VectorOperations::DeleteVectorsByFilter(
                filter,
                vec!["extra_dense".into(), "extra_sparse".into()],
            ),
        ))
        .unwrap();
    let mut expected = updated.clone();
    expected[0].vector = original[0].vector.clone();
    assert_eq!(records(&shard), expected);
    assert_eq!(*shard.config(), config); // Deleting row values does not delete schema.
    query_added(&shard, true);
    shard.flush().unwrap();
    drop(shard);
    let shard = EdgeShard::load(dir.path(), None).unwrap();
    assert_eq!(records(&shard), expected);
    query_added(&shard, true);

    for name in ["extra_dense", "extra_sparse"] {
        shard
            .update(UpdateOperation::VectorNameOperation(
                VectorNameOperations::DeleteVectorName(DeleteVectorName {
                    vector_name: name.into(),
                }),
            ))
            .unwrap();
    }
    assert!(!shard.config().vectors.contains_key("extra_dense"));
    assert!(!shard.config().sparse_vectors.contains_key("extra_sparse"));
    assert_eq!(records(&shard), original);
    shard.flush().unwrap();
    drop(shard);
    let shard = EdgeShard::load(dir.path(), None).unwrap();
    assert!(!shard.config().vectors.contains_key("extra_dense"));
    assert!(!shard.config().sparse_vectors.contains_key("extra_sparse"));
    assert_eq!(records(&shard), original);
    for (name, vector) in [
        ("extra_dense", Vector::new_dense(vec![1.0, 0.0, 0.0])),
        (
            "extra_sparse",
            Vector::new_sparse(vec![9], vec![2.0]).unwrap(),
        ),
    ] {
        assert!(
            matches!(shard.query(request(name, vector, equals("tenant", "a"))),
            Err(OperationError::VectorNameNotExists { received_name }) if received_name == name)
        );
    }
    assert_scores(
        &shard,
        "base",
        Vector::new_dense(vec![1.0, 0.0]),
        &[(2, 2.0), (1, 1.0)],
    );
}

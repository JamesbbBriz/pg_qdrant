//! Concrete public Edge operations on disposable, bounded fixtures.
//! Tenant predicates here are engine inputs, not PostgreSQL authorization proof.

use std::collections::{BTreeMap, BTreeSet};

use qdrant_edge::*;
use serde_json::{Value, json};

fn tenant(value: &str) -> Filter {
    Filter::new_must(Condition::Field(FieldCondition::new_match(
        "tenant".parse().unwrap(),
        Match::new_value(ValueVariants::String(value.into())),
    )))
}

fn selected(id: u64) -> Filter {
    tenant("a").merge(&Filter::new_must(Condition::HasId(HasIdCondition {
        has_id: [PointId::NumId(id)].into_iter().collect(),
    })))
}

fn point(id: u64, revision: u64, coordinate: f32) -> PointStructPersisted {
    PointStruct::new(
        id,
        Vectors::new_named([("dense", Vector::new_dense(vec![coordinate, 0.0, 0.0]))]),
        json!({"tenant":"a","revision":revision,"temporary":true}),
    )
    .into()
}

fn records(shard: &EdgeShard, ids: &[u64]) -> Vec<Record> {
    let mut request = RetrieveRequest::new(ids.iter().copied().map(PointId::NumId).collect());
    request.with_payload = Some(WithPayloadInterface::Bool(true));
    request.with_vector = Some(WithVector::Bool(true));
    shard.retrieve(request).unwrap()
}

fn revision(shard: &EdgeShard, id: u64) -> Option<u64> {
    records(shard, &[id])
        .first()
        .and_then(|record| record.payload.as_ref())
        .and_then(|payload| payload.0.get("revision"))
        .and_then(Value::as_u64)
}

fn conditional(shard: &EdgeShard, id: u64, revision: u64, mode: UpdateMode, condition: Filter) {
    shard
        .update(UpdateOperation::PointOperation(
            PointOperations::UpsertPointsConditional(ConditionalInsertOperation {
                points_op: PointInsertOperations::PointsList(vec![point(id, revision, 0.5)]),
                condition,
                update_mode: Some(mode),
            }),
        ))
        .unwrap();
}

fn payload(shard: &EdgeShard, operation: PayloadOps) {
    shard
        .update(UpdateOperation::PayloadOperation(operation))
        .unwrap();
}

#[test]
fn conditional_mutations_and_partial_updates_survive_flush_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let shard = pg_qdrant_edge_probe::bounded_persistence_fixture(dir.path()).unwrap();
    let original = records(&shard, &[1, 2, 3, 4, 5, 6, 7, 8]);

    conditional(&shard, 20, 1, UpdateMode::InsertOnly, tenant("a"));
    conditional(&shard, 20, 2, UpdateMode::InsertOnly, tenant("a"));
    assert_eq!(revision(&shard, 20), Some(1));
    conditional(&shard, 21, 1, UpdateMode::UpdateOnly, tenant("a"));
    assert!(records(&shard, &[21]).is_empty());
    conditional(&shard, 20, 3, UpdateMode::UpdateOnly, tenant("a"));
    assert_eq!(revision(&shard, 20), Some(3));
    conditional(&shard, 20, 4, UpdateMode::Upsert, tenant("b"));
    assert_eq!(revision(&shard, 20), Some(3));
    conditional(&shard, 21, 1, UpdateMode::Upsert, tenant("a"));
    assert_eq!(revision(&shard, 21), Some(1));

    payload(
        &shard,
        PayloadOps::SetPayload(SetPayloadOp {
            payload: serde_json::from_value(json!({"tag":"new","revision":5})).unwrap(),
            points: None,
            filter: Some(selected(20)),
            key: None,
        }),
    );
    assert_eq!(revision(&shard, 20), Some(5));
    assert_eq!(
        records(&shard, &[20])[0]
            .payload
            .as_ref()
            .unwrap()
            .0
            .get("tag"),
        Some(&json!("new"))
    );
    payload(
        &shard,
        PayloadOps::DeletePayload(DeletePayloadOp {
            keys: vec!["tag".parse().unwrap()],
            points: Some(vec![20.into()]),
            filter: None,
        }),
    );
    assert!(
        !records(&shard, &[20])[0]
            .payload
            .as_ref()
            .unwrap()
            .contains_key("tag")
    );
    payload(
        &shard,
        PayloadOps::OverwritePayload(SetPayloadOp {
            payload: serde_json::from_value(json!({"tenant":"a","revision":6})).unwrap(),
            points: Some(vec![20.into()]),
            filter: None,
            key: None,
        }),
    );
    assert_eq!(records(&shard, &[20])[0].payload.as_ref().unwrap().len(), 2);

    // The concrete update filter must protect the other tenant even when its
    // point ID appears in the same internal update request.
    shard
        .update(UpdateOperation::VectorOperation(
            VectorOperations::UpdateVectors(UpdateVectorsOp {
                points: vec![
                    PointVectorsPersisted {
                        id: 20.into(),
                        vector: point(20, 0, 0.75).vector,
                    },
                    PointVectorsPersisted {
                        id: 4.into(),
                        vector: point(4, 0, 0.75).vector,
                    },
                ],
                update_filter: Some(tenant("a")),
            }),
        ))
        .unwrap();
    let updated = records(&shard, &[20]);
    let Some(VectorStructInternal::Named(vectors)) = &updated[0].vector else {
        panic!("named vectors missing")
    };
    assert_eq!(
        vectors.get("dense"),
        Some(&VectorInternal::Dense(vec![0.75, 0.0, 0.0]))
    );
    shard.flush().unwrap();
    drop(shard);
    let shard = EdgeShard::load(dir.path(), None).unwrap();
    assert_eq!(records(&shard, &[20]), updated);
    assert_eq!(revision(&shard, 20), Some(6));
    shard
        .update(UpdateOperation::VectorOperation(
            VectorOperations::DeleteVectors(vec![PointId::NumId(20)].into(), vec!["dense".into()]),
        ))
        .unwrap();
    let deleted = records(&shard, &[20]);
    let Some(VectorStructInternal::Named(vectors)) = &deleted[0].vector else {
        panic!("named vector result missing")
    };
    assert!(!vectors.contains_key("dense"));

    payload(
        &shard,
        PayloadOps::ClearPayload {
            points: vec![20.into()],
        },
    );
    assert!(
        records(&shard, &[20])[0]
            .payload
            .as_ref()
            .unwrap()
            .is_empty()
    );
    payload(&shard, PayloadOps::ClearPayloadByFilter(selected(21)));
    assert!(
        records(&shard, &[21])[0]
            .payload
            .as_ref()
            .unwrap()
            .is_empty()
    );
    // Restore identity on the private fixture row before the filtered delete.
    payload(
        &shard,
        PayloadOps::SetPayload(SetPayloadOp {
            payload: serde_json::from_value(json!({"tenant":"a"})).unwrap(),
            points: Some(vec![21.into()]),
            filter: None,
            key: None,
        }),
    );
    for _ in 0..2 {
        shard
            .update(UpdateOperation::PointOperation(
                PointOperations::DeletePoints {
                    ids: vec![20.into()],
                },
            ))
            .unwrap();
    }
    shard
        .update(UpdateOperation::PointOperation(
            PointOperations::DeletePointsByFilter(selected(21)),
        ))
        .unwrap();
    assert!(records(&shard, &[20, 21]).is_empty());
    assert_eq!(records(&shard, &[1, 2, 3, 4, 5, 6, 7, 8]), original);

    shard.flush().unwrap();
    drop(shard);
    let reopened = EdgeShard::load(dir.path(), None).unwrap();
    assert!(records(&reopened, &[20, 21]).is_empty());
    assert_eq!(records(&reopened, &[1, 2, 3, 4, 5, 6, 7, 8]), original);
}

#[test]
fn selective_reads_scroll_facets_and_matrix_obey_the_fixture_filter() {
    let dir = tempfile::tempdir().unwrap();
    let shard = pg_qdrant_edge_probe::bounded_persistence_fixture(dir.path()).unwrap();
    let allowed: BTreeSet<PointId> = [1_u64, 2, 3, 5, 6, 7, 8]
        .into_iter()
        .map(PointId::NumId)
        .collect();
    let mut retrieve = RetrieveRequest::new(vec![3.into(), 1.into(), 999.into(), 2.into()]);
    retrieve.with_payload = Some(WithPayloadInterface::Fields(vec![
        "tenant".parse().unwrap(),
    ]));
    retrieve.with_vector = Some(WithVector::Selector(vec!["dense".into()]));
    let selected_records = shard.retrieve(retrieve.clone()).unwrap();
    assert_eq!(
        selected_records
            .iter()
            .map(|row| row.id)
            .collect::<Vec<_>>(),
        vec![3.into(), 1.into(), 2.into()]
    );
    for row in &selected_records {
        assert_eq!(row.payload.as_ref().unwrap().len(), 1);
        assert_eq!(
            row.payload.as_ref().unwrap().0.get("tenant"),
            Some(&json!("a"))
        );
        let Some(VectorStructInternal::Named(vectors)) = &row.vector else {
            panic!("named vectors missing")
        };
        assert_eq!(vectors.len(), 1);
        assert!(vectors.contains_key("dense"));
    }
    retrieve.with_payload = Some(WithPayloadInterface::Selector(
        PayloadSelector::new_include(vec!["document".parse().unwrap()]),
    ));
    assert!(
        shard
            .retrieve(retrieve.clone())
            .unwrap()
            .iter()
            .all(|row| row.payload.as_ref().unwrap().len() == 1)
    );
    retrieve.with_payload = Some(WithPayloadInterface::Selector(
        PayloadSelector::new_exclude(vec![
            "body".parse().unwrap(),
            "body_prefix".parse().unwrap(),
        ]),
    ));
    for row in shard.retrieve(retrieve).unwrap() {
        let payload = row.payload.as_ref().unwrap();
        assert!(!payload.contains_key("body") && !payload.contains_key("body_prefix"));
        assert_eq!(payload.0.get("tenant"), Some(&json!("a")));
    }

    let mut offset = None;
    let mut seen = Vec::new();
    for page in 0..4 {
        let mut request = ScrollRequest::new();
        request.offset = offset;
        request.limit = Some(3);
        request.filter = Some(tenant("a"));
        request.with_payload = Some(WithPayloadInterface::Bool(true));
        let (rows, next) = shard.scroll(request).unwrap();
        assert!(rows.len() <= 3);
        for row in rows {
            assert!(allowed.contains(&row.id));
            seen.push(row.id);
        }
        if next.is_none() {
            break;
        }
        assert!(page < 3, "bounded scroll did not finish");
        offset = next;
    }
    assert_eq!(seen, allowed.iter().copied().collect::<Vec<_>>());
    let count = shard
        .count(CountRequest {
            filter: Some(tenant("a")),
            exact: true,
        })
        .unwrap();
    assert_eq!(count, 7);
    let facets = shard
        .facet(FacetRequest {
            key: "document".parse().unwrap(),
            limit: 16,
            filter: Some(tenant("a")),
            exact: true,
        })
        .unwrap();
    let counts: BTreeMap<_, _> = facets
        .hits
        .into_iter()
        .map(|hit| (hit.value, hit.count))
        .collect();
    assert_eq!(counts.len(), 6);
    assert_eq!(counts.get(&FacetValue::Keyword("doc-a1".into())), Some(&2));
    assert_eq!(counts.values().sum::<usize>(), 7); // Point counts, not document counts.
    assert!(!counts.contains_key(&FacetValue::Keyword("doc-b1".into())));

    let mut matrix = SearchMatrixRequest::new(7, 6, "dense".into());
    matrix.filter = Some(tenant("a"));
    let response = shard.search_matrix(matrix).unwrap();
    assert_eq!(
        response.sample_ids.iter().copied().collect::<BTreeSet<_>>(),
        allowed
    );
    assert_eq!(response.nearests.len(), 7);
    let dense: BTreeMap<_, _> = records(&shard, &[1, 2, 3, 5, 6, 7, 8])
        .into_iter()
        .map(|row| {
            let Some(VectorStructInternal::Named(mut vectors)) = row.vector else {
                panic!("named vectors missing")
            };
            let Some(VectorInternal::Dense(vector)) = vectors.remove("dense") else {
                panic!("dense vector missing")
            };
            (row.id, vector)
        })
        .collect();
    for (id, neighbors) in response.sample_ids.iter().zip(&response.nearests) {
        assert_eq!(neighbors.len(), 6);
        let ids: BTreeSet<_> = neighbors.iter().map(|hit| hit.id).collect();
        assert_eq!(ids.len(), 6);
        for hit in neighbors {
            assert!(allowed.contains(&hit.id) && hit.id != *id);
            let expected: f32 = dense[id]
                .iter()
                .zip(&dense[&hit.id])
                .map(|(a, b)| a * b)
                .sum();
            assert!((hit.score - expected).abs() < 1e-5, "matrix score mismatch");
        }
    }
    let empty = shard
        .search_matrix(SearchMatrixRequest {
            sample_size: 0,
            limit_per_sample: 6,
            filter: Some(tenant("a")),
            using: "dense".into(),
        })
        .unwrap();
    assert!(empty.sample_ids.is_empty() && empty.nearests.is_empty());
    let info = shard.info().unwrap();
    assert!(
        info.segments_count > 0
            && info
                .payload_schema
                .contains_key(&"document".parse().unwrap())
    );
}

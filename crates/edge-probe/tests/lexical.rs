//! Bounded fixed-version matching/analysis contracts, not relevance benchmarks.
//! Predicates use an explicit synthetic tenant; this is not PG authorization.
//! No sparse token IDs are interpreted as text or original-source offsets.

use std::collections::{BTreeSet, HashMap};
use std::num::NonZeroUsize;
use std::path::Path;

use qdrant_edge::bm25_embed::{EdgeBm25, EdgeBm25Config, EdgeBm25Error};
use qdrant_edge::*;
use serde_json::{Value, json};

fn new_shard(path: &Path, sparse: &[&str]) -> EdgeShard {
    let shard = EdgeShard::new(
        path,
        EdgeConfig {
            vectors: HashMap::from([(
                "dense".into(),
                EdgeVectorParams::builder(2, Distance::Dot).build(),
            )]),
            sparse_vectors: sparse
                .iter()
                .map(|name| (name.to_string(), EdgeSparseVectorParams::default()))
                .collect(),
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
    index(
        &shard,
        "tenant",
        PayloadSchemaParams::Keyword(KeywordIndexParams {
            is_tenant: Some(true),
            ..Default::default()
        }),
    );
    shard
}

fn index(shard: &EdgeShard, field: &str, params: PayloadSchemaParams) {
    shard
        .update(UpdateOperation::FieldIndexOperation(
            FieldIndexOperations::CreateIndex(CreateIndex {
                field_name: field.parse().unwrap(),
                field_schema: Some(PayloadFieldSchema::FieldParams(params)),
            }),
        ))
        .unwrap();
}

fn condition(field: &str, value: Match) -> Condition {
    Condition::Field(FieldCondition::new_match(field.parse().unwrap(), value))
}

fn tenant() -> Filter {
    Filter::new_must(condition(
        "tenant",
        Match::new_value(ValueVariants::String("a".into())),
    ))
}

fn request(name: &str, vector: Vector, predicate: Option<Condition>) -> QueryRequest {
    let mut query = QueryRequest::new(16);
    query.query = Some(ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
        using: Some(name.into()),
        query: vector.into(),
    })));
    query.filter = Some(match predicate {
        Some(predicate) => tenant().merge(&Filter::new_must(predicate)),
        None => tenant(),
    });
    query.params = Some(SearchParams {
        exact: true,
        indexed_only: false,
        ..Default::default()
    });
    query.with_payload = WithPayloadInterface::Bool(true);
    query
}

fn ids(shard: &EdgeShard, query: QueryRequest) -> BTreeSet<u64> {
    let hits = shard.query(query).unwrap();
    hits.iter()
        .map(|hit| {
            assert_eq!(
                hit.payload.as_ref().unwrap().0.get("tenant"),
                Some(&json!("a"))
            );
            let PointId::NumId(id) = hit.id else {
                panic!("numeric fixture ID")
            };
            id
        })
        .collect()
}

fn matched(shard: &EdgeShard, field: &str, value: Match) -> BTreeSet<u64> {
    ids(
        shard,
        request(
            "dense",
            Vector::new_dense(vec![1.0, 0.0]),
            Some(condition(field, value)),
        ),
    )
}

fn expected(values: &[u64]) -> BTreeSet<u64> {
    values.iter().copied().collect()
}

fn upsert(shard: &EdgeShard, points: Vec<PointStruct>) {
    shard
        .update(UpdateOperation::PointOperation(
            PointOperations::UpsertPoints(PointInsertOperations::PointsList(
                points.into_iter().map(Into::into).collect(),
            )),
        ))
        .unwrap();
}

fn point(id: u64, payload: Value) -> PointStruct {
    PointStruct::new(
        id,
        Vectors::new_named([("dense", vec![id as f32, 1.0])]),
        payload,
    )
}

fn array_assertions(shard: &EdgeShard) {
    assert_eq!(
        matched(shard, "body", Match::new_text("alpha beta")),
        expected(&[1, 4])
    );
    assert_eq!(
        matched(
            shard,
            "body",
            Match::TextAny(MatchTextAny {
                text_any: "alpha absent".into()
            })
        ),
        expected(&[1, 4])
    );
    assert_eq!(
        matched(shard, "body", Match::new_text("alpha absent")),
        expected(&[])
    );
    assert_eq!(
        matched(
            shard,
            "body",
            Match::Phrase(MatchPhrase {
                phrase: "alpha beta".into()
            })
        ),
        expected(&[1])
    );
    assert_eq!(
        matched(
            shard,
            "body",
            Match::Phrase(MatchPhrase {
                phrase: "beta gamma".into()
            })
        ),
        expected(&[2])
    );
    assert_eq!(
        matched(
            shard,
            "headline",
            Match::Phrase(MatchPhrase {
                phrase: "alpha beta".into()
            })
        ),
        expected(&[3])
    );
    assert_eq!(
        matched(
            shard,
            "sku",
            Match::new_value(ValueVariants::String("SKU-007".into()))
        ),
        expected(&[1])
    );
    assert_eq!(
        matched(shard, "sku", Match::new_prefix("SKU-007")),
        expected(&[1, 2])
    );
    assert_eq!(
        matched(shard, "sku", Match::new_prefix("sku-007")),
        expected(&[3])
    );
}

#[test]
fn phrase_preserves_array_and_field_boundaries_while_text_and_spans_values() {
    let dir = tempfile::tempdir().unwrap();
    let shard = new_shard(dir.path(), &[]);
    for field in ["body", "headline"] {
        index(
            &shard,
            field,
            PayloadSchemaParams::Text(TextIndexParams {
                tokenizer: TokenizerType::Word,
                lowercase: Some(true),
                phrase_matching: Some(true),
                ..Default::default()
            }),
        );
    }
    index(
        &shard,
        "sku",
        PayloadSchemaParams::Keyword(KeywordIndexParams {
            prefix: Some(true),
            ..Default::default()
        }),
    );
    upsert(
        &shard,
        vec![
            point(
                1,
                json!({"tenant":"a","headline":"field","body":["alpha beta","gamma"],"sku":"SKU-007"}),
            ),
            point(
                2,
                json!({"tenant":"a","headline":"alpha","body":["beta gamma"],"sku":"SKU-007X"}),
            ),
            point(
                3,
                json!({"tenant":"a","headline":"alpha beta","body":["delta"],"sku":"sku-007"}),
            ),
            point(
                4,
                json!({"tenant":"a","headline":"field","body":["alpha","beta"],"sku":"SKU-008"}),
            ),
            point(
                5,
                json!({"tenant":"b","headline":"alpha beta","body":["alpha beta"],"sku":"SKU-007B"}),
            ),
        ],
    );
    array_assertions(&shard);
    shard.flush().unwrap();
    drop(shard);
    let reopened = EdgeShard::load(dir.path(), None).unwrap();
    array_assertions(&reopened);
}

#[test]
fn unicode_scalar_lengths_and_long_token_prefix_truncation_are_explicit() {
    let dir = tempfile::tempdir().unwrap();
    let shard = new_shard(dir.path(), &[]);
    for (field, tokenizer) in [
        ("word", TokenizerType::Word),
        ("prefix", TokenizerType::Prefix),
    ] {
        index(
            &shard,
            field,
            PayloadSchemaParams::Text(TextIndexParams {
                tokenizer,
                lowercase: Some(true),
                ascii_folding: Some(false),
                min_token_len: Some(2),
                max_token_len: Some(3),
                ..Default::default()
            }),
        );
    }
    index(
        &shard,
        "whole",
        PayloadSchemaParams::Keyword(KeywordIndexParams {
            prefix: Some(true),
            ..Default::default()
        }),
    );
    upsert(
        &shard,
        ["éclair", "école", "中国", "中国人", "中国人民"]
            .iter()
            .enumerate()
            .map(|(i, text)| {
                point(
                    i as u64 + 1,
                    json!({"tenant":"a","word":text,"prefix":text,"whole":text}),
                )
            })
            .chain([point(
                6,
                json!({"tenant":"b","word":"中国","prefix":"éclair","whole":"éclair"}),
            )])
            .collect(),
    );
    assert_eq!(
        matched(&shard, "word", Match::new_text("中国")),
        expected(&[3])
    );
    assert_eq!(
        matched(&shard, "word", Match::new_text("中国人")),
        expected(&[4])
    );
    assert_eq!(
        matched(&shard, "word", Match::new_text("中国人民")),
        expected(&[])
    );
    assert_eq!(
        matched(&shard, "prefix", Match::new_text("é")),
        expected(&[])
    );
    assert_eq!(
        matched(&shard, "prefix", Match::new_text("éc")),
        expected(&[1, 2])
    );
    assert_eq!(
        matched(&shard, "prefix", Match::new_text("ÉCL")),
        expected(&[1])
    );
    // This is upstream prefix-token truncation, NOT the desired full-length
    // prefix predicate. The planner must reject/handle the exceeding query.
    assert_eq!(
        matched(&shard, "prefix", Match::new_text("éclipse")),
        expected(&[1])
    );
    assert_eq!(
        matched(&shard, "whole", Match::new_prefix("éclipse")),
        expected(&[])
    );
    assert_eq!(
        matched(&shard, "whole", Match::new_prefix("écl")),
        expected(&[1])
    );
    assert_eq!(
        matched(&shard, "whole", Match::new_prefix("ÉCL")),
        expected(&[])
    );
    assert_eq!(
        matched(&shard, "prefix", Match::new_text("中国人民")),
        expected(&[4, 5])
    );
    assert_eq!(
        matched(&shard, "whole", Match::new_prefix("中国人民")),
        expected(&[5])
    );
}

fn neutral(ascii_folding: bool) -> EdgeBm25 {
    // The public config exposes unnameable nested types. This fixed constant
    // remains probe-owned and is not a SQL/serde configuration contract.
    EdgeBm25::new(
        serde_json::from_value(json!({
            "tokenizer":"word", "stemmer":{"type":"none"}, "stopwords":{},
            "lowercase":true, "ascii_folding":ascii_folding, "avg_len":16.0
        }))
        .unwrap(),
    )
    .unwrap()
}

#[test]
fn bm25_and_payload_analysis_remain_independent_until_both_are_configured() {
    let default = EdgeBm25::new(EdgeBm25Config::default()).unwrap();
    let literal = neutral(false);
    let folded = neutral(true);
    let dir = tempfile::tempdir().unwrap();
    let shard = new_shard(dir.path(), &["default_bm25", "literal_bm25", "folded_bm25"]);
    for (field, fold) in [("literal", false), ("folded", true)] {
        index(
            &shard,
            field,
            PayloadSchemaParams::Text(TextIndexParams {
                tokenizer: TokenizerType::Word,
                lowercase: Some(true),
                ascii_folding: Some(fold),
                ..Default::default()
            }),
        );
    }
    upsert(
        &shard,
        ["running", "run", "the run", "RUNNING", "café"]
            .iter()
            .enumerate()
            .map(|(i, text)| {
                PointStruct::new(
                    i as u64 + 1,
                    Vectors::new_named([
                        ("dense", Vector::new_dense(vec![i as f32 + 1.0, 1.0])),
                        ("default_bm25", Vector::from(default.embed_document(text))),
                        ("literal_bm25", Vector::from(literal.embed_document(text))),
                        ("folded_bm25", Vector::from(folded.embed_document(text))),
                    ]),
                    json!({"tenant":"a","literal":text,"folded":text}),
                )
            })
            .collect(),
    );
    let search = |name: &str, encoder: &EdgeBm25, text: &str| {
        ids(
            &shard,
            request(name, Vector::from(encoder.embed_query(text)), None),
        )
    };
    assert_eq!(
        search("default_bm25", &default, "run"),
        expected(&[1, 2, 3, 4])
    );
    assert_eq!(search("literal_bm25", &literal, "run"), expected(&[2, 3]));
    assert_eq!(
        matched(&shard, "literal", Match::new_text("run")),
        expected(&[2, 3])
    );
    assert_eq!(search("literal_bm25", &literal, "the"), expected(&[3]));
    assert_eq!(
        matched(&shard, "literal", Match::new_text("the")),
        expected(&[3])
    );
    assert!(default.embed_query("the").indices.is_empty());
    assert_eq!(search("literal_bm25", &literal, "cafe"), expected(&[]));
    assert_eq!(
        matched(&shard, "literal", Match::new_text("cafe")),
        expected(&[])
    );
    assert_eq!(search("folded_bm25", &folded, "cafe"), expected(&[5]));
    assert_eq!(
        matched(&shard, "folded", Match::new_text("cafe")),
        expected(&[5])
    );
    assert_eq!(
        ids(
            &shard,
            request(
                "default_bm25",
                Vector::from(default.embed_query("run")),
                Some(condition("literal", Match::new_text("run")))
            )
        ),
        expected(&[2, 3])
    );
    let document = default.embed_document("running");
    let query = default.embed_query("run");
    assert_eq!(document.indices, query.indices);
    assert_ne!(
        document.values, query.values,
        "document/query weights are not interchangeable"
    );
    let bad = EdgeBm25::new(EdgeBm25Config {
        language: Some("not_a_language".into()),
        ..Default::default()
    });
    assert!(
        matches!(bad, Err(EdgeBm25Error::UnsupportedLanguage(value)) if value == "not_a_language")
    );
}

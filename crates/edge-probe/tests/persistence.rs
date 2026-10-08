//! Abrupt process termination after the explicit engine persistence boundary.
//! This tests Edge in a disposable process, not PostgreSQL crash isolation.

#[cfg(unix)]
#[test]
fn explicit_flush_survives_sigkill_without_drop() {
    pg_qdrant_edge_probe::cpu::require().expect("fixed Edge native CPU baseline");
    use std::io::{BufRead, BufReader};
    use std::os::unix::process::ExitStatusExt;
    use std::process::{Command, Stdio};
    use std::sync::mpsc;
    use std::time::Duration;

    let dir = tempfile::tempdir().expect("disposable shard directory");
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-qdrant-edge-probe"))
        .arg("--persistence-child")
        .arg(dir.path())
        .arg("flushed")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .expect("start independent engine fixture process");

    let stdout = child.stdout.take().unwrap();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        let outcome = BufReader::new(stdout).read_line(&mut line).map(|_| line);
        let _ = tx.send(outcome);
    });
    let ready = rx.recv_timeout(Duration::from_secs(30));
    // Always stop/reap the child, including a failed startup or timeout.
    let kill_result = child.kill();
    let status = child.wait().expect("reap engine process");
    kill_result.expect("kill the independent engine process");
    assert!(
        !status.success(),
        "the engine fixture must not close gracefully"
    );
    assert_eq!(
        status.signal(),
        Some(9),
        "the child must terminate through SIGKILL"
    );
    assert_eq!(
        ready
            .expect("bounded fixture startup")
            .expect("fixture stdout")
            .trim(),
        "ready"
    );

    let reopened = qdrant_edge::EdgeShard::load(dir.path(), None).expect("reopen after SIGKILL");
    assert_eq!(reopened.count(qdrant_edge::CountRequest::new()).unwrap(), 8);
    let mut retrieval = qdrant_edge::RetrieveRequest::new(vec![qdrant_edge::PointId::NumId(1)]);
    retrieval.with_payload = Some(qdrant_edge::WithPayloadInterface::Bool(true));
    retrieval.with_vector = Some(qdrant_edge::WithVector::Bool(true));
    let points = reopened
        .retrieve(retrieval)
        .expect("persisted point representations");
    assert_eq!(points.len(), 1);
    assert_eq!(points[0].payload.as_ref().unwrap().0["revision"], 1);
    assert!(points[0].vector.is_some());
    let qdrant_edge::VectorStructInternal::Named(vectors) = points[0].vector.as_ref().unwrap()
    else {
        panic!("the reopened fixture must retain named representations");
    };
    for name in ["dense", "bm25", "learned", "tokens"] {
        assert!(vectors.contains_key(name), "missing representation {name}");
    }

    use qdrant_edge::{
        Condition, FieldCondition, Filter, JsonPath, Match, MatchPhrase, NamedQuery, PointId,
        QueryEnum, QueryRequest, ScoredPoint, ScoringQuery, SearchParams, ValueVariants, Vector,
        VectorInternal, WithPayloadInterface,
    };
    let tenant = Filter::new_must(Condition::Field(FieldCondition::new_match(
        "tenant".parse::<JsonPath>().expect("fixture tenant path"),
        Match::new_value(ValueVariants::String("a".into())),
    )));
    let make_query = |scoring: Option<ScoringQuery>, filter: Filter, limit: usize| {
        let mut query = QueryRequest::new(limit);
        query.query = scoring;
        query.filter = Some(filter);
        query.params = Some(SearchParams {
            exact: true,
            indexed_only: false,
            ..Default::default()
        });
        query.with_payload = WithPayloadInterface::Bool(true);
        query
    };
    let nearest = |name: &str, vector: VectorInternal| {
        ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
            query: vector,
            using: Some(name.into()),
        }))
    };
    let assert_scores = |hits: &[ScoredPoint], expected: &[(u64, f32)]| {
        assert_eq!(hits.len(), expected.len());
        for (hit, (id, score)) in hits.iter().zip(expected) {
            assert_eq!(hit.id, PointId::NumId(*id));
            assert!(
                (hit.score - score).abs() < 1e-5,
                "score changed after SIGKILL for point {id}"
            );
            assert_eq!(hit.payload.as_ref().unwrap().0["tenant"], "a");
        }
    };

    // The phrase index must remain usable, including its authorization filter.
    // Point 4 contains the same phrase but belongs to the excluded tenant.
    let phrase_filter = tenant.merge(&Filter::new_must(Condition::Field(
        FieldCondition::new_match(
            "body".parse::<JsonPath>().expect("fixture text path"),
            Match::Phrase(MatchPhrase {
                phrase: "transaction recovery".into(),
            }),
        ),
    )));
    let phrase = reopened
        .query(make_query(None, phrase_filter, 16))
        .expect("phrase after SIGKILL");
    assert_eq!(phrase.len(), 1);
    assert_eq!(phrase[0].id, PointId::NumId(1));
    assert_eq!(phrase[0].payload.as_ref().unwrap().0["tenant"], "a");

    // Supplied sparse scores are analytically fixed by coordinate 7. The
    // excluded tenant's point has weight 5 and must never become a candidate.
    let sparse_vector: VectorInternal = Vector::new_sparse(vec![7], vec![1.0]).unwrap().into();
    let sparse = reopened
        .query(make_query(
            Some(nearest("learned", sparse_vector)),
            tenant.clone(),
            16,
        ))
        .expect("learned sparse query after SIGKILL");
    assert_scores(&sparse, &[(1, 1.0), (2, 0.8)]);

    let token_vector: VectorInternal = Vector::new_multi(vec![vec![1.0, 0.0], vec![0.0, 1.0]])
        .unwrap()
        .into();
    let maxsim = reopened
        .query(make_query(
            Some(nearest("tokens", token_vector)),
            tenant.clone(),
            3,
        ))
        .expect("MaxSim query after SIGKILL");
    assert_scores(&maxsim, &[(1, 2.0), (2, 1.6), (3, 0.8)]);

    // Reconstruct the declared offline encoder and query the persisted BM25
    // representation, instead of accepting vector key presence as recovery.
    let config: qdrant_edge::bm25_embed::EdgeBm25Config =
        serde_json::from_value(serde_json::json!({
            "tokenizer":"multilingual","stemmer":{"type":"none"},"stopwords":{},
            "lowercase":true,"ascii_folding":false,"avg_len":16.0
        }))
        .unwrap();
    let encoder = qdrant_edge::bm25_embed::EdgeBm25::new(config).unwrap();
    let mut bm25 = make_query(
        Some(nearest(
            "bm25",
            VectorInternal::Sparse(encoder.embed_query("transaction recovery")),
        )),
        tenant.clone(),
        16,
    );
    bm25.params.as_mut().unwrap().idf = Some(qdrant_edge::IdfParams::Corpus(
        qdrant_edge::IdfCorpusParams { corpus: tenant },
    ));
    let lexical = reopened.query(bm25).expect("BM25 query after SIGKILL");
    let lexical_ids: std::collections::BTreeSet<_> = lexical.iter().map(|hit| hit.id).collect();
    assert_eq!(
        lexical_ids,
        [PointId::NumId(1), PointId::NumId(2), PointId::NumId(3)]
            .into_iter()
            .collect()
    );
    for hit in &lexical {
        assert!(hit.score.is_finite() && hit.score > 0.0);
        assert_eq!(hit.payload.as_ref().unwrap().0["tenant"], "a");
    }
    reopened.flush().expect("final flush");
}

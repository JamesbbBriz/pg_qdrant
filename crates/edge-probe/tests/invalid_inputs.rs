//! Bounded fixed-release negative-input characterization (F08/V01-V03/L08).
//! Acceptance of malformed input is recorded as an adapter validation gap.
//! Every constructor/engine experiment executes in a separate owned process;
//! no panic, query error or native exit is treated as production recovery proof.
#![cfg(target_os = "linux")]

use std::collections::{BTreeSet, HashMap};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use qdrant_edge::*;
use serde_json::{Value, json};

const TEST_NAME: &str = "core_input_rejection_and_phrase_prerequisites_are_characterized";
const MODE_ENV: &str = "PG_QDRANT_NEGATIVE_INPUT_CHILD";
const ROOT_ENV: &str = "PG_QDRANT_NEGATIVE_INPUT_ROOT";
const CPU_ENV: &str = "PG_QDRANT_NEGATIVE_INPUT_CPU";
const PARENT_ENV: &str = "PG_QDRANT_NEGATIVE_INPUT_PARENT";
const OUTPUT_LIMIT: u64 = 64 * 1024;
const CASES: &[&str] = &[
    "missing_named_query",
    "wrong_dense_query",
    "wrong_dense_update",
    "wrong_tokens_query",
    "wrong_tokens_update",
    "ragged_tokens_constructor",
    "sparse_shape_constructor",
    "sparse_duplicate_constructor",
    "sparse_nan_constructor",
    "sparse_nan_query",
    "sparse_infinity_query",
    "dense_nan_query",
    "raw_sparse_shape_query",
    "raw_sparse_duplicate_query",
    "phrase_no_index",
    "phrase_positions_disabled",
    "phrase_positions_enabled_control",
];

fn procfs_value(key: &str) -> u32 {
    fs::read_to_string("/proc/self/status")
        .unwrap()
        .lines()
        .find_map(|line| line.strip_prefix(key))
        .unwrap()
        .trim()
        .parse()
        .unwrap()
}

fn cpus() -> BTreeSet<u32> {
    let status = fs::read_to_string("/proc/self/status").unwrap();
    let list = status
        .lines()
        .find_map(|line| line.strip_prefix("Cpus_allowed_list:"))
        .unwrap()
        .trim();
    let mut cpus = BTreeSet::new();
    for part in list.split(',') {
        let (start, end) = part.split_once('-').unwrap_or((part, part));
        let (start, end) = (start.parse::<u32>().unwrap(), end.parse::<u32>().unwrap());
        assert!(start <= end && end < 65_536);
        cpus.extend(start..=end);
    }
    assert!(!cpus.is_empty());
    cpus
}

fn guarded_root() -> PathBuf {
    let root = PathBuf::from(std::env::var_os(ROOT_ENV).unwrap());
    assert!(root.is_absolute() && root.canonicalize().unwrap() == root);
    assert!(
        root.file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("pg-qdrant-input-negatives-")
    );
    let metadata = fs::symlink_metadata(&root).unwrap();
    assert!(metadata.is_dir() && metadata.mode() & 0o777 == 0o700);
    assert_eq!(metadata.uid(), fs::metadata("/proc/self").unwrap().uid());
    let parent: u32 = std::env::var(PARENT_ENV).unwrap().parse().unwrap();
    assert_eq!(procfs_value("PPid:"), parent);
    assert_eq!(
        fs::read_to_string(root.join("controller-procfs-pid")).unwrap(),
        parent.to_string()
    );
    let cpu: u32 = std::env::var(CPU_ENV).unwrap().parse().unwrap();
    assert_eq!(cpus(), BTreeSet::from([cpu]));
    assert_eq!(std::thread::available_parallelism().unwrap().get(), 1);
    assert_eq!(std::env::var("QDRANT_NUM_CPUS").as_deref(), Ok("1"));
    root
}

fn tenant() -> Filter {
    Filter::new_must(Condition::Field(FieldCondition::new_match(
        "tenant".parse().unwrap(),
        Match::new_value(ValueVariants::String("a".into())),
    )))
}

fn fixture(path: &Path, case: &str) -> EdgeShard {
    pg_qdrant_edge_probe::cpu::require().expect("fixed Edge native CPU baseline");
    fs::create_dir(path).unwrap();
    let dense = EdgeVectorParams::builder(2, Distance::Dot).build();
    let tokens = EdgeVectorParams::builder(2, Distance::Dot)
        .multivector_config(MultiVectorConfig {
            comparator: MultiVectorComparator::MaxSim,
        })
        .build();
    let shard = EdgeShard::new(
        path,
        EdgeConfig {
            vectors: HashMap::from([("dense".into(), dense), ("tokens".into(), tokens)]),
            sparse_vectors: HashMap::from([("learned".into(), EdgeSparseVectorParams::default())]),
            max_search_threads: Some(1),
            on_disk_payload: Some(false),
            hnsw_config: Some(HnswIndexConfig {
                max_indexing_threads: 1,
                ..Default::default()
            }),
            wal_options: Some(WalOptions {
                segment_capacity: 65_536,
                segment_queue_len: 0,
                retain_closed: std::num::NonZeroUsize::MIN,
            }),
            ..Default::default()
        },
    )
    .unwrap();
    shard
        .update(UpdateOperation::FieldIndexOperation(
            FieldIndexOperations::CreateIndex(CreateIndex {
                field_name: "tenant".parse().unwrap(),
                field_schema: Some(PayloadFieldSchema::FieldType(PayloadSchemaType::Keyword)),
            }),
        ))
        .unwrap();
    if matches!(
        case,
        "phrase_positions_disabled" | "phrase_positions_enabled_control"
    ) {
        shard
            .update(UpdateOperation::FieldIndexOperation(
                FieldIndexOperations::CreateIndex(CreateIndex {
                    field_name: "body".parse().unwrap(),
                    field_schema: Some(PayloadFieldSchema::FieldParams(PayloadSchemaParams::Text(
                        TextIndexParams {
                            tokenizer: TokenizerType::Word,
                            lowercase: Some(true),
                            phrase_matching: Some(case == "phrase_positions_enabled_control"),
                            ..Default::default()
                        },
                    ))),
                }),
            ))
            .unwrap();
    }
    let points = [
        (1_u64, "a", "alpha beta", 1.0),
        (2, "a", "ALPHA BETA", 0.8),
        (3, "a", "xalpha betaZ", 0.5),
        (4, "b", "alpha beta", 100.0),
    ]
    .into_iter()
    .map(|(id, tenant, body, x)| {
        PointStruct::new(
            id,
            Vectors::new_named([
                ("dense", Vector::new_dense(vec![x, 0.0])),
                (
                    "tokens",
                    Vector::new_multi(vec![vec![1.0, 0.0], vec![0.0, 1.0]]).unwrap(),
                ),
                (
                    "learned",
                    Vector::new_sparse(vec![1, 2], vec![1.0, 2.0]).unwrap(),
                ),
            ]),
            json!({"tenant":tenant,"body":body}),
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

fn error(error: OperationError) -> Value {
    let variant = match &error {
        OperationError::WrongVectorDimension { .. } => "WrongVectorDimension",
        OperationError::VectorNameNotExists { .. } => "VectorNameNotExists",
        OperationError::ValidationError { .. } => "ValidationError",
        OperationError::WrongSparse => "WrongSparse",
        OperationError::WrongMulti => "WrongMulti",
        OperationError::NonFiniteNumber { .. } => "NonFiniteNumber",
        _ => "other_operation_error",
    };
    json!({"outcome":"error","variant":variant,"message":error.to_string()})
}

fn constructed(result: Result<Vector, OperationError>) -> Value {
    match result {
        Ok(vector) => {
            let nonfinite = match &vector.0 {
                VectorInternal::Dense(values) => values.iter().filter(|x| !x.is_finite()).count(),
                VectorInternal::Sparse(sparse) => {
                    sparse.values.iter().filter(|x| !x.is_finite()).count()
                }
                _ => 0,
            };
            json!({"outcome":"accepted","nonfinite_input_values":nonfinite})
        }
        Err(err) => error(err),
    }
}

fn query(shard: &EdgeShard, using: &str, vector: VectorInternal) -> Value {
    let mut query = QueryRequest::new(8);
    query.query = Some(ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
        using: Some(using.into()),
        query: vector,
    })));
    query.filter = Some(tenant());
    query.params = Some(SearchParams {
        exact: true,
        indexed_only: false,
        ..Default::default()
    });
    query.with_payload = WithPayloadInterface::Bool(true);
    queried(shard.query(query))
}

fn queried(result: Result<Vec<ScoredPoint>, OperationError>) -> Value {
    match result {
        Err(err) => error(err),
        Ok(hits) => {
            let hits: Vec<_> = hits
                .iter()
                .map(|hit| {
                    assert_eq!(hit.payload.as_ref().unwrap().0["tenant"], "a");
                    let PointId::NumId(id) = hit.id else {
                        panic!("unexpected fixture ID")
                    };
                    let score = if hit.score.is_nan() {
                        json!({"kind":"nan"})
                    } else if hit.score == f32::INFINITY {
                        json!({"kind":"positive_infinity"})
                    } else if hit.score == f32::NEG_INFINITY {
                        json!({"kind":"negative_infinity"})
                    } else {
                        json!({"kind":"finite","value":hit.score})
                    };
                    json!({"id":id,"score":score})
                })
                .collect();
            json!({"outcome":"accepted","hits":hits})
        }
    }
}

fn update(shard: &EdgeShard, using: &str, vector: Vector) -> Value {
    let point = PointStruct::new(
        9_u64,
        Vectors::new_named([(using, vector)]),
        json!({"tenant":"a"}),
    )
    .into();
    match shard.update(UpdateOperation::PointOperation(
        PointOperations::UpsertPoints(PointInsertOperations::PointsList(vec![point])),
    )) {
        Ok(()) => json!({"outcome":"accepted"}),
        Err(err) => error(err),
    }
}

fn exercise(case: &str, shard: Option<&EdgeShard>) -> Value {
    match case {
        "ragged_tokens_constructor" => {
            constructed(Vector::new_multi(vec![vec![1.0, 0.0], vec![1.0]]))
        }
        "sparse_shape_constructor" => constructed(Vector::new_sparse(vec![1, 2], vec![1.0])),
        "sparse_duplicate_constructor" => {
            constructed(Vector::new_sparse(vec![1, 1], vec![1.0, 2.0]))
        }
        "sparse_nan_constructor" => constructed(Vector::new_sparse(vec![1], vec![f32::NAN])),
        "missing_named_query" => query(
            shard.unwrap(),
            "absent",
            VectorInternal::Dense(vec![1.0, 0.0]),
        ),
        "wrong_dense_query" => query(
            shard.unwrap(),
            "dense",
            VectorInternal::Dense(vec![1.0, 0.0, 0.0]),
        ),
        "wrong_dense_update" => update(
            shard.unwrap(),
            "dense",
            Vector::new_dense(vec![1.0, 0.0, 0.0]),
        ),
        "wrong_tokens_query" => query(
            shard.unwrap(),
            "tokens",
            Vector::new_multi(vec![vec![1.0, 0.0, 0.0]]).unwrap().into(),
        ),
        "wrong_tokens_update" => update(
            shard.unwrap(),
            "tokens",
            Vector::new_multi(vec![vec![1.0, 0.0, 0.0]]).unwrap(),
        ),
        "sparse_nan_query" => query(
            shard.unwrap(),
            "learned",
            Vector::new_sparse(vec![1], vec![f32::NAN]).unwrap().into(),
        ),
        "sparse_infinity_query" => query(
            shard.unwrap(),
            "learned",
            Vector::new_sparse(vec![1], vec![f32::INFINITY])
                .unwrap()
                .into(),
        ),
        "dense_nan_query" => query(
            shard.unwrap(),
            "dense",
            VectorInternal::Dense(vec![f32::NAN, 0.0]),
        ),
        // Public re-exported types permit bypassing the checked constructor.
        // No internal module import or unsafe vector fabrication is involved.
        "raw_sparse_shape_query" => query(
            shard.unwrap(),
            "learned",
            VectorInternal::Sparse(SparseVector {
                indices: vec![1, 2],
                values: vec![1.0],
            }),
        ),
        "raw_sparse_duplicate_query" => query(
            shard.unwrap(),
            "learned",
            VectorInternal::Sparse(SparseVector {
                indices: vec![1, 1],
                values: vec![1.0, 2.0],
            }),
        ),
        "phrase_no_index" | "phrase_positions_disabled" | "phrase_positions_enabled_control" => {
            let mut request = QueryRequest::new(8);
            request.filter = Some(tenant().merge(&Filter::new_must(Condition::Field(
                FieldCondition::new_match(
                    "body".parse().unwrap(),
                    Match::Phrase(MatchPhrase {
                        phrase: "alpha beta".into(),
                    }),
                ),
            ))));
            request.with_payload = WithPayloadInterface::Bool(true);
            queried(shard.unwrap().query(request))
        }
        _ => panic!("unknown bounded case"),
    }
}

fn child_body(case: &str) -> ! {
    pg_qdrant_edge_probe::cpu::require().expect("fixed Edge native CPU baseline");
    assert!(CASES.contains(&case));
    let root = guarded_root();
    eprintln!("pg_qdrant_p0_input_stage=fixture");
    let shard = (!case.ends_with("_constructor")).then(|| fixture(&root.join("shard"), case));
    eprintln!("pg_qdrant_p0_input_stage=exercise");
    let outcome = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        exercise(case, shard.as_ref())
    })) {
        Ok(outcome) => outcome,
        Err(payload) => {
            let message = payload
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| payload.downcast_ref::<&str>().map(|x| (*x).to_owned()))
                .unwrap_or_else(|| "non_string_panic".into());
            json!({"outcome":"rust_panic","message":message})
        }
    };
    println!(
        "pg_qdrant_p0_input_case={}",
        json!({"case":case,"observation":outcome,
        "verified_single_cpu":true,"product_adapter_must_validate":case != "phrase_positions_enabled_control"})
    );
    std::io::stdout().flush().unwrap();
    // An error/panic does not establish safe further use. Do not query or flush
    // this disposable owner again, and do not run Edge's Drop flush.
    std::mem::forget(shard);
    std::process::exit(0)
}

struct OwnedChild(Child);
impl Drop for OwnedChild {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
        }
        let _ = self.0.wait();
    }
}

fn text(file: &File) -> String {
    let mut file = file.try_clone().unwrap();
    file.seek(SeekFrom::Start(0)).unwrap();
    let mut bytes = Vec::new();
    file.take(OUTPUT_LIMIT).read_to_end(&mut bytes).unwrap();
    String::from_utf8_lossy(&bytes).into_owned()
}

fn run_case(case: &str, cpu: u32) -> Value {
    let root = tempfile::Builder::new()
        .prefix("pg-qdrant-input-negatives-")
        .tempdir()
        .unwrap();
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let parent = procfs_value("Pid:");
    fs::write(
        root.path().join("controller-procfs-pid"),
        parent.to_string(),
    )
    .unwrap();
    let stdout = tempfile::tempfile_in(root.path()).unwrap();
    let stderr = tempfile::tempfile_in(root.path()).unwrap();
    let mut process = OwnedChild(
        Command::new("/usr/bin/taskset")
            .args(["--cpu-list", &cpu.to_string()])
            .arg(std::env::current_exe().unwrap())
            .args(["--exact", TEST_NAME, "--nocapture", "--test-threads=1"])
            .env(MODE_ENV, case)
            .env(ROOT_ENV, root.path())
            .env(PARENT_ENV, parent.to_string())
            .env(CPU_ENV, cpu.to_string())
            .env("QDRANT_NUM_CPUS", "1")
            .env("RUST_BACKTRACE", "0")
            .stdin(Stdio::null())
            .stdout(Stdio::from(stdout.try_clone().unwrap()))
            .stderr(Stdio::from(stderr.try_clone().unwrap()))
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(30);
    let mut timeout = false;
    let mut output_exceeded = false;
    let status = loop {
        if let Some(status) = process.0.try_wait().unwrap() {
            break status;
        }
        timeout = Instant::now() >= deadline;
        output_exceeded = stdout.metadata().unwrap().len() > OUTPUT_LIMIT
            || stderr.metadata().unwrap().len() > OUTPUT_LIMIT;
        if timeout || output_exceeded {
            let _ = process.0.kill();
            break process.0.wait().unwrap();
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    output_exceeded |= stdout.metadata().unwrap().len() > OUTPUT_LIMIT
        || stderr.metadata().unwrap().len() > OUTPUT_LIMIT;
    let output = text(&stdout);
    let error = text(&stderr);
    let record = output
        .lines()
        .find_map(|line| {
            line.split_once("pg_qdrant_p0_input_case=")
                .map(|(_, record)| record)
        })
        .map(|line| serde_json::from_str::<Value>(line).unwrap());
    let outcome = json!({"case":case,"exit_code":status.code(),"signal":status.signal(),
        "timed_out":timeout,"output_exceeded":output_exceeded,"record":record,
        "stderr":error.replace(root.path().to_str().unwrap(),"<owned_fixture>")});
    println!("pg_qdrant_p0_input_child={outcome}");
    outcome
}

#[test]
fn core_input_rejection_and_phrase_prerequisites_are_characterized() {
    pg_qdrant_edge_probe::cpu::require().expect("fixed Edge native CPU baseline");
    if let Ok(case) = std::env::var(MODE_ENV) {
        child_body(&case);
    }
    assert!(Path::new("/usr/bin/taskset").is_file());
    let cpu = *cpus().first().unwrap();
    let reports: Vec<_> = CASES.iter().map(|case| run_case(case, cpu)).collect();
    for report in &reports {
        assert_eq!(report["exit_code"], 0, "unclassified child: {report}");
        assert_eq!(report["signal"], Value::Null);
        assert_eq!(report["timed_out"], false);
        assert_eq!(report["output_exceeded"], false);
        assert_eq!(report["record"]["case"], report["case"]);
    }
    for report in &reports {
        let case = report["case"].as_str().unwrap();
        let observed = &report["record"]["observation"];
        let error_variant = match case {
            "missing_named_query" => Some("VectorNameNotExists"),
            "wrong_dense_query"
            | "wrong_dense_update"
            | "wrong_tokens_query"
            | "wrong_tokens_update"
            | "ragged_tokens_constructor" => Some("WrongVectorDimension"),
            "sparse_shape_constructor" | "sparse_duplicate_constructor" => Some("ValidationError"),
            _ => None,
        };
        if let Some(variant) = error_variant {
            assert_eq!(observed["outcome"], "error", "case {case}");
            assert_eq!(observed["variant"], variant, "case {case}");
            continue;
        }
        if case == "raw_sparse_shape_query" {
            assert_eq!(observed["outcome"], "rust_panic");
            assert!(
                observed["message"]
                    .as_str()
                    .unwrap()
                    .contains("index out of bounds")
            );
            continue;
        }
        assert_eq!(observed["outcome"], "accepted", "case {case}");
        if case == "sparse_nan_constructor" {
            assert_eq!(observed["nonfinite_input_values"], 1);
            continue;
        }
        let hits = observed["hits"].as_array().unwrap();
        let ids: BTreeSet<u64> = hits.iter().map(|hit| hit["id"].as_u64().unwrap()).collect();
        assert_eq!(ids.len(), hits.len(), "duplicate result ID in {case}");
        match case {
            "sparse_nan_query" | "phrase_positions_disabled" => assert!(hits.is_empty()),
            "sparse_infinity_query" | "dense_nan_query" => {
                assert_eq!(ids, BTreeSet::from([1, 2, 3]));
                let kind = if case == "dense_nan_query" {
                    "nan"
                } else {
                    "positive_infinity"
                };
                assert!(hits.iter().all(|hit| hit["score"]["kind"] == kind));
            }
            "raw_sparse_duplicate_query" => {
                assert_eq!(ids, BTreeSet::from([1, 2, 3]));
                assert!(
                    hits.iter()
                        .all(|hit| hit["score"] == json!({"kind":"finite","value":3.0}))
                );
            }
            "phrase_no_index" => assert_eq!(ids, BTreeSet::from([1, 3])),
            "phrase_positions_enabled_control" => assert_eq!(ids, BTreeSet::from([1, 2])),
            _ => panic!("case lacks a frozen assertion: {case}"),
        }
    }
    println!(
        "pg_qdrant_p0_input_matrix={}",
        json!({"status":"characterization_passed_with_validation_gaps",
            "malformed_input_rejection_gate":"not_satisfied_by_engine_alone",
            "engine_version":"0.8.0","cases":reports,
        "scope":{"owned_child_characterization_only":true,"adapter_validation_implemented":false,
        "postgresql_authorization_verified":false,"safe_post_error_owner_reuse_verified":false,
        "p0_exit_passed":false,"release_supported":false},
        "bounds":{"child_deadline_seconds":30,"output_bytes_per_stream":OUTPUT_LIMIT,
        "single_cpu_affinity":true,"fixture_points":4,"query_limit":8}})
    );
}

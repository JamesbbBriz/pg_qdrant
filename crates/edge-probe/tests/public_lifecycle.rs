//! Fixed Edge 0.8.0 public read-only/update-only lifecycle probes (L01-L04).
//! All filesystem mutations belong to temporary fixtures. The manifest is
//! explicitly supplied by this test; no automatic publisher, archive producer,
//! PostgreSQL transaction, or complete backup/restore contract is claimed.

#![cfg(target_os = "linux")]

use std::collections::{BTreeSet, HashMap};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use qdrant_edge::*;
use serde_json::{Value, json};

const CHILD_ENV: &str = "PG_QDRANT_PUBLIC_LIFECYCLE_CHILD";
const CPU_ENV: &str = "PG_QDRANT_PUBLIC_LIFECYCLE_CPU";
const DATA_ENV: &str = "PG_QDRANT_PUBLIC_LIFECYCLE_DATA";
const TEST_NAME: &str = "public_lifecycle_runs_only_in_bounded_owned_children";
const DIMENSION: usize = 256;
const MAX_FILES: usize = 256;
const MAX_LOGICAL_BYTES: u64 = 512 * 1024 * 1024;
const MAX_COPIED_BYTES: u64 = 64 * 1024 * 1024;
const OUTPUT_LIMIT: u64 = 128 * 1024;

fn stage(name: &str) {
    eprintln!("pg_qdrant_p0_public_lifecycle_stage={name}");
}

fn allowed_cpus() -> BTreeSet<u32> {
    let status = fs::read_to_string("/proc/self/status").expect("refuse: CPU affinity unavailable");
    let list = status
        .lines()
        .find_map(|line| line.strip_prefix("Cpus_allowed_list:"))
        .expect("refuse: kernel CPU list missing")
        .trim();
    let mut cpus = BTreeSet::new();
    for part in list.split(',') {
        let (first, last) = match part.split_once('-') {
            Some((first, last)) => (first.parse::<u32>().unwrap(), last.parse::<u32>().unwrap()),
            None => {
                let cpu = part.parse::<u32>().unwrap();
                (cpu, cpu)
            }
        };
        assert!(
            first <= last && last < 65_536,
            "refuse: unreasonable CPU list"
        );
        cpus.extend(first..=last);
    }
    assert!(!cpus.is_empty(), "refuse: empty CPU affinity");
    cpus
}

fn verify_child_bounds() -> u32 {
    let cpu = std::env::var(CPU_ENV)
        .expect("refuse: expected CPU absent")
        .parse::<u32>()
        .unwrap();
    assert_eq!(
        allowed_cpus(),
        BTreeSet::from([cpu]),
        "refuse: affinity is not one verified CPU"
    );
    // Fixed upstream common/cpu.rs accepts this optional override. Verify it
    // before any pool is created as well as verifying actual kernel affinity.
    assert_eq!(std::env::var("QDRANT_NUM_CPUS").as_deref(), Ok("1"));
    assert_eq!(std::thread::available_parallelism().unwrap().get(), 1);
    let data = std::env::var_os(DATA_ENV).expect("refuse: parent-owned temporary root absent");
    assert!(Path::new(&data).is_absolute() && Path::new(&data).is_dir());
    cpu
}

fn read_output(file: &File) -> String {
    let mut file = file.try_clone().unwrap();
    file.seek(SeekFrom::Start(0)).unwrap();
    let mut bytes = Vec::new();
    file.take(OUTPUT_LIMIT).read_to_end(&mut bytes).unwrap();
    String::from_utf8_lossy(&bytes).into_owned()
}

fn run_child(mode: &str, cpu: u32) -> Value {
    let directory = tempfile::tempdir().unwrap();
    let stdout = tempfile::tempfile_in(directory.path()).unwrap();
    let stderr = tempfile::tempfile_in(directory.path()).unwrap();
    let mut child = Command::new("/usr/bin/taskset")
        .args(["--cpu-list", &cpu.to_string()])
        .arg(std::env::current_exe().unwrap())
        .args(["--exact", TEST_NAME, "--nocapture", "--test-threads=1"])
        .env(CHILD_ENV, mode)
        .env(CPU_ENV, cpu.to_string())
        .env(DATA_ENV, directory.path())
        .env("QDRANT_NUM_CPUS", "1")
        .env("RUST_BACKTRACE", "0")
        .stdout(Stdio::from(stdout.try_clone().unwrap()))
        .stderr(Stdio::from(stderr.try_clone().unwrap()))
        .spawn()
        .expect("refuse: cannot launch affinity-limited child");
    let deadline = Instant::now() + Duration::from_secs(60);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if Instant::now() >= deadline
            || stdout.metadata().unwrap().len() > OUTPUT_LIMIT
            || stderr.metadata().unwrap().len() > OUTPUT_LIMIT
        {
            let _ = child.kill();
            let status = child.wait().unwrap();
            panic!(
                "bounded child {mode} exceeded time/output budget ({status}): {}",
                read_output(&stderr)
            );
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    assert!(stdout.metadata().unwrap().len() <= OUTPUT_LIMIT);
    assert!(stderr.metadata().unwrap().len() <= OUTPUT_LIMIT);
    let stdout = read_output(&stdout);
    let stderr = read_output(&stderr);
    assert!(
        status.success(),
        "child {mode}: {status}\nstdout: {stdout}\nstderr: {stderr}"
    );
    let line = stdout
        .lines()
        .find_map(|line| line.strip_prefix("pg_qdrant_p0_public_lifecycle_report="))
        .expect("successful child must emit its own structured report");
    let report: Value = serde_json::from_str(line).unwrap();
    assert_eq!(report["status"], "passed");
    assert_eq!(report["verified_cpu"], cpu);
    assert_eq!(report["mode"], mode);
    report
}

fn dense(x: f32, y: f32) -> Vec<f32> {
    let mut vector = vec![0.0; DIMENSION];
    vector[0] = x;
    vector[1] = y;
    vector
}

fn tenant() -> Filter {
    Filter::new_must(Condition::Field(FieldCondition::new_match(
        "tenant".parse().unwrap(),
        Match::new_value(ValueVariants::String("a".into())),
    )))
}

fn point(id: u64, revision: u64, x: f32, y: f32, group: &str) -> PointStructPersisted {
    PointStruct::new(
        id,
        Vectors::new_named([("v", dense(x, y))]),
        json!({"tenant":group,"revision":revision}),
    )
    .into()
}

fn fixture(path: &Path) -> EdgeShard {
    stage("fixture_create");
    fs::create_dir(path).unwrap();
    let mut parameters = EdgeVectorParams::builder(DIMENSION, Distance::Dot).build();
    parameters.on_disk = Some(true);
    let shard = EdgeShard::new(
        path,
        EdgeConfig {
            vectors: HashMap::from([("v".into(), parameters)]),
            on_disk_payload: Some(true),
            max_search_threads: Some(2),
            hnsw_config: Some(HnswIndexConfig {
                max_indexing_threads: 1,
                m: 4,
                ef_construct: 8,
                ..Default::default()
            }),
            optimizers: Some(EdgeOptimizersConfig {
                indexing_threshold: Some(1),
                default_segment_number: Some(1),
                prevent_unoptimized: Some(false),
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
                field_schema: Some(PayloadFieldSchema::FieldParams(
                    PayloadSchemaParams::Keyword(KeywordIndexParams {
                        is_tenant: Some(true),
                        ..Default::default()
                    }),
                )),
            }),
        ))
        .unwrap();
    shard
        .update(UpdateOperation::PointOperation(
            PointOperations::UpsertPoints(PointInsertOperations::PointsList(vec![
                point(1, 1, 1.0, 0.0, "a"),
                point(2, 1, 0.8, 0.2, "a"),
                point(3, 1, 0.4, 0.6, "a"),
                point(4, 1, 2.0, 1.0, "b"),
            ])),
        ))
        .unwrap();
    stage("fixture_optimize");
    assert!(
        shard.optimize().unwrap(),
        "the read-only fixture must be genuinely optimized"
    );
    shard.flush().unwrap();
    shard
}

fn records(reader: &impl EdgeShardRead) -> Vec<Record> {
    let mut request = RetrieveRequest::new((1_u64..=4).map(PointId::NumId).collect());
    request.with_payload = Some(WithPayloadInterface::Bool(true));
    request.with_vector = Some(WithVector::Bool(true));
    let mut rows = reader.retrieve(request).unwrap();
    rows.sort_by_key(|row| row.id);
    rows
}

fn assert_query(reader: &impl EdgeShardRead, expected: &[(u64, f32)]) {
    let mut request = QueryRequest::new(4);
    request.query = Some(ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
        using: Some("v".into()),
        query: VectorInternal::Dense(dense(1.0, 0.0)),
    })));
    request.filter = Some(tenant());
    request.params = Some(SearchParams {
        exact: true,
        indexed_only: false,
        ..Default::default()
    });
    request.with_payload = WithPayloadInterface::Bool(true);
    let hits = reader.query(request).unwrap();
    assert_eq!(hits.len(), expected.len());
    for (hit, (id, score)) in hits.iter().zip(expected) {
        assert_eq!(hit.id, PointId::NumId(*id));
        assert!(
            (hit.score - score).abs() < 1e-5,
            "incorrect lifecycle query score"
        );
        assert_eq!(hit.payload.as_ref().unwrap().0["tenant"], "a");
    }
}

#[derive(Default)]
struct CopyBudget {
    files: usize,
    logical: u64,
    copied: u64,
}

// This is fixture transport, not a snapshot API. Refuse links/special files,
// cap traversal and bytes, and preserve zero extents instead of filling them.
fn copy_owned(source: &Path, destination: &Path, depth: usize, budget: &mut CopyBudget) {
    assert!(depth <= 16, "copy depth limit");
    fs::create_dir(destination).unwrap();
    for entry in fs::read_dir(source).unwrap() {
        let entry = entry.unwrap();
        let metadata = fs::symlink_metadata(entry.path()).unwrap();
        let target = destination.join(entry.file_name());
        if metadata.is_dir() {
            copy_owned(&entry.path(), &target, depth + 1, budget);
            continue;
        }
        assert!(
            metadata.is_file() && !metadata.file_type().is_symlink(),
            "refuse nonregular fixture file"
        );
        budget.files += 1;
        budget.logical = budget.logical.checked_add(metadata.len()).unwrap();
        assert!(
            budget.files <= MAX_FILES && budget.logical <= MAX_LOGICAL_BYTES,
            "copy file/logical-byte limit"
        );
        let mut input = File::open(entry.path()).unwrap();
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(target)
            .unwrap();
        let mut remaining = metadata.len();
        let mut buffer = [0_u8; 65_536];
        while remaining > 0 {
            let length = remaining.min(buffer.len() as u64) as usize;
            input.read_exact(&mut buffer[..length]).unwrap();
            if buffer[..length].iter().all(|byte| *byte == 0) {
                output.seek(SeekFrom::Current(length as i64)).unwrap();
            } else {
                budget.copied += length as u64;
                assert!(
                    budget.copied <= MAX_COPIED_BYTES,
                    "copy physical-content budget"
                );
                output.write_all(&buffer[..length]).unwrap();
            }
            remaining -= length as u64;
        }
        output.set_len(metadata.len()).unwrap();
        output.sync_all().unwrap();
    }
}

fn write_manifest(path: &Path, state: SegmentManifestState) -> usize {
    let segments = LocalSegmentEnumerator::new(path).list_segments().unwrap();
    assert!(
        !segments.is_empty() && segments.len() <= 8,
        "bounded public segment enumeration"
    );
    let mut manifest = SegmentsManifest::default();
    for id in segments.keys() {
        manifest.set(*id, state.clone());
    }
    // Test-supplied manifest, using the published serializable type. This is
    // deliberately not evidence that EdgeShard automatically publishes it.
    let data = serde_json::to_vec(&manifest).unwrap();
    assert!(data.len() <= 4096);
    fs::write(path.join("segments_manifest.json"), data).unwrap();
    segments.len()
}

fn update_batch() -> Vec<(u64, UpdateOperation)> {
    vec![
        (
            10_000,
            UpdateOperation::PointOperation(PointOperations::UpsertPoints(
                PointInsertOperations::PointsList(vec![point(1, 2, 0.25, 0.75, "a")]),
            )),
        ),
        (
            10_001,
            UpdateOperation::PayloadOperation(PayloadOps::SetPayload(SetPayloadOp {
                payload: serde_json::from_value(json!({"unused":true})).unwrap(),
                points: Some(vec![PointId::NumId(99)]),
                filter: None,
                key: None,
            })),
        ),
    ]
}

fn unsupported_operations() -> Vec<UpdateOperation> {
    vec![
        UpdateOperation::PointOperation(PointOperations::DeletePointsByFilter(tenant())),
        UpdateOperation::PointOperation(PointOperations::UpsertPointsConditional(
            ConditionalInsertOperation {
                points_op: PointInsertOperations::PointsList(vec![point(1, 2, 0.25, 0.75, "a")]),
                condition: tenant(),
                update_mode: Some(UpdateMode::UpdateOnly),
            },
        )),
        UpdateOperation::FieldIndexOperation(FieldIndexOperations::DeleteIndex(
            "tenant".parse().unwrap(),
        )),
    ]
}

fn observed_unimplemented<T>(operation: impl FnOnce() -> T, expected: &str) -> String {
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(operation));
    let panic = match outcome {
        Err(panic) => panic,
        Ok(_) => panic!("fixed-version unimplemented observation changed: {expected}"),
    };
    let message = panic
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| panic.downcast_ref::<&str>().copied())
        .unwrap_or("non-string panic");
    assert!(message.contains(expected), "unexpected panic: {message}");
    message.to_owned()
}

fn run_lifecycle(cpu: u32) -> Value {
    // The parent owns the outer directory and removes it even if the bounded
    // child must be killed. No existing shard is ever accepted as input.
    let root = tempfile::tempdir_in(std::env::var_os(DATA_ENV).unwrap()).unwrap();
    let source = root.path().join("source");
    let shard = fixture(&source);
    let original = records(&shard);
    assert_eq!(original.len(), 4);
    assert_query(&shard, &[(1, 1.0), (2, 0.8), (3, 0.4)]);
    stage("snapshot_manifest");
    let snapshot_manifest = shard.snapshot_manifest().unwrap();
    snapshot_manifest.validate().unwrap();
    assert!(!snapshot_manifest.is_empty() && snapshot_manifest.len() <= 8);
    let snapshot_ids: BTreeSet<_> = snapshot_manifest.iter().map(|(id, _)| id.clone()).collect();
    let local_ids: BTreeSet<_> = LocalSegmentEnumerator::new(&source)
        .list_segments()
        .unwrap()
        .keys()
        .map(ToString::to_string)
        .collect();
    assert_eq!(
        snapshot_ids, local_ids,
        "snapshot manifest omitted a fixture segment"
    );
    let snapshot_segments = snapshot_manifest.len();
    drop(shard); // No ordinary writer remains while another owner opens it.

    stage("read_only_missing_manifest");
    assert!(!source.join("segments_manifest.json").exists());
    let missing = match ReadOnlyEdgeShard::open_mmap(&source) {
        Err(error) => error,
        Ok(_) => panic!("missing manifest unexpectedly accepted"),
    };
    assert!(missing.to_string().contains("segments_manifest.json"));

    stage("read_only_supplied_manifest");
    let manifest_segments = write_manifest(&source, SegmentManifestState::Active);
    let follower = ReadOnlyEdgeShard::open_mmap(&source).unwrap();
    assert!(follower.segments_count() > 0);
    assert_eq!(
        follower.count(CountRequest::new()).unwrap(),
        4,
        "follower skipped required rows"
    );
    assert_eq!(records(&follower), original);
    assert_query(&follower, &[(1, 1.0), (2, 0.8), (3, 0.4)]);
    write_manifest(&source, SegmentManifestState::Retiring);
    follower.refresh().unwrap();
    assert_eq!(follower.count(CountRequest::new()).unwrap(), 0);
    write_manifest(&source, SegmentManifestState::Active);
    follower.refresh().unwrap();
    assert_eq!(records(&follower), original);
    assert_query(&follower, &[(1, 1.0), (2, 0.8), (3, 0.4)]);
    drop(follower);

    stage("owned_copy");
    let target = root.path().join("update-copy");
    let mut budget = CopyBudget::default();
    copy_owned(&source, &target, 0, &mut budget);

    stage("update_only_preview");
    let writer = UpdateOnlyEdgeShard::open_mmap(&target).unwrap();
    assert!(!writer.segment_configs().is_empty());
    let _plan = UpdateBatchPlan::build(update_batch()).unwrap();
    for operation in unsupported_operations() {
        let error = match UpdateBatchPlan::build(vec![(10_002, operation)]) {
            Err(error) => error,
            Ok(_) => panic!("unsupported update-only operation was accepted"),
        };
        assert!(
            matches!(error, OperationError::ValidationError { .. }),
            "{error}"
        );
    }
    let preview = writer.preview_batch(update_batch()).unwrap();
    assert_eq!(preview.points.len(), 2);
    assert_eq!(preview.points[0].id, PointId::NumId(1));
    let materialized = match &preview.points[0].action {
        PointAction::Store(point) => point,
        _ => panic!("existing point was not materialized"),
    };
    assert_eq!(materialized.id, PointId::NumId(1));
    assert_eq!(materialized.version, 10_000);
    assert_eq!(materialized.payload.0["revision"], 2);
    assert_eq!(
        materialized.updated_vectors.get("v").unwrap().to_owned(),
        VectorInternal::Dense(dense(0.25, 0.75))
    );
    assert_eq!(preview.points[1].id, PointId::NumId(99));
    assert!(matches!(preview.points[1].action, PointAction::Missing));
    drop(writer);
    let reader = EdgeShard::load(&target, None).unwrap();
    assert_eq!(
        records(&reader),
        original,
        "preview mutated persisted records"
    );
    drop(reader);

    stage("update_only_apply_without_writes");
    let writer = UpdateOnlyEdgeShard::open_mmap(&target).unwrap();
    // Only the no-write branches are implemented in fixed 0.8.0. An older
    // operation must skip an existing row, and an update of missing ID99
    // must remain missing; neither requires the unfinished append storages.
    let no_write = || {
        vec![
            (
                0,
                UpdateOperation::PointOperation(PointOperations::DeletePoints {
                    ids: vec![PointId::NumId(1)],
                }),
            ),
            update_batch().pop().unwrap(),
        ]
    };
    let applied = writer.apply_batch(no_write()).unwrap();
    assert_eq!(
        (
            applied.stored,
            applied.deleted,
            applied.skipped,
            applied.missing
        ),
        (0, 0, 1, 1)
    );
    assert_eq!(writer.apply_batch(no_write()).unwrap(), applied);

    stage("update_only_store_unimplemented");
    let store_panic = observed_unimplemented(
        || writer.apply_batch(update_batch()),
        "needs the append-only storages and field indexes of the write target",
    );
    drop(writer);
    let reader = EdgeShard::load(&target, None).unwrap();
    assert_eq!(
        records(&reader),
        original,
        "unimplemented store changed fixture"
    );
    assert_query(&reader, &[(1, 1.0), (2, 0.8), (3, 0.4)]);
    drop(reader);

    stage("update_only_delete_unimplemented");
    let writer = UpdateOnlyEdgeShard::open_mmap(&target).unwrap();
    let deletion = || {
        vec![(
            10_002,
            UpdateOperation::PointOperation(PointOperations::DeletePoints {
                ids: vec![PointId::NumId(2)],
            }),
        )]
    };
    let preview = writer.preview_batch(deletion()).unwrap();
    assert_eq!(preview.points.len(), 1);
    assert_eq!(preview.points[0].id, PointId::NumId(2));
    assert!(matches!(preview.points[0].action, PointAction::Delete));
    let delete_panic = observed_unimplemented(
        || writer.apply_batch(deletion()),
        "needs an appendable deleted-points bitmask",
    );
    drop(writer);
    let reader = EdgeShard::load(&target, None).unwrap();
    assert_eq!(reader.count(CountRequest::new()).unwrap(), 4);
    assert_eq!(
        records(&reader),
        original,
        "unimplemented delete changed fixture"
    );
    assert_query(&reader, &[(1, 1.0), (2, 0.8), (3, 0.4)]);
    drop(reader);
    let unchanged = EdgeShard::load(&source, None).unwrap();
    assert_eq!(
        records(&unchanged),
        original,
        "mutation escaped the owned copy"
    );
    drop(unchanged);

    stage("completed");
    json!({
        "status":"passed","mode":"lifecycle","engine_version":"0.8.0","verified_cpu":cpu,
        "snapshot_manifest_segments":snapshot_segments,"test_supplied_manifest_segments":manifest_segments,
        "copy":{"files":budget.files,"logical_bytes_scanned":budget.logical,"nonzero_bytes_written":budget.copied},
        "read_only":{"row_coverage":4,"exact_filtered_scores":"passed","manual_manifest_refresh":"passed","missing_manifest":"error"},
        "update_only":{"preview_materialized_goldens":"passed","preview_unchanged":"passed","no_write_apply_and_replay":"passed",
            "store_supported":false,"delete_supported":false,"store_panic":store_panic,"delete_panic":delete_panic,
            "records_unchanged_after_panics":true,"unsupported_operations":3,"original_source_records_preserved":true},
        "probe_resources":{"kernel_affinity_cpus":1,"qdrant_num_cpus_override":1,"override_scope":"probe_only","child_deadline_seconds":60},
        "snapshot_archive_created":false,"snapshot_restore_verified":false,"sql_integration_verified":false,
        "automatic_manifest_publication_verified":false,"unflushed_wal_recovery_verified":false
    })
}

fn empty_bootstrap(cpu: u32) -> Value {
    let root = tempfile::tempdir_in(std::env::var_os(DATA_ENV).unwrap()).unwrap();
    fs::create_dir(root.path().join("segments")).unwrap();
    stage("empty_update_only_bootstrap");
    let outcome = std::panic::catch_unwind(|| UpdateOnlyEdgeShard::open_mmap(root.path()));
    let panic = match outcome {
        Err(panic) => panic,
        Ok(_) => panic!("fixed-version unimplemented bootstrap observation changed"),
    };
    let message = panic
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| panic.downcast_ref::<&str>().copied())
        .unwrap_or("non-string panic");
    assert!(
        message.contains("creating the initial appendable segment"),
        "unexpected bootstrap panic: {message}"
    );
    json!({"status":"passed","mode":"empty-bootstrap","engine_version":"0.8.0","verified_cpu":cpu,
        "observed_outcome":"caught_rust_unimplemented_panic","bootstrap_supported":false,
        "sql_integration_verified":false,"panic_message":message})
}

#[test]
fn public_lifecycle_runs_only_in_bounded_owned_children() {
    if let Ok(mode) = std::env::var(CHILD_ENV) {
        let cpu = verify_child_bounds(); // Must precede every engine constructor.
        let report = match mode.as_str() {
            "lifecycle" => run_lifecycle(cpu),
            "empty-bootstrap" => empty_bootstrap(cpu),
            _ => panic!("refuse: unknown private child mode"),
        };
        println!("\npg_qdrant_p0_public_lifecycle_report={report}");
        return;
    }
    assert!(
        Path::new("/usr/bin/taskset").is_file(),
        "refuse: taskset is required for verified child affinity"
    );
    let cpu = *allowed_cpus().first().unwrap();
    let report = run_child("lifecycle", cpu);
    let empty = run_child("empty-bootstrap", cpu);
    println!(
        "{}",
        json!({"kind":"public_lifecycle_probe","status":"passed","lifecycle":report,"empty_bootstrap":empty})
    );
}

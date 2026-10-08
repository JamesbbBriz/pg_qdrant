//! P0 controller-policy experiment, not an Edge WAL replay or PG recovery API.
//! The old generation is never opened after SIGKILL. Only a complete source
//! fixture authorizes construction and validation of a separate generation.
#![cfg(target_os = "linux")]

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use qdrant_edge::*;
use serde_json::{Value, json};

const TEST_NAME: &str = "dirty_generation_is_refused_and_rebuilt_from_complete_source";
const CHILD_MODE: &str = "PG_QDRANT_DIRTY_REBUILD_CHILD";
const ROOT_ENV: &str = "PG_QDRANT_DIRTY_REBUILD_ROOT";
const CPU_ENV: &str = "PG_QDRANT_DIRTY_REBUILD_CPU";
const PARENT_ENV: &str = "PG_QDRANT_DIRTY_REBUILD_PARENT";
const MAX_FILES: usize = 128;
const MAX_LOGICAL_BYTES: u64 = 256 * 1024 * 1024;
const MAX_OUTPUT_BYTES: u64 = 64 * 1024;
const CHILD_TIMEOUT: Duration = Duration::from_secs(45);
const ALL_IDS: &[u64] = &[11, 12, 13, 14, 15, 22];

fn stage(name: &str) {
    eprintln!("pg_qdrant_p0_dirty_rebuild_stage={name}");
}

fn cpus() -> BTreeSet<u32> {
    let status = fs::read_to_string("/proc/self/status").expect("kernel CPU affinity required");
    let list = status
        .lines()
        .find_map(|line| line.strip_prefix("Cpus_allowed_list:"))
        .expect("kernel CPU list missing")
        .trim();
    let mut result = BTreeSet::new();
    for part in list.split(',') {
        let (first, last) = part.split_once('-').unwrap_or((part, part));
        let (first, last) = (first.parse::<u32>().unwrap(), last.parse::<u32>().unwrap());
        assert!(first <= last && last < 65_536, "unreasonable CPU range");
        result.extend(first..=last);
    }
    assert!(!result.is_empty(), "empty CPU affinity");
    result
}

fn procfs_pid() -> u32 {
    fs::read_to_string("/proc/self/status")
        .unwrap()
        .lines()
        .find_map(|line| line.strip_prefix("Pid:"))
        .unwrap()
        .trim()
        .parse()
        .unwrap()
}

fn write_new(path: &Path, value: &Value) {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .unwrap();
    file.write_all(serde_json::to_string(value).unwrap().as_bytes())
        .unwrap();
    file.sync_all().unwrap();
}

fn read_json(path: &Path) -> Value {
    assert!(fs::symlink_metadata(path).unwrap().is_file());
    assert!(fs::metadata(path).unwrap().len() <= MAX_OUTPUT_BYTES);
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn verify_child_root() -> (PathBuf, u32) {
    let root = PathBuf::from(std::env::var_os(ROOT_ENV).expect("parent-owned root required"));
    assert!(root.is_absolute() && root.canonicalize().unwrap() == root);
    assert!(
        root.file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("pg-qdrant-dirty-rebuild-")
    );
    let metadata = fs::symlink_metadata(&root).unwrap();
    assert!(metadata.is_dir() && metadata.mode() & 0o777 == 0o700);
    let status = fs::read_to_string("/proc/self/status").unwrap();
    let parent: u32 = status
        .lines()
        .find_map(|line| line.strip_prefix("PPid:"))
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    let expected_parent: u32 = std::env::var(PARENT_ENV).unwrap().parse().unwrap();
    assert_eq!(parent, expected_parent, "direct parent identity mismatch");
    let permit = read_json(&root.join("controller.json"));
    assert_eq!(permit["controller_procfs_pid"], parent);
    assert_eq!(permit["purpose"], "owned_dirty_rebuild_p0_fixture");
    assert_eq!(metadata.uid(), fs::metadata("/proc/self").unwrap().uid());
    let cpu: u32 = std::env::var(CPU_ENV).unwrap().parse().unwrap();
    assert_eq!(
        cpus(),
        BTreeSet::from([cpu]),
        "must pin before creating any engine pools"
    );
    assert_eq!(std::thread::available_parallelism().unwrap().get(), 1);
    assert_eq!(std::env::var("QDRANT_NUM_CPUS").as_deref(), Ok("1"));
    (root, cpu)
}

fn source_row(id: u64, key: &str, incarnation: u64, revision: u64, tenant: &str, x: f32) -> Value {
    json!({"point_id":id,"payload":{"key":key,"incarnation":incarnation,
        "revision":revision,"tenant":tenant},"dense":[x,0.0]})
}

fn baseline() -> Value {
    json!({"fixture_schema":1,"snapshot":"before_authoritative_changes","rows":[
        source_row(11,"updated",1,1,"a",1.0),
        source_row(12,"reused",1,2,"a",0.75),
        source_row(13,"deleted",1,1,"a",0.5),
        source_row(14,"other-tenant",1,3,"b",100.0)
    ]})
}

fn authoritative() -> Value {
    json!({"fixture_schema":1,"snapshot":"complete_authoritative_recovery_source","rows":[
        source_row(11,"updated",1,2,"a",0.6),
        source_row(22,"reused",2,1,"a",1.0),
        source_row(15,"inserted",1,1,"a",0.2),
        source_row(14,"other-tenant",1,3,"b",100.0)
    ]})
}

fn points(source: &Value) -> Vec<PointStructPersisted> {
    assert_eq!(source["fixture_schema"], 1);
    let rows = source["rows"].as_array().unwrap();
    assert_eq!(
        rows.len(),
        4,
        "complete fixture must contain all four surviving rows"
    );
    rows.iter()
        .map(|row| {
            PointStruct::new(
                row["point_id"].as_u64().unwrap(),
                Vectors::new_named([(
                    "dense",
                    Vector::new_dense(
                        serde_json::from_value::<Vec<f32>>(row["dense"].clone()).unwrap(),
                    ),
                )]),
                row["payload"].clone(),
            )
            .into()
        })
        .collect()
}

fn create(path: &Path, source: &Value) -> EdgeShard {
    pg_qdrant_edge_probe::cpu::require().expect("fixed Edge native CPU baseline");
    fs::create_dir(path).unwrap();
    let mut dense = EdgeVectorParams::builder(2, Distance::Dot).build();
    dense.on_disk = Some(true);
    let shard = EdgeShard::new(
        path,
        EdgeConfig {
            vectors: HashMap::from([("dense".into(), dense)]),
            on_disk_payload: Some(true),
            max_search_threads: Some(1),
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
    for name in ["tenant", "key"] {
        shard
            .update(UpdateOperation::FieldIndexOperation(
                FieldIndexOperations::CreateIndex(CreateIndex {
                    field_name: name.parse().unwrap(),
                    field_schema: Some(PayloadFieldSchema::FieldParams(
                        PayloadSchemaParams::Keyword(KeywordIndexParams {
                            is_tenant: Some(name == "tenant"),
                            ..Default::default()
                        }),
                    )),
                }),
            ))
            .unwrap();
    }
    shard
        .update(UpdateOperation::PointOperation(
            PointOperations::UpsertPoints(PointInsertOperations::PointsList(points(source))),
        ))
        .unwrap();
    shard
}

fn exact_filter(field: &str, value: &str) -> Filter {
    Filter::new_must(Condition::Field(FieldCondition::new_match(
        field.parse().unwrap(),
        Match::new_value(ValueVariants::String(value.into())),
    )))
}

fn verify_complete(shard: &EdgeShard, source: &Value) -> Value {
    assert_eq!(shard.count(CountRequest::new()).unwrap(), 4);
    let mut request = RetrieveRequest::new(ALL_IDS.iter().copied().map(PointId::NumId).collect());
    request.with_payload = Some(WithPayloadInterface::Bool(true));
    request.with_vector = Some(WithVector::Bool(true));
    let records = shard.retrieve(request).unwrap();
    let expected: BTreeMap<u64, &Value> = source["rows"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| (row["point_id"].as_u64().unwrap(), row))
        .collect();
    assert_eq!(records.len(), expected.len());
    for record in &records {
        let PointId::NumId(id) = record.id else {
            panic!("fixture numeric ID changed")
        };
        let row = expected
            .get(&id)
            .expect("deleted or unexpected point returned");
        let expected_payload: Payload = serde_json::from_value(row["payload"].clone()).unwrap();
        assert_eq!(record.payload.as_ref(), Some(&expected_payload));
        let Some(VectorStructInternal::Named(vectors)) = record.vector.as_ref() else {
            panic!("named source representation missing")
        };
        assert_eq!(vectors.len(), 1);
        assert_eq!(
            vectors["dense"],
            VectorInternal::Dense(
                serde_json::from_value::<Vec<f32>>(row["dense"].clone()).unwrap()
            )
        );
    }
    let mut nearest = QueryRequest::new(8);
    nearest.filter = Some(exact_filter("tenant", "a"));
    nearest.params = Some(SearchParams {
        exact: true,
        indexed_only: false,
        ..Default::default()
    });
    nearest.query = Some(ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
        query: VectorInternal::Dense(vec![1.0, 0.0]),
        using: Some("dense".into()),
    })));
    nearest.with_payload = WithPayloadInterface::Bool(true);
    let hits = shard.query(nearest).unwrap();
    let golden = [(22_u64, 1.0_f32), (11, 0.6), (15, 0.2)];
    assert_eq!(hits.len(), golden.len());
    for (hit, (id, score)) in hits.iter().zip(golden) {
        assert_eq!(hit.id, PointId::NumId(id));
        assert!((hit.score - score).abs() < 1e-6);
        assert_eq!(hit.payload.as_ref().unwrap().0["tenant"], "a");
    }
    let mut reused = QueryRequest::new(8);
    reused.filter = Some(exact_filter("tenant", "a").merge(&exact_filter("key", "reused")));
    reused.with_payload = WithPayloadInterface::Bool(true);
    let reused = shard.query(reused).unwrap();
    assert_eq!(reused.len(), 1);
    assert_eq!(reused[0].id, PointId::NumId(22));
    assert_eq!(reused[0].payload.as_ref().unwrap().0["incarnation"], 2);
    json!({"surviving_point_ids":[11,14,15,22],"absent_point_ids":[12,13],
        "exact_tenant_query_ids":[22,11,15],"exact_tenant_query_scores":[1.0,0.6,0.2],
        "reused_business_key":"reused","replacement_point_id":22,"replacement_incarnation":2,
        "payloads_and_named_vectors_equal_complete_source":true})
}

// This test-owned policy performs no Edge call. An update return, a file, or a
// successful load cannot clear the marker; no load of the dirty path is tried.
fn admit_old_generation(root: &Path) -> Result<PathBuf, &'static str> {
    let marker = read_json(&root.join("dirty-generation.json"));
    if marker["generation"] != "generation-old" || marker["state"] != "dirty" {
        return Err("invalid_controller_marker");
    }
    Err("dirty_generation_requires_fresh_source_rebuild")
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
    let mut reader = file.try_clone().unwrap();
    reader.seek(SeekFrom::Start(0)).unwrap();
    let mut bytes = Vec::new();
    reader
        .take(MAX_OUTPUT_BYTES)
        .read_to_end(&mut bytes)
        .unwrap();
    String::from_utf8_lossy(&bytes).into_owned()
}

fn child(root: &Path, cpu: u32, mode: &str) -> (OwnedChild, File, File) {
    let output = tempfile::tempfile_in(root).unwrap();
    let error = tempfile::tempfile_in(root).unwrap();
    let process = Command::new("/usr/bin/taskset")
        .args(["--cpu-list", &cpu.to_string()])
        .arg(std::env::current_exe().unwrap())
        .args(["--exact", TEST_NAME, "--nocapture", "--test-threads=1"])
        .env(CHILD_MODE, mode)
        .env(ROOT_ENV, root)
        .env(CPU_ENV, cpu.to_string())
        .env(PARENT_ENV, procfs_pid().to_string())
        .env("QDRANT_NUM_CPUS", "1")
        .env("RUST_BACKTRACE", "0")
        .stdin(Stdio::null())
        .stdout(Stdio::from(output.try_clone().unwrap()))
        .stderr(Stdio::from(error.try_clone().unwrap()))
        .spawn()
        .unwrap();
    (OwnedChild(process), output, error)
}

fn wait_bounded(
    child: &mut OwnedChild,
    output: &File,
    error: &File,
    ready: Option<&Path>,
) -> Option<ExitStatus> {
    let deadline = Instant::now() + CHILD_TIMEOUT;
    loop {
        assert!(
            Instant::now() < deadline,
            "child deadline; stderr={}",
            text(error)
        );
        assert!(
            output.metadata().unwrap().len() <= MAX_OUTPUT_BYTES
                && error.metadata().unwrap().len() <= MAX_OUTPUT_BYTES,
            "child output budget exceeded"
        );
        if let Some(status) = child.0.try_wait().unwrap() {
            return Some(status);
        }
        if ready.is_some_and(Path::is_file) {
            return None;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn digest(path: &Path, output_root: &Path) -> String {
    // Coreutils operates only on a previously checked owned regular file. Its
    // output and lifetime are bounded just like the engine subprocesses.
    let output = tempfile::tempfile_in(output_root).unwrap();
    let error = tempfile::tempfile_in(output_root).unwrap();
    let mut process = OwnedChild(
        Command::new("/usr/bin/sha256sum")
            .arg("--")
            .arg(path)
            .stdin(Stdio::null())
            .stdout(Stdio::from(output.try_clone().unwrap()))
            .stderr(Stdio::from(error.try_clone().unwrap()))
            .spawn()
            .unwrap(),
    );
    let status = wait_bounded(&mut process, &output, &error, None).unwrap();
    assert!(status.success(), "sha256sum failed: {}", text(&error));
    let hash = text(&output).split_whitespace().next().unwrap().to_owned();
    assert!(hash.len() == 64 && hash.bytes().all(|byte| byte.is_ascii_hexdigit()));
    hash
}

fn inventory(root: &Path, output_root: &Path) -> Value {
    fn visit(
        root: &Path,
        path: &Path,
        output_root: &Path,
        depth: usize,
        files: &mut BTreeMap<String, Value>,
        logical: &mut u64,
    ) {
        assert!(depth <= 12);
        for entry in fs::read_dir(path).unwrap() {
            let entry = entry.unwrap();
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path).unwrap();
            assert_eq!(
                metadata.dev(),
                fs::metadata(root).unwrap().dev(),
                "cross-device fixture rejected"
            );
            if metadata.is_dir() {
                visit(root, &path, output_root, depth + 1, files, logical);
                continue;
            }
            assert!(
                metadata.is_file() && metadata.nlink() == 1,
                "symlink/special/hard-linked file rejected"
            );
            *logical = logical.checked_add(metadata.len()).unwrap();
            assert!(
                *logical <= MAX_LOGICAL_BYTES && files.len() < MAX_FILES,
                "inventory budget exceeded"
            );
            let relative = path
                .strip_prefix(root)
                .unwrap()
                .to_str()
                .unwrap()
                .to_owned();
            files.insert(
                relative,
                json!({"length":metadata.len(),"device":metadata.dev(),
                "inode":metadata.ino(),"mode":metadata.mode(),"sha256":digest(&path, output_root)}),
            );
        }
    }
    let mut files = BTreeMap::new();
    let mut logical = 0;
    visit(root, root, output_root, 0, &mut files, &mut logical);
    assert!(!files.is_empty());
    json!({"files":files,"logical_bytes":logical})
}

fn child_body(mode: &str) {
    pg_qdrant_edge_probe::cpu::require().expect("fixed Edge native CPU baseline");
    let (root, cpu) = verify_child_root();
    if mode == "writer" {
        stage("baseline_create_and_flush");
        let shard = create(
            &root.join("generation-old"),
            &read_json(&root.join("baseline-source.json")),
        );
        shard.flush().unwrap();
        stage("apply_after_baseline_flush");
        let source = read_json(&root.join("complete-source.json"));
        shard
            .update(UpdateOperation::PointOperation(
                PointOperations::DeletePoints {
                    ids: vec![12.into(), 13.into()],
                },
            ))
            .unwrap();
        shard
            .update(UpdateOperation::PointOperation(
                PointOperations::UpsertPoints(PointInsertOperations::PointsList(points(&source))),
            ))
            .unwrap();
        let observed = verify_complete(&shard, &source);
        stage("applied_before_explicit_flush");
        write_new(
            &root.join("writer-ready.pending"),
            &json!({"status":"applied_before_explicit_flush",
            "pid":std::process::id(),"verified_cpu":cpu,"generation":"generation-old",
            "baseline_explicit_flush_completed":true,"explicit_flush_after_mutation":false,
            "in_process_observations":observed}),
        );
        fs::rename(
            root.join("writer-ready.pending"),
            root.join("writer-ready.json"),
        )
        .unwrap();
        // Keep the owner live; the parent terminates it with SIGKILL, avoiding
        // Drop's implicit flush. Internal mmap/kernel persistence is unknown.
        loop {
            std::thread::park();
        }
    }
    assert_eq!(mode, "rebuild", "unsupported fixture child mode");
    stage("fresh_source_rebuild");
    let source = read_json(&root.join("complete-source.json"));
    let path = root.join("generation-rebuilt");
    let shard = create(&path, &source);
    let before = verify_complete(&shard, &source);
    shard.flush().unwrap();
    drop(shard);
    stage("fresh_generation_reopen");
    let shard = EdgeShard::load(&path, None).unwrap();
    let reopened = verify_complete(&shard, &source);
    assert_eq!(before, reopened);
    drop(shard);
    write_new(
        &root.join("rebuild-report.json"),
        &json!({"status":"passed","verified_cpu":cpu,
        "generation":"generation-rebuilt","explicit_flush_then_reopen":true,
        "source":"complete-source.json","observations":reopened}),
    );
}

#[test]
fn dirty_generation_is_refused_and_rebuilt_from_complete_source() {
    pg_qdrant_edge_probe::cpu::require().expect("fixed Edge native CPU baseline");
    if let Ok(mode) = std::env::var(CHILD_MODE) {
        child_body(&mode);
        return;
    }
    assert!(Path::new("/usr/bin/taskset").is_file() && Path::new("/usr/bin/sha256sum").is_file());
    let root = tempfile::Builder::new()
        .prefix("pg-qdrant-dirty-rebuild-")
        .tempdir()
        .unwrap();
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let cpu = *cpus().first().unwrap();
    write_new(
        &root.path().join("controller.json"),
        &json!({"controller_pid":std::process::id(),"controller_procfs_pid":procfs_pid(),
        "purpose":"owned_dirty_rebuild_p0_fixture"}),
    );
    write_new(&root.path().join("baseline-source.json"), &baseline());
    write_new(&root.path().join("complete-source.json"), &authoritative());
    write_new(
        &root.path().join("dirty-generation.json"),
        &json!({"generation":"generation-old",
        "state":"dirty","authority":"test_controller_only","set_before_owner_start":true}),
    );
    let source_before = digest(&root.path().join("complete-source.json"), root.path());
    let baseline_before = digest(&root.path().join("baseline-source.json"), root.path());
    let marker_before = digest(&root.path().join("dirty-generation.json"), root.path());
    assert_ne!(source_before, baseline_before);

    let (mut writer, output, error) = child(root.path(), cpu, "writer");
    let ready_path = root.path().join("writer-ready.json");
    let status = wait_bounded(&mut writer, &output, &error, Some(&ready_path));
    assert!(
        status.is_none(),
        "writer exited before cut: {status:?}; stderr={}",
        text(&error)
    );
    // The writer publishes this complete synced JSON by renaming a separate
    // pending file, so readiness cannot be observed halfway through its write.
    let ready = read_json(&ready_path);
    assert_eq!(ready["pid"], writer.0.id());
    assert_eq!(ready["status"], "applied_before_explicit_flush");
    assert_eq!(ready["explicit_flush_after_mutation"], false);
    writer
        .0
        .kill()
        .expect("SIGKILL only the exact spawned writer");
    let status = writer.0.wait().unwrap();
    assert_eq!(status.signal(), Some(9));
    drop(writer);
    stage("dirty_generation_refused_without_load");
    let old_path = root.path().join("generation-old");
    let identity = fs::metadata(&old_path).unwrap();
    let before = inventory(&old_path, root.path());
    assert_eq!(
        admit_old_generation(root.path()).unwrap_err(),
        "dirty_generation_requires_fresh_source_rebuild"
    );

    let (mut rebuild, output, error) = child(root.path(), cpu, "rebuild");
    let status = wait_bounded(&mut rebuild, &output, &error, None).unwrap();
    assert!(
        status.success(),
        "fresh rebuild failed: {status}; stderr={}",
        text(&error)
    );
    let report = read_json(&root.path().join("rebuild-report.json"));
    assert_eq!(report["status"], "passed");
    assert_eq!(report["verified_cpu"], cpu);
    assert_eq!(
        inventory(&old_path, root.path()),
        before,
        "preserved dirty-generation files changed"
    );
    assert_eq!(fs::metadata(&old_path).unwrap().ino(), identity.ino());
    assert_eq!(
        digest(&root.path().join("complete-source.json"), root.path()),
        source_before
    );
    assert_eq!(
        digest(&root.path().join("baseline-source.json"), root.path()),
        baseline_before
    );
    assert_eq!(
        digest(&root.path().join("dirty-generation.json"), root.path()),
        marker_before
    );
    assert_eq!(
        admit_old_generation(root.path()).unwrap_err(),
        "dirty_generation_requires_fresh_source_rebuild"
    );
    println!(
        "pg_qdrant_p0_dirty_rebuild_report={}",
        json!({
            "status":"passed","engine_version":"0.8.0","capability_ids":["L01","L04","L05","L07"],
            "policy":"test_controller_dirty_refusal_then_complete_source_rebuild",
            "writer":{"termination_signal":9,"ready":ready},
            "preserved_generation":{"path":"generation-old","device":identity.dev(),"inode":identity.ino(),
                "inventory":before,"hashes_and_identity_unchanged":true,"post_kill_edge_open_attempts":0},
            "source":{"baseline_sha256":baseline_before,"complete_source_sha256":source_before,
                "both_digests_unchanged":true,"rebuild_uses_complete_fixture_only":true},
            "rebuild":report,"bounds":{"child_deadline_seconds":45,"output_bytes_per_stream":MAX_OUTPUT_BYTES,
                "kernel_affinity_cpus":1,"qdrant_num_cpus_override":1,"hash_files":MAX_FILES,
                "hash_logical_bytes":MAX_LOGICAL_BYTES,"query_limit":8},
            "scope":{"controller_policy_only":true,"dirty_state_survival_inspected":false,
                "wal_replay_verified":false,"postgresql_ack_or_atomicity_verified":false,
                "production_generation_cutover_verified":false,"machine_loss_durability_verified":false,
                "p0_exit_passed":false,"release_supported":false}
        })
    );
}

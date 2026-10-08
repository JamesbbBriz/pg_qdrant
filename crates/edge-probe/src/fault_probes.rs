//! Standalone, opt-in fault experiments. Never called by the PostgreSQL probe.
//! Writes are confined to owned temporary directories. ENOSPC additionally
//! requires the exact, empty, dedicated small tmpfs described in the README.

use std::fs;
use std::path::{Path, PathBuf};

use qdrant_edge::{
    Condition, CountRequest, EdgeShard, FieldCondition, Filter, Match, MatchPhrase, NamedQuery,
    PointId, QueryEnum, QueryRequest, RetrieveRequest, ScoringQuery, SearchParams, ValueVariants,
    Vector, WithPayloadInterface, WithVector,
};
use serde_json::{Value, json};

const MAX_FAULT_BYTES: u64 = 64 * 1024 * 1024;
// The fixed full-text loader eagerly populates more than the small fixture's
// budget. Its separate, explicit profile has a larger, still bounded mount.
const FULL_TEXT_FAULT_BYTES: u64 = 384 * 1024 * 1024;
const REFUSED_FULL_TEXT_BYTES: u64 = 128 * 1024 * 1024;
const FULL_TEXT_CAPACITY_REFUSAL: &str = "full-text tmpfs capacity must be exactly 384 MiB";

#[derive(Clone, Copy)]
enum TmpfsBudget {
    VectorKeyword,
    FullText,
    Refusal128,
}

impl TmpfsBudget {
    fn validate_capacity(self, bytes: u64) -> Result<(), &'static str> {
        match self {
            Self::VectorKeyword if bytes > 0 && bytes <= MAX_FAULT_BYTES => Ok(()),
            Self::FullText if bytes == FULL_TEXT_FAULT_BYTES => Ok(()),
            Self::Refusal128 if bytes == REFUSED_FULL_TEXT_BYTES => Ok(()),
            Self::VectorKeyword => {
                Err("tmpfs capacity exceeds the selected fixed experiment budget")
            }
            Self::FullText => Err(FULL_TEXT_CAPACITY_REFUSAL),
            Self::Refusal128 => Err("capacity-refusal characterization requires exactly 128 MiB"),
        }
    }
}
const MAX_COPY_LOGICAL_BYTES: u64 = 512 * 1024 * 1024;
const TMPFS_ROOT: &str = "/pgq-p0-faults";
const TMPFS_ENV: &str = "PG_QDRANT_ENOSPC_DIR";

fn require(condition: bool, message: &str) -> Result<(), String> {
    condition.then_some(()).ok_or_else(|| message.to_owned())
}

fn context<T>(result: Result<T, impl std::fmt::Display>, label: &str) -> Result<T, String> {
    result.map_err(|error| format!("{label}: {error}"))
}

fn report(kind: &str, status: &str) -> Value {
    json!({
        "schema_version":1,"engine_version":"0.8.0","kind":kind,"status":status,
        "sql_integration_verified":false,"release_supported":false,
        "oom_verified":false,"power_loss_verified":false
    })
}

pub fn failure(kind: &str, error: String) -> Value {
    let mut result = report(kind, "failed");
    result["error"] = error.into();
    result
}

fn scrub(message: String, root: &Path) -> String {
    message.replace(&root.to_string_lossy().to_string(), "<owned-probe>")
}

fn progress(stage: &str) {
    eprintln!("pg_qdrant_p0_stage={stage}");
}

/// Common disk-fixture assertions do not claim that a text index exists.
fn records_and_vector_observation(shard: &EdgeShard) -> Result<Vec<qdrant_edge::Record>, String> {
    require(
        context(shard.count(CountRequest::new()), "count")? == 8,
        "fixture count changed",
    )?;
    let mut retrieve = RetrieveRequest::new((1..=8).map(PointId::NumId).collect());
    retrieve.with_payload = Some(WithPayloadInterface::Bool(true));
    retrieve.with_vector = Some(WithVector::Bool(true));
    let mut records = context(shard.retrieve(retrieve), "retrieve source representations")?;
    records.sort_by_key(|record| record.id);
    require(records.len() == 8, "missing fixture source records")?;
    for (record, id) in records.iter().zip(1..=8) {
        require(
            record.id == PointId::NumId(id),
            "fixture source identity changed",
        )?;
        let Some(qdrant_edge::VectorStructInternal::Named(vectors)) = &record.vector else {
            return Err("fixture named representations missing".into());
        };
        require(
            vectors.len() == 4
                && ["dense", "bm25", "learned", "tokens"]
                    .iter()
                    .all(|name| vectors.contains_key(*name)),
            "fixture must contain all four named representations",
        )?;
        let payload = record.payload.as_ref().ok_or("fixture payload missing")?;
        require(payload.0["revision"] == 1, "fixture revision changed")?;
    }

    let tenant = Filter::new_must(Condition::Field(FieldCondition::new_match(
        "tenant".parse().map_err(|()| "fixture tenant path")?,
        Match::new_value(ValueVariants::String("a".into())),
    )));
    let mut keyword = QueryRequest::new(16);
    keyword.filter = Some(tenant.merge(&Filter::new_must(Condition::Field(
        FieldCondition::new_match(
            "sku".parse().map_err(|()| "fixture SKU path")?,
            Match::new_prefix("PG-"),
        ),
    ))));
    let keyword = context(shard.query(keyword), "keyword prefix and tenant filter")?;
    let actual_ids: std::collections::BTreeSet<_> = keyword.iter().map(|point| point.id).collect();
    require(
        actual_ids == [PointId::NumId(1), PointId::NumId(2)].into_iter().collect(),
        "keyword prefix or tenant filter changed",
    )?;

    let mut maxsim = QueryRequest::new(3);
    maxsim.filter = Some(tenant);
    maxsim.params = Some(SearchParams {
        exact: true,
        indexed_only: false,
        ..Default::default()
    });
    maxsim.query = Some(ScoringQuery::Vector(QueryEnum::Nearest(NamedQuery {
        using: Some("tokens".into()),
        query: context(
            Vector::new_multi(vec![vec![1.0, 0.0], vec![0.0, 1.0]]),
            "token query",
        )?
        .into(),
    })));
    let maxsim = context(shard.query(maxsim), "reopened MaxSim query")?;
    require(maxsim.len() == 3, "MaxSim fill changed")?;
    for (hit, (id, score)) in maxsim.iter().zip([(1, 2.0), (2, 1.6), (3, 0.8)]) {
        require(
            hit.id == PointId::NumId(id) && (hit.score - score).abs() < 1e-5,
            "MaxSim result changed",
        )?;
    }
    Ok(records)
}

/// The complete corruption fixture also requires a real phrase index.
fn fixture_observation(shard: &EdgeShard) -> Result<Vec<qdrant_edge::Record>, String> {
    let records = records_and_vector_observation(shard)?;
    let tenant = Filter::new_must(Condition::Field(FieldCondition::new_match(
        "tenant".parse().map_err(|()| "fixture tenant path")?,
        Match::new_value(ValueVariants::String("a".into())),
    )));
    let mut phrase = QueryRequest::new(16);
    phrase.filter = Some(tenant.merge(&Filter::new_must(Condition::Field(
        FieldCondition::new_match(
            "body".parse().map_err(|()| "fixture body path")?,
            Match::Phrase(MatchPhrase {
                phrase: "transaction recovery".into(),
            }),
        ),
    ))));
    let phrase = context(shard.query(phrase), "reopened phrase query")?;
    require(
        phrase.len() == 1 && phrase[0].id == PointId::NumId(1),
        "phrase/source filter changed",
    )?;
    let mut prefix = QueryRequest::new(16);
    prefix.filter = Some(
        tenant.merge(&Filter::new_must(Condition::Field(
            FieldCondition::new_match(
                "body_prefix"
                    .parse()
                    .map_err(|()| "fixture token-prefix path")?,
                Match::new_text("trans"),
            ),
        ))),
    );
    let prefix = context(shard.query(prefix), "reopened token-prefix query")?;
    let ids: std::collections::BTreeSet<_> = prefix.iter().map(|point| point.id).collect();
    require(
        ids == [PointId::NumId(1), PointId::NumId(2)].into_iter().collect(),
        "token-prefix/source filter changed",
    )?;
    Ok(records)
}

/// Safe preflight of the disk-specific fixture on an ordinary owned directory.
/// This does not fill a filesystem or provide positive ENOSPC evidence.
pub fn disk_fixture_probe(full_text: bool, provisioned_tmpfs: bool) -> Result<Value, String> {
    #[cfg(target_os = "linux")]
    let guard = if provisioned_tmpfs {
        require(
            full_text,
            "only the full-text clean control uses a provisioned tmpfs",
        )?;
        let path =
            std::env::var_os(TMPFS_ENV).ok_or("clean tmpfs control requires its explicit mount")?;
        Some(validate_tmpfs(Path::new(&path), TmpfsBudget::FullText)?)
    } else {
        None
    };
    #[cfg(not(target_os = "linux"))]
    require(!provisioned_tmpfs, "clean tmpfs control requires Linux")?;
    let mut builder = tempfile::Builder::new();
    builder.prefix("pgq-disk-fixture-");
    #[cfg(target_os = "linux")]
    let owned = if let Some(guard) = &guard {
        builder.tempdir_in(&guard.root)
    } else {
        builder.tempdir()
    };
    #[cfg(not(target_os = "linux"))]
    let owned = builder.tempdir();
    let root = context(owned, "owned disk-fixture directory")?;
    let outcome = (|| {
        progress("clean_fixture_create");
        let shard = if full_text {
            pg_qdrant_edge_probe::bounded_persistence_fixture(root.path())?
        } else {
            pg_qdrant_edge_probe::disk_persistence_fixture(root.path())?
        };
        let observe = if full_text { fixture_observation } else { records_and_vector_observation };
        let expected = observe(&shard)?;
        context(shard.flush(), "disk-fixture explicit flush")?;
        let created_footprint = storage_footprint(root.path())?;
        let mut observations = vec![disk_observation("clean_fixture_flushed", root.path())?];
        drop(shard);
        observations.push(disk_observation("clean_before_reopen", root.path())?);
        progress("recovery_reopen");
        let reopened = context(EdgeShard::load(root.path(), None), "clean disk-fixture reopen")?;
        observations.push(disk_observation("clean_after_reopen", root.path())?);
        require(
            observe(&reopened)? == expected,
            "clean disk-fixture reopen changed source representations",
        )?;
        let reopened_footprint = storage_footprint(root.path())?;
        drop(reopened);
        let mut result = report("edge_disk_fixture_probe", "passed");
        result["profile"] = if full_text { "full_text" } else { "vector_keyword" }.into();
        result["fixture"] = if full_text { full_text_fixture_description() } else { disk_fixture_description() };
        let text_result = if full_text { "passed" } else { "not_tested" };
        result["recovery"] = json!({"explicit_flush":"passed","reopen":"passed","records":8,"all_representation_records_equal":true,"keyword_tenant_and_maxsim_queries":"passed","phrase_query":text_result,"token_prefix_query":text_result});
        result["storage_observations"] = json!({"created_and_flushed":created_footprint,"reopened":reopened_footprint});
        result["observations"] = observations.into();
        result["provisioned_tmpfs"] = provisioned_tmpfs.into();
        result["enospc_verified"] = false.into();
        result["filler_created"] = false.into();
        result["scope"] = "clean fixture reopen and storage observations only; no disk filling or recovery from ENOSPC".into();
        Ok(result)
    })().map_err(|error| scrub(error, root.path()));
    context(root.close(), "clean up owned disk fixture")?;
    outcome
}

fn full_text_fixture_description() -> Value {
    json!({"points":8,"named_representations":["dense","bm25","learned","tokens"],
        "wal_segment_capacity_bytes":65_536,"keyword_indexes":["tenant","document","sku"],
        "text_indexes":["body","body_prefix"],
        "storage_scope":"unchanged bounded full fixture with mutable phrase and token-prefix indexes"})
}

fn disk_fixture_description() -> Value {
    json!({
        "points":8,"named_representations":["dense","bm25","learned","tokens"],
        "wal_segment_capacity_bytes":65_536,"payload_on_disk":true,"dense_and_token_on_disk":true,
        "keyword_indexes":["tenant","document","sku"],"text_indexes":[],
        "storage_scope":"cold payload/vectors and mutable keyword indexes; no text indexes",
        "excluded_combination":"ENOSPC with writable reopen of full mutable phrase/token-prefix indexes"
    })
}

#[cfg(target_os = "linux")]
fn storage_footprint(root: &Path) -> Result<Value, String> {
    use std::os::unix::fs::MetadataExt;

    fn walk(path: &Path, depth: u8, totals: &mut [u64; 3]) -> Result<(), String> {
        require(depth <= 16, "footprint directory depth exceeded")?;
        for entry in context(fs::read_dir(path), "inspect owned footprint directory")? {
            let entry = context(entry, "inspect owned footprint entry")?;
            let metadata = context(
                fs::symlink_metadata(entry.path()),
                "inspect footprint metadata",
            )?;
            if metadata.is_dir() {
                walk(&entry.path(), depth + 1, totals)?;
            } else {
                require(metadata.is_file(), "footprint rejects nonregular files")?;
                totals[0] += 1;
                totals[1] = totals[1]
                    .checked_add(metadata.len())
                    .ok_or("footprint size overflow")?;
                totals[2] = totals[2]
                    .checked_add(
                        metadata
                            .blocks()
                            .checked_mul(512)
                            .ok_or("footprint allocation overflow")?,
                    )
                    .ok_or("footprint allocation overflow")?;
                require(
                    totals[0] <= 512 && totals[1] <= MAX_COPY_LOGICAL_BYTES,
                    "footprint scan budget exceeded",
                )?;
            }
        }
        Ok(())
    }

    let root = context(fs::canonicalize(root), "canonicalize owned footprint path")?;
    let mut totals = [0_u64; 3];
    walk(&root, 0, &mut totals)?;
    let maps = context(
        fs::read_to_string("/proc/self/smaps"),
        "read fixture mapping footprint",
    )?;
    let owned_prefix = format!("{}/", root.display());
    let mut owned = false;
    let mut rss_kib = 0_u64;
    for line in maps.lines() {
        let first = line.split_whitespace().next().unwrap_or("");
        if first.split_once('-').is_some_and(|(start, end)| {
            u64::from_str_radix(start, 16).is_ok() && u64::from_str_radix(end, 16).is_ok()
        }) {
            owned = line.contains(&owned_prefix);
        } else if owned && let Some(value) = line.strip_prefix("Rss:") {
            let value = value
                .split_whitespace()
                .next()
                .ok_or("missing mapping RSS")?;
            rss_kib = rss_kib
                .checked_add(context(value.parse::<u64>(), "parse mapping RSS")?)
                .ok_or("mapping RSS overflow")?;
        }
    }
    Ok(json!({
        "files":totals[0],"logical_bytes":totals[1],"allocated_bytes":totals[2],
        "mapping_rss_kib":rss_kib,
        "scope":"owned shard mappings only; not process RSS or a tmpfs capacity proof"
    }))
}

#[cfg(not(target_os = "linux"))]
fn storage_footprint(_root: &Path) -> Result<Value, String> {
    Ok(json!({"status":"not_available","reason":"Linux /proc mapping observation only"}))
}

/// Emitted before risky native calls so a parent retains partial observations
/// after a signal. This reads owned metadata; it neither touches backing pages
/// nor changes capacity. Logical bytes, allocated bytes and RSS stay distinct.
fn disk_observation(stage: &str, root: &Path) -> Result<Value, String> {
    let mut observation = json!({"stage":stage,"storage":storage_footprint(root)?});
    #[cfg(target_os = "linux")]
    {
        let output = context(
            std::process::Command::new("/usr/bin/stat")
                .args(["--file-system", "--format=%T %S %b %f %a", "--"])
                .arg(root)
                .env("LC_ALL", "C")
                .output(),
            "observe filesystem blocks",
        )?;
        require(output.status.success(), "filesystem observation failed")?;
        let output = context(String::from_utf8(output.stdout), "decode filesystem blocks")?;
        let fields: Vec<_> = output.split_whitespace().collect();
        require(fields.len() == 5, "invalid filesystem observation")?;
        let unit = context(fields[1].parse::<u64>(), "parse filesystem block unit")?;
        let bytes = |field: &str| -> Result<u64, String> {
            context(field.parse::<u64>(), "parse filesystem block count")?
                .checked_mul(unit)
                .ok_or_else(|| "filesystem block size overflow".into())
        };
        observation["filesystem"] = json!({
            "type":fields[0],"capacity_bytes":bytes(fields[2])?,
            "free_bytes":bytes(fields[3])?,"available_bytes":bytes(fields[4])?
        });
    }
    eprintln!("pg_qdrant_p0_observation={observation}");
    Ok(observation)
}

fn copy_owned_tree(source: &Path, destination: &Path) -> Result<(), String> {
    use std::io::{Read, Seek, SeekFrom, Write};

    fn walk(
        source: &Path,
        destination: &Path,
        logical_bytes: &mut u64,
        written_bytes: &mut u64,
        files: &mut u32,
        depth: u8,
    ) -> Result<(), String> {
        require(depth <= 16, "fixture copy depth exceeded")?;
        context(
            fs::create_dir(destination),
            "create copied fixture directory",
        )?;
        for entry in context(fs::read_dir(source), "read fixture directory")? {
            let entry = context(entry, "read fixture entry")?;
            let metadata = context(fs::symlink_metadata(entry.path()), "inspect fixture entry")?;
            let target = destination.join(entry.file_name());
            if metadata.is_dir() {
                walk(
                    &entry.path(),
                    &target,
                    logical_bytes,
                    written_bytes,
                    files,
                    depth + 1,
                )?;
            } else {
                require(
                    metadata.is_file() && !metadata.file_type().is_symlink(),
                    "fixture copy rejects symlinks and special files",
                )?;
                *logical_bytes = logical_bytes
                    .checked_add(metadata.len())
                    .ok_or("fixture byte count overflow")?;
                *files += 1;
                if *logical_bytes > MAX_COPY_LOGICAL_BYTES || *files > 512 {
                    return Err(format!(
                        "fixture copy budget exceeded: {logical_bytes} logical bytes, {files} files, entry {}",
                        entry.file_name().to_string_lossy()
                    ));
                }
                // Edge's sparse storage pages are much larger than this tiny
                // fixture. Preserve zero extents instead of materializing them
                // on the host filesystem. Both scanning and writes are bounded.
                let mut input = context(fs::File::open(entry.path()), "read fixture file")?;
                let mut output = context(
                    fs::OpenOptions::new()
                        .write(true)
                        .create_new(true)
                        .open(target),
                    "create copied fixture file",
                )?;
                let mut buffer = [0_u8; 65_536];
                let zeros = [0_u8; 65_536];
                let mut read_bytes = 0_u64;
                loop {
                    let count = context(input.read(&mut buffer), "read sparse fixture extent")?;
                    if count == 0 {
                        break;
                    }
                    read_bytes += count as u64;
                    require(read_bytes <= metadata.len(), "fixture changed during copy")?;
                    if buffer[..count] == zeros[..count] {
                        context(
                            output.seek(SeekFrom::Current(count as i64)),
                            "preserve zero fixture extent",
                        )?;
                    } else {
                        *written_bytes = written_bytes
                            .checked_add(count as u64)
                            .ok_or("fixture write count overflow")?;
                        require(
                            *written_bytes <= MAX_FAULT_BYTES,
                            "fixture copy content-write budget exceeded",
                        )?;
                        context(output.write_all(&buffer[..count]), "copy fixture extent")?;
                    }
                }
                require(read_bytes == metadata.len(), "fixture changed during copy")?;
                context(
                    output.set_len(metadata.len()),
                    "preserve trailing fixture hole",
                )?;
            }
        }
        Ok(())
    }
    walk(source, destination, &mut 0, &mut 0, &mut 0, 0)
}

fn malformed_copy(copy: &Path, relative_file: &Path) -> Result<String, String> {
    use std::io::Write;
    let file_path = copy.join(relative_file);
    let original = context(fs::read(&file_path), "read file selected for corruption")?;
    context(
        serde_json::from_slice::<Value>(&original),
        "expected valid JSON before corruption",
    )?;
    let mut file = context(
        fs::OpenOptions::new()
            .write(true)
            .truncate(true)
            .open(&file_path),
        "open owned corruption target",
    )?;
    context(
        file.write_all(b"{\"intentionally_truncated\":"),
        "write malformed owned metadata",
    )?;
    context(file.sync_all(), "persist malformed metadata")?;
    drop(file);
    match EdgeShard::load(copy, None) {
        Err(error) => Ok(scrub(error.to_string(), copy)),
        Ok(shard) => {
            drop(shard);
            Err("corrupted metadata unexpectedly reopened; it must not be marked ready".into())
        }
    }
}

pub fn corruption_probe() -> Result<Value, String> {
    let root = context(
        tempfile::Builder::new().prefix("pgq-corruption-").tempdir(),
        "owned corruption directory",
    )?;
    let outcome = (|| {
        let baseline_path = root.path().join("baseline");
        context(fs::create_dir(&baseline_path), "create owned baseline directory")?;
        let shard = pg_qdrant_edge_probe::bounded_persistence_fixture(&baseline_path)?;
        let expected = fixture_observation(&shard)?;
        context(shard.flush(), "baseline explicit flush")?;
        drop(shard);

        let config_copy = root.path().join("bad-config");
        copy_owned_tree(&baseline_path, &config_copy)?;
        let config_error = malformed_copy(&config_copy, Path::new("edge_config.json"))?;

        let segment_copy = root.path().join("bad-segment");
        copy_owned_tree(&baseline_path, &segment_copy)?;
        let mut segments = Vec::new();
        for entry in context(fs::read_dir(segment_copy.join("segments")), "find owned segment")? {
            let entry = context(entry, "read owned segment entry")?;
            if entry.path().join("segment.json").is_file() {
                segments.push(PathBuf::from("segments").join(entry.file_name()).join("segment.json"));
            }
        }
        require(segments.len() == 1, "expected exactly one fixture segment metadata file")?;
        let segment_error = malformed_copy(&segment_copy, &segments[0])?;

        // Recovery is a fresh copy of the known-good, explicitly flushed image;
        // it is not an attempted in-place repair of a damaged generation.
        let recovered_path = root.path().join("recovered");
        copy_owned_tree(&baseline_path, &recovered_path)?;
        let recovered = context(EdgeShard::load(&recovered_path, None), "reopen intact recovery copy")?;
        require(fixture_observation(&recovered)? == expected, "recovered source representations differ")?;
        context(recovered.flush(), "recovered flush")?;
        drop(recovered);
        let baseline = context(EdgeShard::load(&baseline_path, None), "reopen unaffected baseline")?;
        require(fixture_observation(&baseline)? == expected, "original fixture changed")?;
        drop(baseline);

        let mut result = report("edge_corruption_probe", "passed");
        result["cases"] = json!([
            {"target":"edge_config.json","load":"error","ready":false,"error":config_error},
            {"target":"segments/<owned-segment>/segment.json","load":"error","ready":false,"error":segment_error}
        ]);
        result["recovery"] = json!({"source":"separate explicitly flushed image","records":8,"all_representation_records_equal":true,"phrase_and_maxsim_queries":"passed","original_unchanged":true});
        result["scope"] = "malformed JSON metadata only; no arbitrary bit-rot detection or in-place repair claim".into();
        Ok(result)
    })().map_err(|error| scrub(error, root.path()));
    context(root.close(), "clean up owned corruption fixtures")?;
    outcome
}

#[cfg(target_os = "linux")]
struct TmpfsGuard {
    root: PathBuf,
    capacity_bytes: u64,
    device: u64,
    inode: u64,
}

#[cfg(target_os = "linux")]
fn validate_tmpfs(input: &Path, budget: TmpfsBudget) -> Result<TmpfsGuard, String> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    require(
        input == Path::new(TMPFS_ROOT),
        "only the dedicated /pgq-p0-faults mount is allowed",
    )?;
    let root = context(fs::canonicalize(input), "canonicalize dedicated tmpfs")?;
    require(
        root == input,
        "tmpfs path must be canonical and must not be a symlink",
    )?;
    let metadata = context(fs::metadata(&root), "inspect dedicated tmpfs")?;
    require(metadata.is_dir(), "tmpfs root must be a directory")?;
    require(
        metadata.permissions().mode() & 0o7777 == 0o700,
        "tmpfs root must have mode 0700",
    )?;
    let status = context(
        fs::read_to_string("/proc/self/status"),
        "read effective UID",
    )?;
    let uid = status
        .lines()
        .find_map(|line| line.strip_prefix("Uid:"))
        .and_then(|line| line.split_whitespace().nth(1))
        .ok_or("effective UID unavailable")?;
    let uid = context(uid.parse::<u32>(), "parse effective UID")?;
    require(
        metadata.uid() == uid,
        "tmpfs root must be owned by the effective UID",
    )?;
    require(
        context(fs::read_dir(&root), "inspect empty dedicated mount")?
            .next()
            .is_none(),
        "dedicated tmpfs must initially be empty",
    )?;

    // Kernel mountinfo establishes mount identity in the current namespace;
    // requiring its exact root and a unique device rejects ordinary directories,
    // visible bind aliases and shared /dev/shm.
    let mounts = context(
        fs::read_to_string("/proc/self/mountinfo"),
        "read kernel mount table",
    )?;
    let parsed: Vec<Vec<&str>> = mounts
        .lines()
        .map(|line| line.split_whitespace().collect())
        .collect();
    let matches: Vec<_> = parsed
        .iter()
        .filter(|fields| fields.get(4) == Some(&TMPFS_ROOT))
        .collect();
    require(
        matches.len() == 1,
        "dedicated tmpfs must be exactly one mount root",
    )?;
    let mount = matches[0];
    let separator = mount
        .iter()
        .position(|field| *field == "-")
        .ok_or("invalid mountinfo separator")?;
    require(
        mount.get(3) == Some(&"/") && mount.get(separator + 1) == Some(&"tmpfs"),
        "dedicated mount must be a tmpfs filesystem root",
    )?;
    require(
        mount
            .get(5)
            .is_some_and(|options| options.split(',').any(|option| option == "rw")),
        "tmpfs mount must be writable",
    )?;
    let mount_device = mount.get(2).ok_or("mount device unavailable")?;
    require(
        parsed
            .iter()
            .filter(|fields| fields.get(2) == Some(mount_device))
            .count()
            == 1,
        "tmpfs device must have no other mount aliases in the current namespace",
    )?;

    for ancestor in root.ancestors() {
        require(
            !ancestor.join("PG_VERSION").exists() && !ancestor.join("postmaster.pid").exists(),
            "tmpfs intersects a PostgreSQL data directory",
        )?;
    }
    for name in ["PGDATA", "PG_QDRANT_PGDATA"] {
        if let Some(value) = std::env::var_os(name).filter(|value| !value.is_empty()) {
            let data = context(
                fs::canonicalize(value),
                "validate configured PostgreSQL data path",
            )?;
            require(
                !root.starts_with(&data) && !data.starts_with(&root),
                "tmpfs intersects configured PostgreSQL data",
            )?;
            require(
                context(fs::metadata(data), "inspect configured PostgreSQL data")?.dev()
                    != metadata.dev(),
                "tmpfs shares PostgreSQL data filesystem",
            )?;
        }
    }

    // GNU stat is a read-only statfs frontend. A missing/unparseable tool refuses
    // the experiment; no guessed filesystem size or fallback to host disk.
    let output = context(
        std::process::Command::new("/usr/bin/stat")
            .args(["--file-system", "--format=%T %S %b", "--"])
            .arg(&root)
            .env("LC_ALL", "C")
            .output(),
        "read tmpfs capacity",
    )?;
    require(output.status.success(), "statfs capacity query failed")?;
    let output = context(String::from_utf8(output.stdout), "decode statfs capacity")?;
    let fields: Vec<_> = output.split_whitespace().collect();
    require(
        fields.len() == 3 && fields[0] == "tmpfs",
        "statfs must confirm tmpfs",
    )?;
    let block_size = context(fields[1].parse::<u64>(), "statfs block size")?;
    let blocks = context(fields[2].parse::<u64>(), "statfs block count")?;
    let capacity_bytes = block_size
        .checked_mul(blocks)
        .ok_or("tmpfs capacity overflow")?;
    budget.validate_capacity(capacity_bytes)?;
    Ok(TmpfsGuard {
        root,
        capacity_bytes,
        device: metadata.dev(),
        inode: metadata.ino(),
    })
}

#[cfg(target_os = "linux")]
fn run_disk_full(guard: &TmpfsGuard, full_text: bool) -> Result<Value, String> {
    use std::io::Write;
    use std::os::unix::fs::MetadataExt;
    let metadata = context(fs::metadata(&guard.root), "recheck dedicated tmpfs")?;
    require(
        metadata.dev() == guard.device && metadata.ino() == guard.inode,
        "tmpfs identity changed before experiment",
    )?;
    let root = context(
        tempfile::Builder::new()
            .prefix("edge-enospc-")
            .tempdir_in(&guard.root),
        "owned tmpfs experiment directory",
    )?;
    let outcome = (|| {
        let shard_path = root.path().join("baseline");
        context(fs::create_dir(&shard_path), "create owned tmpfs shard directory")?;
        let shard = if full_text {
            progress("full_text_fixture");
            pg_qdrant_edge_probe::bounded_persistence_fixture(&shard_path)?
        } else {
            pg_qdrant_edge_probe::disk_persistence_fixture(&shard_path)?
        };
        progress("fixture_query");
        let observe = if full_text { fixture_observation } else { records_and_vector_observation };
        let expected = observe(&shard)?;
        let mut observations = vec![disk_observation("fixture_query_passed", &shard_path)?];
        let config_path = shard_path.join("edge_config.json");
        let before_bytes = context(fs::read(&config_path), "read persisted baseline config")?;
        let before_memory = shard.config().clone();
        let mut target_config = before_memory.hnsw_config();
        target_config.m = 8;

        // The filler is declared after the shard, so error unwinding frees it
        // before the shard's Drop can attempt its final flush.
        progress("fill");
        let mut filler = context(tempfile::NamedTempFile::new_in(root.path()), "owned tmpfs filler")?;
        require(
            context(filler.as_file().metadata(), "inspect owned filler filesystem")?.dev() == guard.device,
            "filler is not on the validated tmpfs device",
        )?;
        let chunk = [0x5a_u8; 65_536];
        let mut bytes_written = 0_u64;
        let started = std::time::Instant::now();
        loop {
            require(started.elapsed() < std::time::Duration::from_secs(15), "tmpfs fill exceeded 15 seconds")?;
            let remaining = guard.capacity_bytes.saturating_sub(bytes_written);
            require(remaining > 0, "tmpfs byte budget exhausted without ENOSPC")?;
            let length = remaining.min(chunk.len() as u64) as usize;
            // NamedTempFile's Write wrapper replaces the outer io::Error with
            // path context. The owned File preserves raw_os_error for this gate.
            match filler.as_file_mut().write(&chunk[..length]) {
                Ok(0) => return Err("tmpfs filler made no progress".into()),
                Ok(count) => bytes_written += count as u64,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) if error.raw_os_error() == Some(28) => break,
                Err(error) => return Err(format!("tmpfs filler returned a non-ENOSPC error: {error}")),
            }
        }

        observations.push(disk_observation("filled", &shard_path)?);

        progress("config_save");
        let edge_result = shard.set_hnsw_config(target_config);
        progress("config_result");
        let after_failure_memory = shard.config().clone();
        let after_failure_bytes = context(fs::read(&config_path), "read config after failed write")?;
        // Free only our own filler before retrying, reopening, or returning any
        // failed assertion. No mount or foreign file is removed.
        progress("filler_release");
        context(filler.close(), "release owned tmpfs filler")?;
        observations.push(disk_observation("filler_removed", &shard_path)?);
        let edge_error = match edge_result {
            Err(error) => error,
            Ok(()) => return Err("Edge configuration save unexpectedly succeeded on full tmpfs".into()),
        };
        let message = edge_error.to_string();
        require(message.contains("os error 28"), "Edge error did not identify ENOSPC")?;
        require(after_failure_bytes == before_bytes, "failed atomic config save changed persisted bytes")?;
        let memory_changed = after_failure_memory != before_memory;

        progress("recovery");
        context(shard.set_hnsw_config(target_config), "retry Edge config after releasing space")?;
        context(shard.flush(), "successful explicit flush after retry")?;
        observations.push(disk_observation("recovery_flushed", &shard_path)?);
        drop(shard);
        observations.push(disk_observation("recovery_before_reopen", &shard_path)?);
        progress("recovery_reopen");
        let reopened = context(EdgeShard::load(&shard_path, None), "reopen after ENOSPC recovery")?;
        observations.push(disk_observation("recovery_after_reopen", &shard_path)?);
        require(reopened.config().hnsw_config == Some(target_config), "retried config not persisted")?;
        require(observe(&reopened)? == expected, "ENOSPC recovery changed flushed source representations")?;
        context(reopened.flush(), "final recovered flush")?;
        drop(reopened);

        let mut result = report("edge_enospc_probe", "passed");
        result["environment"] = json!({"filesystem":"tmpfs","capacity_bytes":guard.capacity_bytes,"no_mount_aliases_in_current_namespace":true,"mode":"0700","pgdata_disjoint":true});
        result["profile"] = if full_text { "full_text" } else { "vector_keyword" }.into();
        result["fixture"] = if full_text {
            full_text_fixture_description()
        } else { disk_fixture_description() };
        result["observations"] = observations.into();
        result["fill"] = json!({"bytes_written":bytes_written,"errno":28,"filler_removed":true});
        result["engine_failure"] = json!({"operation":"set_hnsw_config","error":scrub(message, root.path()),"persisted_config_unchanged":true,"in_memory_config_changed":memory_changed});
        let text_result = if full_text { "passed" } else { "not_tested" };
        result["recovery"] = json!({"config_retry":"passed","explicit_flush":"passed","reopen":"passed","records":8,"all_representation_records_equal":true,"keyword_tenant_and_maxsim_queries":"passed","phrase_query":text_result,"token_prefix_query":text_result});
        result["scope"] = if full_text {
            "configuration atomic save and recovery with an already-flushed full mutable text/vector fixture; WAL growth, dirty ingestion, PostgreSQL ACK and machine power loss are unverified"
        } else {
            "configuration atomic save with vector/keyword fixture only; mutable text-index recovery, WAL growth, dirty ingestion, PostgreSQL ACK and machine power loss are unverified"
        }.into();
        Ok(result)
    })().map_err(|error| scrub(error, root.path()));
    context(root.close(), "clean up owned tmpfs experiment")?;
    require(
        context(fs::read_dir(&guard.root), "verify tmpfs cleanup")?
            .next()
            .is_none(),
        "dedicated tmpfs is not empty after cleanup",
    )?;
    if outcome.is_ok() {
        progress("completed");
    }
    outcome
}

pub fn disk_full_probe(full_text: bool) -> (Value, i32) {
    progress("guard");
    let mut skipped = report("edge_enospc_probe", "not_run");
    skipped["profile"] = if full_text {
        "full_text"
    } else {
        "vector_keyword"
    }
    .into();
    skipped["disk_writes_attempted"] = false.into();
    let Some(path) = std::env::var_os(TMPFS_ENV) else {
        skipped["reason"] = format!("{TMPFS_ENV} is not set; no disk writes attempted").into();
        return (skipped, 0);
    };
    #[cfg(not(target_os = "linux"))]
    {
        let _ = path;
        skipped["reason"] = "dedicated tmpfs experiment requires Linux".into();
        (skipped, 2)
    }
    #[cfg(target_os = "linux")]
    {
        let budget = if full_text {
            TmpfsBudget::FullText
        } else {
            TmpfsBudget::VectorKeyword
        };
        let guard = match validate_tmpfs(Path::new(&path), budget) {
            Ok(guard) => guard,
            Err(reason) => {
                skipped["reason_code"] = "unsafe_environment".into();
                skipped["reason"] = reason.into();
                return (skipped, 2);
            }
        };
        match run_disk_full(&guard, full_text) {
            Ok(result) => (result, 0),
            Err(error) => {
                let mut result = failure("edge_enospc_probe", error);
                result["profile"] = if full_text {
                    "full_text"
                } else {
                    "vector_keyword"
                }
                .into();
                (result, 1)
            }
        }
    }
}

/// Dedicated, non-writing characterization of the old 128 MiB budget. This
/// cannot count a native crash or an arbitrary guard rejection as a pass.
pub fn full_text_128_capacity_refusal_probe() -> (Value, i32) {
    let kind = "edge_full_text_128_capacity_refusal_probe";
    #[cfg(not(target_os = "linux"))]
    {
        (
            failure(
                kind,
                "capacity-refusal characterization requires Linux".into(),
            ),
            1,
        )
    }
    #[cfg(target_os = "linux")]
    {
        let outcome = (|| {
            progress("capacity_refusal_guard");
            let path = std::env::var_os(TMPFS_ENV)
                .ok_or("capacity-refusal characterization requires its explicit mount")?;
            // All existing ownership, mount, empty-directory and PGDATA checks
            // still apply. Refusal128 is only admitted to this read-only probe.
            let guard = validate_tmpfs(Path::new(&path), TmpfsBudget::Refusal128)?;
            require(
                TmpfsBudget::FullText.validate_capacity(guard.capacity_bytes)
                    == Err(FULL_TEXT_CAPACITY_REFUSAL),
                "expected full-text capacity refusal was not produced",
            )?;
            let before = disk_observation("capacity_refusal_before", &guard.root)?;

            // Exercise the real entry paths, both of which must stop at the
            // shared capacity guard before tempfile or engine construction.
            let (enospc, code) = disk_full_probe(true);
            require(
                code == 2
                    && enospc["status"] == "not_run"
                    && enospc["reason_code"] == "unsafe_environment"
                    && enospc["reason"] == FULL_TEXT_CAPACITY_REFUSAL
                    && enospc["disk_writes_attempted"] == false,
                "ordinary full-text ENOSPC entry did not produce the exact capacity refusal",
            )?;
            let clean_error = match disk_fixture_probe(true, true) {
                Err(error) => error,
                Ok(_) => return Err("ordinary clean entry unexpectedly accepted 128 MiB".into()),
            };
            require(
                clean_error == FULL_TEXT_CAPACITY_REFUSAL,
                "ordinary clean entry did not produce the exact capacity refusal",
            )?;
            let after_guard = validate_tmpfs(Path::new(&path), TmpfsBudget::Refusal128)?;
            require(
                guard.device == after_guard.device && guard.inode == after_guard.inode,
                "capacity-refusal mount identity changed",
            )?;
            let after = disk_observation("capacity_refusal_after", &guard.root)?;
            require(
                before["filesystem"] == after["filesystem"]
                    && before["storage"]["files"] == 0
                    && after["storage"]["files"] == 0
                    && before["storage"]["logical_bytes"] == 0
                    && after["storage"]["logical_bytes"] == 0
                    && before["storage"]["allocated_bytes"] == 0
                    && after["storage"]["allocated_bytes"] == 0,
                "capacity-refusal observation changed the empty mount",
            )?;
            let mut result = report(kind, "passed");
            result["profile"] = "full_text".into();
            result["observed_capacity_bytes"] = REFUSED_FULL_TEXT_BYTES.into();
            result["required_capacity_bytes"] = FULL_TEXT_FAULT_BYTES.into();
            result["expected_refusal"] = FULL_TEXT_CAPACITY_REFUSAL.into();
            result["entry_refusals"] = json!({"enospc":enospc,"enospc_exit_code":code,
                "clean_error":clean_error});
            result["observations"] = json!([before, after]);
            result["disk_writes_attempted"] = false.into();
            result["engine_open_attempted"] = false.into();
            result["filler_created"] = false.into();
            result["enospc_verified"] = false.into();
            result["recovery_verified"] = false.into();
            result["scope"] = "exact 128 MiB capacity refusal by both full-text entry paths; no engine execution, ENOSPC or recovery claim".into();
            progress("capacity_refusal_completed");
            Ok(result)
        })();
        match outcome {
            Ok(result) => (result, 0),
            Err(error) => (failure(kind, error), 1),
        }
    }
}

#[cfg(test)]
mod capacity_tests {
    use super::*;

    #[test]
    fn fixed_full_text_capacity_refuses_every_other_budget() {
        for bytes in [
            0,
            1,
            MAX_FAULT_BYTES,
            REFUSED_FULL_TEXT_BYTES,
            FULL_TEXT_FAULT_BYTES - 1,
            FULL_TEXT_FAULT_BYTES + 1,
            u64::MAX,
        ] {
            assert_eq!(
                TmpfsBudget::FullText.validate_capacity(bytes),
                Err(FULL_TEXT_CAPACITY_REFUSAL)
            );
        }
        assert_eq!(
            TmpfsBudget::FullText.validate_capacity(FULL_TEXT_FAULT_BYTES),
            Ok(())
        );
        assert_eq!(
            TmpfsBudget::Refusal128.validate_capacity(REFUSED_FULL_TEXT_BYTES),
            Ok(())
        );
        assert!(
            TmpfsBudget::Refusal128
                .validate_capacity(FULL_TEXT_FAULT_BYTES)
                .is_err()
        );
        assert!(
            TmpfsBudget::Refusal128
                .validate_capacity(REFUSED_FULL_TEXT_BYTES - 1)
                .is_err()
        );
    }

    #[test]
    fn vector_keyword_capacity_retains_its_original_bound() {
        assert_eq!(TmpfsBudget::VectorKeyword.validate_capacity(1), Ok(()));
        assert_eq!(
            TmpfsBudget::VectorKeyword.validate_capacity(MAX_FAULT_BYTES),
            Ok(())
        );
        for bytes in [
            0,
            MAX_FAULT_BYTES + 1,
            REFUSED_FULL_TEXT_BYTES,
            FULL_TEXT_FAULT_BYTES,
        ] {
            assert!(TmpfsBudget::VectorKeyword.validate_capacity(bytes).is_err());
        }
    }
}

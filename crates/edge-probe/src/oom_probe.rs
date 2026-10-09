//! Private, directed-victim P0 memory experiment. Never a production memory API.
//! Positive execution requires the separately reviewed fresh-container harness.

use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const MEMORY_BYTES: u64 = 768 * 1024 * 1024;
const CHUNK_BYTES: usize = 1024 * 1024;
const FILE_BYTES: u64 = 16 * 1024;
const CGROUP: &str = "/sys/fs/cgroup";
const NONCE_ENV: &str = "PG_QDRANT_P0_OOM_RUN_ID";
type Checked<T> = Result<T, String>;

fn require(condition: bool, reason: &str) -> Checked<()> {
    if condition {
        Ok(())
    } else {
        Err(reason.into())
    }
}

pub fn valid_nonce(nonce: &str) -> bool {
    nonce.len() == 32
        && nonce
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

/// The supervisor forwards only this validated value through env_clear().
pub fn environment_nonce() -> Option<String> {
    std::env::var(NONCE_ENV)
        .ok()
        .filter(|nonce| valid_nonce(nonce))
}

fn bounded_read(path: &Path) -> Checked<String> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
        .map_err(|e| format!("read {}: {e}", path.display()))?;
    let mut bytes = Vec::new();
    file.take(FILE_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    require(
        bytes.len() as u64 <= FILE_BYTES,
        "guard file exceeds byte budget",
    )?;
    String::from_utf8(bytes).map_err(|e| e.to_string())
}

fn number(path: &Path) -> Checked<u64> {
    bounded_read(path)?
        .trim()
        .parse()
        .map_err(|_| format!("non-numeric {}", path.display()))
}

fn keyed_numbers(input: &str) -> Checked<BTreeMap<String, u64>> {
    let mut result = BTreeMap::new();
    for line in input.lines() {
        let mut fields = line.split_whitespace();
        let key = fields.next().ok_or("missing counter name")?;
        let value = fields
            .next()
            .ok_or("missing counter value")?
            .parse::<u64>()
            .map_err(|_| "invalid counter value")?;
        require(
            fields.next().is_none() && result.insert(key.into(), value).is_none(),
            "duplicate or malformed counter",
        )?;
    }
    Ok(result)
}

fn events() -> Checked<BTreeMap<String, u64>> {
    let events = keyed_numbers(&bounded_read(
        &Path::new(CGROUP).join("memory.events.local"),
    )?)?;
    for key in ["max", "oom", "oom_kill", "oom_group_kill"] {
        require(
            events.contains_key(key),
            "required local OOM counter is unavailable",
        )?;
    }
    Ok(events)
}

fn validate_allocation_headroom(profile: &str, soft: u64, virtual_bytes: u64) -> Checked<()> {
    if soft == libc::RLIM_INFINITY {
        return Ok(());
    }
    // A finite helper cap is compatible only if the current mappings leave
    // twice the complete bounded allocation available. This does not turn an
    // allocator refusal into evidence of a kernel OOM.
    require(
        profile == "managed_helper"
            && virtual_bytes
                .checked_add(2 * MEMORY_BYTES)
                .is_some_and(|end| end <= soft),
        "insufficient verified virtual allocation headroom",
    )
}

fn allocation_limits(profile: &str, page_size: usize) -> Checked<Value> {
    let mut address = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    let mut data = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    require(
        unsafe { libc::getrlimit(libc::RLIMIT_AS, &mut address) } == 0
            && unsafe { libc::getrlimit(libc::RLIMIT_DATA, &mut data) } == 0
            && data.rlim_cur == libc::RLIM_INFINITY,
        "restrictive data limit or unreadable process allocation limit",
    )?;
    let pages = bounded_read(Path::new("/proc/self/statm"))?
        .split_whitespace()
        .next()
        .ok_or("missing virtual mapping size")?
        .parse::<u64>()
        .map_err(|_| "invalid virtual mapping size")?;
    let virtual_bytes = pages
        .checked_mul(page_size as u64)
        .ok_or("virtual mapping size overflow")?;
    validate_allocation_headroom(profile, address.rlim_cur, virtual_bytes)?;
    Ok(json!({"address_space_soft_bytes": address.rlim_cur,
        "address_space_hard_bytes": address.rlim_max, "virtual_bytes": virtual_bytes,
        "required_finite_headroom_bytes": 2 * MEMORY_BYTES, "data_soft_unlimited": true}))
}

fn process(pid: u32) -> Checked<Value> {
    let root = PathBuf::from(format!("/proc/{pid}"));
    let metadata = fs::metadata(&root).map_err(|e| e.to_string())?;
    let stat = bounded_read(&root.join("stat"))?;
    let fields: Vec<_> = stat
        .rsplit_once(')')
        .ok_or("invalid process stat")?
        .1
        .split_whitespace()
        .collect();
    require(
        fields.len() >= 20 && fields[0] != "Z",
        "process is not observably alive",
    )?;
    Ok(json!({"pid": pid, "uid": metadata.uid(),
        "start_ticks": fields[19].parse::<u64>().map_err(|_| "invalid process start ticks")?,
        "parent_pid": fields[1].parse::<u32>().map_err(|_| "invalid parent pid")?,
        "cgroup": bounded_read(&root.join("cgroup"))?.trim(),
        "pid_namespace": fs::read_link(root.join("ns/pid")).map_err(|e| e.to_string())?.to_string_lossy(),
        "ipc_namespace": fs::read_link(root.join("ns/ipc")).map_err(|e| e.to_string())?.to_string_lossy(),
        "cgroup_namespace": fs::read_link(root.join("ns/cgroup")).map_err(|e| e.to_string())?.to_string_lossy()}))
}

fn same_identity(expected: &Value, uid: u32, namespaces: &Value) -> Checked<Value> {
    let pid = expected["pid"]
        .as_u64()
        .filter(|pid| *pid > 1 && *pid <= u32::MAX as u64)
        .ok_or("invalid bound process PID")? as u32;
    let current = process(pid)?;
    require(
        current["start_ticks"] == expected["start_ticks"] && current["uid"] == uid,
        "process identity or owner changed",
    )?;
    require(
        current["cgroup"] == "0::/",
        "process is outside the expected private cgroup",
    )?;
    for key in ["pid_namespace", "ipc_namespace", "cgroup_namespace"] {
        require(
            current[key] == namespaces[key],
            "participant namespaces differ",
        )?;
    }
    Ok(current)
}

fn validate_limits(values: &BTreeMap<String, String>) -> Checked<()> {
    for (name, expected) in [
        ("memory.max", "805306368"),
        ("memory.swap.max", "0"),
        ("memory.swap.current", "0"),
        ("memory.oom.group", "0"),
        ("memory.high", "max"),
        ("pids.max", "128"),
        ("cpu.max", "200000 100000"),
        ("cgroup.type", "domain"),
        ("cgroup.subtree_control", ""),
    ] {
        require(
            values.get(name).is_some_and(|v| v == expected),
            &format!("unsafe cgroup setting: {name}"),
        )?;
    }
    Ok(())
}

fn cgroup_state() -> Checked<Value> {
    let root = Path::new(CGROUP);
    let mountinfo = bounded_read(Path::new("/proc/self/mountinfo"))?;
    let mount = mountinfo
        .lines()
        .find(|line| {
            let fields: Vec<_> = line.split_whitespace().collect();
            fields.get(4) == Some(&CGROUP) && line.contains(" - cgroup2 ")
        })
        .ok_or("expected cgroup-v2 mount is absent")?;
    let fields: Vec<_> = mount.split_whitespace().collect();
    require(
        fields[5].split(',').any(|mode| mode == "ro"),
        "expected read-only cgroup mount",
    )?;
    require(
        bounded_read(Path::new("/proc/self/cgroup"))?.trim() == "0::/",
        "private cgroup namespace required",
    )?;
    let mut entries = 0;
    for entry in fs::read_dir(root).map_err(|e| e.to_string())? {
        entries += 1;
        require(entries <= 256, "cgroup directory exceeds inspection budget")?;
        require(
            !entry
                .map_err(|e| e.to_string())?
                .file_type()
                .map_err(|e| e.to_string())?
                .is_dir(),
            "only a leaf cgroup is supported",
        )?;
    }
    let mut values = BTreeMap::new();
    for key in [
        "memory.max",
        "memory.swap.max",
        "memory.swap.current",
        "memory.oom.group",
        "memory.high",
        "pids.max",
        "cpu.max",
        "cgroup.type",
        "cgroup.subtree_control",
    ] {
        values.insert(key.into(), bounded_read(&root.join(key))?.trim().to_owned());
    }
    validate_limits(&values)?;
    let metadata = fs::metadata(root).map_err(|e| e.to_string())?;
    Ok(
        json!({"device": metadata.dev(), "inode": metadata.ino(), "mount_id": fields[0],
        "settings": values, "events": events()?, "memory_current": number(&root.join("memory.current"))?}),
    )
}

fn owned_path(path: &Path, uid: u32, directory: bool) -> Checked<()> {
    let metadata = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
    require(
        !metadata.file_type().is_symlink() && metadata.uid() == uid,
        "marker path must be owned and not a symlink",
    )?;
    require(
        if directory {
            metadata.is_dir() && metadata.mode() & 0o777 == 0o700
        } else {
            metadata.is_file() && metadata.mode() & 0o777 == 0o600
        },
        "unsafe marker type or mode",
    )?;
    require(
        fs::canonicalize(path).map_err(|e| e.to_string())? == path,
        "marker path contains a symlink",
    )
}

fn owned_json(path: &Path, uid: u32) -> Checked<Value> {
    owned_path(path, uid, false)?;
    serde_json::from_str(&bounded_read(path)?).map_err(|e| e.to_string())
}

fn consume_marker(directory: &Path) -> Checked<()> {
    let source = CString::new(directory.join("arm.json").as_os_str().as_bytes())
        .map_err(|e| e.to_string())?;
    let target = CString::new(directory.join("consumed.json").as_os_str().as_bytes())
        .map_err(|e| e.to_string())?;
    // Same owned directory; RENAME_NOREPLACE makes an already consumed nonce fail.
    let result = unsafe {
        libc::renameat2(
            libc::AT_FDCWD,
            source.as_ptr(),
            libc::AT_FDCWD,
            target.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    };
    require(
        result == 0,
        &format!(
            "one-shot marker consumption failed: {}",
            std::io::Error::last_os_error()
        ),
    )
}

struct Guard {
    nonce: String,
    profile: String,
    uid: u32,
    directory: PathBuf,
    marker: Value,
    cgroup: Value,
    page_size: usize,
    log: File,
    allocation_started: bool,
    allocation_limits: Value,
}

impl Guard {
    fn acquire(profile: &str) -> Checked<Self> {
        require(
            cfg!(all(target_os = "linux", target_arch = "x86_64")),
            "Linux x86_64 required",
        )?;
        let uid = unsafe { libc::geteuid() };
        require(uid == 10001, "fresh-container non-root UID10001 required")?;
        require(
            matches!(profile, "direct_worker" | "managed_helper"),
            "invalid experiment profile",
        )?;
        let nonce = environment_nonce().ok_or("missing or invalid one-shot run nonce")?;
        let directory = PathBuf::from(format!("/tmp/pgq-p0-oom-{nonce}"));
        owned_path(&directory, uid, true)?;
        let marker = owned_json(&directory.join("arm.json"), uid)?;
        require(
            marker["schema_version"] == 1
                && marker["nonce"] == nonce
                && marker["profile"] == profile,
            "marker contract mismatch",
        )?;
        require(
            marker["postgresql_marker_verified"] == true,
            "committed PG marker not verified",
        )?;
        let cgroup = cgroup_state()?;
        for key in ["device", "inode", "mount_id"] {
            require(
                cgroup[key] == marker["cgroup"][key],
                "cgroup identity changed",
            )?;
        }
        require(
            cgroup["memory_current"]
                .as_u64()
                .is_some_and(|n| n < MEMORY_BYTES / 2),
            "baseline memory already exceeds half the cap",
        )?;
        require(
            cgroup["events"]["oom_kill"] == 0 && cgroup["events"]["oom_group_kill"] == 0,
            "fresh container already experienced an OOM kill",
        )?;
        let target = same_identity(&marker["target"], uid, &marker["namespaces"])?;
        require(
            target["pid"] == std::process::id(),
            "marker targets another process",
        )?;
        let mut participant_pids = std::collections::BTreeSet::new();
        for role in ["supervisor", "controller", "companion", "postmaster"] {
            let participant = same_identity(&marker[role], uid, &marker["namespaces"])?;
            require(
                participant_pids.insert(participant["pid"].as_u64().unwrap()),
                "experiment roles must identify distinct processes",
            )?;
            if role == "supervisor" {
                require(
                    participant["parent_pid"] == marker["postmaster"]["pid"],
                    "supervisor is not a direct child of the postmaster",
                )?;
            }
        }
        require(
            if profile == "managed_helper" {
                target["parent_pid"] == marker["supervisor"]["pid"]
            } else {
                target["pid"] == marker["supervisor"]["pid"]
            },
            "native owner topology mismatch",
        )?;
        if profile == "managed_helper" {
            require(
                !participant_pids.contains(&u64::from(std::process::id())),
                "helper must be distinct from all PG participants",
            )?;
        }
        let data = PathBuf::from(
            marker["data_dir"]
                .as_str()
                .ok_or("data directory missing")?,
        );
        require(
            data.parent().and_then(Path::parent) == Some(Path::new("/tmp"))
                && data
                    .to_string_lossy()
                    .starts_with("/tmp/pgq-p0-oom-cluster.")
                && data.ends_with("data"),
            "data directory is not the dedicated disposable cluster",
        )?;
        owned_path(&data, uid, true)?;
        require(
            bounded_read(&data.join("postmaster.pid"))?.lines().next()
                == marker["postmaster"]["pid"]
                    .as_u64()
                    .map(|v| v.to_string())
                    .as_deref(),
            "postmaster identity mismatch",
        )?;
        let page_size = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
        require(
            (4096..=65536).contains(&page_size)
                && (page_size as usize).is_power_of_two()
                && CHUNK_BYTES % page_size as usize == 0,
            "unsupported native page size",
        )?;
        let allocation_limits = allocation_limits(profile, page_size as usize)?;
        consume_marker(&directory)?;
        require(
            owned_json(&directory.join("consumed.json"), uid)? == marker,
            "marker changed during consumption",
        )?;
        let log = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(directory.join("native.jsonl"))
            .map_err(|e| e.to_string())?;
        Ok(Self {
            nonce,
            profile: profile.into(),
            uid,
            directory,
            marker,
            cgroup,
            page_size: page_size as usize,
            log,
            allocation_started: false,
            allocation_limits,
        })
    }

    fn barrier(&mut self, previous_score: i32) -> Checked<()> {
        let armed = json!({"event": "armed", "nonce": self.nonce, "profile": self.profile,
            "target": self.marker["target"], "cgroup": self.cgroup, "page_size": self.page_size,
            "oom_score_adj_before": previous_score, "oom_score_adj_during": 1000,
            "allocation_started": false, "requested_bytes_cap": MEMORY_BYTES, "loop_deadline_ms": 10000,
            "allocation_limits": self.allocation_limits});
        writeln!(self.log, "{armed}").map_err(|e| e.to_string())?;
        self.log.sync_data().map_err(|e| e.to_string())?;
        let deadline = Instant::now() + Duration::from_secs(10);
        let go_path = self.directory.join("go.json");
        while Instant::now() < deadline {
            if go_path.exists() {
                let go = owned_json(&go_path, self.uid)?;
                require(
                    go["nonce"] == self.nonce
                        && go["target"] == self.marker["target"]
                        && go["container_inspection_verified"] == true
                        && go["host_pid"].as_u64().is_some_and(|v| v > 1),
                    "outer observation barrier mismatch",
                )?;
                let current = cgroup_state()?;
                for key in ["device", "inode", "mount_id", "settings", "events"] {
                    require(
                        current[key] == self.cgroup[key],
                        "cgroup changed before allocation",
                    )?;
                }
                require(
                    current["memory_current"]
                        .as_u64()
                        .is_some_and(|n| n < MEMORY_BYTES / 2),
                    "memory baseline exceeds half the cap at allocation barrier",
                )?;
                for role in [
                    "target",
                    "supervisor",
                    "controller",
                    "companion",
                    "postmaster",
                ] {
                    same_identity(&self.marker[role], self.uid, &self.marker["namespaces"])?;
                }
                self.allocation_limits = allocation_limits(&self.profile, self.page_size)?;
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        Err("outer observation barrier timed out before allocation".into())
    }
}

struct ScoreAdjustment {
    previous: i32,
    restored: bool,
}
impl ScoreAdjustment {
    fn raise() -> Checked<Self> {
        let path = Path::new("/proc/self/oom_score_adj");
        let previous = bounded_read(path)?
            .trim()
            .parse::<i32>()
            .map_err(|_| "invalid oom_score_adj")?;
        require(
            (0..=1000).contains(&previous),
            "do not replace a protected negative OOM adjustment",
        )?;
        let mut adjustment = Self {
            previous,
            restored: false,
        };
        let changed = fs::write(path, "1000")
            .map_err(|e| format!("set target OOM adjustment: {e}"))
            .and_then(|()| {
                require(
                    bounded_read(path)?.trim() == "1000",
                    "OOM adjustment read-back mismatch",
                )
            });
        if let Err(error) = changed {
            let restored = adjustment.restore();
            return Err(format!("{error}; restoration result: {restored:?}"));
        }
        Ok(adjustment)
    }

    fn restore(&mut self) -> Checked<()> {
        fs::write("/proc/self/oom_score_adj", self.previous.to_string())
            .map_err(|e| e.to_string())?;
        require(
            bounded_read(Path::new("/proc/self/oom_score_adj"))?.trim()
                == self.previous.to_string(),
            "OOM adjustment restoration read-back mismatch",
        )?;
        self.restored = true;
        Ok(())
    }
}
impl Drop for ScoreAdjustment {
    fn drop(&mut self) {
        if !self.restored {
            let _ = self.restore();
        }
    }
}

/// Anonymous private mapping: Linux initializes pages to zero on first touch.
/// Ownership remains local and is released before the final failure report.
struct MemoryMapping {
    pointer: *mut u8,
    length: usize,
}
impl MemoryMapping {
    fn new(length: usize) -> Checked<Self> {
        require(
            length > 0 && length <= MEMORY_BYTES as usize,
            "invalid bounded mapping length",
        )?;
        // SAFETY: no fixed address, file or shared mapping is requested. MAP_FAILED
        // is checked before the pointer can be used; length stays fixed for Drop.
        let pointer = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                length,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
                -1,
                0,
            )
        };
        if pointer == libc::MAP_FAILED {
            return Err(format!(
                "anonymous mapping: {}",
                std::io::Error::last_os_error()
            ));
        }
        if pointer.is_null() {
            // SAFETY: mmap succeeded; reject an address Rust cannot dereference.
            unsafe { libc::munmap(pointer, length) };
            return Err("anonymous mapping returned a null address".into());
        }
        Ok(Self {
            pointer: pointer.cast(),
            length,
        })
    }
    fn touch(&mut self, offset: usize) -> Checked<()> {
        require(offset < self.length, "touch outside bounded mapping")?;
        // SAFETY: this writable mapping is owned for its complete fixed length;
        // the offset is checked and Linux supplies initialized anonymous pages.
        unsafe { std::ptr::write_volatile(self.pointer.add(offset), 0xa5) };
        Ok(())
    }
}
impl Drop for MemoryMapping {
    fn drop(&mut self) {
        // SAFETY: the exact mmap address/length are owned once and never moved,
        // resized, shared or unmapped elsewhere.
        unsafe { libc::munmap(self.pointer.cast(), self.length) };
    }
}

fn allocate(guard: &mut Guard) -> Checked<&'static str> {
    let boundary = json!({"event": "allocation_started", "touched_bytes": 0,
        "companion_identity_verified": guard.marker["companion"],
        "participants_verified_at_barrier": true, "allocation_limits": guard.allocation_limits,
        "allocator": "private anonymous mapping; one volatile write per native page"});
    writeln!(guard.log, "{boundary}").map_err(|e| e.to_string())?;
    guard.log.sync_data().map_err(|e| e.to_string())?;
    guard.allocation_started = true;
    let started = Instant::now();
    let mut memory = match MemoryMapping::new(MEMORY_BYTES as usize) {
        Ok(memory) => memory,
        Err(_) => return Ok("allocation_error"),
    };
    for offset in (0..MEMORY_BYTES as usize).step_by(guard.page_size) {
        if started.elapsed() >= Duration::from_secs(10) {
            return Ok("cooperative_deadline");
        }
        memory.touch(offset)?;
        let touched = offset + guard.page_size;
        if touched % (16 * CHUNK_BYTES) == 0 {
            writeln!(
                guard.log,
                "{{\"event\":\"progress\",\"touched_bytes\":{touched}}}"
            )
            .map_err(|e| e.to_string())?;
        }
    }
    Ok("allocation_cap_reached")
}

/// This entry point is absent from normal builds. No SQL parameter chooses a
/// path, allocation size or duration. Returning is always a failed OOM outcome.
pub fn run(profile: &str) -> Value {
    let mut guard = match Guard::acquire(profile) {
        Ok(guard) => guard,
        Err(error) => {
            return json!({"schema_version": 1, "kind": "p0_kernel_oom_native",
            "status": "not_run", "reason_code": "unsafe_environment", "error": error, "allocation_started": false});
        }
    };
    let mut adjustment = match ScoreAdjustment::raise() {
        Ok(adjustment) => adjustment,
        Err(error) => {
            return json!({"schema_version": 1, "kind": "p0_kernel_oom_native", "status": "not_run",
            "reason_code": "unsafe_environment", "error": error, "allocation_started": false});
        }
    };
    let barrier = guard.barrier(adjustment.previous);
    // allocate() releases its mapping before any final JSON construction.
    let outcome = barrier.and_then(|()| allocate(&mut guard));
    let allocation_started = guard.allocation_started;
    let restored = adjustment.restore();
    let report = json!({"schema_version": 1, "kind": "p0_kernel_oom_native", "status": "failed",
        "allocation_started": allocation_started, "outcome": outcome.as_ref().ok(), "error": outcome.as_ref().err(),
        "oom_score_restored": restored.is_ok(),
        "restore_error": restored.err(), "kernel_oom_observed": false, "target_isolation_gate_passed": false});
    let _ = writeln!(guard.log, "{report}");
    report
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_mapping_initializes_pages_and_rejects_invalid_offsets() {
        // A single page verifies ownership/bounds without running the OOM probe.
        let mut memory = MemoryMapping::new(4096).unwrap();
        // SAFETY: Linux provides initialized anonymous storage for this live map.
        assert_eq!(unsafe { std::ptr::read_volatile(memory.pointer) }, 0);
        assert_eq!(
            unsafe { std::ptr::read_volatile(memory.pointer.add(4095)) },
            0
        );
        memory.touch(0).unwrap();
        memory.touch(4095).unwrap();
        assert_eq!(unsafe { std::ptr::read_volatile(memory.pointer) }, 0xa5);
        assert!(memory.touch(4096).is_err());
        assert!(memory.touch(usize::MAX).is_err());
        assert!(MemoryMapping::new(0).is_err());
        assert!(MemoryMapping::new(MEMORY_BYTES as usize + 1).is_err());
    }

    #[test]
    fn nonce_refuses_paths_and_ambiguous_values_without_native_execution() {
        for invalid in [
            "",
            "../escape",
            "0123456789abcdef",
            "A123456789abcdef0123456789abcdef",
            "0123456789abcdef0123456789abcdef\n",
        ] {
            assert!(!valid_nonce(invalid));
        }
        assert!(valid_nonce("0123456789abcdef0123456789abcdef"));
    }

    #[test]
    fn altered_container_limits_fail_before_allocation() {
        let valid = BTreeMap::from_iter(
            [
                ("memory.max", "805306368"),
                ("memory.swap.max", "0"),
                ("memory.swap.current", "0"),
                ("memory.oom.group", "0"),
                ("memory.high", "max"),
                ("pids.max", "128"),
                ("cpu.max", "200000 100000"),
                ("cgroup.type", "domain"),
                ("cgroup.subtree_control", ""),
            ]
            .map(|(k, v)| (k.into(), v.into())),
        );
        assert!(validate_limits(&valid).is_ok());
        for key in valid.keys() {
            let mut wrong = valid.clone();
            wrong.insert(key.clone(), "unexpected".into());
            assert!(validate_limits(&wrong).is_err(), "accepted altered {key}");
            wrong.remove(key);
            assert!(validate_limits(&wrong).is_err());
        }
    }

    #[test]
    fn counters_are_keyed_and_malformed_evidence_is_rejected() {
        let values = keyed_numbers("oom_kill 1\nmax 7\noom 2\n").unwrap();
        assert_eq!(values["max"], 7);
        for bad in ["max 1\nmax 2", "oom -1", "oom 1 extra", "oom"] {
            assert!(keyed_numbers(bad).is_err());
        }
    }

    #[test]
    fn finite_helper_budget_requires_complete_allocation_headroom() {
        let baseline = 512 * 1024 * 1024;
        let required = baseline + 2 * MEMORY_BYTES;
        assert!(validate_allocation_headroom("managed_helper", required, baseline).is_ok());
        for cap in [0, baseline, required - 1] {
            assert!(validate_allocation_headroom("managed_helper", cap, baseline).is_err());
        }
        assert!(validate_allocation_headroom("direct_worker", required, baseline).is_err());
        assert!(
            validate_allocation_headroom("managed_helper", u64::MAX - 1, u64::MAX - 1).is_err()
        );
        for profile in ["direct_worker", "managed_helper"] {
            assert!(validate_allocation_headroom(profile, libc::RLIM_INFINITY, baseline).is_ok());
        }
    }
}

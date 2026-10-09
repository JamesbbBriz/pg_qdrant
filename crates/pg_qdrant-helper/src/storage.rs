//! Exact epoch ownership and restart-safe retirement under the database owner lock.
use pg_qdrant_protocol::ProbeError;
use serde_json::{Value, json};
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

fn failure(message: impl std::fmt::Display) -> ProbeError {
    ProbeError::new(
        "source_storage_identity",
        message.to_string(),
        "Preserve storage whose exact ownership cannot be verified.",
    )
}

fn root_directory(root: &Path) -> Result<(), ProbeError> {
    let metadata = std::fs::symlink_metadata(root).map_err(failure)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(failure("storage root is not an owned directory"));
    }
    Ok(())
}

fn marker(root: &Path, key: &str, suffix: &str) -> Result<PathBuf, ProbeError> {
    if key.is_empty() || !key.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-') {
        return Err(failure("invalid storage identity path"));
    }
    root_directory(root)?;
    Ok(root.join(format!("{key}.{suffix}")))
}

fn read(path: &Path) -> Result<Value, ProbeError> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(failure)?;
    if !file.metadata().map_err(failure)?.is_file() {
        return Err(failure("ownership marker is not a regular file"));
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(4097)
        .read_to_end(&mut bytes)
        .map_err(failure)?;
    if bytes.len() > 4096 {
        return Err(failure("ownership marker exceeds bounds"));
    }
    serde_json::from_slice(&bytes).map_err(failure)
}

fn publish(path: &Path, value: &Value) -> Result<(), ProbeError> {
    static SERIAL: AtomicU64 = AtomicU64::new(0);
    let pending = path.with_extension(format!(
        "pending-{}-{}",
        std::process::id(),
        SERIAL.fetch_add(1, Ordering::Relaxed)
    ));
    let mut created = false;
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&pending)
            .map_err(failure)?;
        created = true;
        file.write_all(&serde_json::to_vec(value).map_err(failure)?)
            .map_err(failure)?;
        file.sync_all().map_err(failure)?;
        std::fs::hard_link(&pending, path).map_err(failure)?;
        File::open(
            path.parent()
                .ok_or_else(|| failure("missing storage root"))?,
        )
        .and_then(|dir| dir.sync_all())
        .map_err(failure)
    })();
    // Only the exclusive temporary file created for this publication is removed.
    if created {
        let _ = std::fs::remove_file(pending);
    }
    result
}

pub fn bind(root: &Path, key: &str, consumer: &str) -> Result<(), ProbeError> {
    // The database owner lock already requires an existing parent directory.
    // Do not create unchecked ancestors whose entries have not been synced.
    match std::fs::create_dir(root) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(failure(error)),
    }
    root_directory(root)?;
    let parent = root
        .parent()
        .ok_or_else(|| failure("storage root has no parent directory"))?;
    let parent = if parent.as_os_str().is_empty() {
        Path::new(".")
    } else {
        parent
    };
    // Persist the root entry before publishing ownership or creating an epoch.
    // Also sync an existing root: a prior owner may have died before this sync.
    File::open(parent)
        .and_then(|dir| dir.sync_all())
        .map_err(failure)?;
    let binding = marker(root, key, "owner")?;
    if root.join(key).try_exists().map_err(failure)?
        || binding.try_exists().map_err(failure)?
        || marker(root, key, "retired")?
            .try_exists()
            .map_err(failure)?
    {
        return Err(failure("storage epoch already exists; rebuild required"));
    }
    publish(
        &binding,
        &json!({"version":1,"epoch_key":key,"consumer_id":consumer}),
    )
}

pub fn begin_retirement(
    root: &Path,
    key: &str,
    consumer: &str,
    task: &str,
) -> Result<(), ProbeError> {
    let binding = read(&marker(root, key, "owner")?)?;
    if binding != json!({"version":1,"epoch_key":key,"consumer_id":consumer}) {
        return Err(failure("retirement ownership identity mismatch"));
    }
    let receipt = marker(root, key, "retired")?;
    let expected = json!({"version":1,"epoch_key":key,"consumer_id":consumer,"task_id":task});
    match read(&receipt) {
        Ok(observed) if observed == expected => Ok(()),
        Ok(_) => Err(failure("retirement receipt identity mismatch")),
        Err(_) if !receipt.try_exists().map_err(failure)? => publish(&receipt, &expected),
        Err(error) => Err(error),
    }
}

pub fn remove_epoch(root: &Path, key: &str) -> Result<(), ProbeError> {
    root_directory(root)?;
    let path = root.join(key);
    match std::fs::symlink_metadata(&path) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink()
                || !metadata.is_dir()
                || path.canonicalize().map_err(failure)?.parent()
                    != Some(root.canonicalize().map_err(failure)?.as_path())
            {
                return Err(failure("retirement path is outside the owned storage root"));
            }
            std::fs::remove_dir_all(&path).map_err(failure)?;
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(failure(error)),
    }
    // The durable intent survives a crash before this sync or the PostgreSQL ACK.
    File::open(root)
        .and_then(|dir| dir.sync_all())
        .map_err(failure)
}

#[cfg(test)]
mod tests {
    use super::*;
    const KEY: &str = "1-00000000-0000-0000-0000-000000000001-00000000-0000-0000-0000-000000000002";
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            static SERIAL: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!(
                "pgq-epoch-{}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos(),
                SERIAL.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.0).unwrap();
        }
    }

    #[test]
    fn new_root_is_bound_and_existing_root_accepts_a_distinct_epoch() {
        let f = Fixture::new();
        let root = f.0.join("indexes");
        bind(&root, KEY, "consumer").unwrap();
        assert_eq!(
            read(&root.join(format!("{KEY}.owner"))).unwrap(),
            json!({"version":1,"epoch_key":KEY,"consumer_id":"consumer"})
        );
        bind(&root, "ab-cd", "other").unwrap();
        assert!(root.join("ab-cd.owner").is_file());
        assert!(bind(&root, KEY, "consumer").is_err());
    }

    #[test]
    fn missing_parent_and_non_directory_roots_preserve_existing_paths() {
        let f = Fixture::new();
        let missing_parent = f.0.join("missing");
        assert!(bind(&missing_parent.join("indexes"), KEY, "consumer").is_err());
        assert!(!missing_parent.exists());

        let file = f.0.join("file");
        std::fs::write(&file, b"preserve").unwrap();
        assert!(bind(&file, KEY, "consumer").is_err());
        assert_eq!(std::fs::read(&file).unwrap(), b"preserve");

        let outside = Fixture::new();
        std::fs::write(outside.0.join("data"), b"preserve").unwrap();
        let link = f.0.join("link");
        std::os::unix::fs::symlink(&outside.0, &link).unwrap();
        assert!(bind(&link, KEY, "consumer").is_err());
        assert_eq!(std::fs::read(outside.0.join("data")).unwrap(), b"preserve");
        assert!(!outside.0.join(format!("{KEY}.owner")).exists());
    }

    #[test]
    fn restart_replays_exact_retirement_before_and_after_directory_removal() {
        let f = Fixture::new();
        bind(&f.0, KEY, "consumer").unwrap();
        std::fs::create_dir(f.0.join(KEY)).unwrap();
        std::fs::write(f.0.join(KEY).join("data"), b"owned").unwrap();
        begin_retirement(&f.0, KEY, "consumer", "task").unwrap();
        // No in-memory receipt: reopen the persisted intent after an owner restart.
        begin_retirement(&f.0, KEY, "consumer", "task").unwrap();
        remove_epoch(&f.0, KEY).unwrap();
        begin_retirement(&f.0, KEY, "consumer", "task").unwrap();
        remove_epoch(&f.0, KEY).unwrap();
        assert!(bind(&f.0, KEY, "consumer").is_err());
        assert!(begin_retirement(&f.0, KEY, "wrong", "task").is_err());
        assert!(begin_retirement(&f.0, KEY, "consumer", "wrong").is_err());
    }

    #[test]
    fn missing_mismatched_and_symlinked_ownership_preserve_storage() {
        let f = Fixture::new();
        std::fs::create_dir(f.0.join(KEY)).unwrap();
        assert!(begin_retirement(&f.0, KEY, "consumer", "task").is_err());
        std::fs::remove_dir(f.0.join(KEY)).unwrap();
        bind(&f.0, KEY, "consumer").unwrap();
        std::fs::create_dir(f.0.join(KEY)).unwrap();
        assert!(begin_retirement(&f.0, KEY, "other", "task").is_err());
        std::fs::remove_file(f.0.join(format!("{KEY}.owner"))).unwrap();
        std::os::unix::fs::symlink("outside", f.0.join(format!("{KEY}.owner"))).unwrap();
        assert!(begin_retirement(&f.0, KEY, "consumer", "task").is_err());
        assert!(f.0.join(KEY).is_dir());
        assert!(marker(&f.0, "../outside", "owner").is_err());
    }

    #[test]
    fn directory_symlink_is_never_followed_and_unrelated_epoch_survives() {
        let f = Fixture::new();
        let outside = Fixture::new();
        std::fs::write(outside.0.join("data"), b"preserve").unwrap();
        bind(&f.0, KEY, "consumer").unwrap();
        std::os::unix::fs::symlink(&outside.0, f.0.join(KEY)).unwrap();
        begin_retirement(&f.0, KEY, "consumer", "task").unwrap();
        assert!(remove_epoch(&f.0, KEY).is_err());
        assert_eq!(std::fs::read(outside.0.join("data")).unwrap(), b"preserve");
    }

    #[test]
    fn malformed_oversized_and_special_markers_fail_without_touching_epoch() {
        let f = Fixture::new();
        bind(&f.0, KEY, "consumer").unwrap();
        std::fs::create_dir(f.0.join(KEY)).unwrap();
        let binding = f.0.join(format!("{KEY}.owner"));
        for bytes in [b"{".to_vec(), vec![b' '; 4097]] {
            std::fs::write(&binding, bytes).unwrap();
            assert!(begin_retirement(&f.0, KEY, "consumer", "task").is_err());
            assert!(f.0.join(KEY).is_dir());
        }
        std::fs::remove_file(&binding).unwrap();
        std::fs::create_dir(&binding).unwrap();
        assert!(begin_retirement(&f.0, KEY, "consumer", "task").is_err());
        assert!(f.0.join(KEY).is_dir());
    }
}

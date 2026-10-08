//! Safe default coverage: corruption touches only owned copies. Disk filling
//! remains opt-in and these checks supply only paths that must be rejected.

use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

fn bounded_output(mut command: Command) -> Output {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn().expect("start independent fault probe");
    let deadline = Instant::now() + Duration::from_secs(45);
    loop {
        if child.try_wait().expect("poll fault probe").is_some() {
            return child.wait_with_output().expect("collect fault probe");
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let output = child.wait_with_output().expect("reap timed out probe");
            panic!(
                "fault probe exceeded 45 seconds: {}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn metadata_corruption_and_unsafe_disk_environment_are_rejected() {
    let executable = env!("CARGO_BIN_EXE_pg-qdrant-edge-probe");
    let mut corruption = Command::new(executable);
    corruption.arg("--corruption-probe");
    let output = bounded_output(corruption);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["status"], "passed");
    assert_eq!(report["engine_version"], "0.8.0");
    let cases = report["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 2);
    for case in cases {
        assert_eq!(case["load"], "error");
        assert_eq!(case["ready"], false);
        assert!(!case["error"].as_str().unwrap().is_empty());
    }
    assert_eq!(report["recovery"]["original_unchanged"], true);

    let owned_host_directory = tempfile::tempdir().unwrap();
    for path in [
        owned_host_directory.path(),
        std::path::Path::new("/dev/shm"),
    ] {
        let mut refused = Command::new(executable);
        refused
            .arg("--disk-full-probe")
            .env("PG_QDRANT_ENOSPC_DIR", path);
        let output = bounded_output(refused);
        assert_eq!(output.status.code(), Some(2));
        let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(report["status"], "not_run");
        #[cfg(target_os = "linux")]
        {
            assert_eq!(report["reason_code"], "unsafe_environment");
            assert_eq!(report["disk_writes_attempted"], false);
        }
    }
    assert_eq!(
        std::fs::read_dir(owned_host_directory.path())
            .unwrap()
            .count(),
        0
    );

    let mut omitted = Command::new(executable);
    omitted
        .arg("--disk-full-probe")
        .env_remove("PG_QDRANT_ENOSPC_DIR");
    let output = bounded_output(omitted);
    assert!(output.status.success());
    let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(report["status"], "not_run");
}

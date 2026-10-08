//! Real owned-child fixtures for the PostgreSQL supervisor's PG-free module.
//! Include the production file directly so this target does not need libpostgres
//! or a second implementation of its exit/kill bookkeeping.

#[path = "../../pg_qdrant/src/helper_child.rs"]
mod helper_child;

use helper_child::{ManagedChild, ObservedExit};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

fn spawn(script: &str) -> Child {
    Command::new("/bin/sh")
        .args(["-c", script])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("start owned fixture")
}

fn wait_exit(child: &mut ManagedChild) -> ObservedExit {
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        if let Some(exit) = child.poll().expect("poll owned child") {
            return exit;
        }
        thread::sleep(Duration::from_millis(5));
    }
    panic!("owned fixture did not exit before deadline");
}

#[test]
fn already_exited_child_is_reaped_without_cleanup_signal() {
    let mut raw_child = spawn("exit 17");
    // Synchronize an actual child exit before presenting its retained Child
    // handle to cleanup. No timing assumptions or simulated ExitStatus values.
    let deadline = Instant::now() + Duration::from_secs(2);
    let completed = loop {
        if let Some(status) = raw_child.try_wait().unwrap() {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = raw_child.kill();
            panic!("owned exit fixture exceeded its deadline");
        }
        thread::sleep(Duration::from_millis(5));
    };
    assert_eq!(completed.code(), Some(17));
    let mut child = ManagedChild::new(raw_child);
    let exit = child.request_stop("transport_failure").unwrap().unwrap();
    assert_eq!(exit.status.code(), Some(17));
    assert_eq!(exit.details["stop_reason"], "unexpected_exit");
    assert_eq!(exit.details["supervisor_kill_requested"], false);
    assert_eq!(exit.details["kill_attempts"], 0);
    assert!(exit.details["first_kill_attempt"].is_null());
    assert!(exit.details["last_kill_attempt"].is_null());
    let later = child.request_stop("execution_budget").unwrap().unwrap();
    assert_eq!(later.details, exit.details);
}

#[test]
fn explicit_kill_records_result_and_preserves_first_reason() {
    // This child also has its own five-second lifetime if a fixture assertion
    // fails. It is never an unbounded stopped process.
    let mut child = ManagedChild::new(spawn("exec sleep 5"));
    assert!(child.request_stop("execution_budget").unwrap().is_none());
    let observed = child.request_stop("transport_failure").unwrap();
    let exit = observed.unwrap_or_else(|| wait_exit(&mut child));
    assert_eq!(exit.details["signal"], libc::SIGKILL);
    assert!(exit.status.code().is_none());
    assert_eq!(exit.details["stop_reason"], "execution_budget");
    assert_eq!(exit.details["supervisor_kill_requested"], true);
    assert_eq!(exit.details["kill_attempts"], 1);
    assert_eq!(exit.details["first_kill_attempt"]["succeeded"], true);
    assert!(exit.details["first_kill_attempt"]["raw_os_error"].is_null());
    assert_eq!(
        exit.details["first_kill_attempt"],
        exit.details["last_kill_attempt"]
    );
    let later = child.request_stop("supervisor_shutdown").unwrap().unwrap();
    assert_eq!(later.details, exit.details);
}

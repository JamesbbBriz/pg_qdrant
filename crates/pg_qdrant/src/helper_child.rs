//! Owned-child exit observations, without PostgreSQL APIs or wire types.

use serde_json::{Value, json};
use std::io;
use std::os::unix::process::ExitStatusExt;
use std::process::{Child, ExitStatus};
use std::time::Instant;

pub struct ObservedExit {
    pub status: ExitStatus,
    pub details: Value,
}

pub struct ManagedChild {
    child: Child,
    started: Instant,
    stop_reason: Option<&'static str>,
    first_kill: Option<Value>,
    last_kill: Option<Value>,
    kill_attempts: u32,
    kill_succeeded: bool,
}

impl ManagedChild {
    pub fn new(child: Child) -> Self {
        Self {
            child,
            started: Instant::now(),
            stop_reason: None,
            first_kill: None,
            last_kill: None,
            kill_attempts: 0,
            kill_succeeded: false,
        }
    }

    pub fn id(&self) -> u32 {
        self.child.id()
    }

    pub fn poll(&mut self) -> io::Result<Option<ObservedExit>> {
        Ok(self.child.try_wait()?.map(|status| ObservedExit {
            status,
            details: json!({
                "engine_pid": self.id(),
                "exit_code": status.code(),
                "signal": status.signal(),
                "stop_reason": self.stop_reason.unwrap_or("unexpected_exit"),
                "supervisor_kill_requested": self.kill_attempts != 0,
                "kill_attempts": self.kill_attempts,
                "first_kill_attempt": self.first_kill,
                "last_kill_attempt": self.last_kill,
            }),
        }))
    }

    pub fn request_stop(&mut self, reason: &'static str) -> io::Result<Option<ObservedExit>> {
        // EOF can arrive after the preceding supervisor poll. Reap first so an
        // exit observed here is not assigned a subsequent cleanup kill request.
        if let Some(exit) = self.poll()? {
            return Ok(Some(exit));
        }
        // Keep the first request and its actual result. Successful SIGKILL needs
        // no repeated signal; a failed call can be retried by the existing loop.
        if !self.kill_succeeded {
            self.stop_reason.get_or_insert(reason);
            let after_ms = self.started.elapsed().as_millis();
            let result = self.child.kill();
            self.kill_succeeded = result.is_ok();
            let attempt = match result {
                Ok(()) => json!({"after_spawn_ms": after_ms, "succeeded": true,
                    "raw_os_error": null, "error": null}),
                Err(error) => json!({"after_spawn_ms": after_ms, "succeeded": false,
                    "raw_os_error": error.raw_os_error(), "error": error.to_string()}),
            };
            self.kill_attempts = self.kill_attempts.saturating_add(1);
            self.first_kill.get_or_insert_with(|| attempt.clone());
            self.last_kill = Some(attempt);
        }
        Ok(None)
    }
}

impl Drop for ManagedChild {
    fn drop(&mut self) {
        // Graceful Rust cleanup remains best effort and nonblocking. Process
        // exit also closes stdin, which is the helper's parent-death signal.
        let _ = self.request_stop("supervisor_shutdown");
    }
}

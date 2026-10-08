//! P0 supervisor: PostgreSQL remains in this process, Edge runs only after exec.

use crate::ipc::{self, Operation, ProbeError};
use pg_qdrant_protocol::{HelperRequest, RESPONSE_BYTES, VERSION};
use serde_json::{Value, json};
use std::ffi::CStr;
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStringExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub struct Channel {
    input: ChildStdin,
    output: ChildStdout,
    pid: u32,
    sequence: u64,
}

pub type Connection = Arc<Mutex<Channel>>;

pub struct Supervisor {
    executable: PathBuf,
    owner_path: PathBuf,
    child: Option<Child>,
    connection: Option<Connection>,
    attempts: u32,
    next_attempt: Instant,
    last_error: Option<String>,
    last_exit: Option<Value>,
    pending_stop: Option<&'static str>,
}

impl Supervisor {
    pub fn new(directory: &Path, database_oid: u32) -> Result<Self, ProbeError> {
        // This PostgreSQL-global pointer is read only on the worker main thread.
        // No SQL argument or environment variable chooses an executable.
        let binary = unsafe { CStr::from_ptr((&raw const pgrx::pg_sys::my_exec_path).cast()) };
        let binary = PathBuf::from(std::ffi::OsString::from_vec(binary.to_bytes().to_vec()));
        let directory_of_binary = binary
            .parent()
            .filter(|_| binary.is_absolute())
            .ok_or_else(|| ProbeError::invalid("PostgreSQL executable path must be absolute"))?;
        Ok(Self {
            executable: directory_of_binary.join("pg_qdrant_p0_helper"),
            owner_path: directory.join(format!("db-{database_oid}.engine-owner")),
            child: None,
            connection: None,
            attempts: 0,
            next_attempt: Instant::now(),
            last_error: None,
            last_exit: None,
            pending_stop: None,
        })
    }

    pub fn status(&self) -> Value {
        json!({"engine_pid": self.child.as_ref().map(Child::id),
            "engine_ready": self.connection.is_some(),
            "helper_start_attempts": self.attempts,
            "helper_restart_limit": 3,
            "helper_operation_limit_ms": 125_000,
            "helper_last_error": self.last_error,
            "helper_last_exit": self.last_exit,
            "helper_restart_exhausted": self.attempts >= 4 && self.child.is_none()})
    }

    pub fn connection(&self) -> Option<Connection> {
        self.connection.clone()
    }

    pub fn tick(&mut self, active_elapsed: Option<Duration>) {
        // This is an explicit P0 process-stop limit, not native cancellation or
        // the future durable-index shutdown contract. Ownership lasts until exit.
        if active_elapsed.is_some_and(|elapsed| elapsed >= Duration::from_secs(125)) {
            self.stop("execution_budget");
            self.last_error = Some("helper exceeded the 125-second native-operation budget".into());
        }
        if let Some(child) = &mut self.child {
            match child.try_wait() {
                Ok(Some(status)) => {
                    self.last_exit = Some(json!({"engine_pid": child.id(),
                        "exit_code": status.code(), "signal": status.signal(),
                        "stop_reason": self.pending_stop.take().unwrap_or("unexpected_exit")}));
                    self.child = None;
                    self.connection = None;
                    self.last_error = Some(format!("helper exited: {status}"));
                    self.backoff();
                }
                Err(error) => self.last_error = Some(format!("helper wait failed: {error}")),
                Ok(None) => {}
            }
        }
        // Reap the old process and drain its outstanding response before any
        // replacement; the helper's OS owner fence is a second independent gate.
        if self.child.is_none()
            && active_elapsed.is_none()
            && self.attempts < 4
            && Instant::now() >= self.next_attempt
        {
            self.attempts += 1;
            if let Err(error) = self.spawn() {
                self.stop("startup_failure");
                self.last_error = Some(format!("{} ({})", error.message, error.code));
                self.backoff();
            }
        }
    }

    fn backoff(&mut self) {
        self.next_attempt =
            Instant::now() + Duration::from_millis(100 * u64::from(self.attempts.max(1)));
    }

    fn stop(&mut self, reason: &'static str) {
        self.connection = None;
        if let Some(child) = &mut self.child {
            // Keep the first supervisor reason, even if EOF is observed later.
            self.pending_stop.get_or_insert(reason);
            let _ = child.kill();
        }
    }

    fn spawn(&mut self) -> Result<(), ProbeError> {
        let mut child = Command::new(&self.executable)
            .arg(&self.owner_path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .env_clear()
            .env("LANG", "C.UTF-8")
            .spawn()
            .map_err(ProbeError::io)?;
        let pid = child.id();
        let input = child.stdin.take().expect("piped helper stdin");
        let mut output = child.stdout.take().expect("piped helper stdout");
        // Retain the Child immediately so every error path can stop/reap it.
        self.pending_stop = None;
        self.child = Some(child);
        set_nonblocking(&output, true)?;
        let deadline = Instant::now() + Duration::from_secs(2);
        let ready = read_response(&mut output, pid, 0, Some(deadline), ipc::pause_postgres);
        match ready {
            Ok(value)
                if value["ready"] == true
                    && value["helper_version"] == env!("CARGO_PKG_VERSION")
                    && value["fault_injection"] == cfg!(feature = "p0-fault-injection") =>
            {
                set_nonblocking(&output, false)?;
                self.connection = Some(Arc::new(Mutex::new(Channel {
                    input,
                    output,
                    pid,
                    sequence: 0,
                })));
                self.last_error = None;
                Ok(())
            }
            result => {
                // Closing stdin is also a parent-death signal to the helper.
                self.stop("handshake_failure");
                Err(result.err().unwrap_or_else(|| {
                    ProbeError::invalid("helper build/ready handshake mismatch")
                }))
            }
        }
    }

    pub fn completed(&mut self, result: &Result<Value, ProbeError>) {
        if result
            .as_ref()
            .is_err_and(|e| matches!(e.code.as_str(), "protocol_error" | "worker_unavailable"))
        {
            self.stop("transport_failure");
        }
    }
}

impl Drop for Supervisor {
    fn drop(&mut self) {
        // Graceful Rust exits stop the child. PostgreSQL proc_exit/SIGKILL also
        // close every stdin writer; the helper EOF watchdog then exits itself.
        if let Some(child) = &mut self.child {
            let _ = child.kill();
        }
    }
}

fn set_nonblocking(output: &ChildStdout, enabled: bool) -> Result<(), ProbeError> {
    let fd = output.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 {
        return Err(ProbeError::io(io::Error::last_os_error()));
    }
    let flags = if enabled {
        flags | libc::O_NONBLOCK
    } else {
        flags & !libc::O_NONBLOCK
    };
    if unsafe { libc::fcntl(fd, libc::F_SETFL, flags) } < 0 {
        return Err(ProbeError::io(io::Error::last_os_error()));
    }
    Ok(())
}

/// Runs on the owned-data pipe thread. It never calls a PostgreSQL API.
pub fn execute(connection: Connection, operation: Operation) -> Result<Value, ProbeError> {
    let mut io = connection
        .lock()
        .map_err(|_| ProbeError::invalid("helper channel was poisoned"))?;
    io.sequence = io
        .sequence
        .checked_add(1)
        .ok_or_else(|| ProbeError::invalid("helper sequence exhausted"))?;
    let request = HelperRequest {
        protocol_version: VERSION,
        request_id: io.sequence,
        operation,
    };
    let mut bytes = serde_json::to_vec(&request).expect("owned protocol serializes");
    bytes.push(b'\n');
    if bytes.len() > ipc::REQUEST_BYTES {
        return Err(ProbeError::invalid("helper request exceeds byte budget"));
    }
    io.input.write_all(&bytes).map_err(ProbeError::io)?;
    io.input.flush().map_err(ProbeError::io)?;
    let (pid, request_id) = (io.pid, io.sequence);
    // No caller deadline releases native ownership; completion or process death
    // must close this request before the supervisor admits another one.
    read_response(&mut io.output, pid, request_id, None, || {
        std::thread::sleep(Duration::from_millis(10))
    })
}

fn read_response(
    output: &mut ChildStdout,
    pid: u32,
    request_id: u64,
    deadline: Option<Instant>,
    mut pause: impl FnMut(),
) -> Result<Value, ProbeError> {
    let mut bytes = Vec::new();
    loop {
        if deadline.is_some_and(|d| Instant::now() >= d) {
            return Err(ProbeError::timeout());
        }
        let mut chunk = [0_u8; 8192];
        match output.read(&mut chunk) {
            Ok(0) => {
                return Err(ProbeError::new(
                    "worker_unavailable",
                    "managed helper disconnected",
                    "The PostgreSQL supervisor remains available; inspect its helper restart status.",
                ));
            }
            Ok(size) => {
                bytes.extend_from_slice(&chunk[..size]);
                if bytes.len() > RESPONSE_BYTES {
                    return Err(protocol_error("helper response exceeds byte budget"));
                }
                if let Some(end) = bytes.iter().position(|b| *b == b'\n') {
                    if end + 1 != bytes.len() {
                        return Err(protocol_error("multiple helper response frames"));
                    }
                    let envelope: Value = serde_json::from_slice(&bytes)
                        .map_err(|_| protocol_error("invalid helper JSON"))?;
                    let object = envelope
                        .as_object()
                        .ok_or_else(|| protocol_error("helper envelope must be an object"))?;
                    if object.keys().any(|key| {
                        !matches!(
                            key.as_str(),
                            "protocol_version" | "engine_pid" | "request_id" | "result" | "error"
                        )
                    }) || object.contains_key("result") == object.contains_key("error")
                    {
                        return Err(protocol_error("unexpected helper response fields"));
                    }
                    if envelope["protocol_version"] != VERSION
                        || envelope["engine_pid"] != pid
                        || envelope["request_id"] != request_id
                    {
                        return Err(protocol_error("helper response identity mismatch"));
                    }
                    if let Some(error) = envelope.get("error") {
                        return Err(serde_json::from_value(error.clone())
                            .map_err(|_| protocol_error("invalid helper error"))?);
                    }
                    return envelope
                        .get("result")
                        .cloned()
                        .ok_or_else(|| protocol_error("missing helper result"));
                }
            }
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                pause()
            }
            Err(error) => return Err(ProbeError::io(error)),
        }
    }
}

fn protocol_error(message: &str) -> ProbeError {
    ProbeError::new(
        "protocol_error",
        message,
        "The helper was stopped; install matching PostgreSQL and helper builds.",
    )
}

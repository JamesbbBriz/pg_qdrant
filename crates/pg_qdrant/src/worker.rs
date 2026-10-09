//! P0 owner process. The transport is intentionally separate from product APIs.

use crate::ipc::{self, Operation, ProbeError, Request};
use pgrx::bgworkers::{BackgroundWorker, BackgroundWorkerBuilder, SignalWakeFlags};
use pgrx::prelude::*;
use serde_json::{Value, json};
use std::collections::{BTreeMap, VecDeque};
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub fn start(timeout_ms: i32) -> Result<Value, ProbeError> {
    start_owner(timeout_ms, true)
}

/// Product status/wait must observe a live supervisor during helper recovery.
pub fn ensure_owner(timeout_ms: i32) -> Result<Value, ProbeError> {
    start_owner(timeout_ms, false)
}

fn start_owner(timeout_ms: i32, require_engine: bool) -> Result<Value, ProbeError> {
    let deadline = Instant::now() + ipc::validate_timeout(timeout_ms)?;
    // Scalar database identity only; no backend pointer is sent to a worker.
    let database_oid = unsafe { pg_sys::MyDatabaseId };
    let mut existing_owner = false;
    if ipc::socket_path(database_oid.to_u32())?.exists() {
        if let Ok(mut status) = ipc::call(Operation::Ping, timeout_ms.min(250)) {
            if !require_engine || status["engine_ready"] == true {
                status["worker_reused"] = json!(true);
                return Ok(status);
            }
            existing_owner = true;
        }
    }
    let handle = if existing_owner {
        None
    } else {
        Some(
            BackgroundWorkerBuilder::new("pg_qdrant P0 owner")
                .set_type("pg_qdrant P0 owner")
                .set_library("pg_qdrant")
                .set_function("pg_qdrant_p0_worker")
                .enable_spi_access()
                .set_argument(Some(pg_sys::Datum::from(database_oid)))
                .set_notify_pid(unsafe { pg_sys::MyProcPid })
                .set_restart_time(None)
                .load_dynamic()
                .map_err(|_| {
                    ProbeError::new(
                        "worker_unavailable",
                        "PostgreSQL could not register a P0 owner worker",
                        "Check max_worker_processes and the PostgreSQL server log.",
                    )
                })?,
        )
    };

    // Registration does not mean the socket, DB connection, or engine is ready.
    // A concurrent starter can win ownership; both callers then use that owner.
    while Instant::now() < deadline {
        let remaining = deadline
            .saturating_duration_since(Instant::now())
            .as_millis()
            .max(1) as i32;
        // One reply may follow the worker's bounded SPI batch. Repeated tiny
        // deadlines can expire every accepted socket while making a live owner
        // appear unavailable. Use the existing overall startup budget; IPC
        // remains nonblocking and checks PostgreSQL cancellation throughout.
        match ipc::call(Operation::Ping, remaining) {
            Ok(mut status) if !require_engine || status["engine_ready"] == true => {
                let registered_pid = handle
                    .as_ref()
                    .and_then(|handle| handle.pid().ok())
                    .map(|pid| pid as u64);
                status["worker_reused"] =
                    json!(registered_pid != status.get("worker_pid").and_then(Value::as_u64));
                return Ok(status);
            }
            Ok(_) => {}
            Err(error) if error.code == "worker_unavailable" || error.code == "timeout" => {}
            Err(error) => return Err(error),
        }
        ipc::pause_postgres();
    }
    Err(ProbeError::timeout())
}

#[pg_guard]
#[unsafe(no_mangle)]
pub extern "C-unwind" fn pg_qdrant_p0_worker(argument: pg_sys::Datum) {
    BackgroundWorker::attach_signal_handlers(SignalWakeFlags::SIGHUP | SignalWakeFlags::SIGTERM);
    let database_oid = unsafe { pg_sys::Oid::from_datum(argument, false) }
        .expect("P0 worker requires a scalar database OID");
    // P0 entry points are superuser-only. This connection does not service user
    // source-table queries and is not the future product authorization model.
    BackgroundWorker::connect_worker_to_spi_by_oid(Some(database_oid), None);
    if let Err(error) = run(database_oid.to_u32()) {
        pgrx::warning!(
            "pg_qdrant P0 worker stopped: {} ({})",
            error.message,
            error.code
        );
    }
}

struct SocketCleanup(PathBuf);
impl Drop for SocketCleanup {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

struct Client {
    stream: UnixStream,
    incoming: Vec<u8>,
    outgoing: Option<Vec<u8>>,
    written: usize,
    deadline: Instant,
    submitted: bool,
    closed: bool,
}

impl Client {
    fn new(stream: UnixStream) -> Self {
        Self {
            stream,
            incoming: Vec::new(),
            outgoing: None,
            written: 0,
            deadline: Instant::now() + Duration::from_secs(5),
            submitted: false,
            closed: false,
        }
    }

    fn respond(&mut self, result: Result<Value, ProbeError>) {
        if !self.closed && self.outgoing.is_none() {
            self.outgoing = Some(ipc::encode_response(result));
            // A slow reader gets a bounded delivery interval, independent of its
            // completed query deadline; response memory cannot be held forever.
            self.deadline = Instant::now() + Duration::from_secs(5);
        }
    }

    fn read_request(&mut self) -> Option<Result<Request, ProbeError>> {
        let mut chunk = [0_u8; 4096];
        match self.stream.read(&mut chunk) {
            Ok(0) => {
                self.closed = true;
                None
            }
            Ok(size) => {
                if self.submitted || self.outgoing.is_some() {
                    self.closed = true;
                    return None;
                }
                self.incoming.extend_from_slice(&chunk[..size]);
                if self.incoming.len() > ipc::SEARCH_REQUEST_BYTES {
                    return Some(Err(ProbeError::invalid(
                        "P0 request exceeds the byte budget",
                    )));
                }
                if let Some(newline) = self.incoming.iter().position(|b| *b == b'\n') {
                    if newline + 1 != self.incoming.len() {
                        return Some(Err(ProbeError::invalid(
                            "P0 accepts one request per connection",
                        )));
                    }
                    Some(
                        serde_json::from_slice::<Request>(&self.incoming)
                            .map_err(|e| {
                                ProbeError::new(
                                    "protocol_error",
                                    e.to_string(),
                                    "Use the matching P0 protocol version and documented operation.",
                                )
                            })
                            .and_then(|request| {
                                if self.incoming.len() > request.operation.request_byte_limit() {
                                    Err(ProbeError::invalid(
                                        "request exceeds its operation byte budget",
                                    ))
                                } else {
                                    Ok(request)
                                }
                            }),
                    )
                } else {
                    None
                }
            }
            Err(e)
                if e.kind() == io::ErrorKind::WouldBlock
                    || e.kind() == io::ErrorKind::Interrupted =>
            {
                None
            }
            Err(_) => {
                self.closed = true;
                None
            }
        }
    }

    fn flush(&mut self) {
        if let Some(bytes) = &self.outgoing {
            match self.stream.write(&bytes[self.written..]) {
                Ok(0) => self.closed = true,
                Ok(size) => {
                    self.written += size;
                    if self.written == bytes.len() {
                        self.closed = true;
                    }
                }
                Err(e)
                    if e.kind() == io::ErrorKind::WouldBlock
                        || e.kind() == io::ErrorKind::Interrupted => {}
                Err(_) => self.closed = true,
            }
        }
    }
}

struct Active {
    client_id: u64,
    operation: &'static str,
    started: Instant,
    handle: JoinHandle<Result<Value, ProbeError>>,
    batch: Option<pg_qdrant_protocol::SourceBatch>,
}

fn run(database_oid: u32) -> Result<(), ProbeError> {
    let directory = ipc::directory()?;
    fs::create_dir_all(&directory).map_err(ProbeError::io)?;
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).map_err(ProbeError::io)?;
    let owner = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(directory.join(format!("db-{database_oid}.owner")))
        .map_err(ProbeError::io)?;
    // Holding the OS lock for the full lifetime prevents two worker processes
    // from serving one database owner endpoint, including concurrent startup.
    if owner.try_lock().is_err() {
        return Ok(());
    }
    // This descriptor deliberately lasts until OS process exit. Every return
    // path and every Rust unwind must retain the ownership fence while a
    // detached native thread could still be running. There is exactly one run
    // invocation per PostgreSQL worker process, so this is one process-lifetime
    // descriptor, not an accumulating per-request resource leak.
    let _owner_process_lifetime = std::mem::ManuallyDrop::new(owner);
    let path = ipc::socket_path(database_oid)?;
    if path.exists() {
        fs::remove_file(&path).map_err(ProbeError::io)?;
    }
    let listener = UnixListener::bind(&path).map_err(ProbeError::io)?;
    let _cleanup = SocketCleanup(path.clone());
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).map_err(ProbeError::io)?;
    listener.set_nonblocking(true).map_err(ProbeError::io)?;
    let worker_pid = std::process::id();
    let mut clients: BTreeMap<u64, Client> = BTreeMap::new();
    let mut queue: VecDeque<(u64, Operation)> = VecDeque::new();
    let mut active: Option<Active> = None;
    let mut next_client = 0_u64;
    let mut completed = 0_u64;
    let mut expired_or_disconnected = 0_u64;
    let mut rejected = 0_u64;
    #[cfg(feature = "p0-managed-helper")]
    let mut supervisor = crate::helper::Supervisor::new(&directory, database_oid)?;
    #[cfg(feature = "p0-managed-helper")]
    let mut consumer_instance: Option<String> = None;

    loop {
        #[cfg(feature = "p0-managed-helper")]
        supervisor.tick(active.as_ref().map(|job| job.started.elapsed()));
        #[cfg(feature = "p0-managed-helper")]
        let process_status = supervisor.status();
        #[cfg(feature = "p0-managed-helper")]
        if process_status["engine_ready"] == true
            && consumer_instance.as_deref() != process_status["engine_instance"].as_str()
        {
            let instance = process_status["engine_instance"]
                .as_str()
                .expect("ready owner identity");
            crate::consumer::reset(instance);
            consumer_instance = Some(instance.to_owned());
        }
        #[cfg(not(feature = "p0-managed-helper"))]
        let process_status = json!({"engine_pid": worker_pid, "engine_ready": true});
        // Accept a bounded number per tick, even under connection flooding.
        for _ in 0..ipc::CONNECTION_LIMIT {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    stream.set_nonblocking(true).map_err(ProbeError::io)?;
                    if clients.len() >= ipc::CONNECTION_LIMIT {
                        rejected += 1;
                        let bytes = ipc::encode_response(Err(queue_full()));
                        let _ = stream.write(&bytes);
                        continue;
                    }
                    next_client = next_client.wrapping_add(1);
                    clients.insert(next_client, Client::new(stream));
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(ProbeError::io(e)),
            }
        }

        for (&id, client) in &mut clients {
            let request = client.read_request();
            if client.closed && client.outgoing.is_none() {
                expired_or_disconnected += 1;
            }
            if let Some(request) = request {
                match request.and_then(validate_request) {
                    Err(error) => client.respond(Err(error)),
                    Ok(request) => {
                        client.submitted = true;
                        client.deadline =
                            Instant::now() + Duration::from_millis(request.timeout_ms);
                        if matches!(request.operation, Operation::Ping) {
                            let mut status = json!({
                                "worker_pid": worker_pid, "database_oid": database_oid,
                                "process_profile": if cfg!(feature="p0-managed-helper") {"managed_helper"} else {"direct_worker"},
                                "queue_depth": queue.len(), "queue_limit": ipc::QUEUE_LIMIT,
                                "connection_limit": ipc::CONNECTION_LIMIT,
                                "active": active.as_ref().map(|job| json!({
                                    "operation": job.operation,
                                    "elapsed_ms": job.started.elapsed().as_millis() as u64,
                                    "in_flight_native_cancellation": false
                                })),
                                "completed": completed,
                                "expired_or_disconnected": expired_or_disconnected,
                                "rejected": rejected,
                                "request_bytes_limit": ipc::REQUEST_BYTES,
                                "search_request_bytes_limit": ipc::SEARCH_REQUEST_BYTES,
                                "response_bytes_limit": ipc::RESPONSE_BYTES,
                                "engine_concurrency": 1,
                                "stage": "P0_feasibility"
                            });
                            status.as_object_mut().expect("status object").extend(
                                process_status
                                    .as_object()
                                    .expect("process status object")
                                    .clone(),
                            );
                            client.respond(Ok(status));
                        } else if process_status["engine_ready"] != true {
                            client.respond(Err(helper_unavailable()));
                        } else if queue.len() >= ipc::QUEUE_LIMIT {
                            rejected += 1;
                            client.respond(Err(queue_full()));
                        } else {
                            queue.push_back((id, request.operation));
                        }
                    }
                }
            }
            if !client.closed && Instant::now() >= client.deadline {
                if client.outgoing.is_some() {
                    client.closed = true;
                } else {
                    expired_or_disconnected += 1;
                    client.respond(Err(ProbeError::timeout()));
                }
            }
        }

        // Remove expired/cancelled requests before starting another job.
        queue.retain(|(id, _)| {
            clients
                .get(id)
                .is_some_and(|c| !c.closed && c.outgoing.is_none())
        });

        if active.as_ref().is_some_and(|job| job.handle.is_finished()) {
            let job = active.take().expect("active job exists");
            let result = job.handle.join().unwrap_or_else(|_| Err(panic_error()));
            #[cfg(feature = "p0-managed-helper")]
            supervisor.completed(&result);
            completed += 1;
            #[cfg(feature = "p0-managed-helper")]
            if let Some(batch) = job.batch {
                if result.as_ref().is_ok_and(|r| r["flushed"] == true) {
                    supervisor.source_progress();
                }
                crate::consumer::complete(batch, &result);
            }
            if let Some(client) = clients.get_mut(&job.client_id) {
                client.respond(result);
            }
        }

        if active.is_none() {
            if let Some((client_id, operation)) = queue.pop_front() {
                let operation_name = match &operation {
                    Operation::Engine => "engine",
                    Operation::Delay { .. } => "delay",
                    Operation::Ping => "ping",
                    Operation::Panic => "panic",
                    Operation::Abort => "abort",
                    Operation::Oom => "oom",
                    Operation::AddressSpaceProbe => "address_space_probe",
                    Operation::SourceApply { .. } => "source_apply",
                    Operation::SourceSearch { .. } => "source_search",
                    Operation::SourceRetrieve { .. } => "source_retrieve",
                };
                #[cfg(feature = "p0-managed-helper")]
                let Some(connection) = supervisor.connection() else {
                    if let Some(client) = clients.get_mut(&client_id) {
                        client.respond(Err(helper_unavailable()));
                    }
                    continue;
                };
                #[cfg(feature = "p0-managed-helper")]
                let handle = thread::Builder::new()
                    .name("pg_qdrant_p0_helper_io".into())
                    .spawn(move || crate::helper::execute(connection, operation))
                    .map_err(ProbeError::io)?;
                #[cfg(not(feature = "p0-managed-helper"))]
                let handle = thread::Builder::new()
                    .name("pg_qdrant_p0_engine".into())
                    .spawn(move || execute(operation))
                    .map_err(ProbeError::io)?;
                active = Some(Active {
                    client_id,
                    operation: operation_name,
                    started: Instant::now(),
                    handle,
                    batch: None,
                });
            }
        }

        #[cfg(feature = "p0-managed-helper")]
        if active.is_none() && process_status["engine_ready"] == true {
            if let Some(batch) = crate::consumer::next(
                process_status["engine_instance"]
                    .as_str()
                    .expect("ready owner identity"),
            ) {
                let connection = supervisor.connection().expect("ready helper");
                let operation = crate::consumer::operation(batch.clone());
                let handle = thread::Builder::new()
                    .name("pg_qdrant_source_io".into())
                    .spawn(move || crate::helper::execute(connection, operation))
                    .map_err(ProbeError::io)?;
                active = Some(Active {
                    client_id: 0,
                    operation: "source_apply",
                    started: Instant::now(),
                    handle,
                    batch: Some(batch),
                });
            }
        }

        for client in clients.values_mut() {
            client.flush();
        }
        clients.retain(|_, client| !client.closed);
        // A disconnected caller does not release an active engine owner. The
        // JoinHandle remains above until the actual native work has finished.
        if !BackgroundWorker::wait_latch(Some(Duration::from_millis(10))) {
            break;
        }
    }
    // Keep the OS ownership lock until process termination when native work is
    // still present. Dropping the lock first would admit a replacement owner
    // while the old process's detached engine thread could still be running.
    // This forced P0 shutdown is not a durable-index shutdown/replay contract.
    if active.is_some() {
        unsafe { pg_sys::proc_exit(0) };
    }
    drop(clients);
    drop(listener);
    drop(_cleanup);
    Ok(())
}

fn validate_request(request: Request) -> Result<Request, ProbeError> {
    if request.protocol_version != ipc::VERSION {
        return Err(ProbeError::invalid("unsupported P0 protocol version"));
    }
    if matches!(request.operation, Operation::SourceApply { .. }) {
        return Err(ProbeError::invalid("source mutations are worker-owned"));
    }
    if !(1..=ipc::MAX_TIMEOUT_MS as u64).contains(&request.timeout_ms) {
        return Err(ProbeError::invalid("P0 timeout outside accepted bounds"));
    }
    if let Operation::Delay { delay_ms } = request.operation {
        if delay_ms > 120_000 {
            return Err(ProbeError::invalid("P0 delay exceeds 120000 ms"));
        }
    }
    #[cfg(not(feature = "p0-fault-injection"))]
    if matches!(
        request.operation,
        Operation::Panic | Operation::Abort | Operation::Oom | Operation::AddressSpaceProbe
    ) {
        return Err(ProbeError::invalid(
            "fault operation is not compiled into this build",
        ));
    }
    Ok(request)
}

fn helper_unavailable() -> ProbeError {
    ProbeError::new(
        "worker_unavailable",
        "P0 engine helper is not ready",
        "Inspect p0_ping helper status and bounded restart attempts; no request was silently downgraded.",
    )
}

fn queue_full() -> ProbeError {
    ProbeError::new(
        "queue_full",
        "pg_qdrant P0 request queue is full",
        "Wait for the active owner task to finish before retrying.",
    )
}

fn panic_error() -> ProbeError {
    ProbeError::new(
        "engine_panic",
        "P0 engine thread panicked",
        "The probe failed; this is not a successful engine response or a durable index state.",
    )
}

/// No PostgreSQL calls, Datum values, SPI handles, or memory contexts here.
#[cfg(not(feature = "p0-managed-helper"))]
fn execute(operation: Operation) -> Result<Value, ProbeError> {
    std::panic::catch_unwind(|| match operation {
        Operation::Engine => pg_qdrant_edge_probe::run_smoke().map_err(|error| {
            ProbeError::new(
                "engine_error",
                error,
                "The real Edge probe failed; inspect its returned stage and server log.",
            )
        }),
        Operation::Delay { delay_ms } => {
            thread::sleep(Duration::from_millis(delay_ms));
            Ok(json!({"completed_delay_ms": delay_ms, "engine_probe": false}))
        }
        Operation::Ping => Err(ProbeError::invalid("ping belongs on the owner main thread")),
        Operation::SourceApply { .. }
        | Operation::SourceSearch { .. }
        | Operation::SourceRetrieve { .. }
        | Operation::AddressSpaceProbe => Err(ProbeError::invalid(
            "source indexing requires the managed helper build",
        )),
        #[cfg(feature = "p0-fault-injection")]
        Operation::Panic => panic!("intentional P0 engine-thread panic"),
        #[cfg(feature = "p0-fault-injection")]
        Operation::Abort => std::process::abort(),
        #[cfg(feature = "p0-fault-injection")]
        Operation::Oom => Ok(pg_qdrant_edge_probe::oom_probe::run("direct_worker")),
        #[cfg(not(feature = "p0-fault-injection"))]
        Operation::Panic | Operation::Abort | Operation::Oom => {
            Err(ProbeError::invalid("fault operation is disabled"))
        }
    })
    .unwrap_or_else(|_| Err(panic_error()))
}

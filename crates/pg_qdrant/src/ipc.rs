//! Versioned, bounded, owned-data protocol used only by the P0 experiments.

use pgrx::pg_sys;
use serde_json::Value;
use std::ffi::CStr;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub use pg_qdrant_protocol::*;

pub fn raise(error: ProbeError) -> ! {
    use pgrx::PgSqlErrorCode as Code;
    let sqlstate = match error.code.as_str() {
        "invalid_parameter" | "protocol_error" => Code::ERRCODE_INVALID_PARAMETER_VALUE,
        "timeout" => Code::ERRCODE_QUERY_CANCELED,
        "queue_full" => Code::ERRCODE_CONFIGURATION_LIMIT_EXCEEDED,
        "worker_unavailable" => Code::ERRCODE_OBJECT_NOT_IN_PREREQUISITE_STATE,
        _ => Code::ERRCODE_INTERNAL_ERROR,
    };
    pgrx::ereport!(ERROR, sqlstate, error.message, error.detail);
}

pub fn validate_timeout(timeout_ms: i32) -> Result<Duration, ProbeError> {
    if !(1..=MAX_TIMEOUT_MS).contains(&timeout_ms) {
        return Err(ProbeError::invalid(
            "timeout_ms must be between 1 and 120000",
        ));
    }
    Ok(Duration::from_millis(timeout_ms as u64))
}

/// Called only from a PostgreSQL process's main thread. No PG pointer escapes.
pub fn directory() -> Result<PathBuf, ProbeError> {
    // PostgreSQL initializes DataDir before a backend or worker enters our code.
    let raw = unsafe { pg_sys::DataDir };
    if raw.is_null() {
        return Err(ProbeError::new(
            "worker_unavailable",
            "PostgreSQL data directory is unavailable",
            "Run the P0 probe in a connected PostgreSQL backend.",
        ));
    }
    let owned = unsafe { CStr::from_ptr(raw) }.to_bytes().to_vec();
    let path = PathBuf::from(std::ffi::OsString::from_vec(owned));
    Ok(path.join("pg_qdrant_p0"))
}

use std::os::unix::ffi::OsStringExt;

pub fn socket_path(database_oid: u32) -> Result<PathBuf, ProbeError> {
    Ok(directory()?.join(format!("db-{database_oid}.sock")))
}

/// Interruptible wait on the SQL backend thread, never on an engine thread.
pub fn pause_postgres() {
    pgrx::check_for_interrupts!();
    unsafe {
        let flags = pg_sys::WaitLatch(
            pg_sys::MyLatch,
            (pg_sys::WL_LATCH_SET | pg_sys::WL_TIMEOUT | pg_sys::WL_POSTMASTER_DEATH) as i32,
            10,
            pg_sys::PG_WAIT_EXTENSION,
        );
        pg_sys::ResetLatch(pg_sys::MyLatch);
        if flags & pg_sys::WL_POSTMASTER_DEATH as i32 != 0 {
            raise(ProbeError::new(
                "worker_unavailable",
                "PostgreSQL postmaster exited",
                "Reconnect after PostgreSQL recovery finishes.",
            ));
        }
    }
    pgrx::check_for_interrupts!();
}

/// Nonblocking connect is essential: a full Unix-socket backlog must not hide
/// statement_timeout or pg_cancel_backend behind a blocking system call.
pub fn connect(path: &Path, deadline: Instant) -> Result<UnixStream, ProbeError> {
    let bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    if bytes.len() >= address.sun_path.len() {
        return Err(ProbeError::invalid(
            "PostgreSQL data-directory path is too long for the P0 Unix socket",
        ));
    }
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (to, from) in address.sun_path.iter_mut().zip(bytes) {
        *to = *from as libc::c_char;
    }
    let address_len =
        (std::mem::offset_of!(libc::sockaddr_un, sun_path) + bytes.len() + 1) as libc::socklen_t;
    loop {
        if Instant::now() >= deadline {
            return Err(ProbeError::timeout());
        }
        let raw = unsafe {
            libc::socket(
                libc::AF_UNIX,
                libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
                0,
            )
        };
        if raw < 0 {
            return Err(ProbeError::io(io::Error::last_os_error()));
        }
        let fd = unsafe { OwnedFd::from_raw_fd(raw) };
        let status =
            unsafe { libc::connect(fd.as_raw_fd(), (&raw const address).cast(), address_len) };
        if status == 0 {
            return Ok(UnixStream::from(fd));
        }
        let error = io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::EAGAIN) => pause_postgres(),
            Some(libc::EINPROGRESS) => loop {
                if Instant::now() >= deadline {
                    return Err(ProbeError::timeout());
                }
                let mut descriptor = libc::pollfd {
                    fd: fd.as_raw_fd(),
                    events: libc::POLLOUT,
                    revents: 0,
                };
                let ready = unsafe { libc::poll(&mut descriptor, 1, 0) };
                if ready < 0 {
                    return Err(ProbeError::io(io::Error::last_os_error()));
                }
                if ready > 0 {
                    let mut code: libc::c_int = 0;
                    let mut length = std::mem::size_of_val(&code) as libc::socklen_t;
                    let result = unsafe {
                        libc::getsockopt(
                            fd.as_raw_fd(),
                            libc::SOL_SOCKET,
                            libc::SO_ERROR,
                            (&raw mut code).cast(),
                            &mut length,
                        )
                    };
                    if result < 0 {
                        return Err(ProbeError::io(io::Error::last_os_error()));
                    }
                    if code != 0 {
                        return Err(ProbeError::io(io::Error::from_raw_os_error(code)));
                    }
                    return Ok(UnixStream::from(fd));
                }
                pause_postgres();
            },
            _ => return Err(ProbeError::io(error)),
        }
    }
}

pub fn call(operation: Operation, timeout_ms: i32) -> Result<Value, ProbeError> {
    let deadline = Instant::now() + validate_timeout(timeout_ms)?;
    // OID is an integer copied out on the PostgreSQL backend thread.
    let database_oid = unsafe { pg_sys::MyDatabaseId.to_u32() };
    let mut stream = connect(&socket_path(database_oid)?, deadline)?;
    let request = Request {
        protocol_version: VERSION,
        timeout_ms: deadline
            .saturating_duration_since(Instant::now())
            .as_millis()
            .max(1) as u64,
        operation,
    };
    let mut bytes = serde_json::to_vec(&request).map_err(|e| {
        ProbeError::new(
            "protocol_error",
            e.to_string(),
            "P0 request serialization failed.",
        )
    })?;
    bytes.push(b'\n');
    if bytes.len() > REQUEST_BYTES {
        return Err(ProbeError::invalid("P0 request exceeds the byte budget"));
    }
    let mut offset = 0;
    let mut received = Vec::new();
    loop {
        pgrx::check_for_interrupts!();
        if Instant::now() >= deadline {
            return Err(ProbeError::timeout());
        }
        if offset < bytes.len() {
            match stream.write(&bytes[offset..]) {
                Ok(0) => {
                    return Err(ProbeError::io(io::Error::new(
                        io::ErrorKind::WriteZero,
                        "worker socket closed",
                    )));
                }
                Ok(size) => offset += size,
                Err(e)
                    if e.kind() == io::ErrorKind::WouldBlock
                        || e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => return Err(ProbeError::io(e)),
            }
        }
        let mut chunk = [0_u8; 8192];
        match stream.read(&mut chunk) {
            Ok(0) => {
                return Err(ProbeError::io(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "worker disconnected before a complete response",
                )));
            }
            Ok(size) => {
                received.extend_from_slice(&chunk[..size]);
                if received.len() > RESPONSE_BYTES {
                    return Err(ProbeError::invalid("P0 response exceeded its byte budget"));
                }
                if received.last() == Some(&b'\n') {
                    let envelope: Value = serde_json::from_slice(&received).map_err(|e| {
                        ProbeError::new(
                            "protocol_error",
                            e.to_string(),
                            "P0 response was not valid JSON.",
                        )
                    })?;
                    if envelope.get("protocol_version").and_then(Value::as_u64)
                        != Some(VERSION as u64)
                    {
                        return Err(ProbeError::new(
                            "protocol_error",
                            "P0 protocol version mismatch",
                            "Restart the worker after replacing the extension binary.",
                        ));
                    }
                    if let Some(error) = envelope.get("error") {
                        return Err(serde_json::from_value(error.clone()).map_err(|e| {
                            ProbeError::new(
                                "protocol_error",
                                e.to_string(),
                                "Invalid worker error envelope.",
                            )
                        })?);
                    }
                    return envelope.get("result").cloned().ok_or_else(|| {
                        ProbeError::new(
                            "protocol_error",
                            "P0 response is missing result",
                            "Inspect worker logs.",
                        )
                    });
                }
            }
            Err(e)
                if e.kind() == io::ErrorKind::WouldBlock
                    || e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(ProbeError::io(e)),
        }
        pause_postgres();
    }
}

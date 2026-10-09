//! P0 exec-isolation comparison. No PostgreSQL symbols or shared memory.

use pg_qdrant_protocol::{HelperRequest, Operation, ProbeError, VERSION};
use serde_json::{Value, json};
use std::fs::OpenOptions;
use std::io::{self, BufRead, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;
mod lexical;
mod resources;
mod source;

fn main() {
    if let Err(error) = run() {
        eprintln!("pg_qdrant P0 helper: {} ({})", error.message, error.code);
        std::process::exit(70);
    }
}

fn run() -> Result<(), ProbeError> {
    let resource_limits = resources::install()?;
    pg_qdrant_edge_probe::cpu::require().map_err(|message| {
        ProbeError::new(
            "cpu_unsupported",
            message,
            "Use a host satisfying the fixed P0 native CPU baseline before starting the helper.",
        )
    })?;
    let mut arguments = std::env::args_os().skip(1);
    let owner_path = arguments
        .next()
        .ok_or_else(|| ProbeError::invalid("missing engine-owner path"))?;
    if arguments.next().is_some() {
        return Err(ProbeError::invalid("unexpected helper argument"));
    }
    let owner = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(&owner_path)
        .map_err(ProbeError::io)?;
    owner.try_lock().map_err(|e| {
        ProbeError::new(
            "engine_owner_busy",
            e.to_string(),
            "A previous helper still owns this database's P0 engine generation.",
        )
    })?;
    // Native work must never outlive the OS fence, including errors or unwinds.
    let _process_lifetime_owner = std::mem::ManuallyDrop::new(owner);
    let (sender, receiver) = mpsc::sync_channel(1);
    thread::Builder::new()
        .name("pgq_supervisor_watch".into())
        .spawn(move || {
            let mut input = io::BufReader::new(io::stdin());
            loop {
                let mut bytes = Vec::new();
                match input
                    .by_ref()
                    .take((pg_qdrant_protocol::CONSUMER_REQUEST_BYTES + 1) as u64)
                    .read_until(b'\n', &mut bytes)
                {
                    // The supervisor alone owns the write end. EOF terminates every
                    // native thread immediately, even during an uninterruptible call.
                    Ok(0) => std::process::exit(0),
                    Ok(_)
                        if bytes.len() <= pg_qdrant_protocol::CONSUMER_REQUEST_BYTES
                            && bytes.last() == Some(&b'\n') => {}
                    _ => std::process::exit(65),
                }
                let request = match serde_json::from_slice::<HelperRequest>(&bytes) {
                    Ok(request) => request,
                    Err(_) => std::process::exit(65),
                };
                if bytes.len() > request.operation.request_byte_limit() {
                    std::process::exit(65);
                }
                // Never block the EOF watchdog behind a native operation.
                if sender.try_send(request).is_err() {
                    std::process::exit(65);
                }
            }
        })
        .map_err(ProbeError::io)?;

    let pid = std::process::id();
    // A PID can be reused. Each execution receives a fresh owner identity.
    let mut nonce = [0_u8; 16];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut random| random.read_exact(&mut nonce))
        .map_err(ProbeError::io)?;
    let owner_instance: String = nonce.iter().map(|byte| format!("{byte:02x}")).collect();
    respond(
        0,
        pid,
        Ok(json!({"ready": true, "protocol_version": VERSION,
        "source_contract_version": pg_qdrant_protocol::SOURCE_CONTRACT_VERSION,
        "helper_version": env!("CARGO_PKG_VERSION"),
        "fault_injection": cfg!(feature="p0-fault-injection"), "owner_instance": owner_instance,
        "resource_limits": resource_limits})),
    )?;
    let mut previous_id = 0;
    let mut source_owner =
        source::SourceOwner::new(std::path::PathBuf::from(owner_path).with_extension("indexes"))?;
    while let Ok(request) = receiver.recv() {
        if request.protocol_version != VERSION || request.request_id <= previous_id {
            return Err(ProbeError::invalid(
                "helper request identity or protocol mismatch",
            ));
        }
        previous_id = request.request_id;
        let result = match request.operation {
            Operation::SourceApply { batch } => source_owner.apply(batch),
            Operation::SourceSearch {
                index_id,
                generation,
                storage_epoch,
                q,
                top_k,
                representation_query,
                recommendation_query,
                rerank_query,
                fusion,
                predicates,
            } => source_owner.search(
                index_id,
                &generation,
                &storage_epoch,
                &q,
                top_k,
                representation_query,
                recommendation_query,
                rerank_query,
                fusion,
                predicates,
            ),
            op => execute(op),
        };
        respond(request.request_id, pid, result)?;
    }
    Ok(())
}

fn respond(request_id: u64, pid: u32, result: Result<Value, ProbeError>) -> Result<(), ProbeError> {
    let bytes = pg_qdrant_protocol::encode_helper_response(request_id, pid, result);
    let mut output = io::stdout().lock();
    output.write_all(&bytes).map_err(ProbeError::io)?;
    output.flush().map_err(ProbeError::io)
}

fn execute(operation: Operation) -> Result<Value, ProbeError> {
    std::panic::catch_unwind(|| match operation {
        Operation::Engine => pg_qdrant_edge_probe::run_smoke().map_err(|e| {
            ProbeError::new(
                "engine_error",
                e,
                "The real Edge probe failed inside the managed helper.",
            )
        }),
        Operation::Delay { delay_ms } if delay_ms <= 120_000 => {
            thread::sleep(Duration::from_millis(delay_ms));
            Ok(json!({"completed_delay_ms": delay_ms, "engine_probe": false}))
        }
        #[cfg(feature = "p0-fault-injection")]
        Operation::Panic => panic!("intentional P0 managed-helper engine panic"),
        #[cfg(feature = "p0-fault-injection")]
        Operation::Abort => std::process::abort(),
        #[cfg(feature = "p0-fault-injection")]
        Operation::Oom => Ok(pg_qdrant_edge_probe::oom_probe::run("managed_helper")),
        #[cfg(feature = "p0-fault-injection")]
        Operation::AddressSpaceProbe => resources::probe(),
        _ => Err(ProbeError::invalid(
            "unsupported helper operation in this build",
        )),
    })
    .unwrap_or_else(|_| {
        Err(ProbeError::new(
            "engine_panic",
            "P0 helper engine panicked",
            "The failed request is not a successful engine response or durable index state.",
        ))
    })
}

//! Process limits apply only after exec, never to a PostgreSQL backend.
use pg_qdrant_protocol::{HELPER_ADDRESS_SPACE_BYTES, HELPER_MIN_ADDRESS_SPACE_BYTES, ProbeError};
use serde_json::{Value, json};
use std::io;

fn address_space() -> Result<libc::rlimit, ProbeError> {
    let mut limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    // A valid writable local structure; no PostgreSQL or borrowed engine state.
    if unsafe { libc::getrlimit(libc::RLIMIT_AS, &mut limit) } != 0 {
        return Err(ProbeError::io(io::Error::last_os_error()));
    }
    Ok(limit)
}

pub fn install() -> Result<Value, ProbeError> {
    let inherited = address_space()?;
    // Never raise either inherited limit, including a tighter administrator cap.
    let bytes = HELPER_ADDRESS_SPACE_BYTES
        .min(inherited.rlim_cur)
        .min(inherited.rlim_max);
    if bytes < HELPER_MIN_ADDRESS_SPACE_BYTES {
        return Err(ProbeError::new(
            "helper_resource_limit",
            "Inherited address space below 512 MiB",
            "Use a supported helper address-space budget; startup remains refused.",
        ));
    }
    let desired = libc::rlimit {
        rlim_cur: bytes,
        rlim_max: bytes,
    };
    if unsafe { libc::setrlimit(libc::RLIMIT_AS, &desired) } != 0 {
        return Err(ProbeError::io(io::Error::last_os_error()));
    }
    let observed = address_space()?;
    if observed.rlim_cur != bytes || observed.rlim_max != bytes {
        return Err(ProbeError::invalid("Kernel address-space limit mismatch"));
    }
    Ok(
        json!({"address_space_limit_enforced":true,"address_space_soft_bytes":observed.rlim_cur,
       "address_space_hard_bytes":observed.rlim_max,"scope":"helper process virtual address space including mmap",
       "rss_limit_enforced":false,"work_mem_is_edge_limit":false}),
    )
}

#[cfg(feature = "p0-fault-injection")]
pub fn probe() -> Result<Value, ProbeError> {
    let limit = address_space()?;
    let requested = usize::try_from(limit.rlim_cur)
        .ok()
        .and_then(|n| n.checked_add(4096))
        .ok_or_else(|| ProbeError::invalid("Address-space probe overflow"))?;
    // No physical pages are touched. A mapping larger than the total budget
    // must fail even on a host with ample free memory and permissive overcommit.
    let ptr = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            requested,
            libc::PROT_NONE,
            libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
            -1,
            0,
        )
    };
    if ptr != libc::MAP_FAILED {
        unsafe {
            libc::munmap(ptr, requested);
        }
        return Err(ProbeError::invalid("Oversized mmap unexpectedly admitted"));
    }
    let errno = io::Error::last_os_error().raw_os_error();
    if errno != Some(libc::ENOMEM) {
        return Err(ProbeError::invalid("Oversized mmap did not return ENOMEM"));
    }
    Ok(
        json!({"address_space_soft_bytes":limit.rlim_cur,"address_space_hard_bytes":limit.rlim_max,
        "requested_mapping_bytes":requested,"mapping_refused":true,"errno":errno,
        "physical_pages_touched":0,"kernel_oom":false}),
    )
}

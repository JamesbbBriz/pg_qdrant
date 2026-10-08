//! P0 only: an actual PostgreSQL-to-Edge experiment, not the indexing product.
//!
//! The dynamic PostgreSQL worker owns a bounded request queue. It sends owned
//! Rust data to at most one engine thread. Engine threads never call PostgreSQL.

use pgrx::prelude::*;

mod formats;
#[cfg(feature = "p0-managed-helper")]
mod helper;
#[cfg(feature = "p0-managed-helper")]
mod helper_child;
mod identity;
mod ipc;
mod recheck;
mod worker;

::pgrx::pg_module_magic!();

#[pg_schema]
mod qdrant {
    use pgrx::JsonB;
    use pgrx::prelude::*;
    use serde_json::json;

    /// Build facts only. A compiled probe is not a supported indexing API.
    #[pg_extern(volatile, parallel_unsafe)]
    fn build_info() -> JsonB {
        JsonB(json!({
            "schema_version": 1,
            "extension_version": env!("CARGO_PKG_VERSION"),
            "stage": "P0_feasibility",
            "engine": {"name": "qdrant-edge", "version": "0.8.0"},
            "pgrx_version": "0.19.3",
            "postgres_major": 17,
            "features": {"pg17": cfg!(feature="pg17"), "cshim": true,
                "p0_managed_helper": cfg!(feature="p0-managed-helper"),
                "p0_fault_injection": cfg!(feature="p0-fault-injection")},
            "product_indexing_api": false,
            "release_supported_capabilities": [],
            "native_engine_cancellation": false,
            "prototype_process": if cfg!(feature="p0-managed-helper") {
                "postgresql_supervisor_with_exec_helper"
            } else { "dynamic_postgresql_worker_with_engine_thread" }
        }))
    }

    /// Preserve all 54 product requirements without promoting a probe to support.
    #[pg_extern(volatile, parallel_unsafe)]
    fn capabilities(index_name: default!(Option<&str>, "NULL")) -> JsonB {
        if index_name.is_some() {
            pgrx::ereport!(
                ERROR,
                pgrx::PgSqlErrorCode::ERRCODE_FEATURE_NOT_SUPPORTED,
                "index-specific capability discovery is not available in the P0 prototype",
                "Call qdrant.capabilities() to inspect the retained product requirements."
            );
        }
        let mut ids = Vec::new();
        for (prefix, count) in [("F", 20), ("V", 8), ("Q", 14), ("L", 12)] {
            for number in 1..=count {
                ids.push(json!({
                    "id": format!("{prefix}{number:02}"),
                    "product_status": "planned",
                    "release_supported": false,
                    "sql_product_interface": false
                }));
            }
        }
        JsonB(
            json!({"registry_schema_version": 1, "stage": "P0_feasibility",
            "index_catalog_available": false, "capabilities": ids}),
        )
    }
}

#[pg_schema]
mod qdrant_internal {
    use super::{formats, identity, ipc, recheck, worker};
    use pgrx::JsonB;
    use pgrx::prelude::*;

    /// Fixture-only source recheck; no production authorization or capture.
    #[pg_extern(volatile, parallel_unsafe)]
    fn p0_recheck_candidates(
        source: Option<pgrx::PgRelation>,
        key_field: Option<&str>,
        candidates: Option<JsonB>,
    ) -> JsonB {
        require_superuser();
        recheck::recheck(source, key_field, candidates)
    }

    /// Tagged identity encoding, without source reads or point-ID allocation.
    #[pg_extern(volatile, parallel_unsafe)]
    fn p0_identity_roundtrip(
        bigint_key: Option<i64>,
        uuid_key: Option<pgrx::Uuid>,
        text_key: Option<&str>,
    ) -> JsonB {
        require_superuser();
        identity::roundtrip(bigint_key, uuid_key, text_key)
    }

    /// Candidate SQL types only: backend validation and owned JSON roundtrip.
    #[pg_extern(volatile, parallel_unsafe)]
    fn p0_vector_formats(
        dense: Option<Array<'_, f32>>,
        sparse_indices: Option<Array<'_, i64>>,
        sparse_weights: Option<Array<'_, f32>>,
        tokens: Option<Array<'_, f32>>,
    ) -> JsonB {
        require_superuser();
        formats::roundtrip(dense, sparse_indices, sparse_weights, tokens)
    }

    #[pg_extern(volatile, parallel_unsafe)]
    fn p0_start_worker(timeout_ms: default!(i32, 5000)) -> JsonB {
        require_superuser();
        JsonB(worker::start(timeout_ms).unwrap_or_else(|e| ipc::raise(e)))
    }

    #[pg_extern(volatile, parallel_unsafe)]
    fn p0_ping(timeout_ms: default!(i32, 1000)) -> JsonB {
        require_superuser();
        JsonB(ipc::call(ipc::Operation::Ping, timeout_ms).unwrap_or_else(|e| ipc::raise(e)))
    }

    /// Run the real Edge smoke suite in the owner process, never in this backend.
    #[pg_extern(volatile, parallel_unsafe)]
    fn p0_engine_probe(timeout_ms: default!(i32, 120000)) -> JsonB {
        require_superuser();
        JsonB(ipc::call(ipc::Operation::Engine, timeout_ms).unwrap_or_else(|e| ipc::raise(e)))
    }

    /// An owned-data delay makes queue saturation/cancellation reproducible.
    #[pg_extern(volatile, parallel_unsafe)]
    fn p0_delay(delay_ms: i32, timeout_ms: default!(i32, 5000)) -> JsonB {
        require_superuser();
        if !(0..=120_000).contains(&delay_ms) {
            ipc::raise(ipc::ProbeError::invalid(
                "delay_ms must be between 0 and 120000",
            ));
        }
        JsonB(
            ipc::call(
                ipc::Operation::Delay {
                    delay_ms: delay_ms as u64,
                },
                timeout_ms,
            )
            .unwrap_or_else(|e| ipc::raise(e)),
        )
    }

    /// Available only in explicitly compiled, disposable fault-test binaries.
    #[cfg(feature = "p0-fault-injection")]
    #[pg_extern(volatile, parallel_unsafe)]
    fn p0_fault(kind: &str, timeout_ms: default!(i32, 5000)) -> JsonB {
        require_superuser();
        let operation = match kind {
            "panic" => ipc::Operation::Panic,
            "abort" => ipc::Operation::Abort,
            "oom" => ipc::Operation::Oom,
            _ => ipc::raise(ipc::ProbeError::invalid(
                "fault kind must be panic, abort or oom",
            )),
        };
        JsonB(ipc::call(operation, timeout_ms).unwrap_or_else(|e| ipc::raise(e)))
    }

    fn require_superuser() {
        // This check is executed on the calling PostgreSQL backend thread.
        if !unsafe { pg_sys::superuser() } {
            pgrx::ereport!(
                ERROR,
                pgrx::PgSqlErrorCode::ERRCODE_INSUFFICIENT_PRIVILEGE,
                "pg_qdrant P0 experiments require a PostgreSQL superuser",
                "P0 diagnostics do not grant access to a production search index."
            );
        }
    }
}

pgrx::extension_sql!(
    r#"
GRANT USAGE ON SCHEMA qdrant TO PUBLIC;
REVOKE ALL ON SCHEMA qdrant_internal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA qdrant_internal FROM PUBLIC;
"#,
    name = "p0_privileges",
    finalize
);

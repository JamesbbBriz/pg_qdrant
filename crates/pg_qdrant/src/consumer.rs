//! SPI stays on the PostgreSQL worker thread; native work owns only Rust data.
use crate::ipc::{Operation, ProbeError, SourceBatch};
use pgrx::{JsonB, Spi, bgworkers::BackgroundWorker};
use serde_json::{Value, json};

/// Hold the extension's shared object lock for this PostgreSQL transaction.
/// DROP cannot remove a function or ledger relation under an admitted SPI call.
fn installed() -> bool {
    unsafe {
        let oid = pgrx::pg_sys::get_extension_oid(c"pg_qdrant".as_ptr(), true);
        if oid == pgrx::pg_sys::InvalidOid {
            return false;
        }
        pgrx::pg_sys::LockDatabaseObject(
            pgrx::pg_sys::ExtensionRelationId,
            oid,
            0,
            pgrx::pg_sys::AccessShareLock as i32,
        );
        // DROP might have completed between lookup and lock acquisition.
        pgrx::pg_sys::get_extension_oid(c"pg_qdrant".as_ptr(), true) == oid
    }
}

pub fn reset(instance: &str) {
    BackgroundWorker::transaction(|| {
        if installed() {
            Spi::run_with_args(
                "SELECT qdrant_internal.consumer_reset($1)",
                &[instance.into()],
            )
            .expect("consumer reset")
        }
    });
}

pub fn next(instance: &str) -> Option<SourceBatch> {
    BackgroundWorker::transaction(|| {
        if !installed() {
            return None;
        }
        Spi::get_one_with_args::<JsonB>("SELECT qdrant_internal.next_batch($1)", &[instance.into()])
            .expect("select committed source batch")
            .map(|v| {
                let mut batch: SourceBatch =
                    serde_json::from_value(v.0).expect("owned source batch contract");
                if serde_json::to_vec(&batch).expect("batch bytes").len() + 256
                    > pg_qdrant_protocol::CONSUMER_REQUEST_BYTES
                {
                    batch.events.truncate(1);
                }
                batch
            })
    })
}

pub fn complete(batch: SourceBatch, result: &Result<Value, ProbeError>) {
    let ids: Vec<u64> = batch.events.iter().map(|e| e.event_id).collect();
    let receipt = result.as_ref().ok().filter(|r| {
        r["flushed"] == true
            && r["source_contract_version"] == pg_qdrant_protocol::SOURCE_CONTRACT_VERSION
            && r["generation"] == batch.generation
            && r["storage_epoch"] == batch.storage_epoch
            && r["consumer_id"] == batch.consumer_id
            && r["event_ids"] == json!(ids)
    });
    BackgroundWorker::transaction(|| {
        if !installed() {
            return;
        }
        let argument = JsonB(serde_json::to_value(&batch).expect("batch JSON"));
        if receipt.is_some() {
            Spi::run_with_args("SELECT qdrant_internal.ack_batch($1)", &[argument.into()])
                .expect("exact event ACK");
        } else {
            let message = JsonB(
                json!({"index_id":batch.index_id,"epoch":batch.storage_epoch,
                "error":result.as_ref().err().map(|e|e.message.as_str()).unwrap_or("invalid flush receipt")}),
            );
            Spi::run_with_args("UPDATE qdrant_internal.consumer_state c SET state='failed',last_error=$1->>'error' \
              FROM (SELECT index_name FROM qdrant_internal.index_catalog \
                WHERE index_id=($1->>'index_id')::bigint FOR KEY SHARE SKIP LOCKED) i \
              WHERE i.index_name=c.index_name AND c.storage_epoch::text=$1->>'epoch'",&[message.into()])
              .expect("persist consumer failure");
        }
    });
}

pub fn operation(batch: SourceBatch) -> Operation {
    Operation::SourceApply { batch }
}

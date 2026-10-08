//! SPI stays on the PostgreSQL worker thread; native work owns only Rust data.
use crate::ipc::{Operation, ProbeError, SourceBatch};
use pgrx::{JsonB, Spi, bgworkers::BackgroundWorker};
use serde_json::{Value, json};

pub fn reset() {
    BackgroundWorker::transaction(|| {
        Spi::run("SELECT qdrant_internal.consumer_reset()").expect("consumer reset")
    });
}

pub fn next() -> Option<SourceBatch> {
    BackgroundWorker::transaction(|| {
        Spi::get_one::<JsonB>("SELECT qdrant_internal.next_batch()")
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
            && r["generation"] == batch.generation
            && r["storage_epoch"] == batch.storage_epoch
            && r["consumer_id"] == batch.consumer_id
            && r["event_ids"] == json!(ids)
    });
    BackgroundWorker::transaction(|| {
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
              FROM qdrant_internal.index_catalog i WHERE i.index_name=c.index_name \
              AND i.index_id=($1->>'index_id')::bigint AND c.storage_epoch::text=$1->>'epoch'",&[message.into()])
              .expect("persist consumer failure");
        }
    });
}

pub fn operation(batch: SourceBatch) -> Operation {
    Operation::SourceApply { batch }
}

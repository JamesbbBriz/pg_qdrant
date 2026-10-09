-- Cancelled/failed builds retain their outcome while native cleanup progresses.
ALTER TABLE qdrant_internal.generation_reservations
 ADD COLUMN abandoned_state text NOT NULL DEFAULT 'queued' CHECK(abandoned_state IN ('queued','running','cleaned','failed')),
 ADD COLUMN abandoned_cleanup_error text;

CREATE FUNCTION qdrant_internal.next_abandoned_batch(p_instance text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE j qdrant_internal.generation_reservations%ROWTYPE; i qdrant_internal.index_catalog%ROWTYPE;
BEGIN
 SELECT * INTO j FROM qdrant_internal.generation_reservations WHERE state IN ('failed','cancelled')
   AND abandoned_state IN ('queued','running') ORDER BY updated_at LIMIT 1 FOR NO KEY UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT * INTO i FROM qdrant_internal.index_catalog WHERE index_id=j.index_id FOR KEY SHARE SKIP LOCKED;
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF i.generation=j.generation OR (j.engine_instance IS NOT NULL AND j.engine_instance<>p_instance) THEN
   UPDATE qdrant_internal.generation_reservations SET abandoned_state='failed',
     abandoned_cleanup_error='serving_or_owner_changed; preserve storage',updated_at=clock_timestamp() WHERE task_id=j.task_id;
   RETURN NULL;
 END IF;
 IF j.engine_instance IS NULL THEN
   UPDATE qdrant_internal.generation_reservations SET abandoned_state='cleaned',updated_at=clock_timestamp() WHERE task_id=j.task_id;
   RETURN NULL;
 END IF;
 UPDATE qdrant_internal.generation_reservations SET abandoned_state='running',updated_at=clock_timestamp() WHERE task_id=j.task_id;
 RETURN jsonb_build_object('source_contract_version',8,'task_id',j.task_id,'retire',true,'index_id',i.index_id,
   'generation',j.generation,'storage_epoch',j.storage_epoch,'consumer_id',j.consumer_id,
   'representations','{}'::jsonb,'events','[]'::jsonb);
END $$;

ALTER FUNCTION qdrant_internal.ack_retirement_batch(jsonb) RENAME TO ack_lifecycle_retirement;
CREATE FUNCTION qdrant_internal.ack_retirement_batch(p_batch jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE t uuid;
BEGIN
 UPDATE qdrant_internal.generation_reservations SET abandoned_state='cleaned',abandoned_cleanup_error=NULL
 WHERE task_id::text=p_batch->>'task_id' AND index_id=(p_batch->>'index_id')::bigint
   AND state IN ('failed','cancelled') AND abandoned_state='running'
   AND generation::text=p_batch->>'generation' AND storage_epoch::text=p_batch->>'storage_epoch'
   AND consumer_id::text=p_batch->>'consumer_id' RETURNING task_id INTO t;
 IF FOUND THEN
   DELETE FROM qdrant_internal.prior_epochs WHERE index_id=(p_batch->>'index_id')::bigint
     AND generation::text=p_batch->>'generation' AND storage_epoch::text=p_batch->>'storage_epoch';
 ELSE PERFORM qdrant_internal.ack_lifecycle_retirement(p_batch); END IF;
END $$;

ALTER FUNCTION qdrant_internal.fail_batch(jsonb) RENAME TO fail_lifecycle_batch;
CREATE FUNCTION qdrant_internal.fail_batch(p_error jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF coalesce((p_error->>'retire')::boolean,false) THEN
   UPDATE qdrant_internal.generation_reservations SET abandoned_state='failed',
     abandoned_cleanup_error=left(p_error->>'error',4096),updated_at=clock_timestamp()
   WHERE task_id::text=p_error->>'task_id' AND index_id=(p_error->>'index_id')::bigint
     AND storage_epoch::text=p_error->>'epoch' AND state IN ('failed','cancelled') AND abandoned_state='running';
   IF FOUND THEN RETURN; END IF;
 END IF;
 PERFORM qdrant_internal.fail_lifecycle_batch(p_error);
END $$;

ALTER FUNCTION qdrant.task_status(uuid) SET SCHEMA qdrant_internal;
ALTER FUNCTION qdrant_internal.task_status(uuid) RENAME TO lifecycle_task_status;
CREATE FUNCTION qdrant.task_status(p_task uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb; j qdrant_internal.generation_reservations%ROWTYPE; prior bigint;
BEGIN
 result:=qdrant_internal.lifecycle_task_status(p_task); -- checks original owner
 SELECT * INTO j FROM qdrant_internal.generation_reservations WHERE task_id=p_task;
 IF NOT FOUND OR j.state NOT IN ('failed','cancelled') THEN RETURN result; END IF;
 SELECT count(*) INTO prior FROM qdrant_internal.prior_epochs WHERE native_task=p_task;
 RETURN result||jsonb_build_object('abandoned_cleanup_state',CASE WHEN j.abandoned_state='cleaned' AND prior>0 THEN 'failed' ELSE j.abandoned_state END,
   'abandoned_current_epoch_cleaned',j.abandoned_state='cleaned','abandoned_storage_cleanup',j.abandoned_state='cleaned' AND prior=0,
   'abandoned_pending_epochs',prior+CASE WHEN j.abandoned_state='cleaned' THEN 0 ELSE 1 END,
   'abandoned_cleanup_error',CASE WHEN prior>0 THEN 'prior_owner_epochs_preserved' ELSE j.abandoned_cleanup_error END);
END $$;

CREATE FUNCTION qdrant.await_task(p_task uuid,p_timeout_ms integer,p_cleanup boolean) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb; deadline timestamptz;
BEGIN
 IF p_timeout_ms IS NULL OR p_timeout_ms NOT BETWEEN 0 AND 120000 OR p_cleanup IS NULL THEN
   RAISE EXCEPTION 'Invalid task wait options' USING ERRCODE='22023'; END IF;
 IF NOT p_cleanup THEN RETURN qdrant.await_task(p_task,p_timeout_ms); END IF;
 result:=qdrant.await_task(p_task,0); -- snapshot, owner and own-uncommitted checks
 IF result->>'kind'='drop' THEN RETURN qdrant.await_task(p_task,p_timeout_ms); END IF;
 IF result ? 'drop_task' THEN RAISE EXCEPTION 'Archived build cleanup belongs to the returned drop task' USING ERRCODE='55000'; END IF;
 deadline:=clock_timestamp()+p_timeout_ms*interval '1 millisecond';
 LOOP
   result:=qdrant.task_status(p_task);
   IF (result->>'completed')::boolean AND ((result->>'state'='succeeded' AND
       (result->>'retired_generation' IS NULL OR (result->>'retired_storage_cleanup')::boolean OR result->>'cleanup_state'='failed'))
      OR result->>'abandoned_cleanup_state' IN ('cleaned','failed')) THEN RETURN result; END IF;
   IF clock_timestamp()>=deadline THEN RETURN result||jsonb_build_object('timed_out',true,'cleanup_timed_out',true); END IF;
   PERFORM pg_sleep(0.01);
 END LOOP;
END $$;

ALTER FUNCTION qdrant_internal.next_batch(text) RENAME TO next_lifecycle_batch;
CREATE FUNCTION qdrant_internal.next_batch(p_instance text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 IF EXISTS(SELECT 1 FROM qdrant_internal.generation_reservations WHERE state IN ('failed','cancelled')
   AND abandoned_state IN ('queued','running') AND updated_at<clock_timestamp()-interval '100 milliseconds') THEN
   result:=qdrant_internal.next_abandoned_batch(p_instance);
   IF result IS NOT NULL THEN RETURN result; END IF;
 END IF;
 result:=qdrant_internal.next_lifecycle_batch(p_instance);
 IF result IS NOT NULL THEN RETURN result; END IF;
 RETURN qdrant_internal.next_abandoned_batch(p_instance);
END $$;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA qdrant_internal FROM PUBLIC;
GRANT EXECUTE ON FUNCTION qdrant.task_status(uuid),qdrant.await_task(uuid,integer,boolean) TO PUBLIC;

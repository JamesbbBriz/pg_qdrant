-- Logical DROP is transactional; native ownership is retired after commit.
CREATE TABLE qdrant_internal.drop_tasks (
 task_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), index_id bigint NOT NULL,
 index_name text NOT NULL, owner_oid oid NOT NULL, source_oid oid NOT NULL,
 requested_xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','succeeded','failed')),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(), last_error text, last_error_code text
);
CREATE TABLE qdrant_internal.drop_epochs (
 drop_task uuid NOT NULL REFERENCES qdrant_internal.drop_tasks ON DELETE CASCADE,
 native_task uuid NOT NULL, generation uuid NOT NULL, storage_epoch uuid NOT NULL,
 consumer_id uuid NOT NULL, engine_instance text NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','cleaned','failed')),
 last_error text, PRIMARY KEY(drop_task,generation,storage_epoch)
);
CREATE TABLE qdrant_internal.task_archive (
 task_id uuid PRIMARY KEY, owner_oid oid NOT NULL, archived_xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
 status jsonb NOT NULL
);
-- Rotation cannot erase ownership of a retained on-disk epoch. Keep it until
-- an exact retirement receipt, or transfer it into a committed drop task.
CREATE TABLE qdrant_internal.prior_epochs (
 index_id bigint NOT NULL REFERENCES qdrant_internal.index_catalog(index_id) ON DELETE CASCADE,
 generation uuid NOT NULL, storage_epoch uuid NOT NULL, consumer_id uuid NOT NULL,
 engine_instance text NOT NULL, native_task uuid, PRIMARY KEY(index_id,generation,storage_epoch)
);
CREATE FUNCTION qdrant_internal.retain_previous_epoch() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 INSERT INTO qdrant_internal.prior_epochs(index_id,generation,storage_epoch,consumer_id,engine_instance)
 SELECT index_id,OLD.generation,OLD.storage_epoch,OLD.consumer_id,OLD.engine_instance
 FROM qdrant_internal.index_catalog WHERE index_name=OLD.index_name ON CONFLICT DO NOTHING;
 RETURN NEW;
END $$;
CREATE TRIGGER qdrant_retain_epoch BEFORE UPDATE OF generation,storage_epoch ON qdrant_internal.consumer_state
FOR EACH ROW WHEN (OLD.generation IS DISTINCT FROM NEW.generation OR OLD.storage_epoch IS DISTINCT FROM NEW.storage_epoch)
EXECUTE FUNCTION qdrant_internal.retain_previous_epoch();
CREATE FUNCTION qdrant_internal.retain_shadow_epoch() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 INSERT INTO qdrant_internal.prior_epochs(index_id,generation,storage_epoch,consumer_id,engine_instance,native_task)
 VALUES(OLD.index_id,OLD.generation,OLD.storage_epoch,OLD.consumer_id,OLD.engine_instance,OLD.task_id)
 ON CONFLICT DO NOTHING;
 RETURN NEW;
END $$;
CREATE TRIGGER qdrant_retain_shadow_epoch BEFORE UPDATE OF storage_epoch ON qdrant_internal.generation_reservations
FOR EACH ROW WHEN (OLD.engine_instance IS NOT NULL AND OLD.storage_epoch IS DISTINCT FROM NEW.storage_epoch)
EXECUTE FUNCTION qdrant_internal.retain_shadow_epoch();

CREATE FUNCTION qdrant_internal.queue_index_drop(p_name text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; t uuid; slot record;
BEGIN
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_name FOR UPDATE;
 IF NOT pg_has_role(qdrant_internal.actor_oid(),i.owner_oid,'USAGE') THEN
   RAISE EXCEPTION 'Index owner access required' USING ERRCODE='42501'; END IF;
 INSERT INTO qdrant_internal.drop_tasks(index_id,index_name,owner_oid,source_oid)
 VALUES(i.index_id,i.index_name,i.owner_oid,i.source_oid) RETURNING task_id INTO t;
 -- Copy exact owned epochs before deleting their catalog. The active epoch
 -- takes precedence over a completed shadow record of that same generation.
 INSERT INTO qdrant_internal.drop_epochs(drop_task,native_task,generation,storage_epoch,consumer_id,engine_instance)
 SELECT t,t,generation,storage_epoch,consumer_id,engine_instance FROM qdrant_internal.consumer_state
 WHERE index_name=p_name;
 INSERT INTO qdrant_internal.drop_epochs(drop_task,native_task,generation,storage_epoch,consumer_id,engine_instance)
 SELECT t,coalesce(j.task_id,h.native_task,t),h.generation,h.storage_epoch,h.consumer_id,h.engine_instance
 FROM qdrant_internal.prior_epochs h LEFT JOIN qdrant_internal.generation_reservations j
 ON j.index_id=h.index_id AND j.retired_generation=h.generation AND j.retired_epoch=h.storage_epoch
 WHERE h.index_id=i.index_id AND j.retired_state IS DISTINCT FROM 'cleaned' ON CONFLICT DO NOTHING;
 INSERT INTO qdrant_internal.drop_epochs(drop_task,native_task,generation,storage_epoch,consumer_id,engine_instance)
 SELECT t,j.task_id,j.generation,j.storage_epoch,j.consumer_id,j.engine_instance FROM qdrant_internal.generation_reservations j
 WHERE j.index_id=i.index_id AND j.engine_instance IS NOT NULL AND j.abandoned_state<>'cleaned'
   AND NOT EXISTS(SELECT 1 FROM qdrant_internal.generation_reservations later WHERE later.index_id=i.index_id
     AND later.retired_generation=j.generation AND later.retired_epoch=j.storage_epoch)
 ON CONFLICT DO NOTHING;
 INSERT INTO qdrant_internal.drop_epochs(drop_task,native_task,generation,storage_epoch,consumer_id,engine_instance)
 SELECT t,task_id,retired_generation,retired_epoch,retired_consumer,retired_engine_instance
 FROM qdrant_internal.generation_reservations WHERE index_id=i.index_id AND retired_state<>'cleaned'
   AND retired_engine_instance IS NOT NULL ON CONFLICT DO NOTHING;
 INSERT INTO qdrant_internal.task_archive(task_id,owner_oid,status)
 SELECT j.task_id,i.owner_oid,qdrant.task_status(j.task_id)||jsonb_build_object(
   'state',CASE WHEN j.state IN ('succeeded','failed','cancelled') THEN j.state ELSE 'cancelled' END,
   'completed',true,'error',CASE WHEN j.state IN ('succeeded','failed','cancelled') THEN j.last_error ELSE 'index_dropped' END,
   'drop_task',t,'logical_index_removed',true) FROM qdrant_internal.generation_reservations j WHERE j.index_id=i.index_id;
 IF EXISTS(SELECT 1 FROM pg_class WHERE oid=i.source_oid) THEN
   EXECUTE format('DROP TRIGGER IF EXISTS qdrant_p1_rows ON %s',i.source_oid::regclass);
   EXECUTE format('DROP TRIGGER IF EXISTS qdrant_p1_truncate ON %s',i.source_oid::regclass);
   FOR slot IN SELECT name FROM qdrant_internal.representation_catalog WHERE index_name=p_name LOOP
     EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s','qdrant_model_'||slot.name,i.source_oid::regclass);
   END LOOP;
 END IF;
 DELETE FROM qdrant_internal.index_catalog WHERE index_name=p_name;
 IF NOT EXISTS(SELECT 1 FROM qdrant_internal.drop_epochs WHERE drop_task=t) THEN
   UPDATE qdrant_internal.drop_tasks SET state='succeeded' WHERE task_id=t;
 END IF;
 RETURN jsonb_build_object('task_id',t,'kind','drop','logical_index_removed',true,
   'physical_cleanup_completed',NOT EXISTS(SELECT 1 FROM qdrant_internal.drop_epochs WHERE drop_task=t));
END $$;

ALTER FUNCTION qdrant.task_status(uuid) SET SCHEMA qdrant_internal;
ALTER FUNCTION qdrant_internal.task_status(uuid) RENAME TO rebuild_task_status;
CREATE FUNCTION qdrant.task_status(p_task uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE d qdrant_internal.drop_tasks%ROWTYPE; a qdrant_internal.task_archive%ROWTYPE; pending bigint;
BEGIN
 SELECT * INTO d FROM qdrant_internal.drop_tasks WHERE task_id=p_task;
 IF FOUND THEN
   IF NOT pg_has_role(qdrant_internal.actor_oid(),d.owner_oid,'USAGE') THEN
     RAISE EXCEPTION 'Task owner access required' USING ERRCODE='42501'; END IF;
   SELECT count(*) INTO pending FROM qdrant_internal.drop_epochs WHERE drop_task=p_task AND state<>'cleaned';
   RETURN jsonb_build_object('task_id',p_task,'kind','drop','state',d.state,'index_name',d.index_name,
     'committed',d.requested_xid<>pg_current_xact_id(),'completed',d.state IN ('succeeded','failed'),
     'succeeded',d.state='succeeded','pending_epochs',pending,'pending_events',0,'error',d.last_error,
     'error_code',d.last_error_code,
     'logical_index_removed',true,'physical_cleanup_completed',d.state='succeeded');
 END IF;
 SELECT * INTO a FROM qdrant_internal.task_archive WHERE task_id=p_task;
 IF FOUND THEN
   IF NOT pg_has_role(qdrant_internal.actor_oid(),a.owner_oid,'USAGE') THEN
     RAISE EXCEPTION 'Task owner access required' USING ERRCODE='42501'; END IF;
   RETURN a.status||jsonb_build_object('committed',a.archived_xid<>pg_current_xact_id());
 END IF;
 RETURN qdrant_internal.rebuild_task_status(p_task);
END $$;

ALTER FUNCTION qdrant.cancel_task(uuid) SET SCHEMA qdrant_internal;
ALTER FUNCTION qdrant_internal.cancel_task(uuid) RENAME TO rebuild_cancel_task;
CREATE FUNCTION qdrant.cancel_task(p_task uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE status jsonb;
BEGIN
 IF EXISTS(SELECT 1 FROM qdrant_internal.drop_tasks WHERE task_id=p_task)
   OR EXISTS(SELECT 1 FROM qdrant_internal.task_archive WHERE task_id=p_task) THEN
   status:=qdrant.task_status(p_task);
   IF (status->>'completed')::boolean THEN RETURN false; END IF;
   RAISE EXCEPTION 'Committed index drop cannot be cancelled; native cleanup remains required' USING ERRCODE='0A000';
 END IF;
 RETURN qdrant_internal.rebuild_cancel_task(p_task);
END $$;

CREATE FUNCTION qdrant_internal.next_drop_batch(p_instance text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE d qdrant_internal.drop_tasks%ROWTYPE; e qdrant_internal.drop_epochs%ROWTYPE;
BEGIN
 SELECT * INTO d FROM qdrant_internal.drop_tasks WHERE state IN ('queued','running')
   ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT * INTO e FROM qdrant_internal.drop_epochs WHERE drop_task=d.task_id AND state IN ('queued','running')
   ORDER BY generation,storage_epoch LIMIT 1 FOR UPDATE;
 IF NOT FOUND THEN
   UPDATE qdrant_internal.drop_tasks SET state=CASE WHEN EXISTS(SELECT 1 FROM qdrant_internal.drop_epochs
       WHERE drop_task=d.task_id AND state='failed') THEN 'failed' ELSE 'succeeded' END WHERE task_id=d.task_id;
   RETURN NULL;
 END IF;
 IF e.engine_instance<>p_instance THEN
   UPDATE qdrant_internal.drop_epochs SET state='failed',last_error='owner_changed; preserve uncertain storage'
     WHERE drop_task=d.task_id AND generation=e.generation AND storage_epoch=e.storage_epoch;
   UPDATE qdrant_internal.drop_tasks SET state='running',updated_at=clock_timestamp(),
     last_error=coalesce(last_error,'owner_changed; preserve uncertain storage'),
     last_error_code=coalesce(last_error_code,'source_owner_changed') WHERE task_id=d.task_id;
   RETURN NULL;
 END IF;
 UPDATE qdrant_internal.drop_epochs SET state='running' WHERE drop_task=d.task_id AND generation=e.generation AND storage_epoch=e.storage_epoch;
 UPDATE qdrant_internal.drop_tasks SET state='running',updated_at=clock_timestamp() WHERE task_id=d.task_id;
 RETURN jsonb_build_object('source_contract_version',14,'task_id',e.native_task,'retire',true,'index_id',d.index_id,
   'generation',e.generation,'storage_epoch',e.storage_epoch,'consumer_id',e.consumer_id,'representations','{}'::jsonb,'events','[]'::jsonb);
END $$;

ALTER FUNCTION qdrant_internal.ack_retirement_batch(jsonb) RENAME TO ack_generation_retirement;
CREATE FUNCTION qdrant_internal.ack_retirement_batch(p_batch jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE t uuid;
BEGIN
 UPDATE qdrant_internal.drop_epochs e SET state='cleaned',last_error=NULL FROM qdrant_internal.drop_tasks d
 WHERE e.drop_task=d.task_id AND d.index_id=(p_batch->>'index_id')::bigint AND e.state='running'
   AND e.native_task::text=p_batch->>'task_id' AND e.generation::text=p_batch->>'generation'
   AND e.storage_epoch::text=p_batch->>'storage_epoch' AND e.consumer_id::text=p_batch->>'consumer_id'
 RETURNING e.drop_task INTO t;
 IF FOUND THEN
   UPDATE qdrant_internal.drop_tasks SET state=CASE
     WHEN EXISTS(SELECT 1 FROM qdrant_internal.drop_epochs WHERE drop_task=t AND state IN ('queued','running')) THEN 'running'
     WHEN EXISTS(SELECT 1 FROM qdrant_internal.drop_epochs WHERE drop_task=t AND state='failed') THEN 'failed'
     ELSE 'succeeded' END,updated_at=clock_timestamp() WHERE task_id=t;
 ELSE
   PERFORM qdrant_internal.ack_generation_retirement(p_batch);
   DELETE FROM qdrant_internal.prior_epochs h USING qdrant_internal.generation_reservations j
   WHERE j.task_id::text=p_batch->>'task_id' AND j.retired_state='cleaned' AND h.index_id=j.index_id
     AND h.generation=j.retired_generation AND h.storage_epoch=j.retired_epoch;
 END IF;
END $$;

ALTER FUNCTION qdrant_internal.fail_batch(jsonb) RENAME TO fail_source_generation_batch;
CREATE FUNCTION qdrant_internal.fail_batch(p_error jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE t uuid;
BEGIN
 IF coalesce((p_error->>'retire')::boolean,false) THEN
   UPDATE qdrant_internal.drop_epochs e SET state='failed',last_error=left(p_error->>'error',4096)
   FROM qdrant_internal.drop_tasks d WHERE e.drop_task=d.task_id AND d.index_id=(p_error->>'index_id')::bigint
     AND e.native_task::text=p_error->>'task_id' AND e.storage_epoch::text=p_error->>'epoch' AND e.state='running'
   RETURNING e.drop_task INTO t;
   IF FOUND THEN
     UPDATE qdrant_internal.drop_tasks SET state='running',updated_at=clock_timestamp(),
       last_error=coalesce(last_error,left(p_error->>'error',4096)),
       last_error_code=coalesce(last_error_code,left(p_error->>'error_code',128)) WHERE task_id=t;
     RETURN;
   END IF;
 END IF;
 PERFORM qdrant_internal.fail_source_generation_batch(p_error);
END $$;

ALTER FUNCTION qdrant_internal.next_batch(text) RENAME TO next_source_generation_batch;
CREATE FUNCTION qdrant_internal.next_batch(p_instance text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 IF EXISTS(SELECT 1 FROM qdrant_internal.drop_tasks WHERE state IN ('queued','running')
   AND updated_at<clock_timestamp()-interval '100 milliseconds') THEN
   result:=qdrant_internal.next_drop_batch(p_instance);
   IF result IS NOT NULL THEN RETURN result; END IF;
 END IF;
 result:=qdrant_internal.next_source_generation_batch(p_instance);
 IF result IS NOT NULL THEN RETURN result; END IF;
 RETURN qdrant_internal.next_drop_batch(p_instance);
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA qdrant_internal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA qdrant_internal FROM PUBLIC;
GRANT EXECUTE ON FUNCTION qdrant.task_status(uuid),qdrant.cancel_task(uuid) TO PUBLIC;

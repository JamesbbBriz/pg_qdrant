-- Build a second owned native generation without changing the serving identity.
ALTER TABLE qdrant_internal.generation_reservations
 DROP CONSTRAINT generation_reservations_state_check,
 ADD CONSTRAINT generation_reservations_state_check CHECK
  (state IN ('reserved_unbuilt','building','catching_up','dirty','succeeded','cancelled','failed')),
 ADD COLUMN task_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
 ADD COLUMN generation uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
 ADD COLUMN source_generation uuid,
 ADD COLUMN storage_epoch uuid NOT NULL DEFAULT gen_random_uuid(),
 ADD COLUMN consumer_id uuid NOT NULL DEFAULT gen_random_uuid(),
 ADD COLUMN engine_instance text,
 ADD COLUMN requested_xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
 ADD COLUMN updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 ADD COLUMN last_error text,
 ADD COLUMN retired_generation uuid,
 ADD COLUMN retired_epoch uuid,
 ADD COLUMN retired_consumer uuid,
 ADD COLUMN retired_engine_instance text,
 ADD COLUMN retired_state text CHECK (retired_state IN ('queued','running','cleaned','failed')),
 ADD COLUMN cleanup_error text;
CREATE UNIQUE INDEX one_build_per_index ON qdrant_internal.generation_reservations(index_id)
 WHERE state IN ('building','catching_up','dirty');
CREATE TABLE qdrant_internal.generation_receipts (
 task_id uuid NOT NULL REFERENCES qdrant_internal.generation_reservations(task_id) ON DELETE CASCADE,
 event_id bigint NOT NULL REFERENCES qdrant_internal.outbox ON DELETE CASCADE,
 storage_epoch uuid NOT NULL, consumer_id uuid NOT NULL,
 flushed_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(task_id,event_id)
);

CREATE OR REPLACE FUNCTION qdrant.rebuild_index(p_index_name text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; n bigint; t uuid;
BEGIN
 PERFORM qdrant_internal.require_index(p_index_name);
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_index_name FOR NO KEY UPDATE NOWAIT;
 IF qdrant_internal.p1_index_status(p_index_name)->>'capture_state'<>'capturing' OR NOT i.backfill_done
    OR NOT EXISTS(SELECT 1 FROM qdrant_internal.consumer_state c WHERE c.index_name=p_index_name AND c.state='ready'
      AND c.generation=i.generation AND qdrant_internal.p1_service_ready(c.engine_instance)) THEN
   RAISE EXCEPTION 'Rebuild requires a trusted ready source generation' USING ERRCODE='55000';
 END IF;
 IF EXISTS(SELECT 1 FROM qdrant_internal.generation_reservations WHERE index_id=i.index_id
     AND state IN ('building','catching_up','dirty')) THEN
   RAISE EXCEPTION 'An index build is already active' USING ERRCODE='55006';
 END IF;
 SELECT greatest(i.generation_no,coalesce(max(generation_no),i.generation_no))+1 INTO n
   FROM qdrant_internal.generation_reservations WHERE index_id=i.index_id;
 INSERT INTO qdrant_internal.generation_reservations(index_id,generation_no,state,requested_by,source_generation)
 VALUES(i.index_id,n,'building',qdrant_internal.actor_oid(),i.generation) RETURNING task_id INTO t;
 RETURN jsonb_build_object('task_id',t,'index_id',i.index_id,'reserved_generation',n,'state','building',
   'engine_build_started',false,'search_ready',false,'automatic_switch',true);
END $$;

CREATE FUNCTION qdrant.task_status(p_task uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE j qdrant_internal.generation_reservations%ROWTYPE; i qdrant_internal.index_catalog%ROWTYPE; pending bigint;
BEGIN
 SELECT * INTO STRICT j FROM qdrant_internal.generation_reservations WHERE task_id=p_task;
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_id=j.index_id;
 PERFORM qdrant_internal.require_index(i.index_name,true);
 SELECT count(*) INTO pending FROM qdrant_internal.outbox e
 LEFT JOIN qdrant_internal.generation_receipts a ON a.task_id=j.task_id AND a.event_id=e.event_id
 WHERE e.index_name=i.index_name AND (a.event_id IS NULL OR a.storage_epoch<>j.storage_epoch OR a.consumer_id<>j.consumer_id);
 IF j.state='succeeded' THEN pending:=0; END IF;
 RETURN jsonb_build_object('task_id',p_task,'kind','rebuild','state',j.state,'index_name',i.index_name,
   'generation',j.generation,'generation_no',j.generation_no,'active_generation',i.generation,
   'storage_epoch',j.storage_epoch,'consumer_id',j.consumer_id,'pending_events',pending,
   'committed',j.requested_xid IS DISTINCT FROM pg_current_xact_id_if_assigned(),
   'completed',j.state IN ('succeeded','failed','cancelled'),'succeeded',j.state='succeeded',
   'error',j.last_error,'timed_out',false,'retired_storage_cleanup',coalesce(j.retired_state='cleaned',false),
   'retired_generation',j.retired_generation,'cleanup_state',j.retired_state,'cleanup_error',j.cleanup_error);
END $$;

CREATE FUNCTION qdrant.await_task(p_task uuid,p_timeout_ms integer DEFAULT 5000) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb; deadline timestamptz;
BEGIN
 IF p_timeout_ms IS NULL OR p_timeout_ms NOT BETWEEN 0 AND 120000 THEN RAISE EXCEPTION 'timeout must be 0..120000' USING ERRCODE='22023'; END IF;
 IF current_setting('transaction_isolation')<>'read committed' THEN RAISE EXCEPTION 'await_task requires READ COMMITTED' USING ERRCODE='25001'; END IF;
 result:=qdrant.task_status(p_task);
 IF NOT (result->>'committed')::boolean THEN RAISE EXCEPTION 'Cannot await own uncommitted task' USING ERRCODE='55000'; END IF;
 PERFORM qdrant_internal.p1_start_consumer();
 deadline:=clock_timestamp()+p_timeout_ms*interval '1 millisecond';
 LOOP
   result:=qdrant.task_status(p_task);
   IF (result->>'completed')::boolean THEN RETURN result; END IF;
   IF clock_timestamp()>=deadline THEN RETURN result||jsonb_build_object('timed_out',true); END IF;
   PERFORM pg_sleep(0.01);
 END LOOP;
END $$;

CREATE FUNCTION qdrant.cancel_task(p_task uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; changed bigint;
BEGIN
 SELECT idx.* INTO STRICT i FROM qdrant_internal.index_catalog idx JOIN qdrant_internal.generation_reservations j
   ON j.index_id=idx.index_id WHERE j.task_id=p_task FOR KEY SHARE OF idx NOWAIT;
 PERFORM qdrant_internal.require_index(i.index_name,true);
 UPDATE qdrant_internal.generation_reservations SET state='cancelled',updated_at=clock_timestamp()
 WHERE task_id=p_task AND state IN ('building','catching_up','dirty');
 GET DIAGNOSTICS changed=ROW_COUNT;
 RETURN changed=1;
END $$;

CREATE OR REPLACE FUNCTION qdrant.cancel_rebuild(p_index_name text,p_generation bigint) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE t uuid;
BEGIN
 PERFORM qdrant_internal.require_index(p_index_name,true);
 SELECT task_id INTO t FROM qdrant_internal.generation_reservations j JOIN qdrant_internal.index_catalog i USING(index_id)
 WHERE i.index_name=p_index_name AND j.generation_no=p_generation;
 IF t IS NULL THEN RETURN false; END IF;
 RETURN qdrant.cancel_task(t);
END $$;

CREATE OR REPLACE FUNCTION qdrant.rebuild_status(p_index_name text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE;
BEGIN
 PERFORM qdrant_internal.require_index(p_index_name,true);
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_index_name;
 RETURN jsonb_build_object('index_id',i.index_id,'active_generation',i.generation_no,
   'active_generation_search_ready',(SELECT state='ready' AND qdrant_internal.p1_service_ready(engine_instance)
       AND qdrant_internal.p1_index_status(p_index_name)->>'capture_state'='capturing'
       FROM qdrant_internal.consumer_state WHERE index_name=p_index_name),
   'reservations',(SELECT coalesce(jsonb_agg(qdrant.task_status(task_id) ORDER BY generation_no),'[]')
       FROM qdrant_internal.generation_reservations WHERE index_id=i.index_id),
   'switch_available',false,'automatic_switch',true);
END $$;

CREATE FUNCTION qdrant_internal.next_generation_batch(p_instance text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE j qdrant_internal.generation_reservations%ROWTYPE; i qdrant_internal.index_catalog%ROWTYPE; events jsonb;
BEGIN
 SELECT * INTO j FROM qdrant_internal.generation_reservations
 WHERE state IN ('building','catching_up','dirty') ORDER BY updated_at LIMIT 1 FOR NO KEY UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT * INTO i FROM qdrant_internal.index_catalog WHERE index_id=j.index_id FOR KEY SHARE SKIP LOCKED;
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF i.generation<>j.source_generation OR qdrant_internal.p1_index_status(i.index_name)->>'capture_state'<>'capturing' THEN
   UPDATE qdrant_internal.generation_reservations SET state='failed',last_error='source_generation_invalidated',updated_at=clock_timestamp() WHERE task_id=j.task_id;
   RETURN NULL;
 END IF;
 IF j.engine_instance IS DISTINCT FROM p_instance THEN
   UPDATE qdrant_internal.generation_reservations SET storage_epoch=gen_random_uuid(),consumer_id=gen_random_uuid(),
     engine_instance=p_instance,state='building',last_error=NULL WHERE task_id=j.task_id RETURNING * INTO j;
 END IF;
 SELECT coalesce(jsonb_agg(v.data ORDER BY v.event_id),'[]') INTO events FROM (
  SELECT e.event_id,jsonb_build_object('event_id',e.event_id,'point_id',e.point_id,'revision',s.revision,
   'incarnation',e.incarnation,'key',e.tagged_key,'fingerprint',s.fingerprint,
   'body',CASE WHEN s.point_id=e.point_id AND s.incarnation=e.incarnation AND NOT s.tombstone THEN s.body END,
   'vectors',CASE WHEN s.point_id=e.point_id AND s.incarnation=e.incarnation AND NOT s.tombstone
      THEN qdrant_internal.ready_vectors(s.index_name,s.tagged_key,s.incarnation) ELSE '{}'::jsonb END) AS data
  FROM qdrant_internal.outbox e JOIN qdrant_internal.source_state s ON s.index_name=e.index_name AND s.tagged_key=e.tagged_key
  LEFT JOIN qdrant_internal.generation_receipts a ON a.task_id=j.task_id AND a.event_id=e.event_id
  WHERE e.index_name=i.index_name AND (a.event_id IS NULL OR a.storage_epoch<>j.storage_epoch OR a.consumer_id<>j.consumer_id)
  ORDER BY e.event_id LIMIT 16) v;
 UPDATE qdrant_internal.generation_reservations SET state='dirty',updated_at=clock_timestamp() WHERE task_id=j.task_id;
 RETURN jsonb_build_object('source_contract_version',12,'task_id',j.task_id,'index_id',i.index_id,
   'generation',j.generation,'storage_epoch',j.storage_epoch,'consumer_id',j.consumer_id,
   'representations',coalesce((SELECT jsonb_object_agg(name,contract) FROM qdrant_internal.representation_catalog WHERE index_name=i.index_name),'{}'),
   'events',events);
END $$;

CREATE FUNCTION qdrant_internal.try_generation_switch(p_task uuid) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; j qdrant_internal.generation_reservations%ROWTYPE;
BEGIN
 -- The full catalog row lock conflicts with source/outbox FK key-share locks
 -- and admitted searches. Never switch past an uncommitted source write.
 BEGIN
 SELECT idx.* INTO i FROM qdrant_internal.index_catalog idx JOIN qdrant_internal.generation_reservations r USING(index_id)
   WHERE r.task_id=p_task FOR UPDATE OF idx NOWAIT;
 EXCEPTION WHEN lock_not_available THEN RETURN;
 END;
 IF NOT FOUND THEN RETURN; END IF;
 SELECT * INTO STRICT j FROM qdrant_internal.generation_reservations WHERE task_id=p_task FOR UPDATE;
 IF j.state<>'catching_up' OR i.generation<>j.source_generation OR NOT i.backfill_done
   OR qdrant_internal.p1_index_status(i.index_name)->>'capture_state'<>'capturing'
   OR NOT EXISTS(SELECT 1 FROM qdrant_internal.consumer_state c WHERE c.index_name=i.index_name
      AND c.engine_instance=j.engine_instance) THEN RETURN; END IF;
 IF EXISTS(SELECT 1 FROM qdrant_internal.outbox e LEFT JOIN qdrant_internal.generation_receipts a
   ON a.task_id=j.task_id AND a.event_id=e.event_id WHERE e.index_name=i.index_name
   AND (a.event_id IS NULL OR a.storage_epoch<>j.storage_epoch OR a.consumer_id<>j.consumer_id)) THEN RETURN; END IF;
 INSERT INTO qdrant_internal.event_ack(event_id,generation,storage_epoch,consumer_id,flushed_at)
 SELECT a.event_id,j.generation,j.storage_epoch,j.consumer_id,a.flushed_at FROM qdrant_internal.generation_receipts a
 WHERE a.task_id=j.task_id AND a.storage_epoch=j.storage_epoch AND a.consumer_id=j.consumer_id
 ON CONFLICT(event_id) DO UPDATE SET generation=excluded.generation,storage_epoch=excluded.storage_epoch,
   consumer_id=excluded.consumer_id,flushed_at=excluded.flushed_at;
 UPDATE qdrant_internal.generation_reservations SET retired_generation=i.generation,
   retired_epoch=c.storage_epoch,retired_consumer=c.consumer_id,retired_engine_instance=c.engine_instance,retired_state='queued'
 FROM qdrant_internal.consumer_state c WHERE task_id=j.task_id AND c.index_name=i.index_name;
 UPDATE qdrant_internal.index_catalog SET generation=j.generation,generation_no=j.generation_no WHERE index_id=i.index_id;
 UPDATE qdrant_internal.consumer_state SET generation=j.generation,storage_epoch=j.storage_epoch,consumer_id=j.consumer_id,
   engine_instance=j.engine_instance,state='ready',last_error=NULL,updated_at=clock_timestamp() WHERE index_name=i.index_name;
 UPDATE qdrant_internal.generation_reservations SET state='succeeded',updated_at=clock_timestamp() WHERE task_id=j.task_id;
END $$;

CREATE FUNCTION qdrant_internal.ack_generation_batch(p_batch jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE j qdrant_internal.generation_reservations%ROWTYPE; i qdrant_internal.index_catalog%ROWTYPE;
BEGIN
 SELECT * INTO i FROM qdrant_internal.index_catalog WHERE index_id=(p_batch->>'index_id')::bigint FOR NO KEY UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN; END IF;
 SELECT * INTO j FROM qdrant_internal.generation_reservations WHERE task_id=(p_batch->>'task_id')::uuid AND index_id=i.index_id FOR UPDATE;
 IF NOT FOUND OR j.state<>'dirty' OR j.generation::text<>p_batch->>'generation'
   OR j.storage_epoch::text<>p_batch->>'storage_epoch' OR j.consumer_id::text<>p_batch->>'consumer_id'
   OR i.generation<>j.source_generation OR qdrant_internal.p1_index_status(i.index_name)->>'capture_state'<>'capturing' THEN RETURN; END IF;
 INSERT INTO qdrant_internal.generation_receipts(task_id,event_id,storage_epoch,consumer_id)
 SELECT j.task_id,e.event_id,j.storage_epoch,j.consumer_id FROM jsonb_array_elements(p_batch->'events') v
 JOIN qdrant_internal.outbox e ON e.event_id=(v->>'event_id')::bigint AND e.index_name=i.index_name
 ON CONFLICT(task_id,event_id) DO UPDATE SET storage_epoch=excluded.storage_epoch,consumer_id=excluded.consumer_id,flushed_at=clock_timestamp();
 UPDATE qdrant_internal.generation_reservations SET state='catching_up',last_error=NULL,updated_at=clock_timestamp() WHERE task_id=j.task_id;
 PERFORM qdrant_internal.try_generation_switch(j.task_id);
END $$;

CREATE FUNCTION qdrant_internal.fail_batch(p_error jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE;
BEGIN
 SELECT * INTO i FROM qdrant_internal.index_catalog WHERE index_id=(p_error->>'index_id')::bigint FOR KEY SHARE SKIP LOCKED;
 IF NOT FOUND THEN RETURN; END IF;
 IF coalesce((p_error->>'retire')::boolean,false) THEN
   UPDATE qdrant_internal.generation_reservations SET retired_state='failed',cleanup_error=left(p_error->>'error',4096)
   WHERE task_id=(p_error->>'task_id')::uuid AND index_id=i.index_id AND retired_epoch::text=p_error->>'epoch'
     AND retired_state='running';
   RETURN;
 END IF;
 IF p_error->>'task_id' IS NOT NULL THEN
   UPDATE qdrant_internal.generation_reservations SET state='failed',last_error=left(p_error->>'error',4096),updated_at=clock_timestamp()
   WHERE task_id=(p_error->>'task_id')::uuid AND index_id=i.index_id AND storage_epoch::text=p_error->>'epoch'
     AND state IN ('building','dirty','catching_up');
 ELSE
   UPDATE qdrant_internal.consumer_state SET state='failed',last_error=left(p_error->>'error',4096)
   WHERE index_name=i.index_name AND storage_epoch::text=p_error->>'epoch';
 END IF;
END $$;

CREATE FUNCTION qdrant_internal.next_retirement_batch(p_instance text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE j qdrant_internal.generation_reservations%ROWTYPE; i qdrant_internal.index_catalog%ROWTYPE;
BEGIN
 SELECT * INTO j FROM qdrant_internal.generation_reservations WHERE state='succeeded' AND retired_state IN ('queued','running')
   ORDER BY updated_at LIMIT 1 FOR NO KEY UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT * INTO i FROM qdrant_internal.index_catalog WHERE index_id=j.index_id FOR KEY SHARE SKIP LOCKED;
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF i.generation=j.retired_generation OR j.retired_engine_instance IS DISTINCT FROM p_instance THEN
   UPDATE qdrant_internal.generation_reservations SET retired_state='failed',cleanup_error='retired_owner_changed; preserve storage'
    WHERE task_id=j.task_id;
   RETURN NULL;
 END IF;
 UPDATE qdrant_internal.generation_reservations SET retired_state='running' WHERE task_id=j.task_id;
 RETURN jsonb_build_object('source_contract_version',12,'task_id',j.task_id,'retire',true,'index_id',i.index_id,
   'generation',j.retired_generation,'storage_epoch',j.retired_epoch,'consumer_id',j.retired_consumer,
   'representations','{}'::jsonb,'events','[]'::jsonb);
END $$;

CREATE FUNCTION qdrant_internal.ack_retirement_batch(p_batch jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE;
BEGIN
 SELECT * INTO i FROM qdrant_internal.index_catalog WHERE index_id=(p_batch->>'index_id')::bigint FOR KEY SHARE SKIP LOCKED;
 IF NOT FOUND THEN RETURN; END IF;
 UPDATE qdrant_internal.generation_reservations SET retired_state='cleaned',cleanup_error=NULL
 WHERE task_id=(p_batch->>'task_id')::uuid AND index_id=i.index_id AND state='succeeded' AND retired_state='running'
   AND retired_generation::text=p_batch->>'generation' AND i.generation<>retired_generation
   AND retired_epoch::text=p_batch->>'storage_epoch' AND retired_consumer::text=p_batch->>'consumer_id';
END $$;

CREATE FUNCTION qdrant_internal.next_batch(p_instance text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 -- Give a pending build one bounded batch per 100 ms; source writes receive
 -- the intervening dispatches. An idle source allows a build to progress.
 IF EXISTS(SELECT 1 FROM qdrant_internal.generation_reservations WHERE state IN ('building','catching_up','dirty')
   AND updated_at<clock_timestamp()-interval '100 milliseconds') THEN
   result:=qdrant_internal.next_generation_batch(p_instance);
   IF result IS NOT NULL THEN RETURN result; END IF;
 END IF;
 result:=qdrant_internal.next_source_batch(p_instance);
 IF result IS NOT NULL THEN RETURN result; END IF;
 result:=qdrant_internal.next_generation_batch(p_instance);
 IF result IS NOT NULL THEN RETURN result; END IF;
 RETURN qdrant_internal.next_retirement_batch(p_instance);
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA qdrant_internal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA qdrant_internal FROM PUBLIC;
GRANT EXECUTE ON FUNCTION qdrant.task_status(uuid),qdrant.await_task(uuid,integer),qdrant.cancel_task(uuid) TO PUBLIC;

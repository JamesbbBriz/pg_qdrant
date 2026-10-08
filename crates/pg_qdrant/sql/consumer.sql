CREATE FUNCTION qdrant_internal.consumer_reset(p_instance text) RETURNS void
LANGUAGE sql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
 INSERT INTO qdrant_internal.consumer_state(index_name,generation,engine_instance)
 SELECT index_name,generation,p_instance FROM qdrant_internal.index_catalog FOR KEY SHARE SKIP LOCKED
 ON CONFLICT(index_name) DO UPDATE SET generation=excluded.generation,
   storage_epoch=gen_random_uuid(),consumer_id=gen_random_uuid(),engine_instance=excluded.engine_instance,state='building',last_error=NULL
$$;

CREATE FUNCTION qdrant_internal.next_batch(p_instance text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; c qdrant_internal.consumer_state%ROWTYPE; events jsonb;
BEGIN
 -- A row skipped during owner replacement must rotate after management unlocks.
 -- Do not lock an unchanged consumer on every idle poll: even a rejected
 -- ON CONFLICT UPDATE can write a tuple lock and require a WAL commit.
 INSERT INTO qdrant_internal.consumer_state AS old(index_name,generation,engine_instance)
 SELECT idx.index_name,idx.generation,p_instance FROM qdrant_internal.index_catalog idx
 LEFT JOIN qdrant_internal.consumer_state previous USING(index_name)
 WHERE previous.index_name IS NULL OR previous.engine_instance<>p_instance
 FOR KEY SHARE OF idx SKIP LOCKED
 ON CONFLICT(index_name) DO UPDATE SET generation=excluded.generation,
   storage_epoch=gen_random_uuid(),consumer_id=gen_random_uuid(),engine_instance=excluded.engine_instance,
   state='building',last_error=NULL WHERE old.engine_instance<>excluded.engine_instance;
 SELECT idx.* INTO i FROM qdrant_internal.index_catalog idx
 JOIN qdrant_internal.consumer_state cs USING(index_name)
 WHERE cs.state<>'failed'
   AND qdrant_internal.p1_index_status(idx.index_name)->>'capture_state'='capturing'
   AND (cs.state<>'ready' OR NOT idx.backfill_done OR EXISTS (
     SELECT 1 FROM qdrant_internal.outbox e LEFT JOIN qdrant_internal.event_ack a USING(event_id)
     WHERE e.index_name=idx.index_name AND (a.event_id IS NULL OR a.storage_epoch<>cs.storage_epoch)))
 ORDER BY cs.updated_at,idx.index_id LIMIT 1 FOR NO KEY UPDATE OF idx SKIP LOCKED;
 IF NOT FOUND THEN RETURN NULL; END IF;
 PERFORM qdrant_internal.backfill_step(i.index_name);
 SELECT * INTO STRICT c FROM qdrant_internal.consumer_state WHERE index_name=i.index_name;
 -- Reconcile each event against its committed current identity. Old point IDs
 -- are explicitly deleted; old upserts never revive obsolete incarnations.
 SELECT coalesce(jsonb_agg(v.data ORDER BY v.event_id),'[]'::jsonb) INTO events FROM (
   SELECT e.event_id,jsonb_build_object('event_id',e.event_id,'point_id',e.point_id,
     'revision',s.revision,'incarnation',e.incarnation,'key',e.tagged_key,
     'fingerprint',s.fingerprint,'body',CASE WHEN s.point_id=e.point_id
       AND s.incarnation=e.incarnation AND NOT s.tombstone THEN s.body END,
     'vectors',CASE WHEN s.point_id=e.point_id AND s.incarnation=e.incarnation AND NOT s.tombstone
       THEN qdrant_internal.ready_vectors(s.index_name,s.tagged_key,s.incarnation) ELSE '{}'::jsonb END) AS data
   FROM qdrant_internal.outbox e JOIN qdrant_internal.source_state s
     ON s.index_name=e.index_name AND s.tagged_key=e.tagged_key
   LEFT JOIN qdrant_internal.event_ack a ON a.event_id=e.event_id
   WHERE e.index_name=i.index_name AND (a.event_id IS NULL OR a.storage_epoch<>c.storage_epoch)
   ORDER BY e.event_id LIMIT 16
 ) v;
 UPDATE qdrant_internal.consumer_state SET state='dirty',updated_at=clock_timestamp()
 WHERE index_name=i.index_name;
 RETURN jsonb_build_object('source_contract_version',2,'index_id',i.index_id,'generation',i.generation,
   'representations',coalesce((SELECT jsonb_object_agg(name,contract) FROM qdrant_internal.representation_catalog WHERE index_name=i.index_name),'{}'::jsonb),
   'storage_epoch',c.storage_epoch,'consumer_id',c.consumer_id,'events',events);
END $$;

CREATE FUNCTION qdrant_internal.ack_batch(p_batch jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; c qdrant_internal.consumer_state%ROWTYPE;
BEGIN
 SELECT * INTO i FROM qdrant_internal.index_catalog WHERE index_id=(p_batch->>'index_id')::bigint FOR NO KEY UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN; END IF;
 SELECT * INTO STRICT c FROM qdrant_internal.consumer_state WHERE index_name=i.index_name FOR UPDATE;
 IF i.generation::text<>p_batch->>'generation' OR c.storage_epoch::text<>p_batch->>'storage_epoch'
    OR c.consumer_id::text<>p_batch->>'consumer_id' OR c.state<>'dirty' THEN
   -- A replacement owner may already have rotated the epoch while this
   -- receipt was in flight. Discard it; the new exact event set remains pending.
   RETURN;
 END IF;
 IF qdrant_internal.p1_index_status(i.index_name)->>'capture_state'<>'capturing' THEN RETURN; END IF;
 INSERT INTO qdrant_internal.event_ack(event_id,generation,storage_epoch,consumer_id)
 SELECT e.event_id,i.generation,c.storage_epoch,c.consumer_id
 FROM jsonb_array_elements(p_batch->'events') v JOIN qdrant_internal.outbox e
   ON e.event_id=(v->>'event_id')::bigint AND e.index_name=i.index_name
 ON CONFLICT(event_id) DO UPDATE SET generation=excluded.generation,
   storage_epoch=excluded.storage_epoch,consumer_id=excluded.consumer_id,flushed_at=clock_timestamp();
 UPDATE qdrant_internal.consumer_state SET state=CASE WHEN i.backfill_done AND NOT EXISTS (
   SELECT 1 FROM qdrant_internal.outbox e LEFT JOIN qdrant_internal.event_ack a USING(event_id)
   WHERE e.index_name=i.index_name AND (a.event_id IS NULL OR a.storage_epoch<>c.storage_epoch))
   THEN 'ready' ELSE 'building' END,last_error=NULL,updated_at=clock_timestamp() WHERE index_name=i.index_name;
END $$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA qdrant_internal FROM PUBLIC;

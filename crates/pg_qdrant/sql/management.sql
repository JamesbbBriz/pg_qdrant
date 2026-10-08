CREATE FUNCTION qdrant_internal.actor_oid() RETURNS oid LANGUAGE sql STABLE
SET search_path=pg_catalog,pg_temp AS $$
 SELECT oid FROM pg_roles WHERE rolname=coalesce(nullif(current_setting('role'),'none'),session_user)
$$;

CREATE FUNCTION qdrant_internal.require_index(p_name text, p_owner boolean DEFAULT false)
RETURNS void LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; actor oid;
BEGIN
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_name;
 actor:=qdrant_internal.actor_oid();
 IF NOT pg_has_role(actor,i.owner_oid,'USAGE') THEN
   RAISE EXCEPTION 'Index owner access required' USING ERRCODE='42501';
 END IF;
 IF NOT p_owner AND (NOT has_table_privilege(actor,i.source_oid,'SELECT')
     OR NOT has_column_privilege(actor,i.source_oid,i.key_field,'SELECT')
     OR NOT has_column_privilege(actor,i.source_oid,i.text_field,'SELECT')) THEN
   RAISE EXCEPTION 'Source and indexed column SELECT required' USING ERRCODE='42501';
 END IF;
END $$;

CREATE FUNCTION qdrant.create_index(index_name text, source regclass, key_field name, settings jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE owner_id oid; result jsonb;
BEGIN
 IF settings IS NULL OR jsonb_typeof(settings)<>'object'
    OR settings #> '{text,fields}' IS NULL
    OR jsonb_typeof(settings #> '{text,fields}')<>'array' THEN
   RAISE EXCEPTION 'text.fields array required' USING ERRCODE='22023';
 END IF;
 IF jsonb_array_length(settings #> '{text,fields}')<>1
    OR jsonb_typeof(settings #> '{text,fields,0}')<>'string'
    OR settings<>jsonb_build_object('text',jsonb_build_object('fields',settings #> '{text,fields}')) THEN
   RAISE EXCEPTION 'This implementation admits one text field; other representations remain unimplemented'
     USING ERRCODE='0A000';
 END IF;
 SELECT relowner INTO owner_id FROM pg_class WHERE oid=source;
 IF owner_id IS NULL OR NOT pg_has_role(qdrant_internal.actor_oid(),owner_id,'USAGE') THEN
   RAISE EXCEPTION 'Source owner access required' USING ERRCODE='42501';
 END IF;
 result:=qdrant_internal.p1_register(index_name,source,key_field::text,settings #>> '{text,fields,0}');
 PERFORM qdrant_internal.p1_start_consumer();
 RETURN result;
END $$;

-- Capture precedes the bounded scan; row locks serialize snapshots with writes.
CREATE FUNCTION qdrant_internal.backfill_step(p_name text) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; row_data record; n integer:=0;
BEGIN
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_name FOR NO KEY UPDATE;
 IF i.backfill_done THEN RETURN; END IF;
 IF qdrant_internal.p1_index_status(p_name)->>'capture_state'<>'capturing' THEN
   RAISE EXCEPTION 'Source binding changed' USING ERRCODE='55000';
 END IF;
 FOR row_data IN EXECUTE format(
   'SELECT %I::text AS key,%I AS body FROM ONLY %s WHERE ($1 IS NULL OR %I > $1::%s) ORDER BY %I LIMIT 32 FOR UPDATE',
   i.key_field,i.text_field,i.source_oid::regclass,i.key_field,i.key_type::regtype,i.key_field)
   USING i.backfill_cursor LOOP
   IF NOT EXISTS (SELECT 1 FROM qdrant_internal.source_state WHERE index_name=p_name
                  AND tagged_key->>'value'=row_data.key) THEN
     PERFORM qdrant_internal.p1_record(p_name,'upsert',row_data.key,row_data.body,'backfill');
   END IF;
   UPDATE qdrant_internal.index_catalog SET backfill_cursor=row_data.key WHERE index_name=p_name;
   n:=n+1;
 END LOOP;
 IF n<32 THEN UPDATE qdrant_internal.index_catalog SET backfill_done=true WHERE index_name=p_name; END IF;
END $$;

CREATE FUNCTION qdrant.track_changes(index_name text) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM qdrant_internal.require_index(index_name,true);
 RETURN qdrant_internal.p1_track(index_name);
END $$;

CREATE FUNCTION qdrant_internal.ticket_status(p_ticket uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE t qdrant_internal.change_tickets%ROWTYPE; c qdrant_internal.consumer_state%ROWTYPE;
        pending integer; total integer; reason text; ready boolean;
BEGIN
 SELECT * INTO t FROM qdrant_internal.change_tickets WHERE ticket_id=p_ticket;
 IF NOT FOUND THEN RAISE EXCEPTION 'Unknown or uncommitted ticket' USING ERRCODE='22023'; END IF;
 PERFORM qdrant_internal.require_index(t.index_name);
 IF t.source_xid=pg_current_xact_id_if_assigned() THEN
   RAISE EXCEPTION 'Cannot await own uncommitted transaction' USING ERRCODE='55000';
 END IF;
 SELECT * INTO c FROM qdrant_internal.consumer_state WHERE index_name=t.index_name;
 SELECT count(*),count(*) FILTER(WHERE a.event_id IS NULL OR a.generation<>t.generation
       OR a.storage_epoch IS DISTINCT FROM c.storage_epoch OR a.consumer_id IS DISTINCT FROM c.consumer_id)
 INTO total,pending FROM qdrant_internal.outbox e LEFT JOIN qdrant_internal.event_ack a USING(event_id)
 WHERE e.ticket_id=p_ticket;
 IF total<>t.sealed_events THEN RAISE EXCEPTION 'Ticket membership mismatch' USING ERRCODE='XX001'; END IF;
 ready:=c.state='ready' AND c.generation=t.generation AND qdrant_internal.p1_service_ready()
        AND qdrant_internal.p1_index_status(t.index_name)->>'capture_state'='capturing';
 reason:=CASE WHEN c.generation<>t.generation THEN 'generation_invalidated'
              WHEN NOT coalesce(ready,false) THEN coalesce(c.last_error,'index_not_ready') END;
 RETURN jsonb_build_object('ticket',p_ticket,'committed',true,'applied',pending=0 AND coalesce(ready,false),
   'durable',pending=0 AND coalesce(ready,false),'pending_events',pending,'timed_out',false,
   'error',reason,'generation',t.generation);
END $$;

CREATE FUNCTION qdrant.index_status(p_index_name text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb; owner_status jsonb; c qdrant_internal.consumer_state%ROWTYPE;
BEGIN
 PERFORM qdrant_internal.require_index(p_index_name,true);
 owner_status:=qdrant_internal.p1_start_consumer();
 SELECT * INTO c FROM qdrant_internal.consumer_state WHERE index_name=p_index_name;
 result:=qdrant_internal.p1_index_status(p_index_name);
 RETURN result || jsonb_build_object('state',CASE WHEN result->>'capture_state'<>'capturing'
   OR NOT (owner_status->>'engine_ready')::boolean
   THEN 'degraded' ELSE coalesce(c.state,'registered') END,
   'backfill_done',(SELECT backfill_done FROM qdrant_internal.index_catalog WHERE index_name=p_index_name),
   'storage_epoch',c.storage_epoch,'consumer_id',c.consumer_id,'last_error',c.last_error,
   'owner_status',owner_status,
   'engine_index_ready',c.state='ready' AND result->>'capture_state'='capturing'
      AND (owner_status->>'engine_ready')::boolean,
   'pending_events',(SELECT count(*) FROM qdrant_internal.outbox e
      LEFT JOIN qdrant_internal.event_ack a USING(event_id) WHERE e.index_name=p_index_name
      AND (a.event_id IS NULL OR a.storage_epoch IS DISTINCT FROM c.storage_epoch)));
END $$;

CREATE FUNCTION qdrant.await_changes(ticket uuid, timeout_ms integer DEFAULT 5000) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb; deadline timestamptz;
BEGIN
 IF timeout_ms IS NULL OR timeout_ms NOT BETWEEN 0 AND 120000 THEN
   RAISE EXCEPTION 'timeout_ms must be 0..120000' USING ERRCODE='22023';
 END IF;
 IF current_setting('transaction_isolation')<>'read committed' THEN
   RAISE EXCEPTION 'await_changes requires a fresh READ COMMITTED transaction' USING ERRCODE='25001';
 END IF;
 result:=qdrant_internal.ticket_status(ticket);
 PERFORM qdrant_internal.p1_start_consumer();
 deadline:=clock_timestamp()+timeout_ms*interval '1 millisecond';
 LOOP
   result:=qdrant_internal.ticket_status(ticket);
   IF (result->>'durable')::boolean OR result->>'error'='generation_invalidated' THEN RETURN result; END IF;
   IF clock_timestamp()>=deadline THEN RETURN result || jsonb_build_object('timed_out',true); END IF;
   PERFORM pg_sleep(0.01);
 END LOOP;
END $$;

CREATE FUNCTION qdrant.drop_index(p_index_name text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE;
BEGIN
 PERFORM qdrant_internal.require_index(p_index_name,true);
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_index_name FOR UPDATE;
 IF EXISTS(SELECT 1 FROM pg_class WHERE oid=i.source_oid) THEN
   EXECUTE format('DROP TRIGGER IF EXISTS qdrant_p1_rows ON %s',i.source_oid::regclass);
   EXECUTE format('DROP TRIGGER IF EXISTS qdrant_p1_truncate ON %s',i.source_oid::regclass);
 END IF;
 DELETE FROM qdrant_internal.index_catalog WHERE index_name=p_index_name;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA qdrant_internal FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA qdrant_internal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA qdrant_internal FROM PUBLIC;
GRANT EXECUTE ON FUNCTION qdrant.create_index(text,regclass,name,jsonb),qdrant.index_status(text),
 qdrant.drop_index(text),qdrant.track_changes(text),qdrant.await_changes(uuid,integer) TO PUBLIC;

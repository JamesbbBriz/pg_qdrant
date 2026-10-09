-- Full-domain statistics refuse incomplete or concurrently changed source facts.
CREATE FUNCTION qdrant_internal.source_statistics(p_name text,p_facet text,p_limit integer,p_options jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; c qdrant_internal.consumer_state%ROWTYPE;
 plan jsonb; ids jsonb; result jsonb; proof jsonb; current_ids jsonb; valid boolean; facet_kind text;
BEGIN
 IF p_name IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 OR p_options IS NULL
    OR jsonb_typeof(p_options) IS DISTINCT FROM 'object'
    OR p_options-ARRAY['matching','filter','timeout_ms']<>'{}' OR octet_length(p_options::text)>16384 THEN
   RAISE EXCEPTION 'Statistics admit matching/filter/timeout_ms and facet limit 1..100' USING ERRCODE='22023'; END IF;
 IF current_setting('transaction_isolation')<>'read committed' THEN
   RAISE EXCEPTION 'Statistics require read committed source rechecks' USING ERRCODE='0A000'; END IF;
 PERFORM qdrant_internal.require_index(p_name);
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_name FOR KEY SHARE NOWAIT;
 EXECUTE format('LOCK TABLE ONLY %s IN ACCESS SHARE MODE NOWAIT',i.source_oid::regclass);
 PERFORM qdrant_internal.require_readable_source(p_name);
 IF p_facet IS NOT NULL THEN
   SELECT kind INTO facet_kind FROM qdrant_internal.payload_catalog WHERE index_name=p_name AND name=p_facet;
   IF facet_kind IS NULL THEN RAISE EXCEPTION 'Unknown declared facet alias' USING ERRCODE='22023'; END IF;
   IF facet_kind NOT IN ('keyword','integer','bool') THEN
     RAISE EXCEPTION 'Facet scalar kind is unsupported' USING ERRCODE='0A000'; END IF;
 END IF;
 plan:=qdrant.explain_search(p_name,'statistics','text',1,'{}',p_options);
 SELECT * INTO STRICT c FROM qdrant_internal.consumer_state WHERE index_name=p_name;
 IF NOT i.backfill_done OR EXISTS(SELECT 1 FROM qdrant_internal.outbox e LEFT JOIN qdrant_internal.event_ack a USING(event_id)
     WHERE e.index_name=p_name AND (a.event_id IS NULL OR a.storage_epoch<>c.storage_epoch)) THEN
   RAISE EXCEPTION 'Statistics require an entirely durable source generation' USING ERRCODE='55000'; END IF;
 SELECT coalesce(jsonb_agg(point_id ORDER BY point_id),'[]') INTO ids FROM
   (SELECT point_id FROM qdrant_internal.source_state WHERE index_name=p_name AND NOT tombstone ORDER BY point_id LIMIT 1001) s;
 IF jsonb_array_length(ids)>1000 THEN RAISE EXCEPTION 'Statistics source-domain budget exceeds 1000 live points' USING ERRCODE='54000'; END IF;
 result:=qdrant_internal.p1_statistics(jsonb_build_object('operation','source_statistics','index_id',i.index_id,
   'generation',i.generation,'storage_epoch',c.storage_epoch,'point_ids',ids,'facet',p_facet,'facet_limit',p_limit,
   'predicates',(plan->'matching')||CASE WHEN plan->'payload_filter'<>'null'::jsonb
      THEN jsonb_build_object('payload_filter',plan->'payload_filter') ELSE '{}'::jsonb END),(plan->>'timeout_ms')::integer);
 proof:=result->'proof';
 IF jsonb_typeof(proof) IS DISTINCT FROM 'array' OR jsonb_array_length(proof)<>jsonb_array_length(ids)
    OR EXISTS(SELECT 1 FROM jsonb_array_elements(proof) h WHERE NOT ids @> jsonb_build_array(h->'id'))
    OR (SELECT count(DISTINCT h->'id') FROM jsonb_array_elements(proof) h)<>jsonb_array_length(ids)
    OR jsonb_typeof(result->'points') IS DISTINCT FROM 'number'
    OR (result->>'points')::bigint NOT BETWEEN 0 AND jsonb_array_length(ids) THEN
   RAISE EXCEPTION 'Invalid native statistics response' USING ERRCODE='XX000'; END IF;
 PERFORM qdrant_internal.require_readable_source(p_name);
 SELECT coalesce(jsonb_agg(point_id ORDER BY point_id),'[]') INTO current_ids
 FROM qdrant_internal.source_state WHERE index_name=p_name AND NOT tombstone;
 IF current_ids IS DISTINCT FROM ids OR NOT EXISTS(SELECT 1 FROM qdrant_internal.index_catalog x
     JOIN qdrant_internal.consumer_state cs USING(index_name) WHERE x.index_name=p_name
     AND x.generation=i.generation AND cs.generation=c.generation AND cs.storage_epoch=c.storage_epoch
     AND cs.consumer_id=c.consumer_id AND cs.engine_instance=c.engine_instance)
    OR EXISTS(SELECT 1 FROM qdrant_internal.outbox e LEFT JOIN qdrant_internal.event_ack a USING(event_id)
     WHERE e.index_name=p_name AND (a.event_id IS NULL OR a.storage_epoch<>c.storage_epoch)) THEN
   RAISE EXCEPTION 'Source or generation changed during statistics' USING ERRCODE='55000'; END IF;
 -- Validate every source row, including rows the matching/filter clauses exclude.
 EXECUTE format('SELECT count(*)=jsonb_array_length($1) AND coalesce(bool_and((
     s.point_id IS NOT NULL AND h.hit IS NOT NULL AND s.tagged_key=h.hit #> ''{payload,source_key}''
     AND s.incarnation::text=h.hit #>> ''{payload,incarnation}''
     AND s.revision=(h.hit #>> ''{payload,revision}'')::bigint
     AND s.fingerprint=h.hit #>> ''{payload,fingerprint}''
     AND s.payload_fingerprint=h.hit #>> ''{payload,payload_fingerprint}''
     AND encode(sha256(convert_to(t.%I,''UTF8'')),''hex'')=s.fingerprint
     AND encode(sha256(convert_to(qdrant_internal.payload_projection($2,t)::text,''UTF8'')),''hex'')=s.payload_fingerprint) IS TRUE),true)
   FROM ONLY %s t LEFT JOIN qdrant_internal.source_state s ON s.index_name=$2 AND NOT s.tombstone
     AND s.tagged_key->>''value''=t.%I::text
   LEFT JOIN LATERAL (SELECT native.hit FROM jsonb_array_elements($3) AS native(hit) WHERE (native.hit->>''id'')::bigint=s.point_id) h ON true',
   i.text_field,i.source_oid::regclass,i.key_field)
 INTO valid USING ids,p_name,proof;
 IF valid IS DISTINCT FROM true THEN RAISE EXCEPTION 'Current source statistics proof failed' USING ERRCODE='55000'; END IF;
 result:=(result-'proof')||jsonb_build_object('index_name',p_name,'generation',i.generation,'storage_epoch',c.storage_epoch,
   'unit','points','documents',NULL,'exact',true,'domain','entire current durable owner source; explicit matching/filter predicates',
   'live_points',jsonb_array_length(ids),'max_live_points',1000,'matching',plan->'matching','filter',plan->'payload_filter',
   'release_supported',false);
 IF octet_length(result::text)>262144 THEN RAISE EXCEPTION 'Statistics result exceeds 256 KiB' USING ERRCODE='54000'; END IF;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION qdrant_internal.source_statistics(text,text,integer,jsonb),qdrant_internal.p1_statistics(jsonb,integer) FROM PUBLIC;

CREATE FUNCTION qdrant.count(index_name text,options jsonb DEFAULT '{}') RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT qdrant_internal.source_statistics(index_name,NULL,1,options)
$$;
CREATE FUNCTION qdrant.facet(index_name text,field text,limit_count integer DEFAULT 10,options jsonb DEFAULT '{}') RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF field IS NULL THEN RAISE EXCEPTION 'Facet field is required' USING ERRCODE='22023'; END IF;
 RETURN qdrant_internal.source_statistics(index_name,field,limit_count,options);
END $$;
GRANT EXECUTE ON FUNCTION qdrant.count(text,jsonb),qdrant.facet(text,text,integer,jsonb) TO PUBLIC;

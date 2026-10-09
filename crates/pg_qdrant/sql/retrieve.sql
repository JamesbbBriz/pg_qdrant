-- Source-key retrieval: native records are never caller-visible before SQL recheck.
CREATE FUNCTION qdrant_internal.require_readable_source(p_name text) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; c qdrant_internal.consumer_state%ROWTYPE;
BEGIN
 PERFORM qdrant_internal.require_index(p_name);
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_name;
 IF EXISTS(SELECT 1 FROM pg_class WHERE oid=i.source_oid AND (relrowsecurity OR relforcerowsecurity)) THEN
   RAISE EXCEPTION 'RLS read sources are unsupported' USING ERRCODE='0A000'; END IF;
 IF qdrant_internal.p1_index_status(p_name)->>'capture_state' IS DISTINCT FROM 'capturing' THEN
   RAISE EXCEPTION 'Source binding changed; rebuild required' USING ERRCODE='55000'; END IF;
 SELECT * INTO c FROM qdrant_internal.consumer_state WHERE index_name=p_name;
 IF c.state IS DISTINCT FROM 'ready' OR c.generation IS DISTINCT FROM i.generation
    OR NOT coalesce(qdrant_internal.p1_service_ready(c.engine_instance),false) THEN
   RAISE EXCEPTION 'Source generation is not ready' USING ERRCODE='55000'; END IF;
END $$;

CREATE FUNCTION qdrant.retrieve(index_name text,source_keys jsonb,options jsonb DEFAULT '{}') RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; c qdrant_internal.consumer_state%ROWTYPE;
 normalized jsonb:='[]'; ids jsonb; hits jsonb; items jsonb; key_value text; input jsonb;
 timeout_ms integer:=5000;
BEGIN
 IF index_name IS NULL OR source_keys IS NULL OR jsonb_typeof(source_keys) IS DISTINCT FROM 'array'
    OR jsonb_array_length(source_keys) NOT BETWEEN 1 AND 100
    OR octet_length(source_keys::text)>131072 THEN
   RAISE EXCEPTION 'retrieve requires 1..100 source key strings within 128 KiB' USING ERRCODE='22023'; END IF;
 IF options IS NULL OR jsonb_typeof(options) IS DISTINCT FROM 'object' OR options-'timeout_ms'<>'{}' THEN
   RAISE EXCEPTION 'Only retrieve timeout_ms is supported' USING ERRCODE='22023'; END IF;
 IF options ? 'timeout_ms' THEN
   IF jsonb_typeof(options->'timeout_ms') IS DISTINCT FROM 'number' OR (options->>'timeout_ms')!~'^[0-9]{1,5}$' THEN
     RAISE EXCEPTION 'timeout_ms must be an integer in 1..30000' USING ERRCODE='22023'; END IF;
   timeout_ms:=(options->>'timeout_ms')::integer;
 END IF;
 IF timeout_ms NOT BETWEEN 1 AND 30000 THEN RAISE EXCEPTION 'timeout_ms outside 1..30000' USING ERRCODE='22023'; END IF;
 PERFORM qdrant_internal.require_index(index_name);
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog x WHERE x.index_name=retrieve.index_name FOR KEY SHARE NOWAIT;
 EXECUTE format('LOCK TABLE ONLY %s IN ACCESS SHARE MODE NOWAIT',i.source_oid::regclass);
 PERFORM qdrant_internal.require_readable_source(index_name);
 SELECT * INTO STRICT c FROM qdrant_internal.consumer_state x WHERE x.index_name=i.index_name;
 FOR input IN SELECT value FROM jsonb_array_elements(source_keys) LOOP
   IF jsonb_typeof(input) IS DISTINCT FROM 'string' OR octet_length(input#>>'{}')>1024 THEN
     RAISE EXCEPTION 'Each source key must be a string of at most 1024 bytes' USING ERRCODE='22023'; END IF;
   key_value:=input#>>'{}';
   BEGIN
     IF i.key_type='int8'::regtype THEN key_value:=(key_value::bigint)::text;
     ELSIF i.key_type='uuid'::regtype THEN key_value:=(key_value::uuid)::text; END IF;
   EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
     RAISE EXCEPTION 'Source key does not match the registered primary key type' USING ERRCODE='22023';
   END;
   IF normalized ? key_value THEN RAISE EXCEPTION 'Duplicate normalized source key' USING ERRCODE='22023'; END IF;
   normalized:=normalized||jsonb_build_array(key_value);
 END LOOP;
 SELECT coalesce(jsonb_agg(s.point_id ORDER BY k.ordinality),'[]') INTO ids
 FROM jsonb_array_elements_text(normalized) WITH ORDINALITY k(value,ordinality)
 JOIN qdrant_internal.source_state s ON s.index_name=i.index_name AND s.tagged_key->>'value'=k.value AND NOT s.tombstone;
 hits:=qdrant_internal.p1_retrieve(jsonb_build_object('operation','source_retrieve','index_id',i.index_id,
   'generation',i.generation,'storage_epoch',c.storage_epoch,'point_ids',ids),timeout_ms);
 IF jsonb_typeof(hits) IS DISTINCT FROM 'array' OR jsonb_array_length(hits)>jsonb_array_length(ids)
    OR EXISTS(SELECT 1 FROM jsonb_array_elements(hits) h WHERE NOT ids @> jsonb_build_array(h->'id'))
    OR (SELECT count(*) FROM jsonb_array_elements(hits))<>(SELECT count(DISTINCT h->'id') FROM jsonb_array_elements(hits) h) THEN
   RAISE EXCEPTION 'Invalid native retrieve response' USING ERRCODE='XX000'; END IF;
 PERFORM qdrant_internal.require_readable_source(index_name);
 IF NOT EXISTS(SELECT 1 FROM qdrant_internal.index_catalog x JOIN qdrant_internal.consumer_state cs USING(index_name)
     WHERE x.index_name=i.index_name AND x.generation=i.generation AND cs.generation=c.generation
       AND cs.storage_epoch=c.storage_epoch AND cs.consumer_id=c.consumer_id AND cs.engine_instance=c.engine_instance) THEN
   RAISE EXCEPTION 'Generation changed during retrieve' USING ERRCODE='55000'; END IF;
 -- A single fresh SQL statement joins current facts and ledger versions. No stale
 -- native body, vector, score or old-incarnation key can cross this boundary.
 EXECUTE format('SELECT jsonb_agg(CASE WHEN t.%I IS NULL OR s.point_id IS NULL THEN
     jsonb_build_object(''source_key'',k.value,''status'',''source_missing'')
   WHEN h.hit IS NULL THEN jsonb_build_object(''source_key'',k.value,''status'',''native_missing'')
   WHEN s.incarnation::text IS DISTINCT FROM h.hit #>> ''{payload,incarnation}''
     OR s.revision IS DISTINCT FROM (h.hit #>> ''{payload,revision}'')::bigint
     OR s.tagged_key IS DISTINCT FROM h.hit #> ''{payload,source_key}''
     OR s.fingerprint IS DISTINCT FROM h.hit #>> ''{payload,fingerprint}''
     OR s.payload_fingerprint IS DISTINCT FROM h.hit #>> ''{payload,payload_fingerprint}''
     OR encode(sha256(convert_to(qdrant_internal.payload_projection($2,t)::text,''UTF8'')),''hex'') IS DISTINCT FROM s.payload_fingerprint
     OR encode(sha256(convert_to(t.%I,''UTF8'')),''hex'') IS DISTINCT FROM s.fingerprint THEN
       jsonb_build_object(''source_key'',k.value,''status'',''native_stale'')
   ELSE jsonb_build_object(''source_key'',k.value,''status'',''found'',''excerpt'',left(t.%I,240),
     ''point_id'',s.point_id,''revision'',s.revision,''incarnation'',s.incarnation,''source_fingerprint'',s.fingerprint,
     ''attributes'',qdrant_internal.payload_projection($2,t),''payload_fingerprint'',s.payload_fingerprint) END
   ORDER BY k.ordinality)
   FROM jsonb_array_elements_text($1) WITH ORDINALITY k(value,ordinality)
   LEFT JOIN ONLY %s t ON t.%I=k.value::%s
   LEFT JOIN qdrant_internal.source_state s ON s.index_name=$2 AND s.tagged_key->>''value''=k.value AND NOT s.tombstone
   LEFT JOIN LATERAL (SELECT v AS hit FROM jsonb_array_elements($3) v WHERE (v->>''id'')::bigint=s.point_id) h ON true',
   i.key_field,i.text_field,i.text_field,i.source_oid::regclass,i.key_field,i.key_type::regtype)
 INTO items USING normalized,i.index_name,hits;
 IF octet_length(items::text)>262144 THEN RAISE EXCEPTION 'Retrieve result exceeds 256 KiB' USING ERRCODE='54000'; END IF;
 RETURN jsonb_build_object('index_name',i.index_name,'generation',i.generation,'storage_epoch',c.storage_epoch,
   'scope','requested current source keys','ordering','normalized request order','items',items,
   'release_supported',false);
END $$;
GRANT EXECUTE ON FUNCTION qdrant.retrieve(text,jsonb,jsonb) TO PUBLIC;

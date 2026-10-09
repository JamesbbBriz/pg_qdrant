-- Only declared, typed source columns enter the owned payload namespace.
CREATE TABLE qdrant_internal.payload_catalog (
 index_name text NOT NULL REFERENCES qdrant_internal.index_catalog ON DELETE CASCADE,
 name text NOT NULL CHECK(name ~ '^[a-z][a-z0-9_]{0,31}$'),
 field text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('keyword','integer','float','bool')),
 source_type oid NOT NULL,
 PRIMARY KEY(index_name,name)
);

CREATE FUNCTION qdrant_internal.register_payload(p_name text,p_contract jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; slot record; expected oid; actual oid;
BEGIN
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_name;
 IF p_contract IS NULL OR jsonb_typeof(p_contract) IS DISTINCT FROM 'object' THEN
   RAISE EXCEPTION 'payload must be a declared field map' USING ERRCODE='22023'; END IF;
 IF (SELECT count(*) FROM jsonb_each(p_contract))>8 THEN
   RAISE EXCEPTION 'At most eight payload fields are supported' USING ERRCODE='54000'; END IF;
 FOR slot IN SELECT key,value FROM jsonb_each(p_contract) LOOP
   IF slot.key!~'^[a-z][a-z0-9_]{0,31}$' OR jsonb_typeof(slot.value) IS DISTINCT FROM 'object'
      OR slot.value-ARRAY['field','kind']<>'{}'
      OR jsonb_typeof(slot.value->'field') IS DISTINCT FROM 'string'
      OR jsonb_typeof(slot.value->'kind') IS DISTINCT FROM 'string' THEN
     RAISE EXCEPTION 'Each payload alias requires field and kind' USING ERRCODE='22023'; END IF;
   expected:=CASE slot.value->>'kind' WHEN 'keyword' THEN 'text'::regtype::oid
     WHEN 'integer' THEN 'int8'::regtype::oid WHEN 'float' THEN 'float8'::regtype::oid
     WHEN 'bool' THEN 'bool'::regtype::oid ELSE NULL END;
   IF expected IS NULL THEN RAISE EXCEPTION 'Unsupported payload kind' USING ERRCODE='0A000'; END IF;
   SELECT atttypid INTO actual FROM pg_attribute WHERE attrelid=i.source_oid
     AND attname=slot.value->>'field' AND attnum>0 AND NOT attisdropped;
   IF actual IS DISTINCT FROM expected THEN
     RAISE EXCEPTION 'Payload column type does not match kind: %',slot.key USING ERRCODE='22023'; END IF;
   INSERT INTO qdrant_internal.payload_catalog VALUES(p_name,slot.key,slot.value->>'field',slot.value->>'kind',actual);
 END LOOP;
END $$;

-- Preserve an exact event prefix within the IPC byte budget. IDs are membership,
-- never a commit-order watermark; unselected events remain unacknowledged.
CREATE FUNCTION qdrant_internal.pack_source_events(p_events jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE event jsonb; result jsonb:='[]'; used integer:=2; size integer;
BEGIN
 FOR event IN SELECT value FROM jsonb_array_elements(p_events) LOOP
   size:=octet_length(event::text)+2;
   IF used+size>491520 THEN
     IF result='[]'::jsonb THEN RAISE EXCEPTION 'Single source event exceeds the IPC budget' USING ERRCODE='54000'; END IF;
     EXIT;
   END IF;
   result:=result||jsonb_build_array(event); used:=used+size;
 END LOOP;
 RETURN result;
END $$;

CREATE FUNCTION qdrant_internal.payload_binding_valid(p_name text) RETURNS boolean
LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT NOT EXISTS(SELECT 1 FROM qdrant_internal.payload_catalog p
   JOIN qdrant_internal.index_catalog i USING(index_name)
   LEFT JOIN pg_attribute a ON a.attrelid=i.source_oid AND a.attname=p.field AND a.attnum>0 AND NOT a.attisdropped
   WHERE p.index_name=p_name AND a.atttypid IS DISTINCT FROM p.source_type)
$$;

CREATE FUNCTION qdrant_internal.require_payload_select(p_name text,p_actor oid) RETURNS void
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF NOT qdrant_internal.payload_binding_valid(p_name) THEN
   RAISE EXCEPTION 'Payload source binding changed' USING ERRCODE='55000'; END IF;
 IF EXISTS(SELECT 1 FROM qdrant_internal.payload_catalog p JOIN qdrant_internal.index_catalog i USING(index_name)
   WHERE p.index_name=p_name AND NOT has_column_privilege(p_actor,i.source_oid,p.field,'SELECT')) THEN
   RAISE EXCEPTION 'Declared payload column SELECT required' USING ERRCODE='42501'; END IF;
END $$;

CREATE FUNCTION qdrant_internal.payload_contract(p_name text) RETURNS jsonb
LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT coalesce(jsonb_object_agg(name,kind),'{}') FROM qdrant_internal.payload_catalog WHERE index_name=p_name
$$;

CREATE FUNCTION qdrant_internal.payload_projection(p_name text,p_row anyelement) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE slot record; value jsonb; result jsonb:='{}';
BEGIN
 FOR slot IN SELECT * FROM qdrant_internal.payload_catalog WHERE index_name=p_name ORDER BY name LOOP
   EXECUTE format('SELECT to_jsonb(($1).%I)',slot.field) INTO value USING p_row;
   value:=coalesce(value,'null'::jsonb);
   IF value<>'null'::jsonb AND ((slot.kind='keyword' AND (jsonb_typeof(value)<>'string' OR octet_length(value#>>'{}')>1024))
      OR (slot.kind IN ('integer','float') AND jsonb_typeof(value)<>'number')
      OR (slot.kind='bool' AND jsonb_typeof(value)<>'boolean')) THEN
     RAISE EXCEPTION 'Payload value outside its declared scalar contract: %',slot.name USING ERRCODE='22023'; END IF;
   result:=result||jsonb_build_object(slot.name,value);
 END LOOP;
 IF octet_length(result::text)>8192 THEN RAISE EXCEPTION 'Payload projection exceeds 8 KiB' USING ERRCODE='54000'; END IF;
 RETURN result;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA qdrant_internal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA qdrant_internal FROM PUBLIC;

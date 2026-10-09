ALTER FUNCTION qdrant.capabilities(text) SET SCHEMA qdrant_internal;
ALTER FUNCTION qdrant_internal.capabilities(text) RENAME TO capability_registry_base;
REVOKE ALL ON FUNCTION qdrant_internal.capability_registry_base(text) FROM PUBLIC;

CREATE FUNCTION qdrant.capabilities(index_name text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE registry jsonb; status jsonb; modes jsonb; m record; rep record;
 complete_kinds text[]:='{}'; required_ready boolean; index_ready boolean;
 i qdrant_internal.index_catalog%ROWTYPE;
BEGIN
 registry:=qdrant_internal.capability_registry_base(NULL);
 IF index_name IS NOT NULL THEN
   IF NOT (registry->>'index_catalog_available')::boolean THEN
     RAISE EXCEPTION 'Index capability discovery requires the managed helper build' USING ERRCODE='0A000';
   END IF;
   SELECT * INTO i FROM qdrant_internal.index_catalog x WHERE x.index_name=capabilities.index_name FOR KEY SHARE NOWAIT;
   IF NOT FOUND THEN RAISE EXCEPTION 'Unknown index' USING ERRCODE='22023'; END IF;
   PERFORM qdrant_internal.require_index(index_name);
   EXECUTE format('LOCK TABLE ONLY %s IN ACCESS SHARE MODE NOWAIT',i.source_oid::regclass);
   IF EXISTS(SELECT 1 FROM pg_class WHERE oid=i.source_oid AND (relrowsecurity OR relforcerowsecurity)) THEN
     RAISE EXCEPTION 'RLS source discovery is unsupported' USING ERRCODE='0A000';
   END IF;
   status:=qdrant.index_status(index_name);
   index_ready:=coalesce((status->>'engine_index_ready')::boolean,false);
   FOR rep IN SELECT key,value FROM jsonb_each(status->'representations') LOOP
     IF coalesce((rep.value #>> '{rows,ready}')::bigint,0)=(status->>'live_source_keys')::bigint
        AND coalesce((rep.value #>> '{rows,stale}')::bigint,0)=0
        AND coalesce((rep.value #>> '{rows,missing}')::bigint,0)=0
        AND coalesce((rep.value #>> '{rows,failed}')::bigint,0)=0
        AND NOT EXISTS(SELECT 1 FROM qdrant_internal.source_state s
          LEFT JOIN qdrant_internal.representation_state r
            ON r.index_name=s.index_name AND r.tagged_key=s.tagged_key AND r.name=rep.key
          WHERE s.index_name=i.index_name AND NOT s.tombstone AND
            (r.state IS DISTINCT FROM 'ready' OR r.incarnation IS DISTINCT FROM s.incarnation
             OR r.source_fingerprint IS DISTINCT FROM s.fingerprint)) THEN
       complete_kinds:=array_append(complete_kinds,rep.value #>> '{contract,kind}');
     END IF;
   END LOOP;
 END IF;
 modes:='[]';
 FOR m IN SELECT * FROM qdrant_internal.query_modes ORDER BY mode LOOP
   required_ready:=cardinality(m.required_kinds)=0 OR m.required_kinds && complete_kinds;
   modes:=modes||jsonb_build_array(jsonb_build_object('mode',m.mode,'capability_ids',m.capability_ids,
     'implementation_scope',m.implementation_scope,'adapter_available',(registry->>'index_catalog_available')::boolean,
     'required_representation_kinds',m.required_kinds,'required_slot_ready',CASE WHEN index_name IS NULL THEN NULL ELSE required_ready END,
     'index_ready',CASE WHEN index_name IS NULL THEN NULL ELSE index_ready END,
     'admission_ready',CASE WHEN index_name IS NULL THEN NULL ELSE index_ready AND required_ready END,
     'upstream_api_scope','pinned qdrant-edge 0.8.0 primitives; full capability acceptance remains open',
     'release_validation_passed',false,'query_input_validation_required',true,
     'native_query_executed',false,'release_supported',false));
 END LOOP;
 RETURN registry||jsonb_build_object('registry_schema_version',2,'query_modes',modes,
   'index_name',index_name,'index_state',CASE WHEN index_name IS NULL THEN NULL ELSE status-'owner_status' END,
   'ready_scope','current owner-domain source generation and complete declared slot kinds; query-specific budgets/inputs still required');
END $$;
GRANT EXECUTE ON FUNCTION qdrant.capabilities(text) TO PUBLIC;

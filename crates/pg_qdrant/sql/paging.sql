CREATE TABLE qdrant_internal.search_snapshots (
 snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 actor_oid oid NOT NULL,
 index_name text NOT NULL,
 index_id bigint NOT NULL REFERENCES qdrant_internal.index_catalog(index_id) ON DELETE CASCADE,
 generation uuid NOT NULL,
 storage_epoch uuid NOT NULL,
 consumer_id uuid NOT NULL,
 arguments_hash text NOT NULL,
 results jsonb NOT NULL CHECK(jsonb_typeof(results)='array' AND jsonb_array_length(results)<=100),
 candidate_limit integer NOT NULL CHECK(candidate_limit BETWEEN 1 AND 1000),
 captured_limit integer NOT NULL CHECK(captured_limit BETWEEN 1 AND 100),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '120 seconds',
 CHECK(octet_length(results::text)<=262144)
);
REVOKE ALL ON qdrant_internal.search_snapshots FROM PUBLIC;

CREATE FUNCTION qdrant.search_page(index_name text,q text,mode text DEFAULT 'text',
 page_size integer DEFAULT 20,query_vectors jsonb DEFAULT '{}',options jsonb DEFAULT '{}',
 page_cursor jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $pgq$
DECLARE i qdrant_internal.index_catalog%ROWTYPE;
 c qdrant_internal.consumer_state%ROWTYPE;
 s qdrant_internal.search_snapshots%ROWTYPE;
 actor oid; plan jsonb; arguments_hash text; captured_limit integer;
 offset_value integer:=0; selected_snapshot uuid; found_count integer; page jsonb;
BEGIN
 IF page_size IS NULL OR page_size NOT BETWEEN 1 AND 100 THEN
   RAISE EXCEPTION 'Page size requires 1..100' USING ERRCODE='22023';
 END IF;
 BEGIN
   captured_limit:=least(100,coalesce((options->>'candidate_limit')::integer,100));
 EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
   RAISE EXCEPTION 'Invalid candidate budget' USING ERRCODE='22023';
 END;
 plan:=qdrant.explain_search(index_name,q,mode,captured_limit,query_vectors,options);
 IF NOT coalesce((plan->>'search_executable')::boolean,false) THEN
   RAISE EXCEPTION 'Index is not ready for paging' USING ERRCODE='55000';
 END IF;
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog x WHERE x.index_name=search_page.index_name FOR KEY SHARE NOWAIT;
 EXECUTE format('LOCK TABLE ONLY %s IN ACCESS SHARE MODE NOWAIT',i.source_oid::regclass);
 actor:=qdrant_internal.actor_oid();
 SELECT * INTO STRICT c FROM qdrant_internal.consumer_state x WHERE x.index_name=i.index_name;
 arguments_hash:=encode(sha256(convert_to(jsonb_build_object('index_name',index_name,'q',q,'mode',mode,
   'query_vectors',query_vectors,'options',options)::text,'UTF8')),'hex');
 IF page_cursor IS NULL THEN
   -- Serialize quota admission without an unbounded lock wait. A cancelled
   -- initial query rolls back its entire snapshot transaction.
   IF NOT pg_try_advisory_xact_lock(1885824362,2) THEN
     RAISE EXCEPTION 'Paging snapshot admission busy; retry' USING ERRCODE='55P03';
   END IF;
   DELETE FROM qdrant_internal.search_snapshots WHERE expires_at<=clock_timestamp();
   IF (SELECT count(*) FROM qdrant_internal.search_snapshots)>=128 THEN
     RAISE EXCEPTION 'Paging snapshot capacity reached' USING ERRCODE='54000';
   END IF;
   SELECT coalesce(jsonb_agg(to_jsonb(h) ORDER BY h.rank),'[]'::jsonb) INTO s.results
   FROM qdrant.search(index_name,q,mode,captured_limit,query_vectors,options) h;
   IF octet_length(s.results::text)>262144 THEN
     RAISE EXCEPTION 'Paging snapshot exceeds 256 KiB' USING ERRCODE='54000';
   END IF;
   INSERT INTO qdrant_internal.search_snapshots(actor_oid,index_name,index_id,generation,storage_epoch,consumer_id,
     arguments_hash,results,candidate_limit,captured_limit)
   VALUES(actor,i.index_name,i.index_id,i.generation,c.storage_epoch,c.consumer_id,
     arguments_hash,s.results,(plan->>'candidate_limit')::integer,captured_limit)
   RETURNING * INTO s;
 ELSE
   IF jsonb_typeof(page_cursor)<>'object' OR page_cursor-'snapshot'-'offset'<>'{}'::jsonb
      OR jsonb_typeof(page_cursor->'snapshot') IS DISTINCT FROM 'string'
      OR jsonb_typeof(page_cursor->'offset') IS DISTINCT FROM 'number'
      OR (page_cursor->>'offset')!~'^[0-9]+$' THEN
     RAISE EXCEPTION 'Cursor requires snapshot UUID and integer offset only' USING ERRCODE='22023';
   END IF;
   BEGIN
     selected_snapshot:=(page_cursor->>'snapshot')::uuid;
     offset_value:=(page_cursor->>'offset')::integer;
   EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
     RAISE EXCEPTION 'Invalid cursor identity or offset' USING ERRCODE='22023';
   END;
   SELECT * INTO s FROM qdrant_internal.search_snapshots x WHERE x.snapshot_id=selected_snapshot;
   IF NOT FOUND THEN RAISE EXCEPTION 'Paging snapshot is absent or expired' USING ERRCODE='55000'; END IF;
   IF s.actor_oid IS DISTINCT FROM actor THEN RAISE EXCEPTION 'Paging cursor belongs to another role' USING ERRCODE='42501'; END IF;
   IF s.expires_at<=clock_timestamp() OR s.index_id<>i.index_id OR s.generation<>i.generation
      OR s.storage_epoch<>c.storage_epoch OR s.consumer_id<>c.consumer_id THEN
     RAISE EXCEPTION 'Paging snapshot expired or generation changed' USING ERRCODE='55000';
   END IF;
   IF s.arguments_hash<>arguments_hash OR offset_value>jsonb_array_length(s.results) THEN
     RAISE EXCEPTION 'Cursor query or offset differs from its captured domain' USING ERRCODE='22023';
   END IF;
 END IF;
 -- Check every captured identity, including already returned pages. Neither
 -- native segment addresses nor a mutable row location is a paging identity.
 EXECUTE format('SELECT count(*) FROM jsonb_array_elements($1) h(hit)
   JOIN qdrant_internal.source_state ss ON ss.index_name=$2
     AND ss.point_id=(h.hit #>> ''{provenance,point_id}'')::bigint
   JOIN ONLY %s t ON t.%I=(h.hit->>''source_key'')::%s
   WHERE NOT ss.tombstone AND ss.revision=(h.hit #>> ''{provenance,revision}'')::bigint
     AND ss.incarnation::text=h.hit #>> ''{provenance,incarnation}''
     AND encode(sha256(convert_to(t.%I,''UTF8'')),''hex'')=h.hit #>> ''{provenance,source_fingerprint}''',
   i.source_oid::regclass,i.key_field,i.key_type::regtype,i.text_field)
 INTO found_count USING s.results,i.index_name;
 IF found_count<>jsonb_array_length(s.results) THEN
   RAISE EXCEPTION 'Cached source identity or content changed; restart search' USING ERRCODE='55000';
 END IF;
 plan:=qdrant.explain_search(index_name,q,mode,captured_limit,query_vectors,options);
 IF NOT coalesce((plan->>'search_executable')::boolean,false) THEN
   RAISE EXCEPTION 'Index readiness changed during paging' USING ERRCODE='55000';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM qdrant_internal.consumer_state x WHERE x.index_name=i.index_name
   AND x.storage_epoch=s.storage_epoch AND x.consumer_id=s.consumer_id AND x.state='ready') THEN
   RAISE EXCEPTION 'Owner generation changed during paging' USING ERRCODE='55000';
 END IF;
 SELECT coalesce(jsonb_agg(hit ORDER BY n),'[]'::jsonb) INTO page
 FROM jsonb_array_elements(s.results) WITH ORDINALITY h(hit,n)
 WHERE n>offset_value AND n<=offset_value+page_size;
 RETURN jsonb_build_object('hits',page,'next_cursor',CASE WHEN offset_value+page_size<jsonb_array_length(s.results)
   THEN jsonb_build_object('snapshot',s.snapshot_id,'offset',offset_value+page_size) ELSE NULL END,
   'snapshot',s.snapshot_id,'created_at',s.created_at,'expires_at',s.expires_at,
   'generation',s.generation,'storage_epoch',s.storage_epoch,
   'captured_count',jsonb_array_length(s.results),'candidate_limit',s.candidate_limit,'captured_limit',s.captured_limit,
   'statistics_scope','captured authorized source-rechecked bounded candidates',
   'global_match_count',NULL,'global_coverage_verified',false,'source_rechecked',true,
   'native_query_executed',page_cursor IS NULL,'release_supported',false);
END $pgq$;
GRANT EXECUTE ON FUNCTION qdrant.search_page(text,text,text,integer,jsonb,jsonb,jsonb) TO PUBLIC;

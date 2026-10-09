-- Full-domain statistics refuse incomplete or concurrently changed source facts.
CREATE FUNCTION qdrant_internal.admit_matrix(p_name text,p_matrix jsonb) RETURNS void
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE c jsonb; source_id oid; field text; live bigint;
BEGIN
 IF jsonb_typeof(p_matrix) IS DISTINCT FROM 'object'
    OR NOT p_matrix ?& ARRAY['representation','sample_size','neighbors']
    OR p_matrix-ARRAY['representation','sample_size','neighbors']<>'{}'
    OR jsonb_typeof(p_matrix->'representation') IS DISTINCT FROM 'string'
    OR p_matrix->>'sample_size' !~ '^[0-9]{1,2}$' OR p_matrix->>'neighbors' !~ '^[0-9]{1,2}$'
    OR jsonb_typeof(p_matrix->'sample_size') IS DISTINCT FROM 'number'
    OR jsonb_typeof(p_matrix->'neighbors') IS DISTINCT FROM 'number'
    OR (p_matrix->>'sample_size')::integer NOT BETWEEN 1 AND 64
    OR (p_matrix->>'neighbors')::integer NOT BETWEEN 1 AND 32 THEN
   RAISE EXCEPTION 'Matrix requires a declared representation, sample 1..64 and neighbors 1..32' USING ERRCODE='22023'; END IF;
 SELECT contract INTO c FROM qdrant_internal.representation_catalog
   WHERE index_name=p_name AND name=p_matrix->>'representation';
 IF c IS NULL OR c->>'kind'<>'dense' THEN
   RAISE EXCEPTION 'Matrix requires a declared dense representation' USING ERRCODE='22023'; END IF;
 SELECT source_oid INTO STRICT source_id FROM qdrant_internal.index_catalog WHERE index_name=p_name;
 FOREACH field IN ARRAY ARRAY[c->>'vector_field',c->>'fingerprint_field',c->>'incarnation_field',c->>'model_id_field',c->>'model_version_field'] LOOP
   IF NOT has_column_privilege(qdrant_internal.actor_oid(),source_id,field,'SELECT') THEN
     RAISE EXCEPTION 'Matrix representation source column SELECT required' USING ERRCODE='42501'; END IF;
 END LOOP;
 SELECT count(*) INTO live FROM qdrant_internal.source_state WHERE index_name=p_name AND NOT tombstone;
 IF live*(p_matrix->>'sample_size')::bigint*(c->>'dimensions')::bigint>20000000 THEN
   RAISE EXCEPTION 'Matrix scalar work exceeds 20000000' USING ERRCODE='54000'; END IF;
 IF EXISTS(SELECT 1 FROM qdrant_internal.source_state s LEFT JOIN qdrant_internal.representation_state r
   ON r.index_name=s.index_name AND r.tagged_key=s.tagged_key AND r.name=p_matrix->>'representation'
   WHERE s.index_name=p_name AND NOT s.tombstone AND (r.state IS DISTINCT FROM 'ready'
     OR r.incarnation IS DISTINCT FROM s.incarnation OR r.source_fingerprint IS DISTINCT FROM s.fingerprint)) THEN
   RAISE EXCEPTION 'Matrix representation is missing, stale or failed' USING ERRCODE='55000'; END IF;
END $$;
REVOKE ALL ON FUNCTION qdrant_internal.admit_matrix(text,jsonb) FROM PUBLIC;

CREATE FUNCTION qdrant_internal.admit_groups(p_name text,p_groups jsonb) RETURNS void
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE kind text; dimensions bigint; live bigint;
BEGIN
 IF jsonb_typeof(p_groups) IS DISTINCT FROM 'object'
    OR NOT p_groups ?& ARRAY['field','q','groups','group_size','mode','vectors']
    OR p_groups-ARRAY['field','q','groups','group_size','mode','vectors']<>'{}'
    OR jsonb_typeof(p_groups->'field') IS DISTINCT FROM 'string'
    OR jsonb_typeof(p_groups->'q') IS DISTINCT FROM 'string'
    OR jsonb_typeof(p_groups->'mode') IS DISTINCT FROM 'string'
    OR p_groups->>'mode' NOT IN ('text','semantic')
    OR jsonb_typeof(p_groups->'vectors') IS DISTINCT FROM 'object'
    OR jsonb_typeof(p_groups->'groups') IS DISTINCT FROM 'number'
    OR jsonb_typeof(p_groups->'group_size') IS DISTINCT FROM 'number'
    OR p_groups->>'groups' !~ '^[0-9]{1,2}$' OR p_groups->>'group_size' !~ '^[0-9]{1,2}$'
    OR (p_groups->>'groups')::integer NOT BETWEEN 1 AND 32
    OR (p_groups->>'group_size')::integer NOT BETWEEN 1 AND 16
    OR (p_groups->>'groups')::integer*(p_groups->>'group_size')::integer>256 THEN
   RAISE EXCEPTION 'Groups require text/semantic query, declared field, groups 1..32, size 1..16 and at most 256 hits' USING ERRCODE='22023'; END IF;
 SELECT p.kind INTO kind FROM qdrant_internal.payload_catalog p WHERE p.index_name=p_name AND p.name=p_groups->>'field';
 IF kind IS NULL THEN RAISE EXCEPTION 'Unknown grouping alias' USING ERRCODE='22023'; END IF;
 IF kind NOT IN ('keyword','integer') THEN RAISE EXCEPTION 'Grouping requires keyword or integer field' USING ERRCODE='0A000'; END IF;
 IF p_groups->>'mode'='semantic' THEN
   SELECT (contract->>'dimensions')::bigint INTO dimensions FROM qdrant_internal.representation_catalog
     WHERE index_name=p_name AND name=(SELECT key FROM jsonb_each(p_groups->'vectors') LIMIT 1);
   SELECT count(*) INTO live FROM qdrant_internal.source_state WHERE index_name=p_name AND NOT tombstone;
   IF dimensions*live*10>20000000 THEN RAISE EXCEPTION 'Group dense scalar work exceeds 20000000' USING ERRCODE='54000'; END IF;
 END IF;
END $$;
REVOKE ALL ON FUNCTION qdrant_internal.admit_groups(text,jsonb) FROM PUBLIC;

CREATE FUNCTION qdrant_internal.source_statistics(p_name text,p_facet text,p_limit integer,p_options jsonb,p_matrix jsonb DEFAULT NULL,p_groups jsonb DEFAULT NULL,p_lexical jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; c qdrant_internal.consumer_state%ROWTYPE;
 plan jsonb; ids jsonb; result jsonb; proof jsonb; current_ids jsonb; valid boolean; facet_kind text; group_request jsonb;
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
 IF p_lexical IS NOT NULL THEN
   IF p_matrix IS NOT NULL OR p_groups IS NOT NULL OR p_facet IS NOT NULL THEN
     RAISE EXCEPTION 'Lexical query cannot combine statistics operations' USING ERRCODE='22023'; END IF;
   IF jsonb_typeof(p_lexical) IS DISTINCT FROM 'object' OR NOT p_lexical ?& ARRAY['q','kind','slop','top_k']
      OR p_lexical-ARRAY['q','kind','slop','top_k']<>'{}'
      OR jsonb_typeof(p_lexical->'q') IS DISTINCT FROM 'string'
      OR jsonb_typeof(p_lexical->'kind') IS DISTINCT FROM 'string'
      OR p_lexical->>'kind' NOT IN ('fuzzy','proximity')
      OR jsonb_typeof(p_lexical->'slop') IS DISTINCT FROM 'number'
      OR p_lexical->>'slop' !~ '^[0-8]$'
      OR jsonb_typeof(p_lexical->'top_k') IS DISTINCT FROM 'number'
      OR p_lexical->>'top_k' !~ '^[0-9]{1,3}$'
      OR (p_lexical->>'top_k')::integer NOT BETWEEN 1 AND 100
      OR octet_length(p_lexical->>'q')>256 THEN
     RAISE EXCEPTION 'Invalid bounded lexical query' USING ERRCODE='22023'; END IF;
 END IF;
 IF p_matrix IS NOT NULL THEN PERFORM qdrant_internal.admit_matrix(p_name,p_matrix); END IF;
 IF p_groups IS NOT NULL THEN
   IF p_matrix IS NOT NULL OR p_facet IS NOT NULL THEN RAISE EXCEPTION 'Grouping cannot combine statistics operations' USING ERRCODE='22023'; END IF;
   PERFORM qdrant_internal.admit_groups(p_name,p_groups);
 END IF;
 IF p_facet IS NOT NULL THEN
   SELECT kind INTO facet_kind FROM qdrant_internal.payload_catalog WHERE index_name=p_name AND name=p_facet;
   IF facet_kind IS NULL THEN RAISE EXCEPTION 'Unknown declared facet alias' USING ERRCODE='22023'; END IF;
   IF facet_kind NOT IN ('keyword','integer','bool') THEN
     RAISE EXCEPTION 'Facet scalar kind is unsupported' USING ERRCODE='0A000'; END IF;
 END IF;
 IF p_groups IS NULL THEN
   plan:=qdrant.explain_search(p_name,'statistics','text',1,'{}',p_options);
 ELSE
   plan:=qdrant.explain_search(p_name,p_groups->>'q',p_groups->>'mode',1,p_groups->'vectors',p_options);
   IF NOT coalesce((plan->>'search_executable')::boolean,false) THEN RAISE EXCEPTION 'Group index is not ready' USING ERRCODE='55000'; END IF;
   group_request:=(p_groups-ARRAY['mode','vectors'])||jsonb_build_object('representation_query',plan->'representation_query');
 END IF;
 SELECT * INTO STRICT c FROM qdrant_internal.consumer_state WHERE index_name=p_name;
 IF NOT i.backfill_done OR EXISTS(SELECT 1 FROM qdrant_internal.outbox e LEFT JOIN qdrant_internal.event_ack a USING(event_id)
     WHERE e.index_name=p_name AND (a.event_id IS NULL OR a.storage_epoch<>c.storage_epoch)) THEN
   RAISE EXCEPTION 'Statistics require an entirely durable source generation' USING ERRCODE='55000'; END IF;
 SELECT coalesce(jsonb_agg(point_id ORDER BY point_id),'[]') INTO ids FROM
   (SELECT point_id FROM qdrant_internal.source_state WHERE index_name=p_name AND NOT tombstone ORDER BY point_id LIMIT 1001) s;
 IF jsonb_array_length(ids)>1000 THEN RAISE EXCEPTION 'Statistics source-domain budget exceeds 1000 live points' USING ERRCODE='54000'; END IF;
 result:=qdrant_internal.p1_statistics(jsonb_build_object('operation','source_statistics','index_id',i.index_id,
   'generation',i.generation,'storage_epoch',c.storage_epoch,'point_ids',ids,'facet',p_facet,'facet_limit',p_limit,'matrix',p_matrix,'groups',group_request,'lexical',p_lexical,
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
 IF p_lexical IS NOT NULL THEN
   IF jsonb_typeof(result #> '{lexical,hits}') IS DISTINCT FROM 'array'
      OR jsonb_array_length(result #> '{lexical,hits}')>(p_lexical->>'top_k')::integer
      OR EXISTS(SELECT 1 FROM jsonb_array_elements(result #> '{lexical,hits}') h WHERE NOT ids @> jsonb_build_array(h->'id'))
      OR (SELECT count(DISTINCT h->'id') FROM jsonb_array_elements(result #> '{lexical,hits}') h)<>jsonb_array_length(result #> '{lexical,hits}') THEN
     RAISE EXCEPTION 'Invalid lexical source membership' USING ERRCODE='XX000'; END IF;
   result:=jsonb_set(result,'{lexical,hits}',(SELECT coalesce(jsonb_agg(jsonb_build_object(
     'source_key',h #> '{payload,source_key}','score',hit->'score','rank',rank) ORDER BY rank),'[]')
     FROM jsonb_array_elements(result #> '{lexical,hits}') WITH ORDINALITY members(hit,rank)
     JOIN jsonb_array_elements(proof) h ON h->'id'=hit->'id'));
 END IF;
 IF p_groups IS NOT NULL THEN
   PERFORM qdrant_internal.admit_groups(p_name,p_groups);
   plan:=qdrant.explain_search(p_name,p_groups->>'q',p_groups->>'mode',1,p_groups->'vectors',p_options);
   IF NOT coalesce((plan->>'search_executable')::boolean,false) THEN RAISE EXCEPTION 'Group readiness changed' USING ERRCODE='55000'; END IF;
   EXECUTE format('SELECT NOT EXISTS(SELECT 1 FROM jsonb_array_elements($1) g,
     LATERAL jsonb_array_elements(g->''hits'') hit LEFT JOIN qdrant_internal.source_state s
       ON s.index_name=$2 AND s.point_id=(hit->>''id'')::bigint
     LEFT JOIN ONLY %s t ON t.%I::text=s.tagged_key->>''value''
     WHERE s.point_id IS NULL OR qdrant_internal.payload_projection($2,t)->$3 IS DISTINCT FROM g->''value'')',
     i.source_oid::regclass,i.key_field) INTO valid USING result #> '{grouped,groups}',p_name,p_groups->>'field';
   IF valid IS DISTINCT FROM true THEN RAISE EXCEPTION 'Current source grouping key changed' USING ERRCODE='55000'; END IF;
   result:=jsonb_set(result,'{grouped,groups}',(SELECT coalesce(jsonb_agg(jsonb_build_object('value',g->'value','hits',(
     SELECT coalesce(jsonb_agg(jsonb_build_object('source_key',h #> '{payload,source_key}','score',hit->'score','rank',rank) ORDER BY rank),'[]')
       FROM jsonb_array_elements(g->'hits') WITH ORDINALITY members(hit,rank)
       JOIN jsonb_array_elements(proof) h ON h->'id'=hit->'id')) ORDER BY n),'[]')
     FROM jsonb_array_elements(result #> '{grouped,groups}') WITH ORDINALITY grouped(g,n)));
 END IF;
 IF p_matrix IS NOT NULL THEN
   PERFORM qdrant_internal.admit_matrix(p_name,p_matrix);
   -- Convert native IDs through the same complete, current source proof.
   result:=jsonb_set(result,'{matrix,samples}',(SELECT coalesce(jsonb_agg(h->'payload'->'source_key' ORDER BY n),'[]')
     FROM jsonb_array_elements(result #> '{matrix,sample_ids}') WITH ORDINALITY samples(id,n)
     JOIN jsonb_array_elements(proof) h ON h->'id'=samples.id));
   result:=jsonb_set(result,'{matrix,rows}',(SELECT coalesce(jsonb_agg(jsonb_build_object('source_key',h #> '{payload,source_key}',
     'neighbors',(SELECT coalesce(jsonb_agg(jsonb_build_object('source_key',nh #> '{payload,source_key}','score',hit->'score') ORDER BY rank),'[]')
       FROM jsonb_array_elements(row->'neighbors') WITH ORDINALITY neighbors(hit,rank)
       JOIN jsonb_array_elements(proof) nh ON nh->'id'=hit->'id')) ORDER BY n),'[]')
     FROM jsonb_array_elements(result #> '{matrix,rows}') WITH ORDINALITY rows(row,n)
     JOIN jsonb_array_elements(proof) h ON h->'id'=row->'id'));
   result:=result #- '{matrix,sample_ids}';
 END IF;
 result:=(result-'proof')||jsonb_build_object('index_name',p_name,'generation',i.generation,'storage_epoch',c.storage_epoch,
   'unit','points','documents',NULL,'exact',true,'domain','entire current durable owner source; explicit matching/filter predicates',
   'live_points',jsonb_array_length(ids),'max_live_points',1000,'matching',plan->'matching','filter',plan->'payload_filter',
   'release_supported',false);
 IF octet_length(result::text)>262144 THEN RAISE EXCEPTION 'Statistics result exceeds 256 KiB' USING ERRCODE='54000'; END IF;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION qdrant_internal.source_statistics(text,text,integer,jsonb,jsonb,jsonb,jsonb),qdrant_internal.p1_statistics(jsonb,integer) FROM PUBLIC;

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

CREATE FUNCTION qdrant.search_matrix(index_name text,representation text,sample_size integer DEFAULT 10,neighbors integer DEFAULT 5,options jsonb DEFAULT '{}')
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 result:=qdrant_internal.source_statistics(index_name,NULL,1,options,jsonb_build_object(
   'representation',representation,'sample_size',sample_size,'neighbors',neighbors));
 RETURN (result->'matrix')||jsonb_build_object('index_name',index_name,'generation',result->'generation',
   'storage_epoch',result->'storage_epoch','live_points',result->'live_points','max_live_points',1000,
   'filtered_points',result->'points','filtered_count_exact',true,'matching',result->'matching','filter',result->'filter',
   'release_supported',false);
END $$;
GRANT EXECUTE ON FUNCTION qdrant.search_matrix(text,text,integer,integer,jsonb) TO PUBLIC;

CREATE FUNCTION qdrant.search_groups(index_name text,q text,field text,group_limit integer DEFAULT 10,group_size integer DEFAULT 3,
 mode text DEFAULT 'text',query_vectors jsonb DEFAULT '{}',options jsonb DEFAULT '{}') RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 result:=qdrant_internal.source_statistics(index_name,NULL,1,options,NULL,jsonb_build_object(
   'field',field,'q',q,'groups',group_limit,'group_size',group_size,'mode',mode,'vectors',query_vectors));
 RETURN (result->'grouped')||jsonb_build_object('index_name',index_name,'generation',result->'generation',
   'storage_epoch',result->'storage_epoch','live_points',result->'live_points','max_live_points',1000,
   'filtered_points',result->'points','filtered_count_exact',true,'matching',result->'matching','filter',result->'filter',
   'release_supported',false);
END $$;
GRANT EXECUTE ON FUNCTION qdrant.search_groups(text,text,text,integer,integer,text,jsonb,jsonb) TO PUBLIC;

CREATE FUNCTION qdrant.search_lexical(index_name text,q text,kind text,top_k integer DEFAULT 10,slop integer DEFAULT 0,options jsonb DEFAULT '{}')
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 result:=qdrant_internal.source_statistics(index_name,NULL,1,options,NULL,NULL,
   jsonb_build_object('q',q,'kind',kind,'slop',slop,'top_k',top_k));
 RETURN (result->'lexical')||jsonb_build_object('index_name',index_name,'generation',result->'generation',
   'storage_epoch',result->'storage_epoch','live_points',result->'live_points','max_live_points',1000,
   'filtered_points',result->'points','matching',result->'matching','filter',result->'filter','release_supported',false);
END $$;
GRANT EXECUTE ON FUNCTION qdrant.search_lexical(text,text,text,integer,integer,jsonb) TO PUBLIC;

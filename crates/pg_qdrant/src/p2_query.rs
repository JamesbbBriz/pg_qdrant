//! Bounded owner-domain text search; other P2 plans remain unimplemented.

pgrx::extension_sql!(
    r###"
-- Typed and bounded result contract: never return synthetic search hits.
CREATE TYPE qdrant.search_hit AS (
    source_key text,
    rank integer,
    score double precision,
    document_key text,
    excerpt text,
    provenance jsonb
);

-- A permissioned, fail-closed preflight for text search. This prevents callers
-- from confusing capture-only event logging with an available Edge index.
CREATE FUNCTION qdrant.explain_search(
    index_name text,
    q text,
    mode text DEFAULT 'text',
    top_k integer DEFAULT 10,
    query_vectors jsonb DEFAULT '{}'::jsonb,
    options jsonb DEFAULT '{}'::jsonb)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $pgq$
DECLARE
    selected qdrant_internal.index_catalog%ROWTYPE;
    actor_oid oid;
    relation_info record;
    text_field text;
    request_plan text;
    allowed_options text[] := ARRAY['plan','candidate_limit','timeout_ms','fallback'];
    option_key text;
    candidate_limit integer;
    timeout_ms integer;
    matched_triggers integer;
BEGIN
    IF index_name IS NULL OR q IS NULL
       OR octet_length(q) NOT BETWEEN 1 AND 8192 OR btrim(q) = '' THEN
        RAISE EXCEPTION 'Query and index name are required; query at most 8192 bytes'
          USING ERRCODE = '22023';
    END IF;
    IF top_k IS NULL OR top_k NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'top_k outside permitted 1..100 range'
          USING ERRCODE = '22023';
    END IF;
    IF mode IS NULL OR mode NOT IN ('text','semantic','hybrid') THEN
        RAISE EXCEPTION 'Unknown search mode'
          USING ERRCODE = '22023';
    END IF;
    IF options IS NULL OR jsonb_typeof(options) <> 'object'
       OR query_vectors IS NULL OR jsonb_typeof(query_vectors) <> 'object' THEN
        RAISE EXCEPTION 'options and query_vectors must be JSON objects'
          USING ERRCODE = '22023';
    END IF;
    FOR option_key IN SELECT jsonb_object_keys(options) LOOP
        IF NOT option_key = ANY(allowed_options) THEN
            RAISE EXCEPTION 'Unsupported search option: %', option_key
              USING ERRCODE = '22023';
        END IF;
    END LOOP;
    IF mode <> 'text' OR query_vectors <> '{}'::jsonb THEN
        RAISE EXCEPTION 'Semantic/hybrid representation adapter not yet implemented'
          USING ERRCODE = '0A000';
    END IF;
    request_plan := coalesce(options ->> 'plan','text');
    IF request_plan <> 'text' OR
       coalesce(options ->> 'fallback','error') <> 'error' THEN
        RAISE EXCEPTION 'Only an explicit text/error preflight is admitted'
          USING ERRCODE = '0A000';
    END IF;
    IF options ? 'candidate_limit' AND jsonb_typeof(options -> 'candidate_limit') <> 'number'
       OR options ? 'timeout_ms' AND jsonb_typeof(options -> 'timeout_ms') <> 'number' THEN
        RAISE EXCEPTION 'Candidate and timeout budgets must be numbers'
          USING ERRCODE = '22023';
    END IF;
    BEGIN
        candidate_limit := coalesce((options ->> 'candidate_limit')::integer,
                                    greatest(top_k,50));
        timeout_ms := coalesce((options ->> 'timeout_ms')::integer,5000);
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'Candidate or timeout budget is not a valid integer'
          USING ERRCODE = '22023';
    END;
    IF candidate_limit NOT BETWEEN top_k AND 1000 OR
       timeout_ms NOT BETWEEN 1 AND 30000 THEN
        RAISE EXCEPTION 'Out-of-range candidate/timeout budget'
          USING ERRCODE = '22023';
    END IF;

    SELECT * INTO STRICT selected FROM qdrant_internal.index_catalog
    WHERE qdrant_internal.index_catalog.index_name = explain_search.index_name;
    PERFORM qdrant_internal.require_index(index_name);
    IF qdrant_internal.p1_index_status(index_name)->>'capture_state'<>'capturing' THEN
        RAISE EXCEPTION 'Source binding changed; rebuild required' USING ERRCODE='55000';
    END IF;
    actor_oid:=qdrant_internal.actor_oid();
    IF actor_oid IS NULL OR
       NOT has_table_privilege(actor_oid,selected.source_oid,'SELECT') OR
       NOT has_column_privilege(actor_oid,selected.source_oid,
                               selected.key_field::text,'SELECT') THEN
        RAISE EXCEPTION 'Source SELECT and key-column SELECT required'
          USING ERRCODE = '42501';
    END IF;
    SELECT c.relkind,c.relrowsecurity,c.relforcerowsecurity,
           c.relispartition,c.relhassubclass,c.relpersistence,
           a.atttypid,a.attnotnull
    INTO relation_info FROM pg_class c
    JOIN pg_attribute a ON a.attrelid=c.oid
      AND a.attname=selected.key_field AND NOT a.attisdropped
    WHERE c.oid=selected.source_oid;
    IF NOT FOUND OR relation_info.relkind <> 'r'
       OR relation_info.relrowsecurity OR relation_info.relforcerowsecurity
       OR relation_info.relispartition OR relation_info.relhassubclass
       OR relation_info.relpersistence <> 'p'
       OR relation_info.atttypid <> selected.key_type
       OR NOT relation_info.attnotnull THEN
        RAISE EXCEPTION 'Source metadata drift or unsafe row-level security'
          USING ERRCODE = '55000';
    END IF;
    FOR text_field IN
      SELECT jsonb_array_elements_text(selected.settings #> '{text,fields}')
    LOOP
      IF NOT has_column_privilege(actor_oid,selected.source_oid,text_field,'SELECT')
         OR NOT EXISTS (
           SELECT 1 FROM pg_attribute a WHERE a.attrelid=selected.source_oid
            AND a.attname=text_field AND a.atttypid='text'::regtype::oid
            AND NOT a.attisdropped
         ) THEN
          RAISE EXCEPTION 'Missing source text-column SELECT or schema changed'
            USING ERRCODE = '42501';
      END IF;
    END LOOP;
    SELECT count(*) INTO matched_triggers FROM pg_trigger
    WHERE tgrelid=selected.source_oid AND tgenabled IN ('O','A')
      AND NOT tgisinternal AND tgname IN (
       'qdrant_p1_rows', 'qdrant_p1_truncate');
    IF matched_triggers <> 2 OR selected.capture_state <> 'capturing' THEN
        RAISE EXCEPTION 'Capture triggers or lifecycle are not safe'
          USING ERRCODE = '55000';
    END IF;
    RETURN jsonb_build_object(
       'index_name',index_name,'source_oid',selected.source_oid,
       'source_key_type',selected.key_type,
       'requested_mode',mode,'effective_plan','text',
       'text_fields',selected.settings #> '{text,fields}',
       'top_k',top_k,'candidate_limit',candidate_limit,
       'timeout_ms',timeout_ms,
       'permission_preflight','passed_for_registered_source',
       'native_predicates_compiled',false,
       'source_recheck_available',true,
       'edge_generation_ready',(SELECT state='ready' AND qdrant_internal.p1_service_ready() FROM qdrant_internal.consumer_state c WHERE c.index_name=selected.index_name),
       'search_executable',(SELECT state='ready' AND qdrant_internal.p1_service_ready() FROM qdrant_internal.consumer_state c WHERE c.index_name=selected.index_name),
       'permission_domain','registered owner with complete source SELECT',
       'release_supported',false);
END;
$pgq$;

CREATE FUNCTION qdrant.search(
    index_name text,
    q text,
    mode text DEFAULT 'text',
    top_k integer DEFAULT 10,
    query_vectors jsonb DEFAULT '{}'::jsonb,
    options jsonb DEFAULT '{}'::jsonb)
RETURNS SETOF qdrant.search_hit
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $pgq$
DECLARE plan jsonb; i qdrant_internal.index_catalog%ROWTYPE;
        c qdrant_internal.consumer_state%ROWTYPE; hits jsonb;
BEGIN
    plan:=qdrant.explain_search(index_name,q,mode,top_k,query_vectors,options);
    IF NOT coalesce((plan->>'search_executable')::boolean,false) THEN
      RAISE EXCEPTION 'Index is not ready' USING ERRCODE='55000';
    END IF;
    SELECT * INTO STRICT i FROM qdrant_internal.index_catalog idx WHERE idx.index_name=search.index_name;
    SELECT * INTO STRICT c FROM qdrant_internal.consumer_state cs WHERE cs.index_name=i.index_name;
    hits:=qdrant_internal.p1_search(jsonb_build_object('operation','source_search','index_id',i.index_id,
      'generation',i.generation,'storage_epoch',c.storage_epoch,'q',q,
      'top_k',(plan->>'candidate_limit')::integer),(plan->>'timeout_ms')::integer);
    IF NOT EXISTS(SELECT 1 FROM qdrant_internal.consumer_state cs WHERE cs.index_name=i.index_name
        AND cs.storage_epoch=c.storage_epoch AND cs.consumer_id=c.consumer_id AND cs.state='ready') THEN
      RAISE EXCEPTION 'Generation changed during search' USING ERRCODE='55000';
    END IF;
    RETURN QUERY EXECUTE format(
      'SELECT t.%I::text,row_number() OVER(ORDER BY v.ordinality)::integer,(v.hit->>''score'')::double precision,
       t.%I::text,left(t.%I,240),jsonb_build_object(''generation'',$2,''storage_epoch'',$3,''plan'',''text'',
       ''statistics_scope'',''bounded_candidates'',''release_supported'',false)
       FROM jsonb_array_elements($1) WITH ORDINALITY v(hit,ordinality)
       JOIN ONLY %s t ON t.%I=(v.hit #>> ''{payload,source_key,value}'')::%s
       JOIN qdrant_internal.source_state s ON s.index_name=$4 AND s.point_id=(v.hit->>''id'')::bigint
       WHERE NOT s.tombstone AND s.incarnation::text=v.hit #>> ''{payload,incarnation}''
         AND s.revision=(v.hit #>> ''{payload,revision}'')::bigint
         AND encode(sha256(convert_to(t.%I,''UTF8'')),''hex'')=v.hit #>> ''{payload,fingerprint}''
       ORDER BY v.ordinality LIMIT $5',
       i.key_field,i.key_field,i.text_field,i.source_oid::regclass,i.key_field,i.key_type::regtype,i.text_field)
       USING hits,i.generation,c.storage_epoch,i.index_name,top_k;
END;
$pgq$;

-- Composite type and public preflight are visible to authorized callers.
-- The underlying capture tables remain private.
GRANT EXECUTE ON FUNCTION qdrant.explain_search(text,text,text,integer,jsonb,jsonb),
    qdrant.search(text,text,text,integer,jsonb,jsonb) TO PUBLIC;
"###,
    name = "p2_query_admission",
    requires = ["p1_management"]
);

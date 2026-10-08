//! Experimental P2 search-admission boundary, NOT a search implementation.
//! The exposed query shape refuses all native work until durable P1 is ready.

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
    selected qdrant._index_catalog%ROWTYPE;
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

    SELECT * INTO STRICT selected FROM qdrant._index_catalog
    WHERE qdrant._index_catalog.index_name = explain_search.index_name;
    SELECT oid INTO actor_oid FROM pg_roles WHERE rolname=session_user;
    IF actor_oid IS NULL OR
       NOT has_table_privilege(actor_oid,selected.source_table,'SELECT') OR
       NOT has_column_privilege(actor_oid,selected.source_table,
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
    WHERE c.oid=selected.source_table;
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
      IF NOT has_column_privilege(actor_oid,selected.source_table,text_field,'SELECT')
         OR NOT EXISTS (
           SELECT 1 FROM pg_attribute a WHERE a.attrelid=selected.source_table
            AND a.attname=text_field AND a.atttypid='text'::regtype::oid
            AND NOT a.attisdropped
         ) THEN
          RAISE EXCEPTION 'Missing source text-column SELECT or schema changed'
            USING ERRCODE = '42501';
      END IF;
    END LOOP;
    SELECT count(*) INTO matched_triggers FROM pg_trigger
    WHERE tgrelid=selected.source_table AND tgenabled='O'
      AND NOT tgisinternal AND tgname IN (
       'pgq_capture_'||selected.index_id,
       'pgq_truncate_'||selected.index_id);
    IF matched_triggers <> 2 OR selected.lifecycle <> 'capture_only' THEN
        RAISE EXCEPTION 'Capture triggers or lifecycle are not safe'
          USING ERRCODE = '55000';
    END IF;
    RETURN jsonb_build_object(
       'index_name',index_name,'source_oid',selected.source_table,
       'source_key_type',selected.key_type,
       'requested_mode',mode,'effective_plan','text',
       'text_fields',selected.settings #> '{text,fields}',
       'top_k',top_k,'candidate_limit',candidate_limit,
       'timeout_ms',timeout_ms,
       'permission_preflight','passed_for_registered_source',
       'native_predicates_compiled',false,
       'source_recheck_available',false,
       'edge_generation_ready',false,'search_executable',false,
       'blocked_by','P1 native apply and authorization filter compiler');
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
BEGIN
    PERFORM qdrant.explain_search(index_name,q,mode,top_k,query_vectors,options);
    RAISE EXCEPTION 'Native search is not implemented on the P2 draft'
      USING ERRCODE = '0A000', DETAIL = 'No fake hits or fallback to PostgreSQL FTS.';
END;
$pgq$;

-- Composite type and public preflight are visible to authorized callers.
-- The underlying capture tables remain private.
GRANT EXECUTE ON FUNCTION qdrant.explain_search(text,text,text,integer,jsonb,jsonb),
    qdrant.search(text,text,text,integer,jsonb,jsonb) TO PUBLIC;
"###,
    name = "p2_query_admission",
    finalize
);
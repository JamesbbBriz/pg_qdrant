//! Bounded owner-domain text, declared model and native hybrid search.

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
    allowed_options text[] := ARRAY['plan','candidate_limit','timeout_ms','fallback','fusion','matching','formula'];
    option_key text;
    candidate_limit integer;
    timeout_ms integer;
    matched_triggers integer;
    representation_query jsonb;
    recommendation jsonb;
    discovery jsonb;
    feedback jsonb;
    mmr jsonb;
    formula jsonb;
    representation_kind text;
    rerank_query jsonb;
    named_input record;
    input_kind text;
    token_queries integer:=0;
    recall_queries integer:=0;
    token_contract jsonb;
    maxsim_work bigint;
    fusion text;
    effective_plan text;
    matching jsonb;
    matching_entry record;
    matching_bytes integer:=0;
BEGIN
    IF index_name IS NULL OR q IS NULL OR octet_length(q)>8192
       OR (mode IS DISTINCT FROM 'explore' AND btrim(q) = '') THEN
        RAISE EXCEPTION 'Query and index name are required; query at most 8192 bytes'
          USING ERRCODE = '22023';
    END IF;
    IF top_k IS NULL OR top_k NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'top_k outside permitted 1..100 range'
          USING ERRCODE = '22023';
    END IF;
    IF mode IS NULL OR NOT EXISTS(SELECT 1 FROM qdrant_internal.query_modes m WHERE m.mode=explain_search.mode) THEN
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
    IF options ? 'formula' THEN
        IF mode='explore' OR octet_length((options->'formula')::text)>8192 THEN
            RAISE EXCEPTION 'Formula requires non-explore scoring and at most 8192 bytes' USING ERRCODE='22023';
        END IF;
        formula:=qdrant_internal.admit_score_formula(options->'formula');
    END IF;
    matching:=coalesce(options->'matching','{}'::jsonb);
    IF jsonb_typeof(matching)<>'object' THEN
        RAISE EXCEPTION 'matching must be an object' USING ERRCODE='22023';
    END IF;
    FOR matching_entry IN SELECT * FROM jsonb_each(matching) LOOP
        IF matching_entry.key NOT IN ('all','any','exclude','phrase','token_prefix','key_exact','key_prefix')
           OR jsonb_typeof(matching_entry.value)<>'string'
           OR octet_length(matching_entry.value #>> '{}') NOT BETWEEN 1 AND 2048 THEN
            RAISE EXCEPTION 'Unknown matching clause or invalid string/byte budget' USING ERRCODE='22023';
        END IF;
        matching_bytes:=matching_bytes+octet_length(matching_entry.value #>> '{}');
        IF matching_entry.key IN ('key_exact','key_prefix') AND octet_length(matching_entry.value #>> '{}')>1024 THEN
            RAISE EXCEPTION 'Key matching exceeds 1024 bytes' USING ERRCODE='22023';
        END IF;
    END LOOP;
    IF matching_bytes>8192 THEN RAISE EXCEPTION 'Matching exceeds 8192 bytes' USING ERRCODE='22023'; END IF;
    IF matching ? 'token_prefix' AND char_length(matching->>'token_prefix') NOT BETWEEN 2 AND 32 THEN
        RAISE EXCEPTION 'token_prefix requires one alphanumeric token of 2..32 characters' USING ERRCODE='22023';
    END IF;
    IF mode='text' AND query_vectors <> '{}'::jsonb THEN
        RAISE EXCEPTION 'Text search takes no model vector'
          USING ERRCODE = '0A000';
    END IF;
    IF options ? 'fusion' AND (mode NOT IN ('hybrid','precision') OR jsonb_typeof(options->'fusion')<>'string') THEN
        RAISE EXCEPTION 'fusion is a string option for hybrid search only' USING ERRCODE='22023';
    END IF;
    IF mode IN ('hybrid','precision') THEN
        fusion:=coalesce(options->>'fusion','rrf');
        IF fusion NOT IN ('rrf','dbsf') THEN RAISE EXCEPTION 'fusion must be rrf or dbsf' USING ERRCODE='22023'; END IF;
        effective_plan:='hybrid_bm25_dense_'||fusion;
    ELSE effective_plan:=mode; END IF;
    request_plan := coalesce(options ->> 'plan',mode);
    IF request_plan <> mode OR
       coalesce(options ->> 'fallback','error') <> 'error' THEN
        RAISE EXCEPTION 'Requested mode and plan must match; fallback requires a ready implementation'
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
    IF EXISTS(SELECT 1 FROM pg_class WHERE oid=selected.source_oid AND (relrowsecurity OR relforcerowsecurity)) THEN
        RAISE EXCEPTION 'RLS search sources are unsupported' USING ERRCODE='0A000';
    END IF;
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
    IF mode='precision' THEN
        FOR named_input IN SELECT * FROM jsonb_each(query_vectors) LOOP
            SELECT contract->>'kind' INTO input_kind FROM qdrant_internal.representation_catalog
              WHERE representation_catalog.index_name=explain_search.index_name AND name=named_input.key;
            IF input_kind='token_vectors' THEN
                token_queries:=token_queries+1;
                rerank_query:=qdrant_internal.admit_representation(index_name,jsonb_build_object(named_input.key,named_input.value));
            ELSIF input_kind IN ('dense','learned_sparse') THEN
                recall_queries:=recall_queries+1;
                representation_query:=qdrant_internal.admit_representation(index_name,jsonb_build_object(named_input.key,named_input.value));
                representation_kind:=input_kind;
            ELSE RAISE EXCEPTION 'Unknown precision representation' USING ERRCODE='22023';
            END IF;
        END LOOP;
        IF token_queries<>1 OR recall_queries>1 THEN
            RAISE EXCEPTION 'Precision requires one token slot and at most one recall slot' USING ERRCODE='22023';
        END IF;
        IF recall_queries=0 THEN
            IF options ? 'fusion' THEN RAISE EXCEPTION 'Fusion needs a recall-vector branch' USING ERRCODE='22023'; END IF;
            fusion:=NULL; effective_plan:='precision_bm25_maxsim';
        ELSE effective_plan:='precision_bm25_'||representation_kind||'_'||fusion||'_maxsim';
        END IF;
    ELSIF mode='explore' THEN
        IF (SELECT value->>'strategy' FROM jsonb_each(query_vectors) LIMIT 1)='mmr' THEN
            mmr:=qdrant_internal.admit_mmr(index_name,query_vectors);
            effective_plan:='explore_dense_mmr';
        ELSIF (SELECT value->>'strategy' FROM jsonb_each(query_vectors) LIMIT 1)='feedback' THEN
            feedback:=qdrant_internal.admit_feedback(index_name,query_vectors);
            effective_plan:='explore_dense_feedback';
        ELSIF (SELECT value->>'strategy' FROM jsonb_each(query_vectors) LIMIT 1) IN ('discover','context') THEN
            discovery:=qdrant_internal.admit_discovery(index_name,query_vectors);
            effective_plan:='explore_dense_'||(discovery #>> '{query,strategy}');
        ELSE
            recommendation:=qdrant_internal.admit_recommendation(index_name,query_vectors);
            effective_plan:='explore_dense_'||(recommendation #>> '{query,strategy}');
        END IF;
    ELSIF mode IN ('semantic','sparse','hybrid','maxsim') THEN
        representation_query:=qdrant_internal.admit_representation(index_name,query_vectors);
        SELECT contract->>'kind' INTO representation_kind FROM qdrant_internal.representation_catalog
          WHERE representation_catalog.index_name=explain_search.index_name AND name=representation_query->>'representation';
        IF (mode='semantic' AND representation_kind<>'dense') OR (mode='sparse' AND representation_kind<>'learned_sparse')
           OR (mode='maxsim' AND representation_kind<>'token_vectors')
           OR (mode='hybrid' AND representation_kind NOT IN ('dense','learned_sparse')) THEN
            RAISE EXCEPTION 'Search mode does not match representation kind' USING ERRCODE='22023';
        END IF;
        IF mode='hybrid' THEN effective_plan:='hybrid_bm25_'||representation_kind||'_'||fusion; END IF;
    END IF;
    IF mode IN ('maxsim','precision') THEN
        SELECT contract INTO token_contract FROM qdrant_internal.representation_catalog
          WHERE representation_catalog.index_name=explain_search.index_name
            AND name=coalesce(rerank_query,representation_query)->>'representation';
        IF mode='precision' THEN
            maxsim_work:=jsonb_array_length(rerank_query->'vector')::bigint
              *(token_contract->>'max_tokens')::bigint*(token_contract->>'dimensions')::bigint*candidate_limit;
            IF maxsim_work>20000000 THEN
                RAISE EXCEPTION 'MaxSim scalar-work budget exceeds 20000000' USING ERRCODE='22023';
            END IF;
        END IF;
    END IF;
    IF formula IS NOT NULL THEN effective_plan:=effective_plan||'_formula'; END IF;
    RETURN jsonb_build_object(
       'formula',formula,
       'formula_contract',CASE WHEN formula IS NOT NULL THEN jsonb_build_object(
         'scope','bounded authorized candidates from the entire preceding query stage',
         'score_variable','$score[0]','score_domain','pinned Edge Formula arithmetic; descending final score',
         'candidate_limit',candidate_limit,'max_nodes',64,'max_depth',8,'max_arguments',8,
         'max_node_evaluations',64*candidate_limit,'payload_fields_available',false,
         'nonfinite_policy','reject complete query; no partial results',
         'conversion','float32 constants with finite absolute bound 1000000') END,
       'index_name',index_name,'source_oid',selected.source_oid,
       'source_key_type',selected.key_type,
       'requested_mode',mode,'effective_plan',effective_plan,'representation_query',representation_query,'representation_kind',representation_kind,'fusion',fusion,
       'rerank_query',rerank_query,
       'recommendation_query',recommendation->'query',
       'discovery_query',discovery->'query',
       'feedback_query',feedback->'query',
       'mmr_query',mmr->'query',
       'seed_digest',coalesce(recommendation,discovery,feedback,mmr)->'seed_digest',
       'mmr_contract',CASE WHEN mmr IS NOT NULL THEN jsonb_build_object(
         'seed_versions',mmr->'seed_versions','seed_digest',mmr->'seed_digest',
         'seed_scope','current visible PostgreSQL source and ready representation snapshot; durability is a separate ticket',
         'lambda',mmr #> '{query,lambda}','candidate_limit',candidate_limit,
         'score_domain','original dense similarity; rank is native MMR selection order, not descending score',
         'scalar_work_limit',20000000,'work_basis','(owned live points + min(candidate_limit, owned live points)^2) * dimensions; before filters',
         'seed_exclusion','target excluded before nearest candidate truncation',
         'text_query_used_for_scoring',false) END,
       'feedback_contract',CASE WHEN feedback IS NOT NULL THEN jsonb_build_object(
         'seed_versions',feedback->'seed_versions','seed_digest',feedback->'seed_digest',
         'seed_scope','current visible PostgreSQL source and ready representation snapshot; durability is a separate ticket',
         'coefficients',feedback #> '{query,coefficients}','scalar_work_limit',20000000,
         'work_basis','(1 + n*(n-1)) * dimensions * native owned live points; worst-case feedback pairs before execution',
         'seed_exclusion','target and every feedback example excluded before candidate truncation',
         'text_query_used_for_scoring',false) END,
       'discovery_contract',CASE WHEN discovery IS NOT NULL THEN jsonb_build_object(
         'seed_versions',discovery->'seed_versions','seed_digest',discovery->'seed_digest',
         'seed_scope','current visible PostgreSQL source and ready representation snapshot; durability is a separate ticket',
         'strategy',discovery #>> '{query,strategy}','scalar_work_limit',20000000,
         'work_basis','target and context-vector count * dimensions * native owned live points; checked before execution',
         'seed_exclusion','target and every context example excluded before candidate truncation',
         'text_query_used_for_scoring',false) END,
       'recommendation_contract',CASE WHEN recommendation IS NOT NULL THEN jsonb_build_object(
         'seed_versions',recommendation->'seed_versions','seed_digest',recommendation->'seed_digest',
         'seed_scope','current visible PostgreSQL source and ready representation snapshot; durability is a separate ticket',
         'strategy',recommendation #>> '{query,strategy}','scalar_work_limit',20000000,
         'work_basis','example count * dimensions * native owned live points; checked before execution',
         'seed_exclusion','all source examples excluded before candidate truncation',
         'text_query_used_for_scoring',false) END,
       'maxsim_contract',CASE WHEN mode IN ('maxsim','precision') THEN jsonb_build_object(
         'scope',CASE WHEN mode='precision' THEN 'exact within bounded native prefetch candidates' ELSE 'exact over live named token vectors' END,
         'token_width',(SELECT contract->'dimensions' FROM qdrant_internal.representation_catalog
             WHERE representation_catalog.index_name=explain_search.index_name
               AND name=coalesce(rerank_query,representation_query)->>'representation'),
         'candidate_limit',CASE WHEN mode='precision' THEN candidate_limit END,
         'scalar_work_limit',20000000,'scalar_work_upper_bound',maxsim_work,
         'candidate_budget_basis',CASE WHEN mode='precision' THEN 'native prefetch cap' ELSE 'native owned live point count checked at execution' END,
         'comparator','sum of per-query-token maximum dot product') END,
       'fusion_contract',CASE WHEN fusion IS NOT NULL THEN jsonb_build_object('engine','qdrant-edge 0.8.0',
         'branches',jsonb_build_array('bm25',representation_query->>'representation'),'candidate_limit_per_branch',candidate_limit,
         'rrf_k',CASE WHEN fusion='rrf' THEN 2 END,'rrf_weights','equal',
         'idf_scope','live vectors in owned generation','normalization_scope','bounded prefetch distributions') END,
       'text_fields',selected.settings #> '{text,fields}',
       'top_k',top_k,'candidate_limit',candidate_limit,
       'timeout_ms',timeout_ms,
       'numeric_score_budgets',jsonb_build_object('conversion','float32',
          'dense_squared_l2_max',1e16,'sparse_squared_l2_max',1e14,
          'token_squared_l2_max','float32 maximum / (2 * declared max_tokens)',
          'nonfinite_result_policy','reject complete response'),
       'permission_preflight','passed_for_registered_source',
       'matching',matching,
       'native_predicates_compiled',false,
       'native_predicates_available',true,
       'matching_contract',jsonb_build_object('scope','before candidate truncation in every native branch',
         'composition','all supplied clauses required; exclude rejects any analyzed excluded term',
         'validation','SQL shapes/bytes checked; native analyzed-term validation at execution',
         'body_analyzer','multilingual native lossy normalization; lowercase; extra ASCII folding, stopwords and stemming disabled',
         'phrase','contiguous within the single source text field',
         'token_prefix','separate prefix index; one alphanumeric token; 2..32 characters',
         'key','whole primary-key text value; case sensitive; no tokenization',
         'idf_scope','entire live owned generation; independent of matching'),
       'source_recheck_available',true,
       'edge_generation_ready',(SELECT state='ready' AND qdrant_internal.p1_service_ready(c.engine_instance) FROM qdrant_internal.consumer_state c WHERE c.index_name=selected.index_name),
       'search_executable',(SELECT state='ready' AND qdrant_internal.p1_service_ready(c.engine_instance) FROM qdrant_internal.consumer_state c WHERE c.index_name=selected.index_name),
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
        c qdrant_internal.consumer_state%ROWTYPE; hits jsonb; admitted_seed_digest text;
BEGIN
    plan:=qdrant.explain_search(index_name,q,mode,top_k,query_vectors,options);
    IF NOT coalesce((plan->>'search_executable')::boolean,false) THEN
      RAISE EXCEPTION 'Index is not ready' USING ERRCODE='55000';
    END IF;
    SELECT * INTO STRICT i FROM qdrant_internal.index_catalog idx WHERE idx.index_name=search.index_name
      FOR KEY SHARE NOWAIT;
    -- Hold the source binding through native admission and source recheck.
    -- NOWAIT avoids management/source lock-order cycles and unbounded DDL waits.
    EXECUTE format('LOCK TABLE ONLY %s IN ACCESS SHARE MODE NOWAIT',i.source_oid::regclass);
    plan:=qdrant.explain_search(index_name,q,mode,top_k,query_vectors,options);
    IF NOT coalesce((plan->>'search_executable')::boolean,false) THEN
      RAISE EXCEPTION 'Index changed during search admission' USING ERRCODE='55000';
    END IF;
    SELECT * INTO STRICT c FROM qdrant_internal.consumer_state cs WHERE cs.index_name=i.index_name;
    admitted_seed_digest:=plan->>'seed_digest';
    hits:=qdrant_internal.p1_search(jsonb_build_object('operation','source_search','index_id',i.index_id,
      'generation',i.generation,'storage_epoch',c.storage_epoch,'q',q,
      'representation_query',plan->'representation_query',
      'recommendation_query',plan->'recommendation_query',
      'discovery_query',plan->'discovery_query',
      'feedback_query',plan->'feedback_query',
      'mmr_query',plan->'mmr_query',
      'formula',plan->'formula',
      'rerank_query',plan->'rerank_query',
      'fusion',plan->'fusion',
      'predicates',plan->'matching',
      'top_k',(plan->>'candidate_limit')::integer),(plan->>'timeout_ms')::integer);
    IF jsonb_typeof(hits) IS DISTINCT FROM 'array' OR EXISTS(
        SELECT 1 FROM jsonb_array_elements(hits) h WHERE jsonb_typeof(h->'score') IS DISTINCT FROM 'number') THEN
      RAISE EXCEPTION 'Native query returned an invalid score' USING ERRCODE='XX000';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM qdrant_internal.consumer_state cs WHERE cs.index_name=i.index_name
        AND cs.storage_epoch=c.storage_epoch AND cs.consumer_id=c.consumer_id AND cs.state='ready') THEN
      RAISE EXCEPTION 'Generation changed during search' USING ERRCODE='55000';
    END IF;
    -- Permissions and conservative DDL invalidation are checked again before
    -- any native payload can become a caller-visible source result.
    plan:=qdrant.explain_search(index_name,q,mode,top_k,query_vectors,options);
    IF NOT coalesce((plan->>'search_executable')::boolean,false) THEN
      RAISE EXCEPTION 'Index readiness changed during search' USING ERRCODE='55000';
    END IF;
    IF (plan->>'seed_digest') IS DISTINCT FROM admitted_seed_digest THEN
      RAISE EXCEPTION 'Recommendation examples changed during native query; restart search' USING ERRCODE='55000';
    END IF;
    RETURN QUERY EXECUTE format(
      'SELECT t.%I::text,row_number() OVER(ORDER BY v.ordinality)::integer,(v.hit->>''score'')::double precision,
       t.%I::text,left(t.%I,240),jsonb_build_object(''generation'',$2,''storage_epoch'',$3,''plan'',$6,
       ''matching'',$7,''seed_digest'',$8,''point_id'',v.hit->''id'',
       ''incarnation'',v.hit #>> ''{payload,incarnation}'',
       ''revision'',v.hit #>> ''{payload,revision}'',
       ''source_fingerprint'',v.hit #>> ''{payload,fingerprint}'',
       ''statistics_scope'',''bounded_candidates'',''release_supported'',false)
       FROM jsonb_array_elements($1) WITH ORDINALITY v(hit,ordinality)
       JOIN ONLY %s t ON t.%I=(v.hit #>> ''{payload,source_key,value}'')::%s
       JOIN qdrant_internal.source_state s ON s.index_name=$4 AND s.point_id=(v.hit->>''id'')::bigint
       WHERE NOT s.tombstone AND s.incarnation::text=v.hit #>> ''{payload,incarnation}''
         AND s.revision=(v.hit #>> ''{payload,revision}'')::bigint
         AND encode(sha256(convert_to(t.%I,''UTF8'')),''hex'')=v.hit #>> ''{payload,fingerprint}''
       ORDER BY v.ordinality LIMIT $5',
       i.key_field,i.key_field,i.text_field,i.source_oid::regclass,i.key_field,i.key_type::regtype,i.text_field)
       USING hits,i.generation,c.storage_epoch,i.index_name,top_k,plan->>'effective_plan',plan->'matching',admitted_seed_digest;
END;
$pgq$;

-- Composite type and public preflight are visible to authorized callers.
-- The underlying capture tables remain private.
GRANT EXECUTE ON FUNCTION qdrant.explain_search(text,text,text,integer,jsonb,jsonb),
    qdrant.search(text,text,text,integer,jsonb,jsonb) TO PUBLIC;
"###,
    name = "p2_query_admission",
    requires = [
        "p1_representations",
        "p2_mode_registry",
        "p2_recommendation_admission",
        "p2_context_discovery_admission",
        "p2_feedback_admission",
        "p2_mmr_admission",
        qdrant_internal::admit_score_formula
    ]
);

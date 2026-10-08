//! PostgreSQL-side build reservations. A reservation is not a native generation.

pgrx::extension_sql!(
    r###"
CREATE TABLE qdrant_internal.generation_reservations (
    index_id bigint NOT NULL REFERENCES qdrant_internal.index_catalog(index_id) ON DELETE CASCADE,
    generation_no bigint NOT NULL CHECK (generation_no > 1),
    state text NOT NULL DEFAULT 'reserved_unbuilt'
      CHECK (state IN ('reserved_unbuilt','cancelled','failed')),
    requested_by oid NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reason text NOT NULL DEFAULT 'operator_requested_rebuild',
    PRIMARY KEY (index_id,generation_no)
);

CREATE FUNCTION qdrant.rebuild_index(p_index_name text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $pgq$
DECLARE
    selected qdrant_internal.index_catalog%ROWTYPE;
    actor_oid oid;
    next_generation bigint;
BEGIN
    SELECT * INTO STRICT selected FROM qdrant_internal.index_catalog
    WHERE index_name = p_index_name FOR UPDATE;
    actor_oid:=qdrant_internal.actor_oid();
    IF actor_oid IS NULL OR NOT pg_has_role(actor_oid,selected.owner_oid,'USAGE') THEN
        RAISE EXCEPTION 'Source-owner membership required to request rebuild'
            USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid=selected.source_oid
                   AND c.relkind='r' AND NOT c.relrowsecurity
                   AND NOT c.relforcerowsecurity) THEN
        RAISE EXCEPTION 'Source unavailable or RLS enabled; rebuild refused'
            USING ERRCODE = '55000';
    END IF;
    SELECT greatest(selected.generation_no,
                    coalesce(max(generation_no),selected.generation_no)) + 1
    INTO next_generation FROM qdrant_internal.generation_reservations
    WHERE index_id=selected.index_id;
    INSERT INTO qdrant_internal.generation_reservations
        (index_id,generation_no,requested_by)
    VALUES (selected.index_id,next_generation,actor_oid);
    RETURN jsonb_build_object('index_id',selected.index_id,
       'reserved_generation',next_generation,
       'state','reserved_unbuilt','engine_build_started',false,
       'native_flush_verified',false,'search_ready',false);
END;
$pgq$;

CREATE FUNCTION qdrant.rebuild_status(p_index_name text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $pgq$
DECLARE
    selected qdrant_internal.index_catalog%ROWTYPE;
    actor_oid oid;
BEGIN
    SELECT * INTO STRICT selected FROM qdrant_internal.index_catalog
    WHERE index_name=p_index_name;
    actor_oid:=qdrant_internal.actor_oid();
    IF actor_oid IS NULL OR NOT pg_has_role(actor_oid,selected.owner_oid,'USAGE') THEN
       RAISE EXCEPTION 'Source-owner membership required' USING ERRCODE='42501';
    END IF;
    RETURN jsonb_build_object(
       'index_id',selected.index_id,'active_generation',selected.generation_no,
       'active_generation_search_ready',(SELECT c.state='ready'
          AND qdrant_internal.p1_index_status(p_index_name)->>'capture_state'='capturing'
          FROM qdrant_internal.consumer_state c WHERE c.index_name=p_index_name),
       'reservations',(SELECT coalesce(jsonb_agg(jsonb_build_object(
           'generation',generation_no,'state',state,'requested_at',requested_at)
           ORDER BY generation_no),'[]'::jsonb)
          FROM qdrant_internal.generation_reservations
          WHERE index_id=selected.index_id),
       'switch_available',false,
       'pending_gate','native build, exact-event durable flush, generation pin/swap');
END;
$pgq$;

CREATE FUNCTION qdrant.cancel_rebuild(
    p_index_name text, p_generation bigint)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog, pg_temp
AS $pgq$
DECLARE
    selected qdrant_internal.index_catalog%ROWTYPE;
    actor_oid oid;
    changed bigint;
BEGIN
    SELECT * INTO STRICT selected FROM qdrant_internal.index_catalog
    WHERE index_name=p_index_name FOR UPDATE;
    actor_oid:=qdrant_internal.actor_oid();
    IF actor_oid IS NULL OR NOT pg_has_role(actor_oid,selected.owner_oid,'USAGE') THEN
       RAISE EXCEPTION 'Source-owner membership required' USING ERRCODE='42501';
    END IF;
    UPDATE qdrant_internal.generation_reservations SET state='cancelled'
    WHERE index_id=selected.index_id AND generation_no=p_generation
      AND state='reserved_unbuilt';
    GET DIAGNOSTICS changed = ROW_COUNT;
    RETURN changed=1;
END;
$pgq$;

-- These are admission recommendations, NOT kernel-enforced Edge quotas.
CREATE FUNCTION qdrant.operation_limits()
RETURNS jsonb LANGUAGE sql IMMUTABLE
SET search_path=pg_catalog,pg_temp
AS $pgq$
 SELECT jsonb_build_object(
    'schema_version',1,
    'stage','P3_preflight_only',
    'top_k_max',100,
    'candidates_max',1000,
    'query_tokens_max',4096,
    'dense_dimensions_max',4096,
    'rerank_matrix_cells_max',1000000,
    'response_bytes_max',1048576,
    'deadline_ms_max',30000,
    'native_rss_limit_enforced',false,
    'managed_helper_address_space_ceiling_bytes',8589934592,
    'address_space_scope','managed helper virtual memory including mmap; inspect active helper_resource_limits',
    'native_cancel_available',false,
    'work_mem_is_edge_limit',false);
$pgq$;

REVOKE ALL ON qdrant_internal.generation_reservations FROM PUBLIC;
GRANT EXECUTE ON FUNCTION qdrant.rebuild_index(text),
  qdrant.rebuild_status(text), qdrant.cancel_rebuild(text,bigint),
  qdrant.operation_limits() TO PUBLIC;
"###,
    name = "p3_generation_reservations",
    requires = ["p1_transaction_capture"]
);

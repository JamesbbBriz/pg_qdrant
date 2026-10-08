//! Experimental transactional source capture for P1.
//! This is NOT a durable Edge consumer, and no search index is ready.

pgrx::extension_sql!(
    r###"
CREATE TABLE qdrant._index_catalog (
    index_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    index_name text NOT NULL UNIQUE,
    source_table oid NOT NULL,
    source_owner oid NOT NULL,
    key_field name NOT NULL,
    key_type oid NOT NULL,
    settings jsonb NOT NULL,
    generation bigint NOT NULL DEFAULT 1,
    lifecycle text NOT NULL DEFAULT 'capture_only'
        CHECK (lifecycle IN ('capture_only', 'rebuild_required', 'failed')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE qdrant._source_state (
    index_id bigint NOT NULL REFERENCES qdrant._index_catalog ON DELETE CASCADE,
    key_text text COLLATE "C" NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    incarnation uuid NOT NULL,
    is_live boolean NOT NULL,
    fingerprint text,
    PRIMARY KEY (index_id, key_text),
    CHECK (octet_length(key_text) BETWEEN 1 AND 4096)
);

CREATE TABLE qdrant._events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    index_id bigint NOT NULL REFERENCES qdrant._index_catalog ON DELETE CASCADE,
    key_text text COLLATE "C" NOT NULL,
    revision bigint NOT NULL,
    incarnation uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('upsert', 'delete')),
    origin text NOT NULL CHECK (origin IN ('backfill', 'trigger')),
    fingerprint text,
    writer_xid xid8 NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivery_state text NOT NULL DEFAULT 'pending'
        CHECK (delivery_state = 'pending'),
    UNIQUE (index_id, key_text, revision),
    CHECK (octet_length(key_text) BETWEEN 1 AND 4096)
);
CREATE INDEX pgq_events_pending ON qdrant._events (index_id, event_id);

-- Fingerprints are independent of revisions; unindexed columns are excluded.
CREATE FUNCTION qdrant._fingerprint(settings jsonb, source_row jsonb)
RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, pg_temp
AS $pgq$
DECLARE
    field_name text;
    projected jsonb := '{}'::jsonb;
BEGIN
    FOR field_name IN SELECT jsonb_array_elements_text(settings #> '{text,fields}')
    LOOP
        projected := projected || jsonb_build_object(field_name, source_row -> field_name);
    END LOOP;
    RETURN md5(projected::text);
END;
$pgq$;

CREATE FUNCTION qdrant._capture()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $pgq$
DECLARE
    selected qdrant._index_catalog%ROWTYPE;
    key_value text;
    row_value jsonb;
    new_revision bigint;
    new_incarnation uuid;
    current_fingerprint text;
    new_kind text;
BEGIN
    SELECT * INTO STRICT selected FROM qdrant._index_catalog
     WHERE index_id = TG_ARGV[0]::bigint FOR SHARE;
    IF selected.source_table <> TG_RELID OR selected.lifecycle <> 'capture_only' THEN
        RAISE EXCEPTION 'pg_qdrant source binding is not writable'
            USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' AND
       (to_jsonb(OLD) ->> selected.key_field::text)
          IS DISTINCT FROM (to_jsonb(NEW) ->> selected.key_field::text) THEN
        RAISE EXCEPTION 'pg_qdrant does not support primary-key updates yet'
            USING ERRCODE = '0A000';
    END IF;
    IF TG_OP = 'DELETE' THEN
        row_value := to_jsonb(OLD);
        new_kind := 'delete';
        current_fingerprint := NULL;
    ELSE
        row_value := to_jsonb(NEW);
        new_kind := 'upsert';
        IF octet_length(row_value::text) > 2097152 THEN
            RAISE EXCEPTION 'pg_qdrant capture row exceeds 2 MiB'
                USING ERRCODE = '54000';
        END IF;
        current_fingerprint := qdrant._fingerprint(selected.settings, row_value);
    END IF;
    key_value := row_value ->> selected.key_field::text;
    IF key_value IS NULL OR octet_length(key_value) NOT BETWEEN 1 AND 4096 THEN
        RAISE EXCEPTION 'pg_qdrant key must be 1..4096 bytes'
            USING ERRCODE = '22023';
    END IF;
    INSERT INTO qdrant._source_state
      (index_id,key_text,revision,incarnation,is_live,fingerprint)
    VALUES
      (selected.index_id,key_value,1,gen_random_uuid(),
       new_kind = 'upsert',current_fingerprint)
    ON CONFLICT (index_id,key_text) DO UPDATE SET
      revision = qdrant._source_state.revision + 1,
      incarnation = CASE
        WHEN NOT qdrant._source_state.is_live AND TG_OP = 'INSERT'
        THEN gen_random_uuid() ELSE qdrant._source_state.incarnation END,
      is_live = EXCLUDED.is_live,
      fingerprint = EXCLUDED.fingerprint
    RETURNING revision,incarnation INTO new_revision,new_incarnation;
    INSERT INTO qdrant._events
      (index_id,key_text,revision,incarnation,kind,origin,fingerprint,writer_xid)
    VALUES
      (selected.index_id,key_value,new_revision,new_incarnation,
       new_kind,'trigger',current_fingerprint,pg_current_xact_id());
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$pgq$;

-- TRUNCATE bypasses row triggers. Refuse rather than silently drift.
CREATE FUNCTION qdrant._deny_truncate()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $pgq$
BEGIN
    RAISE EXCEPTION 'TRUNCATE registered source is unsupported; use DELETE'
        USING ERRCODE = '0A000';
END;
$pgq$;

CREATE FUNCTION qdrant.create_index(
    index_name text, source regclass, key_field name, settings jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $pgq$
DECLARE
    relation_info record;
    actor_oid oid;
    actor_super boolean;
    pk_type oid;
    field_name text;
    registered_id bigint;
BEGIN
    IF index_name IS NULL OR index_name !~ '^[a-z][a-z0-9_]{0,62}$' THEN
        RAISE EXCEPTION 'Invalid pg_qdrant index name' USING ERRCODE = '22023';
    END IF;
    IF source IS NULL OR key_field IS NULL OR settings IS NULL
       OR jsonb_typeof(settings) <> 'object'
       OR jsonb_typeof(settings #> '{text,fields}') <> 'array' THEN
        RAISE EXCEPTION 'Only text.fields arrays are accepted'
            USING ERRCODE = '22023';
    END IF;
    IF jsonb_array_length(settings #> '{text,fields}') NOT BETWEEN 1 AND 16
       OR settings <> jsonb_build_object('text',settings -> 'text')
       OR (settings -> 'text') <> jsonb_build_object('fields',settings #> '{text,fields}')
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(settings #> '{text,fields}') f
                  WHERE jsonb_typeof(f.value) <> 'string') THEN
        RAISE EXCEPTION 'Settings must contain only 1..16 string text fields'
            USING ERRCODE = '22023';
    END IF;
    SELECT r.oid,r.rolsuper INTO actor_oid,actor_super
    FROM pg_roles r WHERE r.rolname = session_user;
    IF actor_oid IS NULL THEN
        RAISE EXCEPTION 'Caller role unavailable' USING ERRCODE = '42501';
    END IF;
    SELECT c.relowner,c.relkind,c.relpersistence,c.relispartition,
           c.relhassubclass,c.relrowsecurity,c.relforcerowsecurity,
           c.relam,n.nspname
    INTO relation_info FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.oid = source;
    IF NOT FOUND OR relation_info.relkind <> 'r'
       OR relation_info.relpersistence <> 'p'
       OR relation_info.relispartition OR relation_info.relhassubclass
       OR relation_info.relrowsecurity OR relation_info.relforcerowsecurity
       OR relation_info.relam <> (SELECT oid FROM pg_am WHERE amname = 'heap')
       OR relation_info.nspname = 'qdrant'
       OR left(relation_info.nspname,3) = 'pg_'
       OR relation_info.nspname = 'information_schema' THEN
        RAISE EXCEPTION 'Only ordinary permanent heap tables without RLS or partitions'
            USING ERRCODE = '0A000';
    END IF;
    IF NOT (actor_super OR pg_has_role(actor_oid,relation_info.relowner,'MEMBER')) THEN
        RAISE EXCEPTION 'Must own source relation' USING ERRCODE = '42501';
    END IF;
    SELECT a.atttypid INTO pk_type FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
    WHERE i.indrelid = source AND i.indisprimary AND i.indisvalid AND i.indisready
      AND i.indimmediate AND i.indnkeyatts = 1 AND i.indkey[0] > 0
      AND i.indexprs IS NULL AND i.indpred IS NULL
      AND a.attname = key_field AND a.attnotnull AND NOT a.attisdropped
      AND a.attgenerated = '';
    IF pk_type IS NULL OR pk_type NOT IN (
      'bigint'::regtype::oid,'uuid'::regtype::oid,'text'::regtype::oid) THEN
        RAISE EXCEPTION 'Single bigint/uuid/text primary key required'
            USING ERRCODE = '0A000';
    END IF;
    IF pk_type = 'text'::regtype::oid AND EXISTS (
      SELECT 1 FROM pg_attribute a JOIN pg_collation col ON col.oid = a.attcollation
      WHERE a.attrelid = source AND a.attname = key_field
        AND NOT col.collisdeterministic) THEN
        RAISE EXCEPTION 'Nondeterministic key collations unsupported'
            USING ERRCODE = '0A000';
    END IF;
    IF (SELECT count(DISTINCT f.value) FROM jsonb_array_elements_text(settings #> '{text,fields}') f)
         <> jsonb_array_length(settings #> '{text,fields}') THEN
        RAISE EXCEPTION 'Duplicate text fields unsupported' USING ERRCODE = '22023';
    END IF;
    FOR field_name IN SELECT jsonb_array_elements_text(settings #> '{text,fields}')
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = source
            AND a.attname = field_name AND a.atttypid = 'text'::regtype::oid
            AND a.attnum > 0 AND NOT a.attisdropped) THEN
            RAISE EXCEPTION 'Text fields must be built-in text columns'
                USING ERRCODE = '22023';
        END IF;
    END LOOP;

    -- Deliberately block writers during first snapshot; trigger + snapshot
    -- commit atomically and no event may be lost between these operations.
    EXECUTE format('LOCK TABLE %s IN SHARE ROW EXCLUSIVE MODE',source);
    INSERT INTO qdrant._index_catalog
        (index_name,source_table,source_owner,key_field,key_type,settings)
    VALUES (index_name,source,relation_info.relowner,key_field,pk_type,settings)
    RETURNING index_id INTO registered_id;
    EXECUTE format(
      'CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION qdrant._capture(%L)',
      'pgq_capture_'||registered_id,source,registered_id::text);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE TRUNCATE ON %s FOR EACH STATEMENT EXECUTE FUNCTION qdrant._deny_truncate()',
      'pgq_truncate_'||registered_id,source);
    EXECUTE format(
      'INSERT INTO qdrant._source_state
         (index_id,key_text,revision,incarnation,is_live,fingerprint)
       SELECT $1,to_jsonb(t)->>$2::text,1,gen_random_uuid(),true,
              qdrant._fingerprint($3,to_jsonb(t))
       FROM %s t',source)
    USING registered_id,key_field,settings;
    INSERT INTO qdrant._events
        (index_id,key_text,revision,incarnation,kind,origin,fingerprint,writer_xid)
    SELECT registered_id,key_text,revision,incarnation,'upsert','backfill',
           fingerprint,pg_current_xact_id()
    FROM qdrant._source_state WHERE index_id = registered_id;
    RETURN jsonb_build_object('index_id',registered_id,'index_name',index_name,
        'state','capture_only','search_ready',false,'edge_applied',false,
        'backfill_keys',(SELECT count(*) FROM qdrant._source_state
                         WHERE index_id=registered_id));
END;
$pgq$;

CREATE FUNCTION qdrant.index_status(p_index_name text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $pgq$
DECLARE
    selected qdrant._index_catalog%ROWTYPE;
    actor_oid oid;
    actor_super boolean;
    source_valid boolean;
BEGIN
    SELECT * INTO STRICT selected FROM qdrant._index_catalog
    WHERE index_name = p_index_name;
    SELECT r.oid,r.rolsuper INTO actor_oid,actor_super
    FROM pg_roles r WHERE r.rolname = session_user;
    IF actor_oid IS NULL OR NOT (coalesce(actor_super,false)
         OR pg_has_role(actor_oid,selected.source_owner,'MEMBER')) THEN
        RAISE EXCEPTION 'Insufficient privilege to inspect index'
            USING ERRCODE = '42501';
    END IF;
    SELECT EXISTS (
      SELECT 1 FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid
      WHERE c.oid = selected.source_table AND c.relkind = 'r'
        AND NOT c.relrowsecurity AND NOT c.relforcerowsecurity
        AND a.attname = selected.key_field AND a.atttypid = selected.key_type
        AND a.attnotnull AND NOT a.attisdropped
    ) INTO source_valid;
    RETURN jsonb_build_object(
       'index_id',selected.index_id,'state',selected.lifecycle,
       'source_valid',source_valid,'generation',selected.generation,
       'captured_events',(SELECT count(*) FROM qdrant._events
                          WHERE index_id=selected.index_id),
       'live_source_keys',(SELECT count(*) FROM qdrant._source_state
                           WHERE index_id=selected.index_id AND is_live),
       'edge_applied',false,'durable_ack_count',0,'search_ready',false,
       'missing_stage','P1 native Edge consumer and durable ACK');
END;
$pgq$;

CREATE FUNCTION qdrant.drop_index(p_index_name text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $pgq$
DECLARE
    selected qdrant._index_catalog%ROWTYPE;
    actor_oid oid;
    actor_super boolean;
BEGIN
    SELECT * INTO STRICT selected FROM qdrant._index_catalog
    WHERE index_name = p_index_name FOR UPDATE;
    SELECT r.oid,r.rolsuper INTO actor_oid,actor_super
    FROM pg_roles r WHERE r.rolname = session_user;
    IF actor_oid IS NULL OR NOT (coalesce(actor_super,false)
         OR pg_has_role(actor_oid,selected.source_owner,'MEMBER')) THEN
        RAISE EXCEPTION 'Insufficient privilege to remove index'
            USING ERRCODE = '42501';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE oid = selected.source_table) THEN
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s',
           'pgq_capture_'||selected.index_id,selected.source_table::regclass);
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %s',
           'pgq_truncate_'||selected.index_id,selected.source_table::regclass);
    END IF;
    DELETE FROM qdrant._index_catalog WHERE index_id=selected.index_id;
END;
$pgq$;

REVOKE ALL ON qdrant._index_catalog,qdrant._source_state,qdrant._events FROM PUBLIC;
REVOKE ALL ON FUNCTION qdrant._capture(),qdrant._deny_truncate(),
    qdrant._fingerprint(jsonb,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION qdrant.create_index(text,regclass,name,jsonb),
    qdrant.index_status(text),qdrant.drop_index(text) TO PUBLIC;
"###,
    name = "p1_transaction_capture",
    finalize
);
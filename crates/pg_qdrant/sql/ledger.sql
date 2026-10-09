-- Transactional installed source ledger. PostgreSQL source tables remain authoritative.
-- The source table remains authoritative; all rows below are transactional.
CREATE SEQUENCE qdrant_internal.point_seq AS bigint START WITH 1 MINVALUE 1 NO CYCLE;

CREATE TABLE qdrant_internal.index_catalog (
    index_id bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    index_name text PRIMARY KEY,
    settings jsonb NOT NULL DEFAULT '{}',
    generation_no bigint NOT NULL DEFAULT 1,
    backfill_cursor text,
    backfill_done boolean NOT NULL DEFAULT false,
    source_oid oid NOT NULL UNIQUE,
    source_schema text NOT NULL,
    source_name text NOT NULL,
    key_field text NOT NULL,
    text_field text NOT NULL,
    key_type oid NOT NULL,
    owner_oid oid NOT NULL,
    generation uuid NOT NULL DEFAULT pg_catalog.gen_random_uuid(),
    capture_state text NOT NULL DEFAULT 'capturing'
        CHECK (capture_state IN ('capturing', 'degraded')),
    created_xid xid8 NOT NULL DEFAULT pg_catalog.pg_current_xact_id(),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CONSTRAINT p1_name_valid CHECK (index_name ~ '^[a-z][a-z0-9_]{0,62}$')
);

CREATE TABLE qdrant_internal.source_state (
    index_name text NOT NULL REFERENCES qdrant_internal.index_catalog(index_name) ON DELETE CASCADE,
    tagged_key jsonb NOT NULL,
    point_id bigint NOT NULL CHECK (point_id > 0),
    incarnation uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    fingerprint text,
    payload jsonb,
    payload_fingerprint text,
    tombstone boolean NOT NULL,
    body text,
    PRIMARY KEY (index_name, tagged_key),
    UNIQUE (index_name, point_id),
    CHECK ((tombstone AND payload IS NULL AND payload_fingerprint IS NULL)
        OR (NOT tombstone AND payload IS NOT NULL AND payload_fingerprint IS NOT NULL
            AND jsonb_typeof(payload)='object' AND payload_fingerprint ~ '^[0-9a-f]{64}$')),
    CHECK ((tombstone AND body IS NULL)
        OR (NOT tombstone AND fingerprint ~ '^[0-9a-f]{64}$' AND body IS NOT NULL))
);

CREATE TABLE qdrant_internal.change_tickets (
    ticket_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    index_name text NOT NULL REFERENCES qdrant_internal.index_catalog(index_name) ON DELETE CASCADE,
    source_xid xid8 NOT NULL,
    generation uuid NOT NULL,
    sealed_events integer NOT NULL DEFAULT 0 CHECK (sealed_events >= 0)
);

CREATE TABLE qdrant_internal.outbox (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    index_name text NOT NULL REFERENCES qdrant_internal.index_catalog(index_name) ON DELETE CASCADE,
    source_xid xid8 NOT NULL,
    tagged_key jsonb NOT NULL,
    point_id bigint NOT NULL,
    incarnation uuid NOT NULL,
    revision bigint NOT NULL,
    operation text NOT NULL CHECK (operation IN ('upsert', 'delete')),
    fingerprint text,
    projection jsonb,
    origin text NOT NULL CHECK (origin IN ('backfill', 'source_write', 'truncate')),
    ticket_id uuid REFERENCES qdrant_internal.change_tickets(ticket_id) ON DELETE CASCADE,
    CHECK ((operation = 'delete' AND projection IS NULL)
        OR (operation = 'upsert' AND fingerprint ~ '^[0-9a-f]{64}$' AND projection IS NOT NULL))
);
CREATE TABLE qdrant_internal.consumer_state (
    index_name text PRIMARY KEY REFERENCES qdrant_internal.index_catalog ON DELETE CASCADE,
    generation uuid NOT NULL,
    storage_epoch uuid NOT NULL DEFAULT gen_random_uuid(),
    consumer_id uuid NOT NULL DEFAULT gen_random_uuid(),
    engine_instance text NOT NULL CHECK (engine_instance ~ '^[0-9a-f]{32}$'),
    state text NOT NULL DEFAULT 'building' CHECK (state IN ('building','ready','dirty','failed')),
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE qdrant_internal.event_ack (
    event_id bigint PRIMARY KEY REFERENCES qdrant_internal.outbox ON DELETE CASCADE,
    generation uuid NOT NULL,
    storage_epoch uuid NOT NULL,
    consumer_id uuid NOT NULL,
    flushed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX outbox_unsealed
    ON qdrant_internal.outbox (index_name, source_xid, event_id)
    WHERE ticket_id IS NULL;
CREATE INDEX outbox_by_ticket
    ON qdrant_internal.outbox (ticket_id) WHERE ticket_id IS NOT NULL;

-- Record a row mutation in the same PostgreSQL transaction as the source write.
-- ACK is a separate, exact-set consumer receipt after successful Edge flush.
CREATE FUNCTION qdrant_internal.p1_record(
    p_name text, p_operation text, p_key text, p_body text, p_origin text, p_models jsonb DEFAULT '{}', p_payload jsonb DEFAULT '{}'
) RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
DECLARE
    v_idx qdrant_internal.index_catalog%ROWTYPE;
    v_old qdrant_internal.source_state%ROWTYPE;
    v_present boolean;
    v_kind text;
    v_key text;
    v_tagged jsonb;
    v_point bigint;
    v_incarnation uuid;
    v_revision bigint;
    v_body text;
    v_fingerprint text;
    v_event bigint;
    v_vectors jsonb;
    v_payload jsonb;
    v_payload_fingerprint text;
BEGIN
    SELECT * INTO STRICT v_idx FROM qdrant_internal.index_catalog WHERE index_name = p_name;
    IF p_operation NOT IN ('upsert','delete')
       OR p_origin NOT IN ('backfill','source_write','truncate')
       OR p_key IS NULL THEN
        RAISE EXCEPTION 'invalid P1 source operation' USING ERRCODE = '22023';
    END IF;
    v_kind := CASE v_idx.key_type
        WHEN 'pg_catalog.int8'::regtype::oid THEN 'bigint'
        WHEN 'pg_catalog.uuid'::regtype::oid THEN 'uuid'
        WHEN 'pg_catalog.text'::regtype::oid THEN 'text'
        ELSE NULL END;
    IF v_kind IS NULL THEN
        RAISE EXCEPTION 'unsupported P1 source key type' USING ERRCODE = '0A000';
    END IF;
    v_key := p_key;
    IF v_key IS NULL OR octet_length(v_key) > 1024 THEN
        RAISE EXCEPTION 'source key is NULL or exceeds 1024 UTF-8 bytes'
            USING ERRCODE = '22023';
    END IF;
    IF v_kind = 'bigint' THEN
        v_key := (v_key::bigint)::text;
    ELSIF v_kind = 'uuid' THEN
        v_key := (v_key::uuid)::text;
    END IF;
    v_tagged := jsonb_build_object('type', v_kind, 'value', v_key);
    IF p_operation = 'upsert' THEN
        v_body := p_body;
        IF v_body IS NULL OR octet_length(v_body) > 65536 THEN
            RAISE EXCEPTION 'P1 indexed text must be non-null and at most 65536 bytes'
                USING ERRCODE = '22023';
        END IF;
        v_fingerprint := encode(sha256(convert_to(v_body, 'UTF8')), 'hex');
        IF p_payload IS NULL OR jsonb_typeof(p_payload) IS DISTINCT FROM 'object' OR octet_length(p_payload::text)>8192 THEN
            RAISE EXCEPTION 'Invalid payload projection' USING ERRCODE='22023'; END IF;
        v_payload:=p_payload;
        v_payload_fingerprint:=encode(sha256(convert_to(v_payload::text,'UTF8')),'hex');
    END IF;
    SELECT * INTO v_old FROM qdrant_internal.source_state
        WHERE index_name = p_name AND tagged_key = v_tagged FOR UPDATE;
    v_present := FOUND;
    IF v_present THEN
        IF v_old.revision = 9223372036854775807 THEN
            RAISE EXCEPTION 'P1 revision exhausted' USING ERRCODE = '54000';
        END IF;
        v_revision := v_old.revision + 1;
        IF v_old.tombstone AND p_operation = 'upsert' THEN
            v_incarnation := gen_random_uuid();
            v_point := nextval('qdrant_internal.point_seq'::regclass);
        ELSE
            v_incarnation := v_old.incarnation;
            v_point := v_old.point_id;
        END IF;
        IF p_operation = 'delete' THEN
            v_fingerprint := v_old.fingerprint;
        END IF;
        UPDATE qdrant_internal.source_state SET
            point_id = v_point, incarnation = v_incarnation,
            revision = v_revision, fingerprint = v_fingerprint,
            payload = v_payload, payload_fingerprint = v_payload_fingerprint,
            tombstone = p_operation = 'delete',
            body = CASE WHEN p_operation = 'upsert' THEN v_body ELSE NULL END
        WHERE index_name = p_name AND tagged_key = v_tagged;
    ELSE
        v_revision := 1;
        v_incarnation := gen_random_uuid();
        v_point := nextval('qdrant_internal.point_seq'::regclass);
        INSERT INTO qdrant_internal.source_state
          (index_name, tagged_key, point_id, incarnation, revision, fingerprint, tombstone, body, payload, payload_fingerprint)
        VALUES (p_name, v_tagged, v_point, v_incarnation, v_revision,
                v_fingerprint, p_operation='delete', v_body, v_payload, v_payload_fingerprint);
    END IF;
    v_vectors:=qdrant_internal.capture_representations(p_name,v_tagged,v_incarnation,v_fingerprint,p_models,p_operation='delete');
    IF p_operation='upsert' AND octet_length(jsonb_build_object('body',v_body,'vectors',v_vectors,'payload',v_payload)::text)>458752 THEN
      RAISE EXCEPTION 'Text and representation projection exceeds the 448 KiB consumer budget' USING ERRCODE='54000';
    END IF;
    INSERT INTO qdrant_internal.outbox
      (index_name, source_xid, tagged_key, point_id, incarnation, revision,
       operation, fingerprint, projection, origin)
    VALUES
      (p_name, pg_current_xact_id(), v_tagged, v_point, v_incarnation, v_revision,
       p_operation, v_fingerprint,
       CASE WHEN p_operation = 'upsert' THEN jsonb_build_object('body',v_body,'vectors',v_vectors,'payload',v_payload,'payload_fingerprint',v_payload_fingerprint) ELSE NULL END,
       p_origin)
    RETURNING event_id INTO v_event;
    RETURN v_event;
END
$p1$;

CREATE FUNCTION qdrant_internal.p1_capture_row() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
DECLARE
    v_idx qdrant_internal.index_catalog%ROWTYPE;
    v_old_key text;
    v_new_key text;
    v_new_body text;
BEGIN
    IF TG_WHEN <> 'AFTER' OR TG_LEVEL <> 'ROW' OR TG_NARGS <> 1
        OR TG_NAME <> 'qdrant_p1_rows' THEN
        RAISE EXCEPTION 'untrusted P1 trigger invocation' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO STRICT v_idx
      FROM qdrant_internal.index_catalog
      WHERE index_name = TG_ARGV[0] AND source_oid = TG_RELID;
    IF v_idx.capture_state<>'capturing' THEN
        IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        EXECUTE format('SELECT ($1).%I::text', v_idx.key_field)
          INTO v_old_key USING OLD;
        PERFORM qdrant_internal.p1_record(v_idx.index_name,'delete',v_old_key,NULL,'source_write');
        RETURN OLD;
    ELSIF TG_OP = 'INSERT' THEN
        EXECUTE format('SELECT ($1).%I::text, ($1).%I',v_idx.key_field,v_idx.text_field)
          INTO v_new_key,v_new_body USING NEW;
        PERFORM qdrant_internal.p1_record(v_idx.index_name,'upsert',v_new_key,v_new_body,'source_write',
          qdrant_internal.model_projection(v_idx.index_name,NEW),qdrant_internal.payload_projection(v_idx.index_name,NEW));
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        EXECUTE format('SELECT ($1).%I::text',v_idx.key_field)
          INTO v_old_key USING OLD;
        EXECUTE format('SELECT ($1).%I::text, ($1).%I',v_idx.key_field,v_idx.text_field)
          INTO v_new_key,v_new_body USING NEW;
        IF v_old_key IS DISTINCT FROM v_new_key THEN
            PERFORM qdrant_internal.p1_record(v_idx.index_name,'delete',v_old_key,NULL,'source_write');
        END IF;
        PERFORM qdrant_internal.p1_record(v_idx.index_name,'upsert',v_new_key,v_new_body,'source_write',
          qdrant_internal.model_projection(v_idx.index_name,NEW),qdrant_internal.payload_projection(v_idx.index_name,NEW));
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'unsupported P1 trigger event' USING ERRCODE = '0A000';
END
$p1$;

CREATE FUNCTION qdrant_internal.p1_capture_truncate() RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
DECLARE
    v_idx qdrant_internal.index_catalog%ROWTYPE;
    v_row record;
BEGIN
    IF TG_OP <> 'TRUNCATE' OR TG_LEVEL <> 'STATEMENT' OR TG_WHEN <> 'AFTER'
       OR TG_NARGS <> 1 OR TG_NAME <> 'qdrant_p1_truncate' THEN
        RAISE EXCEPTION 'untrusted P1 truncate invocation' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO STRICT v_idx FROM qdrant_internal.index_catalog
      WHERE index_name = TG_ARGV[0] AND source_oid = TG_RELID;
    IF v_idx.capture_state<>'capturing' THEN RETURN NULL; END IF;
    FOR v_row IN SELECT tagged_key FROM qdrant_internal.source_state
                 WHERE index_name = v_idx.index_name AND NOT tombstone
                 ORDER BY point_id LOOP
        PERFORM qdrant_internal.p1_record(v_idx.index_name,'delete',
            v_row.tagged_key->>'value',NULL,'truncate');
    END LOOP;
    RETURN NULL;
END
$p1$;

-- Registration installs capture atomically under the DDL lock. Bounded online
-- backfill starts only after registration commits and capture is visible.
CREATE FUNCTION qdrant_internal.p1_register(
    p_name text, p_source regclass, p_key_field text, p_text_field text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
DECLARE
    v_source record;
    v_capture_count integer;
    v_capture_valid integer;
BEGIN
    IF p_name IS NULL OR p_name !~ '^[a-z][a-z0-9_]{0,62}$'
       OR p_source IS NULL OR p_key_field IS NULL OR p_text_field IS NULL
       OR p_key_field = p_text_field THEN
        RAISE EXCEPTION 'invalid P1 index name or source fields' USING ERRCODE = '22023';
    END IF;
    EXECUTE format('LOCK TABLE %s IN SHARE ROW EXCLUSIVE MODE',p_source);
    SELECT c.oid AS source_oid,c.relowner AS owner_oid,n.nspname,c.relname,ka.atttypid AS key_oid
      INTO v_source
    FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    JOIN pg_attribute ka ON ka.attrelid=c.oid AND ka.attname=p_key_field
      AND ka.attnotnull AND ka.attnum > 0 AND NOT ka.attisdropped AND ka.attgenerated = ''
    JOIN pg_attribute ta ON ta.attrelid=c.oid AND ta.attname=p_text_field
      AND ta.attnotnull AND ta.attnum > 0 AND NOT ta.attisdropped
      AND ta.atttypid = 'pg_catalog.text'::regtype::oid AND ta.attgenerated = ''
    WHERE c.oid=p_source::oid AND c.relkind='r' AND c.relpersistence='p'
      AND c.relam=(SELECT am.oid FROM pg_am am WHERE am.amname='heap')
      AND NOT c.relispartition AND NOT c.relhassubclass
      AND NOT c.relrowsecurity AND NOT c.relforcerowsecurity
      AND left(n.nspname,3) <> 'pg_'
      AND n.nspname NOT IN ('information_schema','qdrant','qdrant_internal')
      AND ka.atttypid IN ('pg_catalog.int8'::regtype::oid,
                           'pg_catalog.uuid'::regtype::oid,
                           'pg_catalog.text'::regtype::oid)
      AND (ka.atttypid <> 'pg_catalog.text'::regtype::oid
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_collation col WHERE col.oid=ka.attcollation AND col.collisdeterministic))
      AND EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_class ic ON ic.oid=i.indexrelid
        JOIN pg_am am ON am.oid=ic.relam AND am.amname='btree'
        JOIN pg_opclass oc ON oc.oid=i.indclass[0]
        JOIN pg_namespace onsp ON onsp.oid=oc.opcnamespace
        WHERE i.indrelid=c.oid AND i.indisprimary AND i.indisunique AND i.indimmediate
          AND i.indisvalid AND i.indisready AND i.indislive
          AND i.indnatts=1 AND i.indnkeyatts=1 AND i.indkey[0]=ka.attnum
          AND oc.opcdefault AND oc.opcintype=ka.atttypid
          AND onsp.nspname='pg_catalog'
      );
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unsupported P1 source: ordinary persistent table, single default btree PK, non-null text, no RLS/partition'
            USING ERRCODE = '0A000';
    END IF;
    -- The public preflight precedes this lock and may have waited behind owner
    -- DDL. Recheck the effective caller against the locked current relation.
    IF NOT pg_has_role(qdrant_internal.actor_oid(),v_source.owner_oid,'USAGE') THEN
        RAISE EXCEPTION 'Source owner access required' USING ERRCODE='42501';
    END IF;
    -- A logical restore recreates user-table triggers but not this extension's
    -- private catalog data. Re-registration creates fresh identities/receipts;
    -- it never imports old native readiness or treats those triggers as capture.
    IF EXISTS (SELECT 1 FROM qdrant_internal.index_catalog
               WHERE index_name=p_name OR source_oid=p_source::oid) THEN
        RAISE EXCEPTION 'Index name or source is already registered' USING ERRCODE='23505';
    END IF;
    SELECT count(*),count(*) FILTER (WHERE NOT t.tgisinternal AND t.tgenabled='O'
      AND t.tgnargs=1 AND t.tgargs=convert_to(p_name,'UTF8')||decode('00','hex')
      AND t.tgqual IS NULL AND t.tgconstraint=0 AND t.tgattr=''::int2vector
      AND ((t.tgname='qdrant_p1_rows' AND t.tgtype=29
            AND t.tgfoid='qdrant_internal.p1_capture_row()'::regprocedure)
        OR (t.tgname='qdrant_p1_truncate' AND t.tgtype=32
            AND t.tgfoid='qdrant_internal.p1_capture_truncate()'::regprocedure)))
      INTO v_capture_count,v_capture_valid FROM pg_trigger t
      WHERE t.tgrelid=p_source::oid AND t.tgname IN ('qdrant_p1_rows','qdrant_p1_truncate');
    IF v_capture_count<>0 THEN
        IF v_capture_count<>2 OR v_capture_valid<>2 THEN
            RAISE EXCEPTION 'Incomplete or unrecognized source capture triggers'
                USING ERRCODE='55000';
        END IF;
        EXECUTE format('DROP TRIGGER qdrant_p1_rows ON %s',p_source);
        EXECUTE format('DROP TRIGGER qdrant_p1_truncate ON %s',p_source);
    END IF;
    INSERT INTO qdrant_internal.index_catalog
      (index_name,source_oid,source_schema,source_name,key_field,text_field,key_type,owner_oid)
    VALUES (p_name,p_source::oid,v_source.nspname,v_source.relname,
            p_key_field,p_text_field,v_source.key_oid,
            v_source.owner_oid);
    EXECUTE format(
      'CREATE TRIGGER qdrant_p1_rows AFTER INSERT OR UPDATE OR DELETE ON %s
       FOR EACH ROW EXECUTE FUNCTION qdrant_internal.p1_capture_row(%L)',
      p_source,p_name);
    EXECUTE format(
      'CREATE TRIGGER qdrant_p1_truncate AFTER TRUNCATE ON %s
       FOR EACH STATEMENT EXECUTE FUNCTION qdrant_internal.p1_capture_truncate(%L)',
      p_source,p_name);
    UPDATE qdrant_internal.index_catalog
      SET settings=jsonb_build_object('text',jsonb_build_object('fields',jsonb_build_array(p_text_field)))
      WHERE index_name=p_name;
    RETURN jsonb_build_object('index_name',p_name,'capture_state','capturing',
      'source_oid',p_source::oid,'backfilled_rows',0,
      'engine_index_ready',false,'online_backfill',true,
      'release_supported',false);
END
$p1$;

-- A ticket is an exact membership set: later events from the same transaction
-- are NOT added. Source event IDs never stand for commit-order watermarks.
CREATE FUNCTION qdrant_internal.p1_track(p_name text) RETURNS uuid
LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
DECLARE v_ticket uuid := gen_random_uuid(); v_count integer;
BEGIN
    PERFORM 1 FROM qdrant_internal.index_catalog WHERE index_name=p_name;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown P1 index' USING ERRCODE = '22023'; END IF;
    INSERT INTO qdrant_internal.change_tickets(ticket_id,index_name,source_xid,generation)
    SELECT v_ticket,p_name,pg_current_xact_id(),generation FROM qdrant_internal.index_catalog
      WHERE index_name=p_name;
    WITH changed AS (
      UPDATE qdrant_internal.outbox SET ticket_id=v_ticket
      WHERE index_name=p_name AND source_xid=pg_current_xact_id()
        AND ticket_id IS NULL RETURNING event_id
    ) SELECT count(*)::integer INTO v_count FROM changed;
    UPDATE qdrant_internal.change_tickets SET sealed_events=v_count WHERE ticket_id=v_ticket;
    RETURN v_ticket;
END
$p1$;

CREATE FUNCTION qdrant_internal.p1_index_status(p_name text) RETURNS jsonb
LANGUAGE sql STABLE
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
  SELECT jsonb_build_object(
    'index_name',i.index_name,'source_oid',i.source_oid,
    'capture_state',CASE
       WHEN c.oid IS NULL OR c.relkind<>'r' OR c.relpersistence<>'p'
         OR c.relowner<>i.owner_oid
         OR c.relispartition OR c.relhassubclass OR c.relname<>i.source_name
         OR c.relnamespace<>(SELECT n.oid FROM pg_namespace n WHERE n.nspname=i.source_schema)
         OR c.relrowsecurity OR c.relforcerowsecurity
         OR c.relam<>(SELECT am.oid FROM pg_am am WHERE am.amname='heap')
         OR NOT EXISTS(SELECT 1 FROM pg_index pk WHERE pk.indrelid=c.oid
             AND pk.indisprimary AND pk.indisvalid AND pk.indisready AND pk.indimmediate
             AND pk.indnkeyatts=1 AND pk.indnatts=1 AND pk.indkey[0]=(
               SELECT a.attnum FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attname=i.key_field))
         OR NOT EXISTS(
           SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid
             AND a.attname=i.key_field AND a.atttypid=i.key_type
             AND a.attnum>0 AND a.attnotnull AND NOT a.attisdropped)
         OR NOT EXISTS(
           SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid
             AND a.attname=i.text_field AND a.atttypid='pg_catalog.text'::regtype::oid
             AND a.attnum>0 AND a.attnotnull AND NOT a.attisdropped)
         OR (SELECT count(*) FROM pg_trigger t
              WHERE t.tgrelid=c.oid AND NOT t.tgisinternal
                AND t.tgargs=convert_to(i.index_name,'UTF8')||decode('00','hex')
                AND t.tgnargs=1 AND t.tgconstraint=0 AND t.tgparentid=0
                AND t.tgenabled IN ('O','A')
                AND ((t.tgname='qdrant_p1_rows'
                    AND t.tgtype=29
                    AND t.tgfoid='qdrant_internal.p1_capture_row()'::regprocedure)
                  OR (t.tgname='qdrant_p1_truncate'
                    AND t.tgtype=32
                    AND t.tgfoid='qdrant_internal.p1_capture_truncate()'::regprocedure)))<>2
       THEN 'degraded' ELSE i.capture_state END,
    'source_exists',c.oid IS NOT NULL,'generation',i.generation,
    'captured_events',(SELECT count(*) FROM qdrant_internal.outbox e
                       WHERE e.index_name=i.index_name),
    'live_source_keys',(SELECT count(*) FROM qdrant_internal.source_state s
                       WHERE s.index_name=i.index_name AND NOT s.tombstone),
    'engine_index_ready',false,'release_supported',false)
  FROM qdrant_internal.index_catalog i LEFT JOIN pg_class c ON c.oid=i.source_oid
  WHERE i.index_name=p_name
$p1$;

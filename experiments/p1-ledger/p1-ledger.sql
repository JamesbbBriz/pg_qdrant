-- P1 experimental source-ledger slice. Installed only by the disposable test
-- harness; this is NOT a product search API or an Edge durability ACK.
-- The source table remains authoritative; all rows below are transactional.
CREATE SEQUENCE qdrant_internal.p1_point_seq AS bigint START WITH 1 MINVALUE 1 NO CYCLE;

CREATE TABLE qdrant_internal.p1_indexes (
    index_name text PRIMARY KEY,
    source_oid oid NOT NULL UNIQUE,
    source_schema text NOT NULL,
    source_table text NOT NULL,
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

CREATE TABLE qdrant_internal.p1_source_state (
    index_name text NOT NULL REFERENCES qdrant_internal.p1_indexes(index_name),
    tagged_key jsonb NOT NULL,
    point_id bigint NOT NULL CHECK (point_id > 0),
    incarnation uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    fingerprint text,
    tombstone boolean NOT NULL,
    body text,
    PRIMARY KEY (index_name, tagged_key),
    UNIQUE (index_name, point_id),
    CHECK ((tombstone AND body IS NULL)
        OR (NOT tombstone AND fingerprint ~ '^[0-9a-f]{64}$' AND body IS NOT NULL))
);

CREATE TABLE qdrant_internal.p1_tickets (
    ticket_id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
    index_name text NOT NULL REFERENCES qdrant_internal.p1_indexes(index_name),
    source_xid xid8 NOT NULL,
    sealed_events integer NOT NULL DEFAULT 0 CHECK (sealed_events >= 0)
);

CREATE TABLE qdrant_internal.p1_outbox (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    index_name text NOT NULL REFERENCES qdrant_internal.p1_indexes(index_name),
    source_xid xid8 NOT NULL,
    tagged_key jsonb NOT NULL,
    point_id bigint NOT NULL,
    incarnation uuid NOT NULL,
    revision bigint NOT NULL,
    operation text NOT NULL CHECK (operation IN ('upsert', 'delete')),
    fingerprint text,
    projection jsonb,
    origin text NOT NULL CHECK (origin IN ('backfill', 'source_write', 'truncate')),
    ticket_id uuid REFERENCES qdrant_internal.p1_tickets(ticket_id),
    CHECK ((operation = 'delete' AND projection IS NULL)
        OR (operation = 'upsert' AND fingerprint ~ '^[0-9a-f]{64}$' AND projection IS NOT NULL))
);
CREATE INDEX p1_outbox_unsealed
    ON qdrant_internal.p1_outbox (index_name, source_xid, event_id)
    WHERE ticket_id IS NULL;
CREATE INDEX p1_outbox_by_ticket
    ON qdrant_internal.p1_outbox (ticket_id) WHERE ticket_id IS NOT NULL;

-- Record a row mutation in the same PostgreSQL transaction as the source write.
-- An immutable event is never marked durable in this slice: no Edge flush exists.
CREATE FUNCTION qdrant_internal.p1_record(
    p_name text, p_operation text, p_key text, p_body text, p_origin text
) RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
DECLARE
    v_idx qdrant_internal.p1_indexes%ROWTYPE;
    v_old qdrant_internal.p1_source_state%ROWTYPE;
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
BEGIN
    SELECT * INTO STRICT v_idx FROM qdrant_internal.p1_indexes WHERE index_name = p_name;
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
    END IF;
    SELECT * INTO v_old FROM qdrant_internal.p1_source_state
        WHERE index_name = p_name AND tagged_key = v_tagged FOR UPDATE;
    v_present := FOUND;
    IF NOT v_present AND p_operation = 'delete' THEN
        RAISE EXCEPTION 'P1 deletion has no previous captured identity'
            USING ERRCODE = '55000';
    END IF;
    IF v_present THEN
        IF v_old.revision = 9223372036854775807 THEN
            RAISE EXCEPTION 'P1 revision exhausted' USING ERRCODE = '54000';
        END IF;
        v_revision := v_old.revision + 1;
        IF v_old.tombstone AND p_operation = 'upsert' THEN
            v_incarnation := gen_random_uuid();
            v_point := nextval('qdrant_internal.p1_point_seq'::regclass);
        ELSE
            v_incarnation := v_old.incarnation;
            v_point := v_old.point_id;
        END IF;
        IF p_operation = 'delete' THEN
            v_fingerprint := v_old.fingerprint;
        END IF;
        UPDATE qdrant_internal.p1_source_state SET
            point_id = v_point, incarnation = v_incarnation,
            revision = v_revision, fingerprint = v_fingerprint,
            tombstone = p_operation = 'delete',
            body = CASE WHEN p_operation = 'upsert' THEN v_body ELSE NULL END
        WHERE index_name = p_name AND tagged_key = v_tagged;
    ELSE
        v_revision := 1;
        v_incarnation := gen_random_uuid();
        v_point := nextval('qdrant_internal.p1_point_seq'::regclass);
        INSERT INTO qdrant_internal.p1_source_state
          (index_name, tagged_key, point_id, incarnation, revision, fingerprint, tombstone, body)
        VALUES (p_name, v_tagged, v_point, v_incarnation, v_revision,
                v_fingerprint, false, v_body);
    END IF;
    INSERT INTO qdrant_internal.p1_outbox
      (index_name, source_xid, tagged_key, point_id, incarnation, revision,
       operation, fingerprint, projection, origin)
    VALUES
      (p_name, pg_current_xact_id(), v_tagged, v_point, v_incarnation, v_revision,
       p_operation, v_fingerprint,
       CASE WHEN p_operation = 'upsert' THEN jsonb_build_object('body',v_body) ELSE NULL END,
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
    v_idx qdrant_internal.p1_indexes%ROWTYPE;
    v_old_key text;
    v_new_key text;
    v_new_body text;
BEGIN
    IF TG_WHEN <> 'AFTER' OR TG_LEVEL <> 'ROW' OR TG_NARGS <> 1
        OR TG_NAME <> 'qdrant_p1_rows' THEN
        RAISE EXCEPTION 'untrusted P1 trigger invocation' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO STRICT v_idx
      FROM qdrant_internal.p1_indexes
      WHERE index_name = TG_ARGV[0] AND source_oid = TG_RELID
        AND capture_state = 'capturing';
    IF TG_OP = 'DELETE' THEN
        EXECUTE format('SELECT ($1).%I::text', v_idx.key_field)
          INTO v_old_key USING OLD;
        PERFORM qdrant_internal.p1_record(v_idx.index_name,'delete',v_old_key,NULL,'source_write');
        RETURN OLD;
    ELSIF TG_OP = 'INSERT' THEN
        EXECUTE format('SELECT ($1).%I::text, ($1).%I',v_idx.key_field,v_idx.text_field)
          INTO v_new_key,v_new_body USING NEW;
        PERFORM qdrant_internal.p1_record(v_idx.index_name,'upsert',v_new_key,v_new_body,'source_write');
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        EXECUTE format('SELECT ($1).%I::text',v_idx.key_field)
          INTO v_old_key USING OLD;
        EXECUTE format('SELECT ($1).%I::text, ($1).%I',v_idx.key_field,v_idx.text_field)
          INTO v_new_key,v_new_body USING NEW;
        IF v_old_key IS DISTINCT FROM v_new_key THEN
            PERFORM qdrant_internal.p1_record(v_idx.index_name,'delete',v_old_key,NULL,'source_write');
        END IF;
        PERFORM qdrant_internal.p1_record(v_idx.index_name,'upsert',v_new_key,v_new_body,'source_write');
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
    v_idx qdrant_internal.p1_indexes%ROWTYPE;
    v_row record;
BEGIN
    IF TG_OP <> 'TRUNCATE' OR TG_LEVEL <> 'STATEMENT' OR TG_WHEN <> 'AFTER'
       OR TG_NARGS <> 1 OR TG_NAME <> 'qdrant_p1_truncate' THEN
        RAISE EXCEPTION 'untrusted P1 truncate invocation' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO STRICT v_idx FROM qdrant_internal.p1_indexes
      WHERE index_name = TG_ARGV[0] AND source_oid = TG_RELID AND capture_state = 'capturing';
    FOR v_row IN SELECT tagged_key FROM qdrant_internal.p1_source_state
                 WHERE index_name = v_idx.index_name AND NOT tombstone
                 ORDER BY point_id LOOP
        PERFORM qdrant_internal.p1_record(v_idx.index_name,'delete',
            v_row.tagged_key->>'value',NULL,'truncate');
    END LOOP;
    RETURN NULL;
END
$p1$;

-- Registration installs capture and takes the initial snapshot atomically.
-- ACCESS EXCLUSIVE is deliberately conservative: bounded to 1000 source rows.
-- This is a correctness fixture, not the production online backfill design.
CREATE FUNCTION qdrant_internal.p1_register(
    p_name text, p_source regclass, p_key_field text, p_text_field text
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
DECLARE
    v_source record;
    v_count bigint;
    v_row record;
    v_inserted bigint := 0;
BEGIN
    IF p_name IS NULL OR p_name !~ '^[a-z][a-z0-9_]{0,62}$'
       OR p_source IS NULL OR p_key_field IS NULL OR p_text_field IS NULL
       OR p_key_field = p_text_field THEN
        RAISE EXCEPTION 'invalid P1 index name or source fields' USING ERRCODE = '22023';
    END IF;
    EXECUTE format('LOCK TABLE %s IN ACCESS EXCLUSIVE MODE',p_source);
    SELECT c.oid AS source_oid,n.nspname,c.relname,ka.atttypid AS key_oid
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
    EXECUTE format('SELECT count(*) FROM (SELECT 1 FROM ONLY %s LIMIT 1001) p', p_source)
      INTO v_count;
    IF v_count > 1000 THEN
        RAISE EXCEPTION 'P1 locked initial backfill supports at most 1000 rows'
            USING ERRCODE = '54000';
    END IF;
    INSERT INTO qdrant_internal.p1_indexes
      (index_name,source_oid,source_schema,source_table,key_field,text_field,key_type,owner_oid)
    VALUES (p_name,p_source::oid,v_source.nspname,v_source.relname,
            p_key_field,p_text_field,v_source.key_oid,
            (SELECT oid FROM pg_roles WHERE rolname=current_user));
    EXECUTE format(
      'CREATE TRIGGER qdrant_p1_rows AFTER INSERT OR UPDATE OR DELETE ON %s
       FOR EACH ROW EXECUTE FUNCTION qdrant_internal.p1_capture_row(%L)',
      p_source,p_name);
    EXECUTE format(
      'CREATE TRIGGER qdrant_p1_truncate AFTER TRUNCATE ON %s
       FOR EACH STATEMENT EXECUTE FUNCTION qdrant_internal.p1_capture_truncate(%L)',
      p_source,p_name);
    FOR v_row IN EXECUTE format(
       'SELECT %I::text AS key, %I AS body FROM ONLY %s',
       p_key_field,p_text_field,p_source) LOOP
        PERFORM qdrant_internal.p1_record(p_name,'upsert',v_row.key,v_row.body,'backfill');
        v_inserted := v_inserted+1;
    END LOOP;
    RETURN jsonb_build_object('index_name',p_name,'capture_state','capturing',
      'source_oid',p_source::oid,'backfilled_rows',v_inserted,
      'engine_index_ready',false,'online_backfill',false,'source_lock','ACCESS EXCLUSIVE',
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
    PERFORM 1 FROM qdrant_internal.p1_indexes WHERE index_name=p_name;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown P1 index' USING ERRCODE = '22023'; END IF;
    INSERT INTO qdrant_internal.p1_tickets(ticket_id,index_name,source_xid)
    VALUES (v_ticket,p_name,pg_current_xact_id());
    WITH changed AS (
      UPDATE qdrant_internal.p1_outbox SET ticket_id=v_ticket
      WHERE index_name=p_name AND source_xid=pg_current_xact_id()
        AND ticket_id IS NULL RETURNING event_id
    ) SELECT count(*)::integer INTO v_count FROM changed;
    UPDATE qdrant_internal.p1_tickets SET sealed_events=v_count WHERE ticket_id=v_ticket;
    RETURN v_ticket;
END
$p1$;

-- Same-transaction waits are rejected. No production ACK exists yet, so this
-- function cannot report durable=true for a nonempty ticket.
CREATE FUNCTION qdrant_internal.p1_ticket_status(p_ticket uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
DECLARE v_ticket qdrant_internal.p1_tickets%ROWTYPE; v_count integer;
BEGIN
    SELECT * INTO v_ticket FROM qdrant_internal.p1_tickets WHERE ticket_id=p_ticket;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown or uncommitted P1 ticket'
        USING ERRCODE = '22023'; END IF;
    IF v_ticket.source_xid = pg_current_xact_id_if_assigned() THEN
        RAISE EXCEPTION 'cannot await uncommitted changes in the writing transaction'
            USING ERRCODE = '55000';
    END IF;
    SELECT count(*)::integer INTO v_count FROM qdrant_internal.p1_outbox
      WHERE ticket_id=p_ticket;
    IF v_count <> v_ticket.sealed_events THEN
        RAISE EXCEPTION 'P1 ticket membership is inconsistent' USING ERRCODE = 'XX001';
    END IF;
    RETURN jsonb_build_object('ticket',p_ticket,'index_name',v_ticket.index_name,
      'committed',true,'sealed_events',v_count,'pending_events',v_count,
      'applied',v_count=0,'durable',v_count=0,'timed_out',false,
      'engine_acknowledgement_supported',false,'status',
      CASE WHEN v_count=0 THEN 'empty' ELSE 'pending_engine_application' END);
END
$p1$;

CREATE FUNCTION qdrant_internal.p1_index_status(p_name text) RETURNS jsonb
LANGUAGE sql STABLE
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
  SELECT jsonb_build_object(
    'index_name',i.index_name,'source_oid',i.source_oid,
    'capture_state',CASE
       WHEN c.oid IS NULL OR c.relname<>i.source_table
         OR c.relnamespace<>(SELECT n.oid FROM pg_namespace n WHERE n.nspname=i.source_schema)
         OR c.relrowsecurity OR c.relforcerowsecurity
         OR c.relam<>(SELECT am.oid FROM pg_am am WHERE am.amname='heap')
         OR (SELECT count(*) FROM pg_trigger t
              WHERE t.tgrelid=c.oid AND NOT t.tgisinternal
                AND t.tgenabled IN ('O','A')
                AND ((t.tgname='qdrant_p1_rows'
                    AND t.tgfoid='qdrant_internal.p1_capture_row()'::regprocedure)
                  OR (t.tgname='qdrant_p1_truncate'
                    AND t.tgfoid='qdrant_internal.p1_capture_truncate()'::regprocedure)))<>2
       THEN 'degraded' ELSE i.capture_state END,
    'source_exists',c.oid IS NOT NULL,'generation',i.generation,
    'captured_events',(SELECT count(*) FROM qdrant_internal.p1_outbox e
                       WHERE e.index_name=i.index_name),
    'live_source_keys',(SELECT count(*) FROM qdrant_internal.p1_source_state s
                       WHERE s.index_name=i.index_name AND NOT s.tombstone),
    'engine_index_ready',false,'release_supported',false)
  FROM qdrant_internal.p1_indexes i LEFT JOIN pg_class c ON c.oid=i.source_oid
  WHERE i.index_name=p_name
$p1$;


-- Advisory eligibility only. A pre-write check is not atomic with an Edge
-- operation; the production consumer must fence and recheck around flush/ACK.
CREATE FUNCTION qdrant_internal.p1_event_verdict(p_event_id bigint) RETURNS jsonb
LANGUAGE sql STABLE
SET search_path = pg_catalog, qdrant_internal, pg_temp
AS $p1$
 SELECT jsonb_build_object(
   'event_id',e.event_id,
   'current', s.point_id=e.point_id AND s.incarnation=e.incarnation
     AND s.revision=e.revision AND s.fingerprint IS NOT DISTINCT FROM e.fingerprint
     AND s.tombstone=(e.operation='delete'),
   'point_id',e.point_id,'incarnation',e.incarnation,'revision',e.revision,
   'operation',e.operation,'engine_applied',false,'durable_ack',false)
 FROM qdrant_internal.p1_outbox e
 JOIN qdrant_internal.p1_source_state s
   ON s.index_name=e.index_name AND s.tagged_key=e.tagged_key
 WHERE e.event_id=p_event_id
$p1$;

-- REVOKE does not revoke execution by installed triggers: privileges on the
-- trigger function are checked when the trigger is created, not at DML time.
REVOKE ALL ON ALL TABLES IN SCHEMA qdrant_internal FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA qdrant_internal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA qdrant_internal FROM PUBLIC;

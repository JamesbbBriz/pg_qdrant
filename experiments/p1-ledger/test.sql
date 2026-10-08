CREATE TABLE public.p1_chunks (id bigint PRIMARY KEY, body text NOT NULL);
INSERT INTO public.p1_chunks VALUES (1,'before'),(2,'delete me');
DO $test$
DECLARE j jsonb;
BEGIN
 j := qdrant_internal.p1_register('kb','public.p1_chunks'::regclass,'id','body');
 IF (j->>'backfilled_rows')::int <> 2 OR
   (SELECT count(*) FROM qdrant_internal.p1_outbox WHERE origin='backfill') <> 2
 THEN RAISE EXCEPTION 'backfill not atomic'; END IF;
END $test$;
INSERT INTO public.p1_chunks VALUES (3,'third');
UPDATE public.p1_chunks SET body='after' WHERE id=1;
DELETE FROM public.p1_chunks WHERE id=2;
DO $test$
BEGIN
 IF (SELECT count(*) FROM qdrant_internal.p1_outbox) <> 5
 THEN RAISE EXCEPTION 'missing DML events'; END IF;
 IF (SELECT revision FROM qdrant_internal.p1_source_state
    WHERE tagged_key='{"type":"bigint","value":"1"}'::jsonb) <> 2
 THEN RAISE EXCEPTION 'revision mismatch'; END IF;
 IF NOT (SELECT tombstone FROM qdrant_internal.p1_source_state
    WHERE tagged_key='{"type":"bigint","value":"2"}'::jsonb)
 THEN RAISE EXCEPTION 'delete tombstone missing'; END IF;
END $test$;

-- Earlier events are stale after a newer revision; latest event is eligible.
DO $test$
DECLARE old_state jsonb; new_state jsonb;
BEGIN
 SELECT qdrant_internal.p1_event_verdict(min(event_id))
 INTO old_state FROM qdrant_internal.p1_outbox
 WHERE tagged_key='{"type":"bigint","value":"1"}'::jsonb;
 SELECT qdrant_internal.p1_event_verdict(max(event_id))
 INTO new_state FROM qdrant_internal.p1_outbox
 WHERE tagged_key='{"type":"bigint","value":"1"}'::jsonb;
 IF old_state->>'current'<>'false' OR new_state->>'current'<>'true'
    OR new_state->>'durable_ack'<>'false'
 THEN RAISE EXCEPTION 'event-version fencing verdict incorrect'; END IF;
 IF (SELECT fingerprint FROM qdrant_internal.p1_source_state
      WHERE tagged_key='{"type":"bigint","value":"1"}'::jsonb)
    <> encode(sha256(convert_to('after','UTF8')),'hex')
 THEN RAISE EXCEPTION 'UTF8 fingerprint mismatch'; END IF;
END $test$;

BEGIN;
 INSERT INTO public.p1_chunks VALUES (4,'will rollback');
ROLLBACK;
BEGIN;
 SAVEPOINT save;
 INSERT INTO public.p1_chunks VALUES (5,'savepoint rollback');
 ROLLBACK TO save;
 INSERT INTO public.p1_chunks VALUES (6,'committed');
COMMIT;
DO $test$
BEGIN
 IF (SELECT count(*) FROM qdrant_internal.p1_outbox) <> 6
 OR EXISTS (SELECT 1 FROM qdrant_internal.p1_source_state
            WHERE tagged_key IN ('{"type":"bigint","value":"4"}'::jsonb,
                                 '{"type":"bigint","value":"5"}'::jsonb))
 THEN RAISE EXCEPTION 'aborted event or state leaked'; END IF;
END $test$;
INSERT INTO public.p1_chunks VALUES (2,'new incarnation');
DO $test$
DECLARE npoint bigint; ninc bigint; nrev bigint[];
BEGIN
 SELECT count(DISTINCT point_id),count(DISTINCT incarnation),array_agg(revision ORDER BY revision)
 INTO npoint,ninc,nrev FROM qdrant_internal.p1_outbox
 WHERE tagged_key='{"type":"bigint","value":"2"}'::jsonb;
 IF npoint <> 2 OR ninc <> 2 OR nrev <> ARRAY[1::bigint,2::bigint,3::bigint]
 THEN RAISE EXCEPTION 'point/incarnation not renewed on key reuse'; END IF;
END $test$;
BEGIN;
 INSERT INTO public.p1_chunks VALUES (10,'first ticket event');
 DO $test$
 DECLARE v uuid;
 BEGIN
   v := qdrant_internal.p1_track('kb');
   IF (SELECT sealed_events FROM qdrant_internal.p1_tickets WHERE ticket_id=v) <> 1
   THEN RAISE EXCEPTION 'ticket wrong membership'; END IF;
   BEGIN
     PERFORM qdrant_internal.p1_ticket_status(v);
     RAISE EXCEPTION 'uncommitted wait wrongly allowed';
   EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
   END;
 END $test$;
 INSERT INTO public.p1_chunks VALUES (11,'later event');
 SELECT qdrant_internal.p1_track('kb') AS second_ticket \gset
COMMIT;
DO $test$
DECLARE item record; state jsonb;
BEGIN
 IF (SELECT count(*) FROM qdrant_internal.p1_tickets) <> 2
 THEN RAISE EXCEPTION 'wrong ticket count'; END IF;
 FOR item IN SELECT ticket_id,sealed_events FROM qdrant_internal.p1_tickets LOOP
  state := qdrant_internal.p1_ticket_status(item.ticket_id);
  IF item.sealed_events<>1 OR (state->>'pending_events')::int<>1
    OR state->>'committed'<>'true' OR state->>'durable'<>'false'
    OR state->>'engine_acknowledgement_supported'<>'false'
  THEN RAISE EXCEPTION 'false ticket durability or membership: %',state; END IF;
 END LOOP;
END $test$;
BEGIN;
 INSERT INTO public.p1_chunks VALUES (12,'rolled back ticket');
 SELECT qdrant_internal.p1_track('kb') AS rolled_ticket \gset
ROLLBACK;
SELECT 1/(CASE WHEN EXISTS(
 SELECT 1 FROM qdrant_internal.p1_tickets WHERE ticket_id=:'rolled_ticket'::uuid
) THEN 0 ELSE 1 END) AS rollback_ticket_clean;
CREATE ROLE p1_writer LOGIN;
GRANT USAGE ON SCHEMA public TO p1_writer;
GRANT INSERT,UPDATE,DELETE,SELECT ON public.p1_chunks TO p1_writer;
SET ROLE p1_writer;
INSERT INTO public.p1_chunks VALUES (20,'normal user DML');
RESET ROLE;
DO $test$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM qdrant_internal.p1_source_state
   WHERE tagged_key='{"type":"bigint","value":"20"}'::jsonb AND NOT tombstone)
 THEN RAISE EXCEPTION 'non-superuser row was not captured'; END IF;
 IF has_table_privilege('p1_writer','qdrant_internal.p1_outbox','SELECT')
 THEN RAISE EXCEPTION 'private outbox readable'; END IF;
END $test$;
TRUNCATE public.p1_chunks;
DO $test$
BEGIN
 IF EXISTS(SELECT 1 FROM qdrant_internal.p1_source_state WHERE NOT tombstone)
 THEN RAISE EXCEPTION 'truncate failed to create tombstones'; END IF;
 IF (SELECT count(*) FROM qdrant_internal.p1_outbox WHERE origin='truncate')<>7
 THEN RAISE EXCEPTION 'truncate missed live source rows'; END IF;
END $test$;
CREATE TABLE public.p1_restricted (id uuid PRIMARY KEY, body text NOT NULL);
ALTER TABLE public.p1_restricted ENABLE ROW LEVEL SECURITY;
DO $test$
BEGIN
 BEGIN
  PERFORM qdrant_internal.p1_register('no_rls','public.p1_restricted'::regclass,'id','body');
  RAISE EXCEPTION 'RLS table incorrectly supported';
 EXCEPTION WHEN SQLSTATE '0A000' THEN NULL;
 END;
 IF EXISTS(SELECT 1 FROM qdrant_internal.p1_indexes WHERE index_name='no_rls')
 THEN RAISE EXCEPTION 'rejected registration leaked'; END IF;
END $test$;

-- Both other initially supported source PK types register and capture.
CREATE TABLE public.p1_uuid (id uuid PRIMARY KEY, body text NOT NULL);
SELECT qdrant_internal.p1_register('uuid_idx','public.p1_uuid'::regclass,'id','body');
INSERT INTO public.p1_uuid VALUES ('11111111-2222-4333-8444-555555555555','uuid indexed text');
CREATE TABLE public.p1_text (id text COLLATE "C" PRIMARY KEY, body text NOT NULL);
SELECT qdrant_internal.p1_register('text_idx','public.p1_text'::regclass,'id','body');
INSERT INTO public.p1_text VALUES ('你好-key','chinese and ascii');
DO $test$
BEGIN
 IF (SELECT count(*) FROM qdrant_internal.p1_outbox
     WHERE index_name='uuid_idx' AND tagged_key->>'type'='uuid')<>1
    OR (SELECT count(*) FROM qdrant_internal.p1_outbox
     WHERE index_name='text_idx' AND tagged_key->>'value'='你好-key')<>1
 THEN RAISE EXCEPTION 'uuid or text key capture unsupported'; END IF;
END $test$;

-- Capture-integrity status fails closed if a registered trigger is disabled.
ALTER TABLE public.p1_text DISABLE TRIGGER qdrant_p1_rows;
DO $test$
DECLARE result jsonb;
BEGIN
 result:=qdrant_internal.p1_index_status('text_idx');
 IF result->>'capture_state'<>'degraded'
 THEN RAISE EXCEPTION 'disabled trigger silently marked ready'; END IF;
END $test$;

-- An intentionally small, locked backfill budget refuses oversized sources.
CREATE TABLE public.p1_large (id bigint PRIMARY KEY, body text NOT NULL);
INSERT INTO public.p1_large
 SELECT x,'fixture' FROM generate_series(1,1001) AS x;
DO $test$
BEGIN
 BEGIN
  PERFORM qdrant_internal.p1_register('too_many','public.p1_large'::regclass,'id','body');
  RAISE EXCEPTION 'oversized locked backfill incorrectly allowed';
 EXCEPTION WHEN SQLSTATE '54000' THEN NULL;
 END;
 IF EXISTS(SELECT 1 FROM qdrant_internal.p1_indexes WHERE index_name='too_many')
 THEN RAISE EXCEPTION 'failed backfill left registration metadata'; END IF;
END $test$;


-- COPY must traverse ordinary row triggers; wide unindexed columns must not
-- be serialized into the search projection.
CREATE TABLE public.p1_copy (id bigint PRIMARY KEY, body text NOT NULL, unrelated bytea);
SELECT qdrant_internal.p1_register('copy_idx','public.p1_copy'::regclass,'id','body');
COPY public.p1_copy (id,body) FROM STDIN;
901	copy source a
902	复制 row b
\.
INSERT INTO public.p1_copy VALUES (903,'narrow searchable body',repeat('Z', 1048576)::bytea);
UPDATE public.p1_copy SET body=body||' updated' WHERE id IN (901,902);
DELETE FROM public.p1_copy WHERE id=901;
DO $test$
BEGIN
 IF (SELECT count(*) FROM qdrant_internal.p1_outbox WHERE index_name='copy_idx')<>6
 THEN RAISE EXCEPTION 'COPY/multirow UPDATE/DELETE missed events'; END IF;
 IF (SELECT revision FROM qdrant_internal.p1_source_state
     WHERE index_name='copy_idx' AND tagged_key='{"type":"bigint","value":"902"}'::jsonb)<>2
 THEN RAISE EXCEPTION 'multirow revision invalid'; END IF;
 IF (SELECT projection->>'body' FROM qdrant_internal.p1_outbox
     WHERE index_name='copy_idx' AND tagged_key='{"type":"bigint","value":"903"}'::jsonb)<> 'narrow searchable body'
 THEN RAISE EXCEPTION 'wide unrelated column entered projection'; END IF;
END $test$;
DO $test$
BEGIN
 BEGIN
   INSERT INTO public.p1_copy VALUES (904, repeat('Y',65537),NULL);
   RAISE EXCEPTION 'over-budget body not rejected';
 EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
 END;
 IF EXISTS(SELECT 1 FROM public.p1_copy WHERE id=904)
    OR EXISTS(SELECT 1 FROM qdrant_internal.p1_outbox
       WHERE index_name='copy_idx' AND tagged_key='{"type":"bigint","value":"904"}'::jsonb)
 THEN RAISE EXCEPTION 'trigger exception failed to roll back source/outbox together'; END IF;
END $test$;


-- Renaming an indexed column invalidates the registered capture contract.
ALTER TABLE public.p1_copy RENAME COLUMN body TO renamed_body;
DO $test$
DECLARE j jsonb;
BEGIN
 j:=qdrant_internal.p1_index_status('copy_idx');
 IF j->>'capture_state'<>'degraded'
 THEN RAISE EXCEPTION 'renamed source field not reported degraded'; END IF;
END $test$;
DROP TABLE public.p1_chunks;
DO $test$
DECLARE j jsonb;
BEGIN
 j := qdrant_internal.p1_index_status('kb');
 IF j->>'capture_state'<>'degraded' OR j->>'source_exists'<>'false'
 THEN RAISE EXCEPTION 'dropped source incorrectly ready'; END IF;
END $test$;

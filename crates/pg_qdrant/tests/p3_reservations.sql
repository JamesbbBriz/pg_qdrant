-- Transactional task admission/cancellation and no-false-readiness contract.
-- Actual committed generation builds are tested in verify_generations.py.
\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS pg_qdrant;
DROP TABLE IF EXISTS public.pgq_p3_fixture CASCADE;
CREATE TABLE public.pgq_p3_fixture (id bigint PRIMARY KEY, body text NOT NULL);
INSERT INTO public.pgq_p3_fixture VALUES (1,'generation reservation');
SELECT qdrant.create_index('p3_fixture','public.pgq_p3_fixture'::regclass,
    'id'::name,'{"text":{"fields":["body"]}}');
DO $t$
DECLARE
 r1 jsonb;
 r2 jsonb;
 state jsonb;
 deadline timestamptz:=clock_timestamp()+interval '20 seconds';
BEGIN
 WHILE NOT coalesce((qdrant.index_status('p3_fixture')->>'engine_index_ready')::boolean,false) LOOP
   IF clock_timestamp()>deadline THEN RAISE EXCEPTION 'fixture did not become ready'; END IF;
   PERFORM pg_sleep(0.01);
 END LOOP;
 r1 := qdrant.rebuild_index('p3_fixture');
 BEGIN
   PERFORM qdrant.rebuild_index('p3_fixture');
   RAISE EXCEPTION 'duplicate live task admitted';
 EXCEPTION WHEN object_in_use THEN NULL;
 END;
 IF NOT qdrant.cancel_rebuild('p3_fixture',2) OR qdrant.cancel_rebuild('p3_fixture',2) THEN
   RAISE EXCEPTION 'cancellation must change only an active task';
 END IF;
 r2 := qdrant.rebuild_index('p3_fixture');
 IF r1->>'state'<>'building' OR r2->>'state'<>'building'
    OR (r1->>'engine_build_started')::boolean OR (r2->>'engine_build_started')::boolean
    OR (r1->>'search_ready')::boolean OR (r2->>'search_ready')::boolean THEN
   RAISE EXCEPTION 'uncommitted task claimed native execution or readiness';
 END IF;
 IF (r1->>'reserved_generation')::bigint <> 2 OR
    (r2->>'reserved_generation')::bigint <> 3 THEN
   RAISE EXCEPTION 'generation reservation sequence invalid';
 END IF;
 IF NOT qdrant.cancel_task((r2->>'task_id')::uuid) THEN RAISE EXCEPTION 'second task cancellation failed'; END IF;
 state := qdrant.rebuild_status('p3_fixture');
 IF (state->>'active_generation')::bigint <> 1 OR
    (state->>'switch_available')::boolean THEN
   RAISE EXCEPTION 'uncommitted task switched the working generation';
 END IF;
 IF (qdrant.operation_limits()->>'native_rss_limit_enforced')::boolean THEN
   RAISE EXCEPTION 'preflight claimed to control engine RSS';
 END IF;
END
$t$;
SELECT qdrant.drop_index('p3_fixture');
DROP TABLE public.pgq_p3_fixture;
SELECT 'P3 task admission/cancellation and bounds assertions done' AS result;

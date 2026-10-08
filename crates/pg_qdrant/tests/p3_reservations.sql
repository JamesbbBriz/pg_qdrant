-- P3 generation reservation and no-false-readiness contract.
-- Disposable PG17 database only; NOT an Edge generation build test.
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
BEGIN
 r1 := qdrant.rebuild_index('p3_fixture');
 r2 := qdrant.rebuild_index('p3_fixture');
 IF (r1->>'reserved_generation')::bigint <> 2 OR
    (r2->>'reserved_generation')::bigint <> 3 THEN
   RAISE EXCEPTION 'generation reservation sequence invalid';
 END IF;
 IF NOT qdrant.cancel_rebuild('p3_fixture',2) OR
    qdrant.cancel_rebuild('p3_fixture',2) THEN
   RAISE EXCEPTION 'cancellation must change only an active reservation';
 END IF;
 state := qdrant.rebuild_status('p3_fixture');
 IF (state->>'active_generation')::bigint <> 1 OR
    (state->>'active_generation_search_ready')::boolean OR
    (state->>'switch_available')::boolean THEN
   RAISE EXCEPTION 'unbuilt generation switched or reported ready';
 END IF;
 IF (qdrant.operation_limits()->>'native_rss_limit_enforced')::boolean THEN
   RAISE EXCEPTION 'preflight claimed to control engine RSS';
 END IF;
END
$t$;
SELECT qdrant.drop_index('p3_fixture');
DROP TABLE public.pgq_p3_fixture;
SELECT 'P3 reservation and bounds assertions done, no native build' AS result;
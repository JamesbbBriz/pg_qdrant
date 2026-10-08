-- Disposable PostgreSQL 17 preflight test. No search hits are expected.
\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS pg_qdrant;
DROP TABLE IF EXISTS public.pgq_p2_fixture CASCADE;
CREATE TABLE public.pgq_p2_fixture (id bigint PRIMARY KEY, body text NOT NULL);
INSERT INTO public.pgq_p2_fixture VALUES (1,'index must not claim readiness');
SELECT qdrant.create_index('p2_fixture','public.pgq_p2_fixture'::regclass,
    'id'::name,'{"text":{"fields":["body"]}}');
DO $t$
DECLARE plan jsonb;
BEGIN
 plan := qdrant.explain_search('p2_fixture','recovery');
 IF plan->>'permission_preflight' <> 'passed_for_registered_source'
    OR (plan->>'search_executable')::boolean
    OR (plan->>'edge_generation_ready')::boolean THEN
  RAISE EXCEPTION 'admission made a false search-readiness claim';
 END IF;
 BEGIN
  PERFORM qdrant.explain_search('p2_fixture','recovery','text',10,
    '{}'::jsonb,'{"unknown_option":1}'::jsonb);
  RAISE EXCEPTION 'unknown option accepted';
 EXCEPTION WHEN invalid_parameter_value THEN NULL;
 END;
 BEGIN
  PERFORM qdrant.explain_search('p2_fixture','recovery','text',1000);
  RAISE EXCEPTION 'unbounded candidate accepted';
 EXCEPTION WHEN invalid_parameter_value THEN NULL;
 END;
 BEGIN
  PERFORM qdrant.explain_search('p2_fixture','recovery','hybrid');
  RAISE EXCEPTION 'unimplemented mode accepted';
 EXCEPTION WHEN feature_not_supported THEN NULL;
 END;
 BEGIN
  PERFORM qdrant.search('p2_fixture','recovery');
  RAISE EXCEPTION 'native search returned before an engine generation existed';
 EXCEPTION WHEN feature_not_supported THEN NULL;
 END;
END
$t$;
ALTER TABLE public.pgq_p2_fixture ENABLE ROW LEVEL SECURITY;
DO $t$
BEGIN
 BEGIN
  PERFORM qdrant.explain_search('p2_fixture','recovery');
  RAISE EXCEPTION 'RLS unexpectedly accepted';
 EXCEPTION WHEN object_not_in_prerequisite_state THEN NULL;
 END;
END
$t$;
ALTER TABLE public.pgq_p2_fixture DISABLE ROW LEVEL SECURITY;
SELECT qdrant.drop_index('p2_fixture');
DROP TABLE public.pgq_p2_fixture;
SELECT 'P2 admission-only SQL fixture completed, native search still absent' AS result;
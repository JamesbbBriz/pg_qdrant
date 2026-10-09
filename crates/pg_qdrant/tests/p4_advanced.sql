-- P4 advanced request SHAPE tests; not an engine capability test.
\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS pg_qdrant;
DO $t$
DECLARE
 accepted jsonb;
BEGIN
 accepted:=qdrant.validate_advanced_request(
   '{"kind":"recommend","positive":[1],"negative":[2],"limit":10}'::jsonb);
 IF accepted->>'family' <> 'recommend' OR
    (accepted->>'search_executable')::boolean OR
    (accepted->>'source_authorization_checked')::boolean THEN
  RAISE EXCEPTION 'shape admission made an execution/authorization claim';
 END IF;
 accepted:=qdrant.validate_advanced_request(
   '{"kind":"formula","expression":{"op":"add","args":[{"op":"field","name":"popularity"},{"op":"constant","value":2}]},"limit":5}'::jsonb);
 IF accepted->>'family' <> 'formula' THEN
  RAISE EXCEPTION 'validated formula family missing';
 END IF;
 BEGIN
   PERFORM qdrant.validate_advanced_request(
    '{"kind":"mmr","candidates":[1,2],"lambda":1.5,"limit":2}'::jsonb);
   RAISE EXCEPTION 'invalid MMR lambda accepted';
 EXCEPTION WHEN invalid_parameter_value THEN NULL;
 END;
 BEGIN
   PERFORM qdrant.validate_advanced_request(
    '{"kind":"facet","field":"unsafe.sql","limit":10}'::jsonb);
   RAISE EXCEPTION 'unsafe facet field accepted';
 EXCEPTION WHEN invalid_parameter_value THEN NULL;
 END;
 BEGIN
   PERFORM qdrant.validate_advanced_request(
    '{"kind":"recommend","positive":[1],"negative":[1],"limit":5}'::jsonb);
   RAISE EXCEPTION 'overlapping examples accepted';
 EXCEPTION WHEN invalid_parameter_value THEN NULL;
 END;
END
$t$;
SELECT 'P4 shape tests complete; this fixture does not execute advanced Edge queries' AS result;

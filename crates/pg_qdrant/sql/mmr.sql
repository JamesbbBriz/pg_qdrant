CREATE FUNCTION qdrant_internal.admit_mmr(p_name text,p_queries jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE rep text; input jsonb; admitted jsonb; versions jsonb;
BEGIN
 IF jsonb_typeof(p_queries) IS DISTINCT FROM 'object' THEN
   RAISE EXCEPTION 'One named MMR slot required' USING ERRCODE='22023'; END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(p_queries))<>1 THEN
   RAISE EXCEPTION 'One named MMR slot required' USING ERRCODE='22023'; END IF;
 SELECT key,value INTO rep,input FROM jsonb_each(p_queries);
 IF jsonb_typeof(input) IS DISTINCT FROM 'object' THEN
   RAISE EXCEPTION 'MMR model, source target and lambda required' USING ERRCODE='22023'; END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(input))<>5
   OR NOT input ?& ARRAY['model_id','model_version','strategy','target','lambda']
   OR jsonb_typeof(input->'model_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(input->'model_version') IS DISTINCT FROM 'string'
   OR input->>'strategy' IS DISTINCT FROM 'mmr'
   OR jsonb_typeof(input->'target') IS DISTINCT FROM 'string'
   OR jsonb_typeof(input->'lambda') IS DISTINCT FROM 'number' THEN
   RAISE EXCEPTION 'MMR takes a current source key and explicit lambda only' USING ERRCODE='22023'; END IF;
 IF (input->>'lambda')::numeric NOT BETWEEN 0 AND 1 THEN
   RAISE EXCEPTION 'MMR lambda must be in [0,1]' USING ERRCODE='22023'; END IF;
 admitted:=qdrant_internal.admit_recommendation(p_name,jsonb_build_object(rep,jsonb_build_object(
   'model_id',input->'model_id','model_version',input->'model_version',
   'positive',jsonb_build_array(input->'target'),'negative','[]'::jsonb,'strategy','sum_scores')));
 versions:=jsonb_build_array((admitted->'seed_versions'->0)-'side'||jsonb_build_object('role','target'));
 RETURN jsonb_build_object('query',jsonb_build_object('representation',rep,
   'model_id',input->'model_id','model_version',input->'model_version',
   'target',admitted #> '{query,positive,0}','lambda',input->'lambda',
   'exclude_id',admitted #> '{query,exclude_ids,0}'),
   'seed_versions',versions,'seed_digest',encode(sha256(convert_to(
     jsonb_build_object('versions',versions,'lambda',input->'lambda')::text,'UTF8')),'hex'));
END $$;
REVOKE ALL ON FUNCTION qdrant_internal.admit_mmr(text,jsonb) FROM PUBLIC;

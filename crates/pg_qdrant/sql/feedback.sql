CREATE FUNCTION qdrant_internal.admit_feedback(p_name text,p_queries jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE rep text; input jsonb; item jsonb; coefficients jsonb; admitted jsonb; field text;
 keys jsonb; roles jsonb; versions jsonb:='[]'; feedback jsonb:='[]'; native_query jsonb; n integer:=0;
BEGIN
 IF jsonb_typeof(p_queries) IS DISTINCT FROM 'object' THEN
   RAISE EXCEPTION 'One named feedback slot required' USING ERRCODE='22023'; END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(p_queries))<>1 THEN
   RAISE EXCEPTION 'One named feedback slot required' USING ERRCODE='22023'; END IF;
 SELECT key,value INTO rep,input FROM jsonb_each(p_queries);
 IF jsonb_typeof(input) IS DISTINCT FROM 'object' THEN
   RAISE EXCEPTION 'Feedback model, target, scores and coefficients required' USING ERRCODE='22023'; END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(input))<>6
    OR NOT input ?& ARRAY['model_id','model_version','strategy','target','feedback','coefficients']
    OR jsonb_typeof(input->'model_id') IS DISTINCT FROM 'string'
    OR jsonb_typeof(input->'model_version') IS DISTINCT FROM 'string'
    OR input->>'strategy' IS DISTINCT FROM 'feedback'
    OR jsonb_typeof(input->'target') IS DISTINCT FROM 'string'
    OR jsonb_typeof(input->'feedback') IS DISTINCT FROM 'array'
    OR jsonb_typeof(input->'coefficients') IS DISTINCT FROM 'object' THEN
   RAISE EXCEPTION 'Feedback takes current source keys, scores and explicit coefficients only' USING ERRCODE='22023'; END IF;
 coefficients:=input->'coefficients';
 IF (SELECT count(*) FROM jsonb_object_keys(coefficients))<>3 OR NOT coefficients ?& ARRAY['a','b','c'] THEN
   RAISE EXCEPTION 'Feedback requires explicit a, b and c coefficients' USING ERRCODE='22023'; END IF;
 FOREACH field IN ARRAY ARRAY['a','b','c'] LOOP
   IF jsonb_typeof(coefficients->field) IS DISTINCT FROM 'number' THEN
     RAISE EXCEPTION 'Feedback coefficients must be bounded numbers' USING ERRCODE='22023'; END IF;
   -- Compare as numeric before converting to avoid overflow from untrusted input.
   IF (coefficients->>field)::numeric NOT BETWEEN -16 AND 16
      OR (field='b' AND (coefficients->>field)::numeric NOT BETWEEN 0 AND 4) THEN
     RAISE EXCEPTION 'Feedback a/c must be in [-16,16] and b in [0,4]' USING ERRCODE='22023'; END IF;
 END LOOP;
 IF jsonb_array_length(input->'feedback') NOT BETWEEN 1 AND 31 THEN
   RAISE EXCEPTION 'Feedback requires 1 to 31 examples and at most 32 distinct source keys' USING ERRCODE='22023'; END IF;
 keys:=jsonb_build_array(input->'target');roles:=jsonb_build_array(jsonb_build_object('role','target'));
 FOR item IN SELECT x.value FROM jsonb_array_elements(input->'feedback') x LOOP
   IF jsonb_typeof(item) IS DISTINCT FROM 'object' THEN
     RAISE EXCEPTION 'Feedback item requires a source key and score' USING ERRCODE='22023'; END IF;
   IF (SELECT count(*) FROM jsonb_object_keys(item))<>2 OR NOT item ?& ARRAY['key','score']
      OR jsonb_typeof(item->'key') IS DISTINCT FROM 'string' OR jsonb_typeof(item->'score') IS DISTINCT FROM 'number' THEN
     RAISE EXCEPTION 'Feedback item takes a source key and bounded score only' USING ERRCODE='22023'; END IF;
   IF (item->>'score')::numeric NOT BETWEEN -16 AND 16 THEN
     RAISE EXCEPTION 'Feedback scores must be in [-16,16]' USING ERRCODE='22023'; END IF;
   keys:=keys||jsonb_build_array(item->'key');
   roles:=roles||jsonb_build_array(jsonb_build_object('role','feedback','ordinal',n,'score',item->'score'));n:=n+1;
 END LOOP;
 admitted:=qdrant_internal.admit_recommendation(p_name,jsonb_build_object(rep,jsonb_build_object(
   'model_id',input->'model_id','model_version',input->'model_version',
   'positive',keys,'negative','[]'::jsonb,'strategy','sum_scores')));
 FOR n IN 0..jsonb_array_length(keys)-1 LOOP
   versions:=versions||jsonb_build_array(((admitted->'seed_versions'->n)-'side')||(roles->n));
   IF n>0 THEN feedback:=feedback||jsonb_build_array(jsonb_build_object(
     'vector',admitted->'query'->'positive'->n,'score',roles->n->'score')); END IF;
 END LOOP;
 native_query:=jsonb_build_object('representation',rep,'model_id',input->'model_id','model_version',input->'model_version',
   'target',admitted #> '{query,positive,0}','feedback',feedback,'coefficients',coefficients,
   'exclude_ids',admitted #> '{query,exclude_ids}');
 IF octet_length(native_query::text)>65536 THEN
   RAISE EXCEPTION 'Resolved feedback examples exceed 64 KiB' USING ERRCODE='54000'; END IF;
 RETURN jsonb_build_object('query',native_query,'seed_versions',versions,
   'seed_digest',encode(sha256(convert_to(jsonb_build_object('versions',versions,'coefficients',coefficients)::text,'UTF8')),'hex'));
END $$;
REVOKE ALL ON FUNCTION qdrant_internal.admit_feedback(text,jsonb) FROM PUBLIC;

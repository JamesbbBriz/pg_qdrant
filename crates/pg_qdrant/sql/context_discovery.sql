CREATE FUNCTION qdrant_internal.admit_discovery(p_name text,p_queries jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE rep text; input jsonb; strategy text; pair jsonb; admitted jsonb; n integer; offset_value integer;
 keys jsonb:='[]'; roles jsonb:='[]'; versions jsonb:='[]'; pairs jsonb:='[]'; target jsonb; native_query jsonb;
BEGIN
 IF jsonb_typeof(p_queries) IS DISTINCT FROM 'object' THEN
   RAISE EXCEPTION 'One named discovery slot required' USING ERRCODE='22023'; END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(p_queries))<>1 THEN
   RAISE EXCEPTION 'One named discovery slot required' USING ERRCODE='22023'; END IF;
 SELECT key,value INTO rep,input FROM jsonb_each(p_queries);
 IF jsonb_typeof(input) IS DISTINCT FROM 'object' THEN
   RAISE EXCEPTION 'Discovery requires a model and context object' USING ERRCODE='22023'; END IF;
 strategy:=input->>'strategy';
 IF jsonb_typeof(input->'strategy') IS DISTINCT FROM 'string' OR strategy NOT IN ('discover','context')
    OR jsonb_typeof(input->'context') IS DISTINCT FROM 'array'
    OR jsonb_typeof(input->'model_id') IS DISTINCT FROM 'string'
    OR jsonb_typeof(input->'model_version') IS DISTINCT FROM 'string' THEN
   RAISE EXCEPTION 'Discovery strategy, model and context pairs required' USING ERRCODE='22023'; END IF;
 IF strategy='discover' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(input))<>5 OR NOT input ?& ARRAY['strategy','model_id','model_version','target','context']
      OR jsonb_typeof(input->'target') IS DISTINCT FROM 'string' THEN
     RAISE EXCEPTION 'Discover requires one source target and context pairs' USING ERRCODE='22023'; END IF;
   keys:=jsonb_build_array(input->'target'); roles:=jsonb_build_array(jsonb_build_object('role','target'));
 ELSE
   IF (SELECT count(*) FROM jsonb_object_keys(input))<>4 OR NOT input ?& ARRAY['strategy','model_id','model_version','context'] THEN
     RAISE EXCEPTION 'Context-only search takes no target' USING ERRCODE='22023'; END IF;
 END IF;
 IF jsonb_array_length(input->'context') NOT BETWEEN 1 AND 16
    OR jsonb_array_length(input->'context')*2+jsonb_array_length(keys)>32 THEN
   RAISE EXCEPTION 'Nonempty context and at most 32 distinct source examples required' USING ERRCODE='22023'; END IF;
 n:=0;
 FOR pair IN SELECT value FROM jsonb_array_elements(input->'context') LOOP
   IF jsonb_typeof(pair) IS DISTINCT FROM 'object' THEN
     RAISE EXCEPTION 'Context pair requires positive and negative source keys' USING ERRCODE='22023'; END IF;
   IF (SELECT count(*) FROM jsonb_object_keys(pair))<>2 OR NOT pair ?& ARRAY['positive','negative']
      OR jsonb_typeof(pair->'positive') IS DISTINCT FROM 'string' OR jsonb_typeof(pair->'negative') IS DISTINCT FROM 'string' THEN
     RAISE EXCEPTION 'Context pair requires positive and negative source keys only' USING ERRCODE='22023'; END IF;
   keys:=keys||jsonb_build_array(pair->'positive',pair->'negative');
   roles:=roles||jsonb_build_array(jsonb_build_object('role','positive','pair',n),jsonb_build_object('role','negative','pair',n));
   n:=n+1;
 END LOOP;
 -- Resolve only current authorized source identities through the established
 -- complete-slot model admission. No caller vector or native point ID is used.
 admitted:=qdrant_internal.admit_recommendation(p_name,jsonb_build_object(rep,jsonb_build_object(
   'model_id',input->'model_id','model_version',input->'model_version',
   'positive',keys,'negative','[]'::jsonb,'strategy','sum_scores')));
 FOR n IN 0..jsonb_array_length(keys)-1 LOOP
   versions:=versions||jsonb_build_array(((admitted->'seed_versions'->n)-'side')||(roles->n));
 END LOOP;
 offset_value:=0;
 IF strategy='discover' THEN target:=admitted #> '{query,positive,0}'; offset_value:=1; END IF;
 FOR n IN 0..jsonb_array_length(input->'context')-1 LOOP
   pairs:=pairs||jsonb_build_array(jsonb_build_object(
     'positive',admitted->'query'->'positive'->(offset_value+2*n),
     'negative',admitted->'query'->'positive'->(offset_value+2*n+1)));
 END LOOP;
 native_query:=jsonb_build_object('representation',rep,
   'model_id',input->'model_id','model_version',input->'model_version','strategy',strategy,
   'target',target,'context',pairs,'exclude_ids',admitted #> '{query,exclude_ids}');
 IF octet_length(native_query::text)>65536 THEN
   RAISE EXCEPTION 'Resolved discovery examples exceed 64 KiB' USING ERRCODE='54000'; END IF;
 RETURN jsonb_build_object('query',native_query,
   'seed_versions',versions,'seed_digest',encode(sha256(convert_to(versions::text,'UTF8')),'hex'));
END $$;
REVOKE ALL ON FUNCTION qdrant_internal.admit_discovery(text,jsonb) FROM PUBLIC;

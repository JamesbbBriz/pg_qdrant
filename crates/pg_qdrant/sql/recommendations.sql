CREATE FUNCTION qdrant_internal.admit_recommendation(p_name text,p_queries jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE rep text; input jsonb; c jsonb; i qdrant_internal.index_catalog%ROWTYPE;
 side text; example jsonb; seed jsonb; normalized jsonb; field text;
 positives jsonb:='[]'; negatives jsonb:='[]'; versions jsonb:='[]'; ids jsonb:='[]';
BEGIN
 IF jsonb_typeof(p_queries) IS DISTINCT FROM 'object' OR (SELECT count(*) FROM jsonb_object_keys(p_queries))<>1 THEN
   RAISE EXCEPTION 'One named recommendation slot required' USING ERRCODE='22023'; END IF;
 SELECT key,value INTO rep,input FROM jsonb_each(p_queries);
 IF jsonb_typeof(input) IS DISTINCT FROM 'object' OR NOT input ?& ARRAY['model_id','model_version','positive','negative','strategy']
    OR (SELECT count(*) FROM jsonb_object_keys(input))<>5
    OR jsonb_typeof(input->'model_id') IS DISTINCT FROM 'string' OR jsonb_typeof(input->'model_version') IS DISTINCT FROM 'string'
    OR jsonb_typeof(input->'positive') IS DISTINCT FROM 'array' OR jsonb_typeof(input->'negative') IS DISTINCT FROM 'array'
    OR jsonb_typeof(input->'strategy') IS DISTINCT FROM 'string' OR input->>'strategy' NOT IN ('best_score','sum_scores') THEN
   RAISE EXCEPTION 'Recommendation model, positive/negative key arrays and fixed strategy required' USING ERRCODE='22023'; END IF;
 IF jsonb_array_length(input->'positive')<1 OR jsonb_array_length(input->'positive')+jsonb_array_length(input->'negative')>32
    OR EXISTS(SELECT 1 FROM jsonb_array_elements((input->'positive')||(input->'negative')) x WHERE jsonb_typeof(x)<>'string')
    OR (SELECT count(*) FROM jsonb_array_elements((input->'positive')||(input->'negative'))) <>
       (SELECT count(DISTINCT value) FROM jsonb_array_elements((input->'positive')||(input->'negative'))) THEN
   RAISE EXCEPTION 'One positive and at most 32 distinct source keys required' USING ERRCODE='22023'; END IF;
 PERFORM qdrant_internal.require_index(p_name,false);
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_name;
 IF EXISTS(SELECT 1 FROM pg_class WHERE oid=i.source_oid AND (relrowsecurity OR relforcerowsecurity)) THEN
   RAISE EXCEPTION 'RLS recommendation sources are unsupported' USING ERRCODE='0A000'; END IF;
 SELECT contract INTO c FROM qdrant_internal.representation_catalog WHERE index_name=p_name AND name=rep;
 IF NOT FOUND OR c->>'kind'<>'dense' THEN RAISE EXCEPTION 'Recommendation requires a declared dense slot' USING ERRCODE='22023'; END IF;
 IF input->>'model_id' IS DISTINCT FROM c->>'model_id' OR input->>'model_version' IS DISTINCT FROM c->>'model_version' THEN
   RAISE EXCEPTION 'Recommendation model differs from index contract' USING ERRCODE='22023'; END IF;
 FOREACH field IN ARRAY ARRAY[i.key_field,i.text_field,c->>'vector_field',c->>'fingerprint_field',c->>'incarnation_field',c->>'model_id_field',c->>'model_version_field'] LOOP
   IF NOT has_column_privilege(qdrant_internal.actor_oid(),i.source_oid,field,'SELECT') THEN
     RAISE EXCEPTION 'Recommendation source column SELECT required' USING ERRCODE='42501'; END IF;
 END LOOP;
 FOREACH side IN ARRAY ARRAY['positive','negative'] LOOP
   FOR example IN SELECT value FROM jsonb_array_elements(input->side) LOOP
     EXECUTE format('SELECT jsonb_build_object(''key'',s.tagged_key,''point_id'',s.point_id,''revision'',s.revision,
       ''incarnation'',s.incarnation,''source_fingerprint'',s.fingerprint,''representation_fingerprint'',r.representation_fingerprint,''vector'',r.vector)
       FROM qdrant_internal.source_state s JOIN qdrant_internal.representation_state r
         ON r.index_name=s.index_name AND r.tagged_key=s.tagged_key AND r.name=$3
       JOIN ONLY %s t ON t.%I=(s.tagged_key->>''value'')::%s
       WHERE s.index_name=$1 AND s.tagged_key->>''value''=$2 AND NOT s.tombstone
         AND r.state=''ready'' AND r.incarnation=s.incarnation AND r.source_fingerprint=s.fingerprint
         AND encode(sha256(convert_to(t.%I,''UTF8'')),''hex'')=s.fingerprint',
         i.source_oid::regclass,i.key_field,i.key_type::regtype,i.text_field)
       INTO seed USING p_name,example#>>'{}',rep;
     IF seed IS NULL THEN RAISE EXCEPTION 'Recommendation source example is absent, stale or missing' USING ERRCODE='55000'; END IF;
     normalized:=qdrant_internal.validate_representation(seed->'vector',c);
     IF side='positive' THEN positives:=positives||jsonb_build_array(normalized);
     ELSE negatives:=negatives||jsonb_build_array(normalized); END IF;
     versions:=versions||jsonb_build_array((seed-'vector')||jsonb_build_object('side',side));
     ids:=ids||jsonb_build_array(seed->'point_id');
   END LOOP;
 END LOOP;
 -- Reuse the complete-slot and effective-role admission, never a count-only readiness approximation.
 PERFORM qdrant_internal.admit_representation(p_name,jsonb_build_object(rep,jsonb_build_object(
   'model_id',c->>'model_id','model_version',c->>'model_version','vector',positives->0)));
 IF octet_length(jsonb_build_object('positive',positives,'negative',negatives,'exclude_ids',ids)::text)>65536 THEN
   RAISE EXCEPTION 'Resolved recommendation examples exceed 64 KiB' USING ERRCODE='54000'; END IF;
 RETURN jsonb_build_object('query',jsonb_build_object('representation',rep,'model_id',c->>'model_id','model_version',c->>'model_version',
   'strategy',input->>'strategy','positive',positives,'negative',negatives,'exclude_ids',ids),
   'seed_versions',versions,'seed_digest',encode(sha256(convert_to(versions::text,'UTF8')),'hex'));
END $$;
REVOKE ALL ON FUNCTION qdrant_internal.admit_recommendation(text,jsonb) FROM PUBLIC;

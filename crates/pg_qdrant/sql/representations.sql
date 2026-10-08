-- Named BYOV slots are fixed for a generation. Source columns own model output.
CREATE TABLE qdrant_internal.representation_catalog (
 index_name text NOT NULL REFERENCES qdrant_internal.index_catalog ON DELETE CASCADE,
 name text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,31}$' AND name <> 'bm25'),
 contract jsonb NOT NULL,
 PRIMARY KEY(index_name,name)
);
CREATE TABLE qdrant_internal.representation_state (
 index_name text NOT NULL,
 name text NOT NULL,
 tagged_key jsonb NOT NULL,
 incarnation uuid NOT NULL,
 source_fingerprint text,
 representation_fingerprint text,
 state text NOT NULL CHECK(state IN ('ready','stale','missing','failed')),
 vector jsonb,
 PRIMARY KEY(index_name,name,tagged_key),
 FOREIGN KEY(index_name,name) REFERENCES qdrant_internal.representation_catalog ON DELETE CASCADE,
 FOREIGN KEY(index_name,tagged_key) REFERENCES qdrant_internal.source_state ON DELETE CASCADE,
 CHECK((state='ready' AND vector IS NOT NULL AND representation_fingerprint IS NOT NULL)
       OR (state<>'ready' AND vector IS NULL))
);

CREATE FUNCTION qdrant_internal.validate_dense(p_vector jsonb, p_contract jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE x jsonb; values_real real[]:='{}'; value_real real; norm double precision:=0;
BEGIN
 IF p_vector IS NULL OR jsonb_typeof(p_vector)<>'array' THEN
   RAISE EXCEPTION 'Dense vector must be a numeric array' USING ERRCODE='22023';
 END IF;
 IF jsonb_array_length(p_vector)<>(p_contract->>'dimensions')::integer THEN
   RAISE EXCEPTION 'Dense dimensions do not match the declared model' USING ERRCODE='22023';
 END IF;
 FOR x IN SELECT jsonb_array_elements(p_vector) LOOP
   IF jsonb_typeof(x)<>'number' THEN RAISE EXCEPTION 'Finite numeric dense values required' USING ERRCODE='22023'; END IF;
   BEGIN value_real:=(x::text)::real;
   EXCEPTION WHEN numeric_value_out_of_range THEN
     RAISE EXCEPTION 'Dense value exceeds float32 range' USING ERRCODE='22023';
   END;
   IF value_real::text IN ('NaN','Infinity','-Infinity') THEN
     RAISE EXCEPTION 'Finite dense values required' USING ERRCODE='22023';
   END IF;
   values_real:=array_append(values_real,value_real);
   norm:=norm+value_real::double precision*value_real::double precision;
 END LOOP;
 IF p_contract->>'normalization'='unit' AND abs(norm-1)>0.0001 THEN
   RAISE EXCEPTION 'Model contract requires a unit vector' USING ERRCODE='22023';
 END IF;
 IF p_contract->>'distance'='cosine' AND norm=0 THEN
   RAISE EXCEPTION 'Cosine vectors must be nonzero' USING ERRCODE='22023';
 END IF;
 IF p_contract->>'kind'='token_vectors'
    AND norm>3.4028234663852886e38/(2*(p_contract->>'max_tokens')::double precision) THEN
   RAISE EXCEPTION 'Token norm can overflow finite float32 MaxSim scoring' USING ERRCODE='22023';
 END IF;
 RETURN to_jsonb(values_real);
END $$;

CREATE FUNCTION qdrant_internal.validate_sparse(p_vector jsonb,p_contract jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE n integer; k integer; entry jsonb; token bigint; previous bigint:=-1;
        weight real; indices bigint[]:='{}'; weights real[]:='{}';
BEGIN
 IF p_vector IS NULL OR jsonb_typeof(p_vector)<>'object'
    OR NOT p_vector ?& ARRAY['indices','values'] OR (SELECT count(*) FROM jsonb_object_keys(p_vector))<>2
    OR jsonb_typeof(p_vector->'indices')<>'array' OR jsonb_typeof(p_vector->'values')<>'array' THEN
   RAISE EXCEPTION 'Sparse indices and values arrays required' USING ERRCODE='22023';
 END IF;
 n:=jsonb_array_length(p_vector->'indices');
 IF n>2048 OR n<>jsonb_array_length(p_vector->'values') THEN
   RAISE EXCEPTION 'Sparse length mismatch or more than 2048 nonzero entries' USING ERRCODE='22023';
 END IF;
 FOR k IN 0..n-1 LOOP
   entry:=p_vector->'indices'->k;
   IF jsonb_typeof(entry)<>'number' OR entry::text !~ '^[0-9]+$' THEN
     RAISE EXCEPTION 'Sparse token IDs must be integers' USING ERRCODE='22023';
   END IF;
   BEGIN token:=(entry::text)::bigint;
   EXCEPTION WHEN numeric_value_out_of_range THEN RAISE EXCEPTION 'Sparse token ID overflow' USING ERRCODE='22023'; END;
   IF token<=previous OR token>4294967295 OR token>=(p_contract->>'dimensions')::bigint THEN
     RAISE EXCEPTION 'Sparse IDs must be ordered, unique and within vocabulary' USING ERRCODE='22023';
   END IF;
   entry:=p_vector->'values'->k;
   IF jsonb_typeof(entry)<>'number' THEN RAISE EXCEPTION 'Finite sparse numeric weights required' USING ERRCODE='22023'; END IF;
   BEGIN weight:=(entry::text)::real;
   EXCEPTION WHEN numeric_value_out_of_range THEN RAISE EXCEPTION 'Sparse weight overflow' USING ERRCODE='22023'; END;
   IF weight::text IN ('NaN','Infinity','-Infinity') OR weight=0 THEN
     RAISE EXCEPTION 'Finite nonzero sparse float32 weights required' USING ERRCODE='22023';
   END IF;
   indices:=array_append(indices,token); weights:=array_append(weights,weight); previous:=token;
 END LOOP;
 RETURN jsonb_build_object('indices',indices,'values',weights);
END $$;

CREATE FUNCTION qdrant_internal.validate_tokens(p_vector jsonb,p_contract jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE token jsonb; normalized jsonb:='[]';
BEGIN
 IF p_vector IS NULL OR jsonb_typeof(p_vector)<>'array' THEN
   RAISE EXCEPTION 'Token vectors require a numeric matrix' USING ERRCODE='22023';
 END IF;
 IF jsonb_array_length(p_vector) NOT BETWEEN 1 AND (p_contract->>'max_tokens')::integer THEN
   RAISE EXCEPTION 'Token count exceeds the declared 1..max_tokens contract' USING ERRCODE='22023';
 END IF;
 FOR token IN SELECT jsonb_array_elements(p_vector) LOOP
   normalized:=normalized||jsonb_build_array(qdrant_internal.validate_dense(token,p_contract));
 END LOOP;
 RETURN normalized;
END $$;

CREATE FUNCTION qdrant_internal.validate_representation(p_vector jsonb,p_contract jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF p_contract->>'kind'='learned_sparse' THEN RETURN qdrant_internal.validate_sparse(p_vector,p_contract); END IF;
 IF p_contract->>'kind'='token_vectors' THEN RETURN qdrant_internal.validate_tokens(p_vector,p_contract); END IF;
 RETURN qdrant_internal.validate_dense(p_vector,p_contract);
END $$;

CREATE FUNCTION qdrant_internal.register_representations(p_name text,p_slots jsonb) RETURNS void
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; slot record; c jsonb; k text; field text;
        field_type oid; fields text[]:='{}'; dimensions bigint; allowed text[];
        common_fields text[]:=ARRAY['kind','model_id','model_version','tokenizer','dimensions','distance',
          'normalization','storage_precision','vector_field','fingerprint_field','incarnation_field',
          'model_id_field','model_version_field'];
BEGIN
 IF p_slots IS NULL OR jsonb_typeof(p_slots)<>'object' THEN
   RAISE EXCEPTION 'representations must be an object' USING ERRCODE='22023';
 END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(p_slots))>4 THEN
   RAISE EXCEPTION 'At most four model slots per generation' USING ERRCODE='54000';
 END IF;
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_name;
 FOR slot IN SELECT * FROM jsonb_each(p_slots) LOOP
   c:=slot.value;
   allowed:=common_fields;
   IF c->>'kind'='learned_sparse' THEN allowed:=allowed||ARRAY['vocabulary','idf_policy','idf_revision']; END IF;
   IF c->>'kind'='token_vectors' THEN allowed:=allowed||ARRAY['max_tokens','comparator']; END IF;
   IF slot.key !~ '^[a-z][a-z0-9_]{0,31}$' OR slot.key='bm25'
      OR jsonb_typeof(c)<>'object' THEN
     RAISE EXCEPTION 'Invalid representation name or contract' USING ERRCODE='22023';
   END IF;
   IF (SELECT count(*) FROM jsonb_object_keys(c))<>array_length(allowed,1) THEN
     RAISE EXCEPTION 'A complete model contract is required' USING ERRCODE='22023';
   END IF;
   FOR k IN SELECT jsonb_object_keys(c) LOOP
     IF NOT k=ANY(allowed) OR (k NOT IN ('dimensions','max_tokens') AND (jsonb_typeof(c->k)<>'string'
          OR octet_length(c->>k) NOT BETWEEN 1 AND 256)) THEN
       RAISE EXCEPTION 'Invalid model contract field: %',k USING ERRCODE='22023';
     END IF;
   END LOOP;
   IF jsonb_typeof(c->'dimensions')<>'number' OR c->>'dimensions' !~ '^[0-9]+$' THEN
     RAISE EXCEPTION 'Integer dimensions required' USING ERRCODE='22023';
   END IF;
   BEGIN dimensions:=(c->>'dimensions')::bigint;
   EXCEPTION WHEN numeric_value_out_of_range THEN
     RAISE EXCEPTION 'Representation dimensions exceed bounds' USING ERRCODE='22023';
   END;
   IF c->>'kind'='learned_sparse' THEN
     IF dimensions NOT BETWEEN 1 AND 4294967296 OR c->>'distance'<>'dot' OR c->>'normalization'<>'none'
        OR c->>'storage_precision'<>'float32' OR c->>'idf_policy' NOT IN ('none','external','engine')
        OR (c->>'idf_policy'='engine' AND c->>'idf_revision'<>'qdrant-edge:0.8.0') THEN
       RAISE EXCEPTION 'Unsupported sparse vocabulary or IDF contract' USING ERRCODE='0A000';
     END IF;
   ELSIF c->>'kind'='token_vectors' THEN
     IF dimensions NOT BETWEEN 1 AND 4096 OR c->>'distance'<>'dot' OR c->>'comparator'<>'maxsim'
        OR c->>'normalization' NOT IN ('none','unit') OR c->>'storage_precision'<>'float32' THEN
       RAISE EXCEPTION 'Unsupported token vector contract' USING ERRCODE='0A000';
     END IF;
     IF jsonb_typeof(c->'max_tokens')<>'number' OR c->>'max_tokens' !~ '^[0-9]{1,3}$'
        OR (c->>'max_tokens')::integer NOT BETWEEN 1 AND 128 THEN
       RAISE EXCEPTION 'max_tokens must be an integer in 1..128' USING ERRCODE='22023';
     END IF;
   ELSIF dimensions NOT BETWEEN 1 AND 4096 OR c->>'kind'<>'dense'
      OR c->>'distance' NOT IN ('dot','cosine','euclid','manhattan')
      OR c->>'normalization' NOT IN ('none','unit') OR c->>'storage_precision'<>'float32' THEN
     RAISE EXCEPTION 'Unsupported dense model contract' USING ERRCODE='0A000';
   END IF;
   fields:='{}';
   FOREACH k IN ARRAY ARRAY['vector_field','fingerprint_field','incarnation_field','model_id_field','model_version_field'] LOOP
     field:=c->>k;
     field_type:=CASE k WHEN 'vector_field' THEN 'jsonb'::regtype WHEN 'incarnation_field' THEN 'uuid'::regtype ELSE 'text'::regtype END;
     IF octet_length(field)>63 OR field=ANY(fields) OR field IN (i.key_field,i.text_field)
        OR NOT EXISTS(SELECT 1 FROM pg_attribute a WHERE a.attrelid=i.source_oid AND a.attname=field
             AND a.attnum>0 AND NOT a.attisdropped AND a.attgenerated='' AND a.atttypid=field_type) THEN
       RAISE EXCEPTION 'Missing or unsafe model output column: %',field USING ERRCODE='22023';
     END IF;
     fields:=array_append(fields,field);
   END LOOP;
   INSERT INTO qdrant_internal.representation_catalog VALUES(p_name,slot.key,c);
   EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OF %s ON %s
      FOR EACH ROW EXECUTE FUNCTION qdrant_internal.model_output_guard(%L,%L)',
      'qdrant_model_'||slot.key,(SELECT string_agg(format('%I',f),',') FROM unnest(fields) f),
      i.source_oid::regclass,p_name,slot.key);
 END LOOP;
END $$;

-- Extract declared fields only. Unrelated source values do not enter the ledger.
CREATE FUNCTION qdrant_internal.model_projection(p_name text,p_row anyelement) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE slot record; result jsonb:='{}'; value jsonb;
BEGIN
 FOR slot IN SELECT * FROM qdrant_internal.representation_catalog WHERE index_name=p_name LOOP
   EXECUTE format('SELECT jsonb_build_object(''vector'',($1).%I,''fingerprint'',($1).%I,
     ''incarnation'',($1).%I,''model_id'',($1).%I,''model_version'',($1).%I)',
     slot.contract->>'vector_field',slot.contract->>'fingerprint_field',slot.contract->>'incarnation_field',
     slot.contract->>'model_id_field',slot.contract->>'model_version_field') INTO value USING p_row;
   result:=result||jsonb_build_object(slot.name,value);
 END LOOP;
 RETURN result;
END $$;

CREATE FUNCTION qdrant_internal.model_output_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; s qdrant_internal.source_state%ROWTYPE;
        key_value text; body text; projected jsonb; slot record; output jsonb;
BEGIN
 IF TG_WHEN<>'BEFORE' OR TG_LEVEL<>'ROW' OR TG_OP<>'UPDATE' OR TG_NARGS<>2 OR TG_NAME<>'qdrant_model_'||TG_ARGV[1] THEN
   RAISE EXCEPTION 'Untrusted model trigger invocation' USING ERRCODE='42501';
 END IF;
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=TG_ARGV[0] AND source_oid=TG_RELID;
 IF i.capture_state<>'capturing' THEN RETURN NEW; END IF;
 EXECUTE format('SELECT ($1).%I::text,($1).%I',i.key_field,i.text_field) INTO key_value,body USING NEW;
 SELECT * INTO s FROM qdrant_internal.source_state WHERE index_name=i.index_name AND tagged_key->>'value'=key_value;
 projected:=qdrant_internal.model_projection(i.index_name,NEW);
 FOR slot IN SELECT * FROM qdrant_internal.representation_catalog WHERE index_name=i.index_name AND name=TG_ARGV[1] LOOP
   output:=projected->slot.name;
   -- A NULL vector clears the slot without requiring a model completion token.
   IF output->'vector'<>'null'::jsonb THEN
     IF s.incarnation IS NULL OR s.tombstone OR output->>'incarnation' IS DISTINCT FROM s.incarnation::text
        OR output->>'fingerprint' IS DISTINCT FROM encode(sha256(convert_to(body,'UTF8')),'hex')
        OR output->>'model_id' IS DISTINCT FROM slot.contract->>'model_id'
        OR output->>'model_version' IS DISTINCT FROM slot.contract->>'model_version' THEN
       RAISE EXCEPTION 'Stale or incompatible model output: %',slot.name USING ERRCODE='55000';
     END IF;
     PERFORM qdrant_internal.validate_representation(output->'vector',slot.contract);
   END IF;
 END LOOP;
 RETURN NEW;
END $$;

CREATE FUNCTION qdrant_internal.capture_representations(p_name text,p_key jsonb,p_inc uuid,
 p_fingerprint text,p_outputs jsonb,p_deleted boolean) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
DECLARE slot record; output jsonb; status text; vector jsonb; vectors jsonb:='{}'; fingerprint text;
BEGIN
 FOR slot IN SELECT * FROM qdrant_internal.representation_catalog WHERE index_name=p_name LOOP
   output:=p_outputs->slot.name; vector:=NULL; fingerprint:=NULL;
   status:=CASE WHEN p_deleted OR output IS NULL OR output->'vector'='null'::jsonb THEN 'missing'
     WHEN output->>'incarnation' IS DISTINCT FROM p_inc::text OR output->>'fingerprint' IS DISTINCT FROM p_fingerprint
       OR output->>'model_id' IS DISTINCT FROM slot.contract->>'model_id'
       OR output->>'model_version' IS DISTINCT FROM slot.contract->>'model_version' THEN 'stale' ELSE 'ready' END;
   IF status='ready' THEN
     vector:=qdrant_internal.validate_representation(output->'vector',slot.contract);
     fingerprint:=encode(sha256(convert_to(jsonb_build_object('model',slot.contract,'source',p_fingerprint,
                  'incarnation',p_inc,'vector',vector)::text,'UTF8')),'hex');
     vectors:=vectors||jsonb_build_object(slot.name,vector);
   END IF;
   INSERT INTO qdrant_internal.representation_state VALUES(p_name,slot.name,p_key,p_inc,p_fingerprint,fingerprint,status,vector)
   ON CONFLICT(index_name,name,tagged_key) DO UPDATE SET incarnation=excluded.incarnation,
     source_fingerprint=excluded.source_fingerprint,representation_fingerprint=excluded.representation_fingerprint,
     state=excluded.state,vector=excluded.vector;
 END LOOP;
 RETURN vectors;
END $$;

CREATE FUNCTION qdrant_internal.ready_vectors(p_name text,p_key jsonb,p_inc uuid) RETURNS jsonb
LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT coalesce(jsonb_object_agg(name,vector),'{}'::jsonb) FROM qdrant_internal.representation_state
 WHERE index_name=p_name AND tagged_key=p_key AND incarnation=p_inc AND state='ready'
$$;
CREATE FUNCTION qdrant_internal.representation_summary(p_name text) RETURNS jsonb
LANGUAGE sql STABLE SET search_path=pg_catalog,pg_temp AS $$
 SELECT coalesce(jsonb_object_agg(c.name,jsonb_build_object('contract',c.contract,'rows',
   (SELECT jsonb_build_object('ready',count(*) FILTER(WHERE s.state='ready'),
      'stale',count(*) FILTER(WHERE s.state='stale'),'missing',count(*) FILTER(WHERE s.state='missing'),
      'failed',count(*) FILTER(WHERE s.state='failed')) FROM qdrant_internal.representation_state s
    JOIN qdrant_internal.source_state src USING(index_name,tagged_key)
    WHERE s.index_name=c.index_name AND s.name=c.name AND NOT src.tombstone))),'{}'::jsonb)
 FROM qdrant_internal.representation_catalog c WHERE c.index_name=p_name
$$;

CREATE FUNCTION qdrant.encoding_inputs(p_name text,p_representation text,p_limit integer DEFAULT 100)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE i qdrant_internal.index_catalog%ROWTYPE; c jsonb; result jsonb; field text;
BEGIN
 IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'encoding limit must be 1..100' USING ERRCODE='22023'; END IF;
 PERFORM qdrant_internal.require_index(p_name);
 SELECT * INTO STRICT i FROM qdrant_internal.index_catalog WHERE index_name=p_name FOR KEY SHARE NOWAIT;
 EXECUTE format('LOCK TABLE ONLY %s IN ACCESS SHARE MODE NOWAIT',i.source_oid::regclass);
 IF qdrant_internal.p1_index_status(p_name)->>'capture_state'<>'capturing' THEN
   RAISE EXCEPTION 'Source binding changed' USING ERRCODE='55000';
 END IF;
 SELECT contract INTO STRICT c FROM qdrant_internal.representation_catalog WHERE index_name=p_name AND name=p_representation;
 FOREACH field IN ARRAY ARRAY[c->>'vector_field',c->>'fingerprint_field',c->>'incarnation_field',c->>'model_id_field',c->>'model_version_field'] LOOP
   IF NOT has_column_privilege(qdrant_internal.actor_oid(),i.source_oid,field,'UPDATE') THEN
     RAISE EXCEPTION 'Model output column UPDATE required' USING ERRCODE='42501';
   END IF;
 END LOOP;
 EXECUTE format('SELECT coalesce(jsonb_agg(row_data),''[]''::jsonb) FROM
   (SELECT jsonb_build_object(''key'',s.tagged_key,''incarnation'',s.incarnation,''revision'',s.revision,
    ''source_fingerprint'',s.fingerprint,''text'',t.%I,''model'', $2,''representation'',$3) AS row_data
    FROM qdrant_internal.source_state s JOIN ONLY %s t ON t.%I=(s.tagged_key->>''value'')::%s
    JOIN qdrant_internal.representation_state r ON r.index_name=s.index_name AND r.tagged_key=s.tagged_key AND r.name=$3
    WHERE s.index_name=$1 AND NOT s.tombstone AND r.state<>''ready''
      AND s.fingerprint=encode(sha256(convert_to(t.%I,''UTF8'')),''hex'') ORDER BY s.point_id LIMIT $4) rows',
    i.text_field,i.source_oid::regclass,i.key_field,i.key_type::regtype,i.text_field)
    INTO result USING p_name,c,p_representation,p_limit;
 RETURN result;
END $$;

CREATE FUNCTION qdrant_internal.admit_representation(p_name text,p_queries jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path=pg_catalog,pg_temp AS $$
DECLARE rep text; input jsonb; c jsonb; normalized jsonb; field text; source_id oid;
BEGIN
 IF (SELECT count(*) FROM jsonb_object_keys(p_queries))<>1 THEN
   RAISE EXCEPTION 'One named representation query required' USING ERRCODE='22023';
 END IF;
 SELECT key,value INTO rep,input FROM jsonb_each(p_queries);
 IF jsonb_typeof(input)<>'object' OR NOT input ?& ARRAY['model_id','model_version','vector']
    OR (SELECT count(*) FROM jsonb_object_keys(input)) NOT IN (3,5)
    OR jsonb_typeof(input->'model_id')<>'string' OR jsonb_typeof(input->'model_version')<>'string' THEN
   RAISE EXCEPTION 'Query vector, model_id and model_version required' USING ERRCODE='22023';
 END IF;
 SELECT contract INTO c FROM qdrant_internal.representation_catalog WHERE index_name=p_name AND name=rep;
 IF NOT FOUND THEN RAISE EXCEPTION 'Unknown named representation' USING ERRCODE='22023'; END IF;
 IF input->>'model_id' IS DISTINCT FROM c->>'model_id' OR input->>'model_version' IS DISTINCT FROM c->>'model_version' THEN
   RAISE EXCEPTION 'Query model differs from index model contract' USING ERRCODE='22023';
 END IF;
 IF c->>'kind'='learned_sparse' THEN
   IF (SELECT count(*) FROM jsonb_object_keys(input))<>5 OR NOT input ?& ARRAY['vocabulary','idf_revision']
      OR jsonb_typeof(input->'vocabulary')<>'string' OR jsonb_typeof(input->'idf_revision')<>'string'
      OR input->>'vocabulary' IS DISTINCT FROM c->>'vocabulary' OR input->>'idf_revision' IS DISTINCT FROM c->>'idf_revision' THEN
     RAISE EXCEPTION 'Query vocabulary and IDF revision must match the model contract' USING ERRCODE='22023';
   END IF;
 ELSIF (SELECT count(*) FROM jsonb_object_keys(input))<>3 THEN
   RAISE EXCEPTION 'Dense query contains unsupported fields' USING ERRCODE='22023';
 END IF;
 SELECT source_oid INTO STRICT source_id FROM qdrant_internal.index_catalog WHERE index_name=p_name;
 FOREACH field IN ARRAY ARRAY[c->>'vector_field',c->>'fingerprint_field',c->>'incarnation_field',c->>'model_id_field',c->>'model_version_field'] LOOP
   IF NOT has_column_privilege(qdrant_internal.actor_oid(),source_id,field,'SELECT') THEN
     RAISE EXCEPTION 'Representation source column SELECT required' USING ERRCODE='42501';
   END IF;
 END LOOP;
 normalized:=qdrant_internal.validate_representation(input->'vector',c);
 IF EXISTS(SELECT 1 FROM qdrant_internal.source_state s
   LEFT JOIN qdrant_internal.representation_state r ON r.index_name=s.index_name AND r.tagged_key=s.tagged_key AND r.name=rep
   WHERE s.index_name=p_name AND NOT s.tombstone AND (r.state IS DISTINCT FROM 'ready'
     OR r.incarnation IS DISTINCT FROM s.incarnation OR r.source_fingerprint IS DISTINCT FROM s.fingerprint)) THEN
   RAISE EXCEPTION 'Required representation is missing, stale or failed' USING ERRCODE='55000';
 END IF;
 RETURN jsonb_build_object('representation',rep,'model_id',c->>'model_id','model_version',c->>'model_version','vector',normalized);
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA qdrant_internal FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA qdrant_internal FROM PUBLIC;
GRANT EXECUTE ON FUNCTION qdrant.encoding_inputs(text,text,integer) TO PUBLIC;

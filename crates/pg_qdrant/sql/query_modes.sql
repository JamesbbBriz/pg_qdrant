CREATE TABLE qdrant_internal.query_modes (
 mode text PRIMARY KEY,
 required_kinds text[] NOT NULL,
 capability_ids text[] NOT NULL,
 implementation_scope text NOT NULL
);
INSERT INTO qdrant_internal.query_modes VALUES
 ('text','{}',ARRAY['F01','F05','F06','F07','F08','F09','F10','F11','Q12'],'one source text field; fixed native analysis and bounded predicates'),
 ('explore',ARRAY['dense'],ARRAY['Q04','Q05','V01','Q12'],'dense source-example recommendation/discover/context; pinned native scoring; before-cap predicates and exclusion'),
 ('semantic',ARRAY['dense'],ARRAY['V01','Q12'],'declared named dense BYOV; exact native query'),
 ('sparse',ARRAY['learned_sparse'],ARRAY['F02','V02','Q12'],'declared sparse BYOV with explicit vocabulary and live-generation IDF contract'),
 ('hybrid',ARRAY['dense','learned_sparse'],ARRAY['F01','V01','V02','Q02','Q03','Q12'],'BM25 and one dense or sparse branch; fixed native RRF/DBSF'),
 ('maxsim',ARRAY['token_vectors'],ARRAY['V03','Q12'],'declared bounded exact token MaxSim; scalar-work guard'),
 ('precision',ARRAY['token_vectors'],ARRAY['F01','V03','Q02','Q03','Q12'],'bounded BM25 or hybrid candidates followed by exact candidate-domain MaxSim');
REVOKE ALL ON qdrant_internal.query_modes FROM PUBLIC;

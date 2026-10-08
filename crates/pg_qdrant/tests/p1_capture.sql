-- Only for a disposable PostgreSQL 17 installation with the P1 draft binary.
-- Does not exercise the native engine, durable ACK or cross-session races.
\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS pg_qdrant;
DROP TABLE IF EXISTS public.pgq_p1_fixture CASCADE;
CREATE TABLE public.pgq_p1_fixture (
  id bigint PRIMARY KEY,
  title text NOT NULL,
  body text NOT NULL,
  ignored text
);
INSERT INTO public.pgq_p1_fixture VALUES
  (1,'first','backfill a','x'),(2,'second','backfill b','y');
SELECT qdrant.create_index('p1_fixture','public.pgq_p1_fixture'::regclass,
                           'id'::name,'{"text":{"fields":["title","body"]}}');
DO $t$
BEGIN
 IF (SELECT count(*) FROM qdrant._events e JOIN qdrant._index_catalog c USING (index_id)
     WHERE c.index_name='p1_fixture' AND origin='backfill') <> 2 THEN
  RAISE EXCEPTION 'backfill rows were not captured';
 END IF;
 IF (qdrant.index_status('p1_fixture')->>'search_ready')::boolean THEN
  RAISE EXCEPTION 'capture-only index incorrectly reports ready';
 END IF;
END
$t$;
BEGIN;
INSERT INTO public.pgq_p1_fixture VALUES (3,'rolled back','x','x');
ROLLBACK;
DO $t$
BEGIN
 IF EXISTS (SELECT 1 FROM qdrant._events e JOIN qdrant._index_catalog c USING(index_id)
            WHERE c.index_name='p1_fixture' AND e.key_text='3') THEN
  RAISE EXCEPTION 'rolled-back capture leaked';
 END IF;
END
$t$;
BEGIN;
INSERT INTO public.pgq_p1_fixture VALUES (4,'savepoint','x','x');
SAVEPOINT early;
UPDATE public.pgq_p1_fixture SET title='later' WHERE id=4;
ROLLBACK TO SAVEPOINT early;
COMMIT;
DO $t$
BEGIN
 IF (SELECT count(*) FROM qdrant._events e JOIN qdrant._index_catalog c USING(index_id)
     WHERE c.index_name='p1_fixture' AND key_text='4') <> 1 THEN
  RAISE EXCEPTION 'savepoint rollback leaked event';
 END IF;
END
$t$;
UPDATE public.pgq_p1_fixture SET ignored='changed' WHERE id=1;
DELETE FROM public.pgq_p1_fixture WHERE id=1;
INSERT INTO public.pgq_p1_fixture VALUES (1,'reused','x','z');
DO $t$
DECLARE
 old_inc uuid;
 new_inc uuid;
BEGIN
 SELECT e.incarnation INTO STRICT old_inc FROM qdrant._events e
 JOIN qdrant._index_catalog c USING(index_id)
 WHERE c.index_name='p1_fixture' AND key_text='1' AND revision=1;
 SELECT e.incarnation INTO STRICT new_inc FROM qdrant._events e
 JOIN qdrant._index_catalog c USING(index_id)
 WHERE c.index_name='p1_fixture' AND key_text='1' AND revision=4;
 IF old_inc = new_inc THEN RAISE EXCEPTION 'reinsert reused old incarnation'; END IF;
 IF (SELECT count(DISTINCT fingerprint) FROM qdrant._events e
     JOIN qdrant._index_catalog c USING(index_id)
     WHERE c.index_name='p1_fixture' AND key_text='1' AND revision IN(1,2)) <> 1 THEN
  RAISE EXCEPTION 'unindexed column changed projection fingerprint';
 END IF;
END
$t$;
DO $t$
BEGIN
 BEGIN
  UPDATE public.pgq_p1_fixture SET id=9 WHERE id=1;
  RAISE EXCEPTION 'primary-key mutation unexpectedly accepted';
 EXCEPTION WHEN feature_not_supported THEN NULL;
 END;
 BEGIN
  TRUNCATE public.pgq_p1_fixture;
  RAISE EXCEPTION 'TRUNCATE unexpectedly accepted';
 EXCEPTION WHEN feature_not_supported THEN NULL;
 END;
END
$t$;
SELECT qdrant.drop_index('p1_fixture');
DO $t$
BEGIN
 IF EXISTS (SELECT 1 FROM qdrant._index_catalog WHERE index_name='p1_fixture') THEN
  RAISE EXCEPTION 'drop_index left catalog entry';
 END IF;
END
$t$;
DROP TABLE public.pgq_p1_fixture;
SELECT 'P1 transactional capture-only smoke completed (no Edge apply)' AS result;
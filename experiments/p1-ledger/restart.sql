-- Source/outbox WAL-only crash recovery; Edge is not involved.
DO $test$
DECLARE v_count bigint; j jsonb;
BEGIN
 SELECT count(*) INTO v_count FROM qdrant_internal.p1_outbox
 WHERE index_name='parallel';
 IF v_count<>4 THEN
    RAISE EXCEPTION 'lost one of four committed parallel events: %',v_count;
 END IF;
 SELECT count(*) INTO v_count FROM qdrant_internal.p1_outbox
 WHERE index_name='copy_idx';
 IF v_count<>6 THEN
    RAISE EXCEPTION 'lost committed COPY/multirow source events';
 END IF;
 SELECT count(*) INTO v_count FROM qdrant_internal.p1_tickets WHERE index_name='kb';
 IF v_count<>2 THEN RAISE EXCEPTION 'lost committed exact ticket membership'; END IF;
 SELECT count(*) INTO v_count FROM qdrant_internal.p1_tickets t
 WHERE t.sealed_events = 1 AND (
  SELECT count(*) FROM qdrant_internal.p1_outbox e WHERE e.ticket_id=t.ticket_id
 )=1;
 IF v_count<>2 THEN RAISE EXCEPTION 'ticket membership was altered by restart'; END IF;
 j:=qdrant_internal.p1_index_status('kb');
 IF j->>'capture_state'<>'degraded' OR j->>'engine_index_ready'<>'false'
 THEN RAISE EXCEPTION 'restarted orphan source reported ready'; END IF;
 j:=qdrant_internal.p1_ticket_status(
  (SELECT ticket_id FROM qdrant_internal.p1_tickets WHERE index_name='kb' LIMIT 1));
 IF j->>'durable'<>'false' OR j->>'pending_events'<>'1'
 THEN RAISE EXCEPTION 'unacknowledged events suddenly durable after restart'; END IF;
END $test$;

#!/usr/bin/env python3
"""Disposable two-session commit inversion and same-key serialization proof."""
import json
import os
import subprocess
import time


def psql(sql: str):
    result = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", sql],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy(),
        timeout=20, check=True,
    )
    return result.stdout.strip()


def start(sql: str):
    return subprocess.Popen(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", sql],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy(),
    )


def collect(child, label: str):
    try:
        out, err = child.communicate(timeout=12)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        raise AssertionError(f"{label} stuck")
    if child.returncode:
        raise AssertionError(f"{label} failed: {err[-2000:]}")
    return out


psql("CREATE TABLE public.p1_parallel (id bigint PRIMARY KEY, body text NOT NULL)")
psql("SELECT qdrant_internal.p1_register('parallel','public.p1_parallel'::regclass,'id','body')")
first = start("BEGIN; INSERT INTO public.p1_parallel VALUES (100,'slow'); SELECT pg_sleep(2); COMMIT;")
time.sleep(0.55)
psql("INSERT INTO public.p1_parallel VALUES (101,'fast')")
assert psql("SELECT count(*) FROM qdrant_internal.p1_outbox WHERE index_name='parallel'") == "1"
assert psql("SELECT tagged_key->>'value' FROM qdrant_internal.p1_outbox WHERE index_name='parallel'") == "101"
collect(first, "slow-first-transaction")
rows = json.loads(psql("""
 SELECT json_agg(json_build_object('key',tagged_key->>'value','id',event_id) ORDER BY event_id)
 FROM qdrant_internal.p1_outbox WHERE index_name='parallel'
"""))
assert len(rows) == 2 and rows[0]["key"] == "100" and rows[1]["key"] == "101", rows
second = start("BEGIN; UPDATE public.p1_parallel SET body='before' WHERE id=100; SELECT pg_sleep(1.5); COMMIT;")
time.sleep(0.35)
psql("UPDATE public.p1_parallel SET body='after' WHERE id=100")
collect(second, "same-key-first-update")
state = json.loads(psql("""
 SELECT json_build_object('revision',revision,'body',body,'tombstone',tombstone)
 FROM qdrant_internal.p1_source_state
 WHERE index_name='parallel' AND tagged_key='{"type":"bigint","value":"100"}'::jsonb
"""))
assert state == {"revision": 3, "body": "after", "tombstone": False}, state
print(json.dumps({
    "schema_version": 1,
    "kind": "p1_ledger_commit_order_probe",
    "commit_inversion": "confirmed",
    "event_id_first_commit_later": rows[0]["id"],
    "event_id_second_commit_earlier": rows[1]["id"],
    "same_key_final_revision": state["revision"],
    "same_key_final_body": state["body"],
    "engine_ack_verified": False,
    "release_supported": False,
}, sort_keys=True))

"""Real logical source restore with explicit fresh text-index registration."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import verify_model_restore


def run(sql, ready, ticket_from, checks, spawn, finish, wait_session):
    original_database = os.environ.get('PGDATABASE')
    suffix = secrets.token_hex(6)
    source_database = 'pgq_dump_source_' + suffix
    restored_database = 'pgq_dump_restored_' + suffix
    dump = Path('/src/artifacts') / ('source-restore-' + suffix + '.dump')
    declaration = "qdrant.create_index('restored_docs','restored_docs','id','{\"text\":{\"fields\":[\"body\"]}}')"

    def trigger_ids():
        return sql("SELECT jsonb_object_agg(tgname,oid::text) FROM pg_trigger "
                   "WHERE tgrelid='restored_docs'::regclass AND NOT tgisinternal")

    try:
        sql('CREATE DATABASE ' + source_database + ' TEMPLATE template0')
        os.environ['PGDATABASE'] = source_database
        sql('CREATE EXTENSION pg_qdrant; CREATE TABLE restored_docs(id bigint PRIMARY KEY,body text NOT NULL)')
        sql('CREATE FUNCTION public.source_audit() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$; '
            'CREATE TRIGGER source_audit AFTER INSERT OR UPDATE OR DELETE ON restored_docs '
            'FOR EACH ROW EXECUTE FUNCTION public.source_audit()')
        sql("INSERT INTO restored_docs VALUES(1,'logical backup original'),(2,'logical backup second'); SELECT " + declaration)
        old_status = ready('restored_docs')
        old_incarnation = sql("SELECT incarnation FROM qdrant_internal.source_state WHERE index_name='restored_docs' AND tagged_key->>'value'='1'")
        old_ticket = ticket_from(sql("BEGIN; UPDATE restored_docs SET body='logical backup committed' WHERE id=1; "
                                    "SELECT qdrant.track_changes('restored_docs'); COMMIT"))
        assert json.loads(sql("SELECT qdrant.await_changes('" + old_ticket + "',60000)"))['durable']
        model_fixture = verify_model_restore.prepare(sql, ready, ticket_from)
        subprocess.run(['pg_dump', '--format=custom', '--no-owner', '--no-acl', '--file', str(dump)],
                       check=True, timeout=30)
        sql('CREATE DATABASE ' + restored_database + ' TEMPLATE template0')
        subprocess.run(['pg_restore', '--exit-on-error', '--no-owner', '--no-acl',
                        '--dbname', restored_database, str(dump)], check=True, timeout=40)
        os.environ['PGDATABASE'] = restored_database
        assert sql('SELECT count(*) FROM restored_docs') == '2'
        assert sql('SELECT count(*) FROM qdrant_internal.index_catalog') == '0'
        assert sql('SELECT count(*) FROM qdrant_internal.change_tickets') == '0'
        before = trigger_ids()
        assert set(json.loads(before)) == {'qdrant_p1_rows', 'qdrant_p1_truncate', 'source_audit'}
        assert 'P0002' in sql("UPDATE restored_docs SET body='unregistered write' WHERE id=1", ok=False)
        unknown_ticket = sql("SELECT qdrant.await_changes('" + old_ticket + "',100)", ok=False)
        assert '22023' in unknown_ticket and 'Unknown or uncommitted ticket' in unknown_ticket, unknown_ticket
        checks.append('actual pg_dump/pg_restore retains source facts and capture triggers but imports no private readiness, identities or durable tickets')

        assert '42501' in sql('SET ROLE pgq_writer; SELECT ' + declaration, ok=False)
        negatives = [
            'DROP TRIGGER qdrant_p1_truncate ON restored_docs',
            'ALTER TABLE restored_docs DISABLE TRIGGER qdrant_p1_rows',
            "DROP TRIGGER qdrant_p1_rows ON restored_docs; CREATE TRIGGER qdrant_p1_rows AFTER INSERT OR UPDATE OR DELETE ON restored_docs "
            "FOR EACH ROW EXECUTE FUNCTION qdrant_internal.p1_capture_row('different_index')",
            "DROP TRIGGER qdrant_p1_rows ON restored_docs; CREATE TRIGGER qdrant_p1_rows AFTER INSERT ON restored_docs "
            "FOR EACH ROW EXECUTE FUNCTION qdrant_internal.p1_capture_row('restored_docs')",
            "DROP TRIGGER qdrant_p1_rows ON restored_docs; CREATE TRIGGER qdrant_p1_rows AFTER INSERT OR UPDATE OR DELETE ON restored_docs "
            "FOR EACH ROW EXECUTE FUNCTION public.source_audit()",
            "DROP TRIGGER qdrant_p1_rows ON restored_docs; CREATE TRIGGER qdrant_p1_rows AFTER INSERT OR UPDATE OR DELETE ON restored_docs "
            "FOR EACH ROW WHEN (true) EXECUTE FUNCTION qdrant_internal.p1_capture_row('restored_docs')",
            "CREATE TRIGGER qdrant_model_legacy BEFORE UPDATE OF body ON restored_docs "
            "FOR EACH ROW EXECUTE FUNCTION qdrant_internal.model_output_guard('restored_docs','legacy')",
        ]
        for change in negatives:
            assert '55000' in sql('BEGIN; ' + change + '; SELECT ' + declaration, ok=False)
            assert trigger_ids() == before
            assert sql('SELECT count(*) FROM qdrant_internal.index_catalog') == '0'
        invalid = declaration.replace('"fields":["body"]}}', '"fields":["body"]},"payload":{"invalid":{}}}')
        assert '22023' in sql('SELECT ' + invalid, ok=False)
        assert trigger_ids() == before
        checks.append('restored registration rejects non-owner, incomplete/disabled/wrong-argument/wrong-event/wrong-function/conditional capture and orphan model guards; invalid configuration rolls back cleanup')

        # Trigger replacement and a fresh registration are in one transaction.
        sql('BEGIN; SELECT ' + declaration + '; ROLLBACK')
        assert trigger_ids() == before
        assert sql('SELECT count(*) FROM qdrant_internal.index_catalog') == '0'
        sql('SELECT ' + declaration)
        current = ready('restored_docs')
        assert current['generation'] != old_status['generation']
        assert sql("SELECT incarnation FROM qdrant_internal.source_state WHERE index_name='restored_docs' AND tagged_key->>'value'='1'") != old_incarnation
        assert json.loads(sql("SELECT jsonb_agg(source_key ORDER BY rank) FROM qdrant.search('restored_docs','committed')")) == ['1']
        assert trigger_ids() != before
        assert json.loads(trigger_ids())['source_audit'] == json.loads(before)['source_audit']
        restored_triggers = trigger_ids()
        assert '23505' in sql('SELECT ' + declaration, ok=False)
        assert trigger_ids() == restored_triggers
        assert ready('restored_docs')['generation'] == current['generation']
        checks.append('explicit text re-registration replaces only a complete orphan trigger pair atomically, allocates fresh generation/incarnation and actually queries rebuilt Edge points')

        ticket = ticket_from(sql("BEGIN; DELETE FROM restored_docs WHERE id=1; INSERT INTO restored_docs VALUES(1,'logical restored replacement'); "
                                 "UPDATE restored_docs SET body='logical restored update' WHERE id=2; SELECT qdrant.track_changes('restored_docs'); COMMIT"))
        assert json.loads(sql("SELECT qdrant.await_changes('" + ticket + "',60000)"))['durable']
        assert sql("SELECT count(*) FROM qdrant.search('restored_docs','committed')") == '0'
        assert sql("SELECT count(*) FROM qdrant.search('restored_docs','restored')") == '2'
        checks.append('ordinary restored-source DML, delete/key reuse and a newly committed fixed ticket reach real Edge flush/ACK without reusing the old backup ticket')
        verify_model_restore.recover(sql, ready, ticket_from, checks, model_fixture)
        sql('CREATE TABLE ownership_race(id bigint PRIMARY KEY,body text NOT NULL); ALTER TABLE ownership_race OWNER TO pgq_writer')
        owner_name = sql('SELECT session_user').replace('"', '""')
        holder = spawn('BEGIN; LOCK TABLE ownership_race IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(3); '
                       'ALTER TABLE ownership_race OWNER TO "' + owner_name + '"; COMMIT', 'pgq_restore_owner_lock')
        wait_session('pgq_restore_owner_lock', "wait_event='PgSleep'")
        registration = spawn("SET ROLE pgq_writer; SELECT qdrant.create_index('ownership_race','ownership_race','id',"
                             "'{\"text\":{\"fields\":[\"body\"]}}')", 'pgq_restore_old_owner')
        wait_session('pgq_restore_old_owner', "wait_event_type='Lock' AND wait_event='relation'")
        finish(holder)
        output, failure = registration.communicate(timeout=15)
        assert registration.returncode != 0 and '42501' in failure, (output, failure)
        assert sql("SELECT count(*) FROM qdrant_internal.index_catalog WHERE index_name='ownership_race'") == '0'
        assert sql("SELECT count(*) FROM pg_trigger WHERE tgrelid='ownership_race'::regclass AND NOT tgisinternal") == '0'
        checks.append('registration blocked behind actual concurrent owner DDL rechecks owner membership under its acquired source lock before installing capture')
        # The enclosing disposable cluster owns both databases and their workers.
        # No production database or existing fixture is removed here.
    finally:
        if original_database is None:
            os.environ.pop('PGDATABASE', None)
        else:
            os.environ['PGDATABASE'] = original_database

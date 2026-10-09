"""Hot PG17 base backup and source-derived native replay in a separate cluster."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import verify_model_restore


def run(sql, ready, ticket_from, checks):
    original = {key: os.environ.get(key) for key in ('PGHOST', 'PGPORT', 'PGDATABASE')}
    root = Path(tempfile.mkdtemp(prefix='pgq-physical.', dir='/tmp')).resolve()
    assert root.parent == Path('/tmp') and root.name.startswith('pgq-physical.')
    artifacts = Path(os.environ.get('PG_QDRANT_ARTIFACT_DIR', '/src/artifacts'))
    artifacts.mkdir(parents=True, exist_ok=True)
    bindir = Path(subprocess.check_output(['pg_config', '--bindir'], text=True).strip())
    source, clone = root / 'source', root / 'clone'
    report = {'status': 'incomplete', 'cluster_root': str(root), 'release_supported': False,
              'source_revision': os.environ.get('PG_QDRANT_SOURCE_SHA', 'unrecorded')}
    started = []
    backup = None
    success = False
    began = time.monotonic()

    def command(name, *args, timeout=30):
        result = subprocess.run([str(bindir / name), *map(str, args)],
                                capture_output=True, text=True, timeout=timeout)
        assert result.returncode == 0, (name, result.stdout, result.stderr)
        return result.stdout

    def start(data, socket, port, label):
        socket.mkdir()
        log = artifacts / ('physical-' + label + '-postgres.log')
        started.append(data)
        command('pg_ctl', '-D', data, '-l', log, '-o',
                "-c listen_addresses='' -c unix_socket_directories='" + str(socket) +
                "' -c port=" + str(port) + ' -c fsync=on -c synchronous_commit=on', '-w', 'start')
        os.environ.update(PGHOST=str(socket), PGPORT=str(port), PGDATABASE='postgres')
        assert Path(sql('SHOW data_directory')).resolve() == data
        assert sql('SHOW fsync') == 'on' and sql('SHOW synchronous_commit') == 'on'

    def identities():
        return json.loads(sql("SELECT jsonb_object_agg(index_name,jsonb_build_object("
                              "'storage_epoch',storage_epoch,'consumer_id',consumer_id,"
                              "'engine_instance',engine_instance)) FROM qdrant_internal.consumer_state"))

    def incarnations():
        return json.loads(sql("SELECT jsonb_object_agg(index_name||':'||(tagged_key->>'value'),incarnation) "
                              "FROM qdrant_internal.source_state WHERE NOT tombstone"))

    def text_keys(query):
        return json.loads(sql("SELECT coalesce(jsonb_agg(source_key ORDER BY rank),'[]') "
                              "FROM qdrant.search('physical_docs','" + query + "')"))

    def provision_native_storage(data, label, database_oid):
        # PG17 basebackup rejects long ordinary-file paths. Derived Edge files
        # live outside PGDATA and are rebuilt, never restored through a symlink.
        target = root / ('native-' + label)
        target.mkdir(mode=0o700)
        ipc = data / 'pg_qdrant_p0'
        ipc.mkdir(mode=0o700, exist_ok=True)
        link = ipc / ('db-' + database_oid + '.indexes')
        assert not link.exists() and not link.is_symlink(), link
        link.symlink_to(target, target_is_directory=True)
        return target

    try:
        command('initdb', '-D', source, '--no-locale', '--encoding=UTF8', '--auth=trust')
        start(source, root / 'source-socket', 55437, 'source')
        sql('CREATE EXTENSION pg_qdrant; CREATE TABLE physical_docs(id bigint PRIMARY KEY,body text NOT NULL)')
        database_oid = sql("SELECT oid FROM pg_database WHERE datname=current_database()")
        source_native = provision_native_storage(source, 'source', database_oid)
        report['build_info'] = json.loads(sql('SELECT qdrant.build_info()'))
        report['postgres_version'] = sql('SHOW server_version')
        sql("INSERT INTO physical_docs VALUES(1,'physical backup old'),(2,'physical backup second'); "
            "SELECT qdrant.create_index('physical_docs','physical_docs','id','{\"text\":{\"fields\":[\"body\"]}}')")
        ready('physical_docs')
        slots, _, model_generation = verify_model_restore.prepare(sql, ready, ticket_from)
        baseline_ticket = ticket_from(sql("BEGIN; UPDATE physical_docs SET body='physical backup second' WHERE id=2; "
                                          "SELECT qdrant.track_changes('physical_docs'); COMMIT"))
        assert json.loads(sql("SELECT qdrant.await_changes('" + baseline_ticket + "',60000)"))['durable']
        report['baseline_ticket'] = baseline_ticket
        report['source_before'] = identities()
        report['source_generation'] = ready('physical_docs')['generation']
        report['model_generation'] = model_generation
        old_incarnations = incarnations()
        label = 'pgq_physical_' + root.name.split('.')[-1]
        backup_env = dict(os.environ, PGAPPNAME=label)
        backup_began = time.monotonic()
        with (artifacts / 'physical-basebackup.log').open('w', encoding='utf-8') as log:
            backup = subprocess.Popen([str(bindir / 'pg_basebackup'), '--pgdata', str(clone),
                                       '--format=plain', '--wal-method=stream', '--checkpoint=fast',
                                       '--max-rate=4M', '--no-password', '--label', label],
                                      stdout=log, stderr=subprocess.STDOUT, env=backup_env)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert backup.poll() is None, 'base backup ended before the concurrent-write cut'
                phase = sql("SELECT coalesce(jsonb_agg(p.phase),'[]') FROM pg_stat_progress_basebackup p "
                            "JOIN pg_stat_activity a USING(pid) WHERE a.application_name='" + label + "'")
                if 'streaming database files' in json.loads(phase):
                    break
                time.sleep(.02)
            else:
                raise AssertionError('base backup did not reach streaming database files')
            report['concurrent_backup_phase'] = json.loads(phase)
            ticket = ticket_from(sql("BEGIN; DELETE FROM physical_docs WHERE id=1; "
                                     "INSERT INTO physical_docs VALUES(1,'physical duringbackup replacement'); "
                                     "UPDATE physical_docs SET body='physical duringbackup update' WHERE id=2; "
                                     "SELECT qdrant.track_changes('physical_docs'); COMMIT"))
            assert backup.poll() is None, 'concurrent source transaction was not inside the hot backup'
            report['backup_ticket'] = ticket
            report['source_ticket'] = json.loads(sql("SELECT qdrant.await_changes('" + ticket + "',60000)"))
            assert report['source_ticket']['durable'], report['source_ticket']
            assert backup.wait(timeout=180) == 0, (artifacts / 'physical-basebackup.log').read_text()
        report['backup_elapsed_seconds'] = time.monotonic() - backup_began
        report['backup_verification'] = command('pg_verifybackup', clone, timeout=60).strip()
        manifest_bytes = (clone / 'backup_manifest').read_bytes()
        report['backup_manifest_sha256'] = hashlib.sha256(manifest_bytes).hexdigest()
        manifest = json.loads(manifest_bytes)
        report['backup_files'] = len(manifest['Files'])
        report['backup_bytes'] = sum(entry['Size'] for entry in manifest['Files'])
        report['source_native_files'] = sum(path.is_file() for path in source_native.rglob('*'))
        report['copied_native_files'] = sum('.indexes/' in entry.get('Path', '')
                                          for entry in manifest['Files'])
        assert report['source_native_files'] > 0 and report['copied_native_files'] == 0
        assert not (clone / 'pg_qdrant_p0' / ('db-' + database_oid + '.indexes')).exists()
        preserved_incarnations = incarnations()
        assert preserved_incarnations['physical_docs:1'] != old_incarnations['physical_docs:1']
        report['source_at_backup'] = identities()
        report['source_incarnations'] = preserved_incarnations
        # This committed write is after pg_basebackup has exited and must stay out of the clone.
        post = ticket_from(sql("BEGIN; INSERT INTO physical_docs VALUES(3,'physical postbackup only'); "
                               "SELECT qdrant.track_changes('physical_docs'); COMMIT"))
        assert json.loads(sql("SELECT qdrant.await_changes('" + post + "',60000)"))['durable']
        checks.append('actual PG17 streaming-WAL base backup overlaps committed source update/delete/key reuse and passes full pg_verifybackup checks')

        clone_native = provision_native_storage(clone, 'clone', database_oid)
        assert clone_native != source_native and not any(clone_native.iterdir())
        start(clone, root / 'clone-socket', 55438, 'clone')
        assert sql('SELECT count(*) FROM physical_docs') == '2'
        assert sql("SELECT count(*) FROM physical_docs WHERE body LIKE '%postbackup%'") == '0'
        assert incarnations() == preserved_incarnations
        report['clone_before_replay'] = identities()
        assert report['clone_before_replay'] == report['source_at_backup']
        assert int(sql('SELECT count(*) FROM qdrant_internal.event_ack')) > 0
        assert '22023' in sql("SELECT qdrant.await_changes('" + post + "',100)", ok=False)
        report['copied_ticket_without_owner'] = json.loads(sql("SELECT qdrant_internal.ticket_status('" + baseline_ticket + "')"))
        copied = report['copied_ticket_without_owner']
        assert copied['pending_events'] == 0 and not copied['durable'] and not copied['applied'], copied
        # The first restored wait must establish a current native owner, not trust old ACKs.
        replay_began = time.monotonic()
        report['clone_ticket'] = json.loads(sql("SELECT qdrant.await_changes('" + ticket + "',60000)"))
        assert report['clone_ticket']['durable'], report['clone_ticket']
        clone_status = ready('physical_docs')
        model_status = ready('restored_models')
        report['replay_elapsed_seconds'] = time.monotonic() - replay_began
        report['clone_after_replay'] = identities()
        assert clone_status['generation'] == report['source_generation']
        assert model_status['generation'] == model_generation
        for index, before in report['source_at_backup'].items():
            after = report['clone_after_replay'][index]
            assert all(after[key] != before[key] for key in ('storage_epoch', 'consumer_id', 'engine_instance')), (index, before, after)
        assert sql("SELECT count(*) FROM qdrant_internal.event_ack a JOIN qdrant_internal.outbox e USING(event_id) "
                   "JOIN qdrant_internal.consumer_state c USING(index_name) "
                   "WHERE a.storage_epoch<>c.storage_epoch OR a.consumer_id<>c.consumer_id OR a.generation<>c.generation") == '0'
        assert sorted(text_keys('duringbackup')) == ['1', '2']
        assert text_keys('old') == [] and text_keys('postbackup') == []
        checks.append('physical clone preserves PG identities/generations but rotates every native owner/epoch/consumer and replays restored tickets before serving correct BM25 results')

        model_results = {}
        for name, contract in slots.items():
            query = dict(model_id=contract['model_id'], model_version=contract['model_version'],
                         vector=verify_model_restore.vector(name, '1'))
            if name == 'learned':
                query.update(vocabulary=contract['vocabulary'], idf_revision=contract['idf_revision'])
            mode = {'dense': 'semantic', 'learned': 'sparse', 'tokens': 'maxsim'}[name]
            model_results[name] = json.loads(sql("SELECT coalesce(jsonb_agg(source_key ORDER BY rank),'[]') "
                                                "FROM qdrant.search('restored_models','backup','" + mode + "',10,'" +
                                                json.dumps({name: query}) + "')"))
            assert model_status['representations'][name]['rows']['ready'] == 2, model_status
            assert model_results[name] and model_results[name][0] == '1', model_results
        report['clone_model_results'] = model_results
        checks.append('physically preserved current BYOV outputs rebuild actual dense/sparse/MaxSim points under the new native owner without inventing new model identities')

        clone_ticket = ticket_from(sql("BEGIN; DELETE FROM physical_docs WHERE id=1; "
                                       "INSERT INTO physical_docs VALUES(1,'physical cloneonly replacement'); "
                                       "SELECT qdrant.track_changes('physical_docs'); COMMIT"))
        report['clone_update_ticket'] = json.loads(sql("SELECT qdrant.await_changes('" + clone_ticket + "',60000)"))
        assert report['clone_update_ticket']['durable'], report['clone_update_ticket']
        assert text_keys('cloneonly') == ['1'] and text_keys('replacement') == ['1']
        assert text_keys('duringbackup') == ['2']
        os.environ.update(PGHOST=str(root / 'source-socket'), PGPORT='55437', PGDATABASE='postgres')
        assert text_keys('cloneonly') == [] and text_keys('postbackup') == ['3']
        assert sorted(text_keys('duringbackup')) == ['1', '2']
        checks.append('restored ordinary DML and key reuse reach fresh durable native receipts while the concurrently running original cluster remains independent')
        success = True
    finally:
        active_failure = sys.exc_info()[0] is not None
        if backup is not None and backup.poll() is None:
            backup.terminate()
            try:
                backup.wait(timeout=10)
            except subprocess.TimeoutExpired:
                backup.kill()
                backup.wait(timeout=10)
        stop_results = []
        for data in reversed(started):
            try:
                result = subprocess.run([str(bindir / 'pg_ctl'), '-D', str(data), '-m', 'immediate', '-w', 'stop'],
                                        capture_output=True, text=True, timeout=30)
                stop_results.append({'cluster': str(data), 'returncode': result.returncode,
                                     'stdout': result.stdout, 'stderr': result.stderr})
            except subprocess.TimeoutExpired:
                stop_results.append({'cluster': str(data), 'returncode': -1, 'error': 'stop deadline exceeded'})
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        report['stop_results'] = stop_results
        report['elapsed_seconds'] = time.monotonic() - began
        success = success and all(result['returncode'] == 0 for result in stop_results)
        report['status'] = 'passed' if success else 'failed'
        (artifacts / 'physical-restore-results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        if success:
            assert root.resolve().parent == Path('/tmp') and root.name.startswith('pgq-physical.')
            shutil.rmtree(root)
        else:
            print('Failed physical recovery clusters retained at ' + str(root), flush=True)
        if not active_failure:
            assert success, report

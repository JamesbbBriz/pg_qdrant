#!/usr/bin/env python3
"""Real ENOSPC in disposable Edge storage, with PostgreSQL WAL on another device."""
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

if not __debug__:
    raise RuntimeError('optimized Python is unsupported for guarded fault tests')

PSQL = ['psql', '-X', '-A', '-t', '-q', '-v', 'ON_ERROR_STOP=1']

def sql(query):
    result = subprocess.run(PSQL + ['-c', query], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()

def status():
    return json.loads(sql("SELECT qdrant.index_status('disk')"))

def ready():
    value = status()
    return value if value['engine_index_ready'] and value['pending_events'] == 0 else None

def failure():
    value = status()
    return value if value['state'] == 'failed' or value['owner_status']['helper_restart_exhausted'] else None

def wait_for(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(.02)
    raise AssertionError(('bounded disk observation timed out', last))

data = Path(os.environ['PG_QDRANT_DISPOSABLE_DATA']).resolve()
disk = Path(os.environ['PG_QDRANT_P1_DISK_DIR']).resolve()
assert os.getuid() != 0
assert data.parent.parent == Path('/tmp') and data.parent.name.startswith('pgq-p1-product.')
assert Path(sql('SHOW data_directory')).resolve() == data
assert disk == Path('/pgq-p1-faults') and disk.stat().st_uid == os.getuid()
mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
mount = next(row for row in mounts if row[4] == str(disk))
assert mount[mount.index('-') + 1] == 'tmpfs'
capacity = os.statvfs(disk)
capacity_bytes = capacity.f_blocks * capacity.f_frsize
assert capacity_bytes == 384 * 1024 * 1024 and not list(disk.iterdir())
engine_root = disk / 'indexes'
engine_root.mkdir(mode=0o700)
ipc = data / 'pg_qdrant_p0'
ipc.mkdir(mode=0o700)
database_oid = sql("SELECT oid FROM pg_database WHERE datname=current_database()")
(ipc / ('db-' + database_oid + '.indexes')).symlink_to(engine_root, target_is_directory=True)
assert os.stat(data).st_dev != os.stat(engine_root).st_dev

sql('CREATE EXTENSION pg_qdrant')
sql('CREATE TABLE disk(id bigint PRIMARY KEY,body text NOT NULL)')
sql("INSERT INTO disk VALUES(1,'diskinitial recovery')")
sql("SELECT qdrant.create_index('disk','disk','id','{\"text\":{\"fields\":[\"body\"]}}')")
before = wait_for(ready)
pid = before['owner_status']['engine_pid']
index_id = sql("SELECT index_id FROM qdrant_internal.index_catalog WHERE index_name='disk'")
old_path = engine_root / ('%s-%s-%s' % (index_id, before['generation'], before['storage_epoch']))
assert old_path.is_dir()
ballast = disk / 'owned-ballast'
written = 0
stopped = False
try:
    os.kill(pid, signal.SIGSTOP)
    stopped = True
    with ballast.open('xb', buffering=0) as stream:
        block = b'\x00' * (1024 * 1024)
        while written <= capacity_bytes:
            try:
                written += stream.write(block)
            except OSError as error:
                assert error.errno == errno.ENOSPC, error
                break
        else:
            raise AssertionError('dedicated tmpfs did not reach ENOSPC')
    assert os.statvfs(disk).f_bavail == 0
    output = sql("BEGIN; UPDATE disk SET body=repeat('diskchanged ',5000) WHERE id=1; SELECT qdrant.track_changes('disk'); COMMIT")
    ticket = next(line for line in output.splitlines() if len(line) == 36 and line.count('-') == 4)
    pending = json.loads(sql("SELECT qdrant.await_changes('" + ticket + "',100)"))
    assert not pending['durable'] and pending['pending_events'] > 0, pending
    os.kill(pid, signal.SIGCONT)
    stopped = False
    failed = wait_for(failure)
    # A full tmpfs can fault a mapped native page with SIGBUS instead of
    # returning an I/O error. Require the actual error/exit, never only timeout.
    native_exit = failed['owner_status'].get('helper_last_exit') or {}
    assert 'space' in (failed['last_error'] or '').lower() or native_exit.get('signal') == signal.SIGBUS, failed
    refused = json.loads(sql("SELECT qdrant.await_changes('" + ticket + "',100)"))
    assert refused['committed'] and not refused['durable'] and refused['pending_events'] > 0, refused
    assert sql("SELECT body LIKE 'diskchanged %' FROM disk WHERE id=1") == 't'
    assert old_path.is_dir()
finally:
    if stopped:
        os.kill(pid, signal.SIGCONT)
    if ballast.exists():
        ballast.unlink()

# Preserve failed storage; a replacement owner rebuilds from committed facts.
# Exhausted restart budgets require explicit supervisor recovery. SIGTERM
# stops this disposable database's worker without crashing PostgreSQL.
# The snapshot's helper can already have died from SIGBUS. Restart the
# surviving supervisor instead of signalling a stale/reused helper PID.
worker_pid = failed['owner_status']['worker_pid']
assert worker_pid == before['owner_status']['worker_pid']
assert sql("SELECT count(*) FROM pg_stat_activity WHERE pid=" + str(worker_pid)
           + " AND backend_type='pg_qdrant P0 owner'") == '1'
os.kill(worker_pid, signal.SIGTERM)
wait_for(lambda: sql('SELECT count(*) FROM pg_stat_activity WHERE pid=' + str(worker_pid)) == '0')
durable = json.loads(sql("SELECT qdrant.await_changes('" + ticket + "',60000)"))
after = status()
assert durable['durable'] and durable['pending_events'] == 0, durable
assert after['storage_epoch'] != before['storage_epoch'] and after['engine_instance'] != before['engine_instance']
assert old_path.is_dir(), 'failed generation must be retained for recovery inspection'
assert sql("SELECT source_key FROM qdrant.search('disk','diskchanged')") == '1'
assert sql("SELECT count(*) FROM qdrant.search('disk','diskinitial')") == '0'
print(json.dumps({'status': 'passed', 'cut': 'actual_edge_storage_enospc',
    'source_revision': os.environ.get('PG_QDRANT_SOURCE_SHA', 'unrecorded'),
    'cargo_lock_sha256': hashlib.sha256(Path('/src/Cargo.lock').read_bytes()).hexdigest(),
    'tmpfs_capacity_bytes': capacity_bytes, 'ballast_bytes': written,
    'postgres_and_edge_separate_devices': True, 'before': before, 'failed': failed,
    'ticket_pending': pending, 'ticket_refused': refused, 'after': after,
    'ticket_durable': durable, 'failed_storage_preserved': True,
    'final_source_keys': ['1'], 'release_supported': False}, indent=2))

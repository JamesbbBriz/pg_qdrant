#!/usr/bin/env python3
"""Positive CI-only OOM observations in a fresh owned PostgreSQL cluster.

This driver cannot attest a kernel victim by itself. The outer inspected Docker
runner releases the native barrier and performs the separate attribution gate.
"""

if not __debug__:
    raise RuntimeError("optimized Python is unsupported for guarded P0 experiments")

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time


PSQL = os.environ["PG_QDRANT_PSQL"]
ARTIFACTS = Path(os.environ["PG_QDRANT_ARTIFACT_DIR"])
DATA = Path(os.environ["PG_QDRANT_DISPOSABLE_DATA"])
NONCE = os.environ["PG_QDRANT_P0_OOM_RUN_ID"]
HELPER = os.environ.get("PG_QDRANT_MANAGED_HELPER") == "1"
PROFILE = "managed_helper" if HELPER else "direct_worker"
CGROUP = Path("/sys/fs/cgroup")
DIRECTORY = Path(f"/tmp/pgq-p0-oom-{NONCE}")
REPORT = {"schema_version": 1, "kind": "p0_kernel_oom_sql", "status": "running",
          "profile": PROFILE, "nonce": NONCE, "stage": "preflight",
          "kernel_oom_observed": False, "victim_attribution": "none",
          "target_isolation_gate_passed": False, "production_memory_isolation_verified": False,
          "unflushed_edge_recovery_verified": False, "postgres_edge_atomicity_verified": False,
          "caller_client_kill_requested": False, "companion_client_kill_requested": False}
PROCESSES = []


def bounded_text(path, limit=65536):
    with Path(path).open("rb") as stream:
        value = stream.read(limit + 1)
    assert len(value) <= limit, f"bounded observation exceeded: {path}"
    return value.decode()


def command(sql):
    return [PSQL, "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-c", sql]


def run(sql, timeout=3, check=True):
    result = subprocess.run(command(sql), capture_output=True, text=True, timeout=timeout)
    if check:
        assert result.returncode == 0, result.stderr[-8192:]
    return result


def scalar(sql):
    return run(sql).stdout.strip()


def jsql(sql, timeout=5):
    return json.loads(run(sql, timeout=timeout).stdout)


def spawn(sql, role):
    child = subprocess.Popen(command(sql), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    PROCESSES.append((role, child))
    return child


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("bounded observation timed out")


def identity(pid):
    proc = Path(f"/proc/{int(pid)}")
    uid = proc.stat().st_uid
    fields = bounded_text(proc / "stat").rsplit(")", 1)[1].split()
    return {"pid": int(pid), "start_ticks": int(fields[19]), "uid": uid,
            "state": fields[0], "parent_pid": int(fields[1]),
            "cgroup": bounded_text(proc / "cgroup").strip(),
            **{name + "_namespace": os.readlink(proc / "ns" / name) for name in ("pid", "ipc", "cgroup")}}


def same(before, after):
    return before["pid"] == after["pid"] and before["start_ticks"] == after["start_ticks"]


def gone(before):
    try:
        current = identity(before["pid"])
        return not same(before, current) or current["state"] == "Z"
    except (FileNotFoundError, ProcessLookupError):
        return True


def counters():
    return {key: int(value) for key, value in
            (line.split() for line in bounded_text(CGROUP / "memory.events.local").splitlines())}


def cgroup_identity():
    lines = bounded_text("/proc/self/mountinfo").splitlines()
    matches = [line for line in lines if line.split()[4] == str(CGROUP) and " - cgroup2 " in line]
    assert len(matches) == 1
    metadata = CGROUP.stat()
    return {"device": metadata.st_dev, "inode": metadata.st_ino, "mount_id": matches[0].split()[0]}


def write_owned(path, value):
    path = Path(path)
    pending = path.with_name(path.name + ".pending")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(pending, path, follow_symlinks=False)
    finally:
        pending.unlink()


def native_lines():
    path = DIRECTORY / "native.jsonl"
    if not path.exists():
        return []
    text = bounded_text(path)
    # A process can die during its final write; retain complete records only.
    lines = text.splitlines(keepends=True)
    return [json.loads(line) for line in lines if line.endswith("\n")]


def observe():
    assert os.geteuid() == 10001 and re.fullmatch(r"[a-f0-9]{32}", NONCE)
    assert DATA.resolve() == DATA and str(DATA).startswith("/tmp/pgq-p0-oom-cluster.")
    assert Path(scalar("SHOW data_directory")).resolve() == DATA
    assert scalar("SHOW fsync") == "on" and scalar("SHOW synchronous_commit") == "on"
    assert int(scalar("SHOW server_version_num")) // 10000 == 17
    run("CREATE EXTENSION pg_qdrant")
    build = jsql("SELECT qdrant.build_info()")
    assert build["features"]["p0_fault_injection"] is True
    assert build["features"]["p0_managed_helper"] is HELPER
    assert build["native_engine_cancellation"] is False
    run("CREATE TABLE p0_oom_marker(id integer PRIMARY KEY, value text); "
        "INSERT INTO p0_oom_marker VALUES(1, 'committed before native OOM')")
    if HELPER:
        run("CREATE TABLE p1_oom_source(id bigint PRIMARY KEY,body text NOT NULL); "
            "INSERT INTO p1_oom_source VALUES(1,'oomoriginal recovery'); "
            "SELECT qdrant.create_index('oom_source','p1_oom_source','id','{\"text\":{\"fields\":[\"body\"]}}')", timeout=8)
        def source_ready():
            value = jsql("SELECT qdrant.index_status('oom_source')", timeout=8)
            return value if value['engine_index_ready'] and value['pending_events'] == 0 else None
        REPORT['source_before'] = wait_for(source_ready, seconds=15)
    state = jsql("SELECT qdrant_internal.p0_start_worker(5000)", timeout=8)
    assert state["engine_ready"] and state["active"] is None
    postmaster = identity(int(bounded_text(DATA / "postmaster.pid").splitlines()[0]))
    target = identity(state["engine_pid"])
    supervisor = identity(state["worker_pid"])
    controller = identity(os.getpid())
    companion_started = time.monotonic()
    companion = spawn("SET application_name='pgq_p0_oom_companion'; BEGIN; SELECT pg_sleep(30); COMMIT", "companion")
    companion_pid = int(wait_for(lambda: scalar("SELECT pid FROM pg_stat_activity "
        "WHERE application_name='pgq_p0_oom_companion' AND state='active' AND wait_event='PgSleep'")))
    companion_identity = identity(companion_pid)
    for process in [postmaster, target, supervisor, controller, companion_identity]:
        assert process["uid"] == 10001 and process["state"] != "Z" and process["cgroup"] == "0::/"
    namespaces = {key: target[key] for key in ("pid_namespace", "ipc_namespace", "cgroup_namespace")}
    marker = {"schema_version": 1, "nonce": NONCE, "profile": PROFILE,
              "target": target, "supervisor": supervisor, "controller": controller,
              "postmaster": postmaster, "companion": companion_identity,
              "namespaces": namespaces, "cgroup": cgroup_identity(), "data_dir": str(DATA),
              "postgresql_marker_verified": scalar("SELECT value FROM p0_oom_marker WHERE id=1") == "committed before native OOM"}
    DIRECTORY.mkdir(mode=0o700)
    write_owned(DIRECTORY / "arm.json", marker)
    REPORT.update(stage="native_guard", identities=marker, build=build, counters_before=counters())
    caller = spawn("SET application_name='pgq_p0_oom_caller'; SELECT qdrant_internal.p0_fault('oom',30000)", "caller")

    def armed():
        records = native_lines()
        if records:
            assert records[0]["event"] == "armed"
            return records[0]
        if caller.poll() is not None:
            stdout, stderr = caller.communicate(timeout=1)
            REPORT["early_native_result"] = {"returncode": caller.returncode,
                "stdout": stdout[-16384:], "stderr": stderr[-16384:]}
            raise AssertionError("native guard did not arm; positive allocation was not established")
        return None

    armed_record = wait_for(armed)
    caller_pid = int(scalar("SELECT pid FROM pg_stat_activity WHERE application_name='pgq_p0_oom_caller' AND state='active'"))
    REPORT['caller_backend'] = identity(caller_pid)
    assert armed_record["allocation_started"] is False
    assert armed_record["target"] == target and armed_record["requested_bytes_cap"] == 805306368
    assert companion.poll() is None and same(companion_identity, identity(companion_pid))
    if HELPER:
        # Commit while the exact native owner is held at its pre-allocation
        # barrier. The pending source event must survive the kernel victim.
        output = scalar("BEGIN; DELETE FROM p1_oom_source WHERE id=1; "
            "INSERT INTO p1_oom_source VALUES(1,'oomreplacement recovery'); "
            "SELECT qdrant.track_changes('oom_source'); COMMIT")
        ticket = next(line for line in output.splitlines() if len(line) == 36 and line.count('-') == 4)
        pending = jsql("SELECT qdrant_internal.ticket_status('" + ticket + "')")
        assert pending['committed'] and not pending['durable'] and pending['pending_events'] == 2, pending
        REPORT.update(source_ticket=ticket, source_ticket_pending=pending)
    REPORT["armed"] = armed_record
    REPORT["stage"] = "outer_barrier_and_kernel_event"
    write_owned(ARTIFACTS / "oom-ready.json", {"schema_version": 1, "nonce": NONCE,
        "profile": PROFILE, "target": target, "cgroup": marker["cgroup"], "armed": armed_record})
    started = time.monotonic()
    samples = []
    event_observed = False
    overlap = False
    deadline = started + 30
    while time.monotonic() < deadline:
        current = counters()
        samples.append({"after_ms": int((time.monotonic()-started)*1000),
                        "memory_current": int(bounded_text(CGROUP / "memory.current"))})
        records = native_lines()
        boundaries = [record for record in records if record.get("event") == "allocation_started"]
        if boundaries:
            assert len(boundaries) == 1
            # Native guard rechecks this exact live identity immediately before
            # allocation. The 25 s observation bound is below the already
            # verified 30 s PgSleep, even when direct-worker OOM recovery has
            # already removed the companion before this polling iteration.
            overlap = (boundaries[0].get("participants_verified_at_barrier") is True
                and boundaries[0].get("companion_identity_verified") == companion_identity
                and time.monotonic() - companion_started < 25)
        if current["oom_kill"] > REPORT["counters_before"]["oom_kill"]:
            event_observed = True
            if gone(target):
                break
        if caller.poll() is not None and not event_observed:
            break
        time.sleep(0.05)
    REPORT["memory_samples"] = samples
    REPORT["counters_after"] = counters()
    delta = {key: REPORT["counters_after"][key] - value for key, value in REPORT["counters_before"].items()}
    REPORT["counter_delta"] = delta
    REPORT["native_records"] = native_lines()
    REPORT["companion_overlapped_allocation"] = overlap
    REPORT["companion_overlap_evidence"] = "observed_PgSleep_then_exact_live_identity_at_native_boundary_within_25s_of_30s_sleep"
    REPORT["kernel_oom_observed"] = all(delta.get(key, 0) > 0 for key in ("max", "oom", "oom_kill"))
    assert REPORT["kernel_oom_observed"] and delta["oom_kill"] == 1 and delta.get("oom_group_kill", 0) == 0
    assert overlap and gone(target), "target exit or overlapping companion was not established"
    stdout, stderr = caller.communicate(timeout=5)
    REPORT["faulting_sql"] = {"returncode": caller.returncode, "stdout": stdout[-16384:], "stderr": stderr[-16384:]}
    assert caller.returncode != 0, "a successful SQL response is not an OOM death result"
    REPORT["stage"] = "postgresql_recovery"

    def ready():
        try:
            return run("SELECT 1", timeout=1, check=False).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    wait_for(ready, seconds=20)
    assert same(postmaster, identity(postmaster["pid"]))
    assert scalar("SELECT value FROM p0_oom_marker WHERE id=1") == "committed before native OOM"
    replacement = jsql("SELECT qdrant_internal.p0_start_worker(5000)", timeout=8)
    REPORT["replacement"] = replacement
    assert replacement["engine_ready"] and replacement["engine_pid"] != target["pid"]
    if HELPER:
        assert replacement["worker_pid"] == supervisor["pid"] and same(supervisor, identity(supervisor["pid"]))
        last_exit = replacement["helper_last_exit"]
        REPORT["target_exit"] = last_exit
        assert last_exit["engine_pid"] == target["pid"] and last_exit["signal"] == signal.SIGKILL
        assert last_exit["supervisor_kill_requested"] is False and last_exit["kill_attempts"] == 0
        assert last_exit["stop_reason"] == "unexpected_exit"
        if replacement["helper_start_attempts"] == 1:
            # An exact durable source batch resets the consecutive-failure
            # budget. Verify progress from the replacement execution identity.
            progressed = source_ready()
            assert progressed and progressed['engine_instance'] == replacement['engine_instance']
            assert progressed['storage_epoch'] != REPORT['source_before']['storage_epoch']
        else:
            assert replacement["helper_start_attempts"] == 2
    else:
        assert replacement["worker_pid"] != supervisor["pid"]
        log = bounded_text(ARTIFACTS / "postgresql.log", limit=256*1024)
        target_logged = bool(re.search(rf"\(PID {target['pid']}\).*terminated by signal 9", log))
        caller_logged = bool(re.search(rf"\(PID {caller_pid}\).*terminated by signal 9", log))
        # SIGCHLD ordering can report the faulting SQL backend first after
        # worker death. Exact native victim attribution still belongs to the
        # outer kernel PID/start-ticks/cgroup gate, never this collateral log.
        assert target_logged or caller_logged
        assert "reinitializing" in log
        REPORT["postgresql_crash_log"] = {'target_logged': target_logged, 'caller_logged': caller_logged,
            'caller_pid': caller_pid, 'reinitializing': True}
        REPORT["target_exit"] = {"engine_pid": target["pid"], "signal": 9 if target_logged else None,
            "supervisor_kill_requested": False, "kill_attempts": 0,
            "source": "exact_postgresql_child_exit_log" if target_logged else "requires_exact_kernel_victim_record"}
    companion_out, companion_err = companion.communicate(timeout=35)
    REPORT["companion_result"] = {"returncode": companion.returncode,
        "stdout": companion_out[-4096:], "stderr": companion_err[-8192:]}
    assert (companion.returncode == 0) is HELPER
    REPORT.update(companion_survived=HELPER, supervisor_preserved=HELPER,
                  postmaster_preserved=True, committed_marker_retained=True,
                  recovery_ms=int((time.monotonic()-started)*1000))
    if HELPER:
        durable = jsql("SELECT qdrant.await_changes('" + ticket + "',15000)", timeout=18)
        after = source_ready()
        assert durable['durable'] and durable['pending_events'] == 0 and after, durable
        assert after['storage_epoch'] != REPORT['source_before']['storage_epoch']
        assert after['engine_instance'] != REPORT['source_before']['engine_instance']
        assert scalar("SELECT source_key FROM qdrant.search('oom_source','oomreplacement')") == '1'
        assert scalar("SELECT count(*) FROM qdrant.search('oom_source','oomoriginal')") == '0'
        # Verify obsolete incarnation cleanup before the SQL source JOIN.
        native = jsql("SELECT qdrant_internal.p1_search((SELECT jsonb_build_object('operation','source_search',"
            "'index_id',i.index_id,'generation',i.generation,'storage_epoch',c.storage_epoch,"
            "'q','oomoriginal','top_k',10) FROM qdrant_internal.index_catalog i "
            "JOIN qdrant_internal.consumer_state c USING(index_name) WHERE i.index_name='oom_source'),30000)",timeout=8)
        assert native == [], native
        REPORT.update(source_after=after, source_ticket_durable=durable,
            source_ledger_recovery_verified=True, obsolete_native_incarnation_removed=True,
            source_final_keys=['1'])
    smoke = jsql("SELECT qdrant_internal.p0_engine_probe(120000)", timeout=135)
    assert smoke["status"] == "passed" and smoke["engine_version"] == "0.8.0"
    write_owned(ARTIFACTS / "oom-replacement-engine.json", smoke)
    REPORT["counters_after_replacement_smoke"] = counters()
    assert REPORT["counters_after_replacement_smoke"]["oom_kill"] == REPORT["counters_after"]["oom_kill"]
    assert REPORT["counters_after_replacement_smoke"]["oom_group_kill"] == REPORT["counters_after"]["oom_group_kill"]
    REPORT.update(stage="complete", status="observed", replacement_edge_smoke_passed=True,
                  victim_attribution="correlated_only", target_isolation_gate_passed=False)


if __name__ == "__main__":
    try:
        observe()
    except Exception as error:
        REPORT.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        try:
            for role, child in PROCESSES:
                try:
                    if child.poll() is None:
                        REPORT[role + "_client_kill_requested"] = True
                        child.kill()
                    child.wait(timeout=3)
                except Exception as error:
                    REPORT.update(status="failed", cleanup_error=f"{role}: {type(error).__name__}: {error}")
            if (DIRECTORY / "native.jsonl").exists():
                shutil.copyfile(DIRECTORY / "native.jsonl", ARTIFACTS / "oom-native.jsonl")
        except Exception as error:
            REPORT.update(status="failed", collection_error=f"{type(error).__name__}: {error}")
        finally:
            (ARTIFACTS / "oom-sql.json").write_text(json.dumps(REPORT, indent=2) + "\n")
            print(json.dumps(REPORT), flush=True)

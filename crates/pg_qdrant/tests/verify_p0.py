#!/usr/bin/env python3
"""Assertions for the disposable PostgreSQL 17 P0 cluster; no Python packages."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import traceback


PSQL = os.environ["PG_QDRANT_PSQL"]
ARTIFACTS = Path(os.environ["PG_QDRANT_ARTIFACT_DIR"])
FAULTS = os.environ.get("PG_QDRANT_FAULT_TESTS") == "1"
HELPER = os.environ.get("PG_QDRANT_MANAGED_HELPER") == "1"
REPORT = {"schema_version": 1, "stage": "P0", "status": "running", "checks": [],
          "process_profile": "managed_helper" if HELPER else "direct_worker"}


def command(sql):
    return [PSQL, "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-c", sql]


def run(sql, timeout=15, check=True):
    result = subprocess.run(command(sql), text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise AssertionError(f"SQL failed: {sql}\n{result.stderr}")
    return result


def spawn(sql):
    return subprocess.Popen(command(sql), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def finish(process, timeout=15, expected_error=None):
    out, err = process.communicate(timeout=timeout)
    if expected_error:
        assert process.returncode != 0 and re.search(rf"\b{expected_error}\b", err), (out, err)
    else:
        assert process.returncode == 0, (out, err)
    return out.strip(), err


def scalar(sql):
    return run(sql).stdout.strip()


def jsql(sql, timeout=15):
    return json.loads(run(sql, timeout).stdout.strip())


def ping():
    return jsql("SELECT qdrant_internal.p0_ping(1000)")


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition did not become true within its bounded wait")


def idle():
    def is_idle():
        state = ping()
        return state if state["active"] is None and state["queue_depth"] == 0 and state["engine_ready"] else None
    return wait_for(is_idle)


def record(name, **details):
    REPORT["checks"].append({"name": name, "status": "passed", **details})
    print(json.dumps(REPORT["checks"][-1]), flush=True)


def expect_error(sql, state):
    result = run(sql, check=False)
    assert result.returncode != 0 and re.search(rf"\b{state}\b", result.stderr), result.stderr


def cancel_application(name):
    # Test application names are hardcoded below, not supplied by a user.
    pid = wait_for(lambda: scalar(
        "SELECT pid FROM pg_stat_activity "
        f"WHERE application_name = '{name}' AND state = 'active'"
    ))
    assert scalar(f"SELECT pg_cancel_backend({int(pid)})") == "t"


def exercise():
    assert int(scalar("SHOW server_version_num")) // 10000 == 17
    run("CREATE EXTENSION pg_qdrant")
    actual_data = scalar("SHOW data_directory")
    assert Path(actual_data).resolve() == Path(os.environ["PG_QDRANT_DISPOSABLE_DATA"]).resolve()

    build = jsql("SELECT qdrant.build_info()")
    assert build["product_indexing_api"] is False
    assert build["native_engine_cancellation"] is False
    assert build["engine"] == {"name": "qdrant-edge", "version": "0.8.0"}
    assert build["features"]["p0_fault_injection"] is FAULTS
    assert build["features"]["p0_managed_helper"] is HELPER
    capabilities = jsql("SELECT qdrant.capabilities()")
    expected = {f"{prefix}{i:02}" for prefix, count in [("F", 20), ("V", 8), ("Q", 14), ("L", 12)] for i in range(1, count + 1)}
    assert {row["id"] for row in capabilities["capabilities"]} == expected
    assert all(not row["release_supported"] for row in capabilities["capabilities"])
    expect_error("SELECT qdrant.capabilities('missing')", "0A000")
    assert scalar("SELECT bool_and(provolatile='v' AND proparallel='u' AND NOT prosecdef) "
                  "FROM pg_proc WHERE pronamespace IN ('qdrant'::regnamespace,'qdrant_internal'::regnamespace)") == "t"
    record("install_and_honest_capabilities", count=54, postgres_major=17)

    starts = [spawn("SELECT qdrant_internal.p0_start_worker(5000)") for _ in range(2)]
    owners = [json.loads(finish(process)[0])["worker_pid"] for process in starts]
    assert len(set(owners)) == 1
    owner_pid = owners[0]
    state = ping()
    assert state["worker_pid"] == owner_pid and state["engine_ready"]
    assert (state["engine_pid"] != owner_pid) is HELPER
    if HELPER:
        assert scalar(f"SELECT count(*) FROM pg_stat_activity WHERE pid={state['engine_pid']}") == "0"
    record("concurrent_start_unique_owner", worker_pid=owner_pid, engine_pid=state["engine_pid"])

    run("CREATE ROLE pgq_p0_unprivileged")
    expect_error("SET ROLE pgq_p0_unprivileged; SELECT qdrant_internal.p0_ping(1000)", "42501")
    # Verify runtime authorization independently of schema/function ACLs.
    run("GRANT USAGE ON SCHEMA qdrant_internal TO pgq_p0_unprivileged; "
        "GRANT EXECUTE ON FUNCTION qdrant_internal.p0_ping(integer) TO pgq_p0_unprivileged")
    expect_error("SET ROLE pgq_p0_unprivileged; SELECT qdrant_internal.p0_ping(1000)", "42501")
    run("DROP OWNED BY pgq_p0_unprivileged; DROP ROLE pgq_p0_unprivileged")
    expect_error("SELECT qdrant_internal.p0_delay(-1,1000)", "22023")
    expect_error("SELECT qdrant_internal.p0_ping(0)", "22023")
    record("acl_runtime_superuser_and_input_validation")

    begin = time.monotonic()
    concurrent = [spawn("SELECT qdrant_internal.p0_delay(250,5000)") for _ in range(2)]
    for process in concurrent:
        assert json.loads(finish(process)[0])["completed_delay_ms"] == 250
    elapsed = time.monotonic() - begin
    assert 0.45 <= elapsed < 8
    assert ping()["worker_pid"] == owner_pid
    record("two_sql_sessions_single_execution_owner", elapsed_seconds=elapsed)

    idle()
    process = spawn("SET application_name='pgq_p0_cancel_active'; "
                    "SELECT qdrant_internal.p0_delay(1600,5000)")
    wait_for(lambda: ping()["active"] is not None)
    begin = time.monotonic()
    cancel_application("pgq_p0_cancel_active")
    finish(process, expected_error="57014")
    elapsed = time.monotonic() - begin
    assert elapsed < 1.2
    state = ping()
    assert state["active"] is not None and state["worker_pid"] == owner_pid
    idle()
    record("caller_cancel_retains_active_owner", cancel_seconds=elapsed, native_call_cancelled=False)

    begin = time.monotonic()
    expect_error("SELECT qdrant_internal.p0_delay(800,60)", "57014")
    elapsed = time.monotonic() - begin
    assert elapsed < 0.7 and ping()["active"] is not None
    idle()
    # PostgreSQL's own timeout must interrupt the same wait path.
    expect_error("SET statement_timeout='60ms'; SELECT qdrant_internal.p0_delay(800,5000)", "57014")
    idle()
    record("request_deadline_and_statement_timeout", request_timeout_seconds=elapsed)

    baseline = idle()["completed"]
    blocker = spawn("SELECT qdrant_internal.p0_delay(1600,5000)")
    wait_for(lambda: ping()["active"] is not None)
    queued = spawn("SET application_name='pgq_p0_cancel_queued'; SELECT qdrant_internal.p0_delay(50,5000)")
    wait_for(lambda: ping()["queue_depth"] >= 1)
    cancel_application("pgq_p0_cancel_queued")
    finish(queued, expected_error="57014")
    finish(blocker)
    assert idle()["completed"] == baseline + 1
    record("cancelled_queued_request_never_executes")

    baseline = idle()
    blocker = spawn("SELECT qdrant_internal.p0_delay(1800,7000)")
    wait_for(lambda: ping()["active"] is not None)
    saturated = [spawn("SELECT qdrant_internal.p0_delay(10,7000)") for _ in range(10)]
    outcomes = []
    for process in saturated:
        out, err = process.communicate(timeout=12)
        if process.returncode:
            assert re.search(r"\b53400\b", err), err
            outcomes.append("rejected")
        else:
            assert json.loads(out)["completed_delay_ms"] == 10
            outcomes.append("completed")
    finish(blocker)
    assert outcomes.count("rejected") >= 2
    final = idle()
    assert final["rejected"] >= baseline["rejected"] + 2
    record("bounded_queue_admission", outcomes=outcomes, queue_limit=final["queue_limit"])

    engine = jsql("SELECT qdrant_internal.p0_engine_probe(120000)", timeout=135)
    assert engine["status"] == "passed" and engine["engine_version"] == "0.8.0"
    assert engine["kind"] == "synthetic_engine_smoke" and engine["checks"]
    (ARTIFACTS / "sql-engine-probe.json").write_text(json.dumps(engine, indent=2) + "\n")
    record("real_sql_to_edge_smoke", checks=len(engine["checks"]))

    if FAULTS:
        exercise_faults()
    else:
        assert scalar("SELECT to_regprocedure('qdrant_internal.p0_fault(text,integer)') IS NULL") == "t"
        record("fault_entrypoint_absent_in_normal_build")


def wait_recovery():
    def ready():
        try:
            return run("SELECT 1", timeout=2, check=False).returncode == 0
        except subprocess.TimeoutExpired:
            return False
    wait_for(ready, seconds=30)


def spectator():
    delay = 2 if HELPER else 30
    process = spawn(f"SET application_name='pgq_p0_spectator'; BEGIN; SELECT pg_sleep({delay}); COMMIT")
    wait_for(lambda: scalar("SELECT count(*) FROM pg_stat_activity WHERE application_name='pgq_p0_spectator' AND state='active'") == "1")
    return process


def exercise_faults():
    assert FAULTS
    run("CREATE TABLE p0_recovery_marker(id integer PRIMARY KEY, value text); "
        "INSERT INTO p0_recovery_marker VALUES(1,'committed before worker failure')")
    owner_before = ping()["worker_pid"]
    expect_error("SELECT qdrant_internal.p0_fault('panic',5000)", "XX000")
    assert idle()["worker_pid"] == owner_before
    record("caught_engine_thread_panic", worker_survived=True)

    for kind in ["sigkill", "abort"]:
        state_before = idle()
        owner_before = state_before["worker_pid"]
        engine_before = state_before["engine_pid"]
        observer = spectator()
        if kind == "sigkill":
            os.kill(engine_before, signal.SIGKILL)
        else:
            outcome = run("SELECT qdrant_internal.p0_fault('abort',5000)", check=False)
            assert outcome.returncode != 0
        out, err = observer.communicate(timeout=15)
        collateral = observer.returncode != 0
        if HELPER:
            assert not collateral, ("helper death must preserve companion SQL session", out, err)
        else:
            assert collateral, "expected PG17 shared-memory worker crash to terminate the companion session"
        wait_recovery()
        assert scalar("SELECT value FROM p0_recovery_marker WHERE id=1") == "committed before worker failure"
        state = jsql("SELECT qdrant_internal.p0_start_worker(5000)")
        if HELPER:
            assert state["worker_pid"] == owner_before and state["engine_pid"] != engine_before
            last_exit = state["helper_last_exit"]
            assert last_exit["engine_pid"] == engine_before
            assert last_exit["signal"] == (signal.SIGKILL if kind == "sigkill" else signal.SIGABRT)
            if kind == "sigkill":
                # The idle helper has no in-flight pipe response to clean up.
                assert last_exit["stop_reason"] == "unexpected_exit"
                assert last_exit["supervisor_kill_requested"] is False
                assert last_exit["kill_attempts"] == 0
                assert last_exit["first_kill_attempt"] is None
        else:
            assert state["worker_pid"] != owner_before
        record(f"{'helper' if HELPER else 'worker'}_{kind}_collateral_recovery", companion_session_terminated=collateral,
               committed_postgresql_marker_retained=True, original_worker_pid=owner_before,
               replacement_worker_pid=state["worker_pid"], original_engine_pid=engine_before,
               replacement_engine_pid=state["engine_pid"])

    if HELPER:
        exercise_helper_operation_budget()
        exercise_helper_parent_exit()
        exercise_helper_supervisor_sigkill()
        exercise_helper_restart_exhaustion()

    REPORT["unexecuted_fault_gates"] = [
        {"name": "oom", "status": "not_run", "reason": "requires an isolated memory cgroup and recorded victim/recovery evidence"},
        {"name": "disk_full", "status": "not_run", "reason": "requires a bounded fault filesystem; never fill the host filesystem"},
        {"name": "engine_disk_corruption", "status": "not_run", "reason": "requires durable generation/replay integration beyond the disposable probe"},
    ]
    REPORT["background_worker_failure_isolation"] = (
        "native_engine_helper_failure_preserved_companion_sessions_in_this_probe" if HELPER else
        "not_sufficient_for_no_collateral_session_failure")


def exercise_helper_parent_exit():
    state = idle()
    worker_pid, engine_pid = state["worker_pid"], state["engine_pid"]
    active = spawn("SELECT qdrant_internal.p0_delay(10000,15000)")
    wait_for(lambda: ping()["active"] is not None)
    os.kill(worker_pid, signal.SIGTERM)
    out, err = active.communicate(timeout=5)
    assert active.returncode != 0, (out, err)

    def stopped():
        try:
            # A zombie has no executing native work; init may reap it later.
            stat = Path(f"/proc/{engine_pid}/stat").read_text()
            return stat.rsplit(")", 1)[1].strip().split()[0] == "Z"
        except (FileNotFoundError, ProcessLookupError):
            return True

    wait_for(stopped, seconds=5)
    replacement = jsql("SELECT qdrant_internal.p0_start_worker(5000)")
    assert replacement["worker_pid"] != worker_pid and replacement["engine_pid"] != engine_pid
    assert replacement["engine_ready"]
    assert jsql("SELECT qdrant_internal.p0_delay(1,1000)")["completed_delay_ms"] == 1
    record("helper_supervisor_exit_closes_native_owner", old_engine_stopped=True,
           replacement_owner_ready=True, shutdown_signal="SIGTERM")


def exercise_helper_operation_budget():
    state = idle()
    supervisor_pid, engine_pid = state["worker_pid"], state["engine_pid"]
    assert state["helper_start_attempts"] == 3
    assert state["helper_operation_limit_ms"] == 125000
    observer = spawn("SET application_name='pgq_p0_timer_observer'; SELECT pg_sleep(130)")
    active = spawn("SET application_name='pgq_p0_timer_caller'; SELECT qdrant_internal.p0_delay(10000,15000)")
    try:
        wait_for(lambda: ping()["active"] is not None)
        os.kill(engine_pid, signal.SIGSTOP)
        cancel_application("pgq_p0_timer_caller")
        finish(active, expected_error="57014")
        held = ping()
        assert held["engine_pid"] == engine_pid and held["active"] is not None
        assert held["worker_pid"] == supervisor_pid
        remaining = max(0, (125000 - held["active"]["elapsed_ms"]) / 1000)
        begin = time.monotonic()

        def replacement_ready():
            current = ping()
            assert current["worker_pid"] == supervisor_pid
            if current["engine_pid"] == engine_pid:
                assert current["active"] is not None, "cancellation released a live native owner"
            return current if current["engine_ready"] and current["engine_pid"] != engine_pid else None

        # This long assertion runs only in the disposable remote CI profile.
        # Each SQL poll remains bounded; the actual 125s limit is not shortened.
        deadline = time.monotonic() + 140
        replacement = None
        while time.monotonic() < deadline:
            replacement = replacement_ready()
            if replacement:
                break
            time.sleep(0.2)
        assert replacement is not None, "helper process-stop budget did not replace the stopped engine"
        elapsed = time.monotonic() - begin
        assert elapsed >= remaining - 0.5, "helper stopped before its independent execution budget"
        assert replacement["helper_start_attempts"] == 4
        last_exit = replacement["helper_last_exit"]
        assert last_exit["engine_pid"] == engine_pid and last_exit["signal"] == signal.SIGKILL
        assert last_exit["stop_reason"] == "execution_budget"
        assert last_exit["supervisor_kill_requested"] is True
        assert last_exit["kill_attempts"] == 1
        assert last_exit["first_kill_attempt"]["succeeded"] is True
        assert last_exit["first_kill_attempt"]["raw_os_error"] is None
        assert last_exit["first_kill_attempt"] == last_exit["last_kill_attempt"]
        finish(observer, timeout=12)
        record("helper_operation_budget_stops_frozen_engine", fault="SIGSTOP", configured_limit_ms=125000,
               observed_remaining_seconds=elapsed, caller_cancelled=True, native_cancellation=False,
               supervisor_preserved=True, companion_session_completed=True,
               stop_reason=last_exit["stop_reason"], stopped_engine_pid=engine_pid,
               supervisor_kill_requested=last_exit["supervisor_kill_requested"],
               kill_attempts=last_exit["kill_attempts"], first_kill_attempt=last_exit["first_kill_attempt"],
               replacement_engine_pid=replacement["engine_pid"])
    finally:
        # A failed assertion must not leave our deliberately stopped child alive.
        try:
            stat = Path(f"/proc/{engine_pid}/stat").read_text()
            if stat.rsplit(")", 1)[1].strip().split()[0] != "Z":
                os.kill(engine_pid, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):
            pass
        for process in (active, observer):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)



def exercise_helper_supervisor_sigkill():
    # This kills a real PG shared-memory worker, so collateral SQL termination
    # and postmaster crash recovery are expected. Native-helper containment is
    # exercised separately by helper_sigkill_collateral_recovery above.
    import fcntl
    import stat

    assert HELPER and FAULTS and os.geteuid() != 0
    data = Path(os.environ["PG_QDRANT_DISPOSABLE_DATA"]).resolve()
    assert Path(scalar("SHOW data_directory")).resolve() == data
    assert scalar("SHOW restart_after_crash") == "on"

    def identity(pid):
        proc = Path(f"/proc/{int(pid)}")
        assert proc.stat().st_uid == os.geteuid(), "fault target must be an owned process"
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": int(pid), "start_ticks": int(fields[19]),
                "state": fields[0], "parent_pid": int(fields[1])}

    def same_process(before, after):
        return (before["pid"], before["start_ticks"]) == (after["pid"], after["start_ticks"])

    def stopped(before):
        try:
            current = identity(before["pid"])
            return not same_process(before, current) or current["state"] == "Z"
        except (FileNotFoundError, ProcessLookupError):
            return True

    postmaster_pid = int((data / "postmaster.pid").read_text().splitlines()[0])
    postmaster_before = identity(postmaster_pid)
    before = idle()
    assert before["helper_start_attempts"] == 1
    worker_before = identity(before["worker_pid"])
    engine_before = identity(before["engine_pid"])
    assert worker_before["parent_pid"] == postmaster_pid
    assert engine_before["parent_pid"] == worker_before["pid"]
    assert scalar(f"SELECT backend_type FROM pg_stat_activity WHERE pid={worker_before['pid']}") == "pg_qdrant P0 owner"
    assert scalar(f"SELECT count(*) FROM pg_stat_activity WHERE pid={engine_before['pid']}") == "0"
    fence = data / "pg_qdrant_p0" / f"db-{int(before['database_oid'])}.engine-owner"
    fence_identity = None

    def check_fence(expected_locked):
        nonlocal fence_identity
        fd = os.open(fence, os.O_RDWR | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(fd)
            assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid()
            assert stat.S_IMODE(metadata.st_mode) == 0o600
            current_identity = (metadata.st_dev, metadata.st_ino)
            if fence_identity is None:
                fence_identity = current_identity
            assert current_identity == fence_identity, "replacement changed the ownership-fence inode"
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                locked = True
            else:
                locked = False
                fcntl.flock(fd, fcntl.LOCK_UN)
            assert locked is expected_locked, "unexpected native ownership-fence state"
        finally:
            os.close(fd)

    check_fence(True)
    log_path = ARTIFACTS / "postgresql.log"
    log_offset = log_path.stat().st_size
    processes = []
    supervisor_kill_sent = False
    try:
        observer = spawn("SET application_name='pgq_p0_supervisor_kill_observer'; "
                         "BEGIN; SELECT pg_sleep(30); COMMIT")
        processes.append(observer)
        observer_pid = int(wait_for(lambda: scalar(
            "SELECT pid FROM pg_stat_activity "
            "WHERE application_name='pgq_p0_supervisor_kill_observer' "
            "AND state='active' AND wait_event='PgSleep'")))
        observer_before = identity(observer_pid)
        active = spawn("SET application_name='pgq_p0_supervisor_kill_caller'; "
                       "SELECT qdrant_internal.p0_delay(10000,15000)")
        processes.append(active)
        wait_for(lambda: ping()["active"] is not None)
        assert observer.poll() is None and not stopped(observer_before)
        assert not stopped(engine_before)
        assert same_process(worker_before, identity(worker_before["pid"]))
        check_fence(True)

        started = time.monotonic()
        os.kill(worker_before["pid"], signal.SIGKILL)
        supervisor_kill_sent = True
        # Enforce the child-stop bound from the kill, before waiting on SQL
        # clients; their separate waits must not extend the orphan deadline.
        wait_for(lambda: stopped(engine_before), seconds=5)
        helper_stop_confirmed_seconds = time.monotonic() - started
        active_out, active_err = active.communicate(timeout=8)
        assert active.returncode != 0, (active_out, active_err)
        observer_out, observer_err = observer.communicate(timeout=8)
        assert observer.returncode != 0, "PG supervisor SIGKILL must terminate the companion SQL session"
        assert stopped(worker_before) and stopped(observer_before)
        # No replacement is requested before the old native process is gone.
        check_fence(False)
        wait_recovery()
        assert int((data / "postmaster.pid").read_text().splitlines()[0]) == postmaster_pid
        assert same_process(postmaster_before, identity(postmaster_pid))
        assert scalar("SELECT value FROM p0_recovery_marker WHERE id=1") == "committed before worker failure"
        with log_path.open("rb") as logs:
            logs.seek(log_offset)
            after_crash = logs.read(65536).decode("utf-8", errors="replace")
        assert re.search(rf"\(PID {worker_before['pid']}\).*terminated by signal 9", after_crash), after_crash[-4000:]
        assert "reinitializing" in after_crash, after_crash[-4000:]

        starts = [spawn("SELECT qdrant_internal.p0_start_worker(5000)") for _ in range(2)]
        processes.extend(starts)
        replacements = [json.loads(finish(process)[0]) for process in starts]
        assert len({(row["worker_pid"], row["engine_pid"]) for row in replacements}) == 1
        replacement = idle()
        worker_after = identity(replacement["worker_pid"])
        engine_after = identity(replacement["engine_pid"])
        assert not same_process(worker_before, worker_after)
        assert not same_process(engine_before, engine_after)
        assert worker_after["parent_pid"] == postmaster_pid
        assert engine_after["parent_pid"] == worker_after["pid"]
        assert replacement["helper_start_attempts"] == 1
        assert stopped(engine_before)
        check_fence(True)
        recovery_seconds = time.monotonic() - started
        engine = jsql("SELECT qdrant_internal.p0_engine_probe(120000)", timeout=135)
        assert engine["status"] == "passed" and engine["kind"] == "synthetic_engine_smoke"
        assert engine["engine_version"] == "0.8.0" and engine["checks"]
        (ARTIFACTS / "sql-engine-after-supervisor-sigkill.json").write_text(json.dumps(engine, indent=2) + "\n")
        record("helper_supervisor_sigkill_recovers_postgresql_and_native_owner",
               shutdown_signal="SIGKILL", postgres_crash_recovery_observed=True,
               companion_session_terminated=True, caller_failed=True,
               postmaster_identity_preserved=True, committed_postgresql_marker_retained=True,
               old_engine_stopped_before_replacement=True,
               helper_stop_confirmed_after_seconds=helper_stop_confirmed_seconds, recovery_seconds=recovery_seconds,
               same_fence_inode=True, fence_released_then_reacquired=True,
               concurrent_replacement_starts_converged=True,
               old_supervisor=worker_before, replacement_supervisor=worker_after,
               old_engine=engine_before, replacement_engine=engine_after,
               replacement_edge_smoke_checks=len(engine["checks"]),
               native_helper_failure_containment_claim=False,
               durable_index_recovery_verified=False,
               ownership_scope="P0 native-owner fence; no persistent product index generation")
    finally:
        # Only clean up identities created and observed by this disposable test.
        # A fallback kill after failure is never successful automatic cleanup evidence.
        if supervisor_kill_sent and not stopped(engine_before):
            try:
                os.kill(engine_before["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def exercise_helper_restart_exhaustion():
    # The preceding supervisor-replacement check starts a fresh attempt budget.
    state = idle()
    assert state["helper_start_attempts"] == 1
    supervisor_pid = state["worker_pid"]
    for attempt in range(1, 5):
        state = idle()
        assert state["helper_start_attempts"] == attempt
        os.kill(state["engine_pid"], signal.SIGKILL)
        if attempt < 4:
            previous_pid = state["engine_pid"]
            wait_for(lambda: (current := ping())["engine_ready"] and current["engine_pid"] != previous_pid)
    def exhausted_status():
        current = ping()
        return current if current["helper_restart_exhausted"] else None

    exhausted = wait_for(exhausted_status)
    assert exhausted["worker_pid"] == supervisor_pid and not exhausted["engine_ready"]
    assert exhausted["engine_pid"] is None and exhausted["helper_start_attempts"] == 4
    time.sleep(0.5)
    assert ping()["helper_start_attempts"] == 4
    expect_error("SELECT qdrant_internal.p0_delay(1,1000)", "55000")
    record("helper_restart_budget_exhaustion_is_visible", start_attempts=4, automatic_restarts=3,
           supervisor_preserved=True, unavailable_requests_refused=True)


try:
    exercise()
    REPORT["status"] = "passed"
except Exception as exc:
    REPORT["status"] = "failed"
    REPORT["failure"] = str(exc)
    REPORT["traceback"] = traceback.format_exc()
    raise
finally:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    filename = "p0-fault-results.json" if FAULTS else "p0-sql-results.json"
    (ARTIFACTS / filename).write_text(json.dumps(REPORT, indent=2) + "\n")

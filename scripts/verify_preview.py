#!/usr/bin/env python3
"""Exercise a package-only installation in a new disposable PostgreSQL cluster."""
import json
import os
from pathlib import Path
import subprocess
import time

from preview_package import INSTALL_PATHS, digest

REPORT = {"kind": "installed_preview", "status": "running", "release_supported": False, "checks": []}
ARTIFACTS = Path(os.environ["PG_QDRANT_ARTIFACT_DIR"])
PSQL = ["psql", "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose"]


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def sql(query, expected_error=None):
    result = subprocess.run(PSQL + ["-c", query], capture_output=True, text=True, timeout=60)
    if expected_error:
        require(result.returncode != 0 and expected_error in result.stderr, result.stderr)
    else:
        require(result.returncode == 0, result.stderr)
    return result.stdout.strip()


def owned(query):
    return sql("SET ROLE preview_owner; " + query)


def ready():
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        status = json.loads(owned("SELECT qdrant.index_status('preview_docs')"))
        if status["engine_index_ready"] and status["pending_events"] == 0:
            return status
        require(status["state"] != "failed", str(status))
        time.sleep(0.05)
    raise RuntimeError("preview source did not become ready")


def search():
    return json.loads(owned("SELECT coalesce(jsonb_agg(jsonb_build_object('key',h.source_key,'body',d.body) "
        "ORDER BY h.rank),'[]') FROM qdrant.search('preview_docs','transaction','text',10) h "
        "JOIN preview_docs d ON d.id=h.source_key"))


def drop():
    task = json.loads(owned("SELECT qdrant.drop_index('preview_docs')"))
    completed = json.loads(owned("SELECT qdrant.await_task('" + task["task_id"] + "',45000)"))
    REPORT.setdefault("drops", []).append(completed)
    require(completed["succeeded"] and completed["physical_cleanup_completed"]
            and completed["pending_epochs"] == 0, "public drop left native storage pending")


def exercise():
    require(not Path("/src").exists(), "runtime unexpectedly contains the original build/source tree")
    manifest = json.loads(Path("/package-tools/manifest.json").read_text(encoding="utf-8"))
    expected = {row["path"]: row["sha256"] for row in manifest["files"]}
    REPORT["source_commit"] = manifest["source_commit"]
    REPORT["installed_sha256"] = {}
    for name in INSTALL_PATHS:
        observed = digest(Path("/") / name)
        require(observed == expected[name], "installed artifact differs from archive: " + name)
        REPORT["installed_sha256"][name] = observed
    require(sql("SHOW fsync") == sql("SHOW synchronous_commit") == "on", "durable PG settings required")
    require(Path(sql("SHOW data_directory")).resolve() == Path(os.environ["PG_QDRANT_DISPOSABLE_DATA"]),
            "not the owned disposable cluster")
    sql("CREATE EXTENSION pg_qdrant")
    build = json.loads(sql("SELECT qdrant.build_info()"))
    REPORT["build_info"] = build
    require(build["features"]["p0_managed_helper"] and not build["features"]["p0_fault_injection"]
            and build["postgres_major"] == 17 and build["engine"]["version"] == "0.8.0", "wrong package build")
    require(sql("SELECT count(*) FROM pg_proc WHERE proname='p0_fault'") == "0", "fault function packaged")
    REPORT["checks"].append("four archive hashes match installed normal helper/extension/control/SQL; fresh PG17 CREATE EXTENSION without source tree or fault function")
    sql("CREATE ROLE preview_owner NOLOGIN; CREATE ROLE preview_writer NOLOGIN; "
        "CREATE TABLE preview_docs(id text PRIMARY KEY,body text NOT NULL); "
        "ALTER TABLE preview_docs OWNER TO preview_owner; "
        "GRANT SELECT,INSERT,UPDATE,DELETE ON preview_docs TO preview_writer; "
        "INSERT INTO preview_docs VALUES('doc-a','durable transaction recovery'),('doc-b','数据库 索引')")
    register = "SELECT qdrant.create_index('preview_docs','preview_docs','id','{\"text\":{\"fields\":[\"body\"]}}')"
    owned(register)
    first = ready()
    require(search() == [{"key": "doc-a", "body": "durable transaction recovery"}], "initial native/source JOIN mismatch")
    sql("SET ROLE preview_writer; INSERT INTO preview_docs VALUES('doc-c','ordinary transaction write')")
    ready()
    require({row["key"] for row in search()} == {"doc-a", "doc-c"}, "ordinary writer was not captured")
    sql("SET ROLE preview_writer; SELECT count(*) FROM qdrant_internal.outbox", "42501")
    sql("SET ROLE preview_writer; SELECT * FROM qdrant.search('preview_docs','transaction')", "42501")
    ticket = owned("BEGIN; UPDATE preview_docs SET body='updated transaction recovery' WHERE id='doc-a'; "
                   "SELECT qdrant.track_changes('preview_docs'); COMMIT")
    require(len(ticket) == 36, "missing fixed-membership ticket")
    outcome = json.loads(owned("SELECT qdrant.await_changes('" + ticket + "',45000)"))
    require(outcome["committed"] and outcome["applied"] and outcome["durable"]
            and outcome["pending_events"] == 0 and not outcome["timed_out"], "ticket was not durable")
    REPORT["ticket"] = outcome
    require({row["body"] for row in search()} == {"updated transaction recovery", "ordinary transaction write"},
            "updated native/source JOIN mismatch")
    REPORT["checks"].append("ordinary table backfill/DML, exact durable ticket and actual BM25 source JOIN; writer has no ledger or index-owner access")
    task = json.loads(owned("SELECT qdrant.rebuild_index('preview_docs')"))
    rebuilt = json.loads(owned("SELECT qdrant.await_task('" + task["task_id"] + "',45000)"))
    require(rebuilt["succeeded"] and rebuilt["generation"] != first["generation"], "rebuild did not switch generation")
    REPORT["rebuild"] = rebuilt
    ready()
    before = search()
    subprocess.run(["pg_ctl", "-D", os.environ["PG_QDRANT_DISPOSABLE_DATA"], "-m", "fast", "-w",
                    "restart", "-l", str(ARTIFACTS / "postgres.log")], check=True, timeout=40,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    ready()
    require(search() == before, "source replay after PG restart changed results")
    REPORT["checks"].append("actual public rebuild switches generation; PG restart/source replay returns the same native matches")
    drop()
    sql("DROP EXTENSION pg_qdrant")
    require(sql("SELECT count(*) FROM preview_docs") == "3", "uninstall changed source facts")
    sql("CREATE EXTENSION pg_qdrant")
    owned(register)
    ready()
    require(search() == before, "reinstalled package/source backfill changed results")
    drop()
    sql("DROP EXTENSION pg_qdrant")
    REPORT["checks"].append("public drop, extension uninstall and reinstall preserve source facts and reproduce native matches")
    REPORT["status"] = "passed"


if __name__ == "__main__":
    try:
        exercise()
    except Exception as error:
        REPORT.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        temporary = ARTIFACTS / "preview-result.pending"
        temporary.write_text(json.dumps(REPORT, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, ARTIFACTS / "preview-result.json")
        print(json.dumps(REPORT), flush=True)

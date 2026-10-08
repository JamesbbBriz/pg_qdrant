#!/usr/bin/env bash
set -euo pipefail

# Positive allocation belongs only to the inspected fresh-container runner.
# This script is separate from the ordinary SQL and restart-exhaustion suites.
[[ "$(id -u)" == 10001 ]] || { echo 'OOM experiment requires the fixed non-root container user.' >&2; exit 2; }
[[ "${PG_QDRANT_P0_OOM_RUN_ID:-}" =~ ^[a-f0-9]{32}$ ]] || { echo 'Missing one-shot OOM nonce.' >&2; exit 2; }
pgq_test_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pgq_config="${PGRX_PG_CONFIG_PATH:-pg_config}"
pgq_bin="$("$pgq_config" --bindir)"
pgq_artifacts="${PG_QDRANT_ARTIFACT_DIR:-/src/artifacts}"
mkdir -p "$pgq_artifacts"
pgq_artifacts="$(cd "$pgq_artifacts" && pwd)"
pgq_cluster="$(mktemp -d /tmp/pgq-p0-oom-cluster.XXXXXXXX)"
pgq_data="$pgq_cluster/data"
pgq_socket="$pgq_cluster/socket"
mkdir "$pgq_socket"
cleanup() {
  local result=$?
  "$pgq_bin/pg_ctl" -D "$pgq_data" -m immediate -w stop >/dev/null 2>&1 || true
  rm -rf -- "$pgq_cluster"
  exit "$result"
}
trap cleanup EXIT
"$pgq_bin/initdb" -D "$pgq_data" --no-locale --encoding=UTF8 --auth=trust >"$pgq_artifacts/initdb.log" 2>&1
"$pgq_bin/pg_ctl" -D "$pgq_data" -l "$pgq_artifacts/postgresql.log" \
  -o "-c listen_addresses='' -c unix_socket_directories='$pgq_socket' -c port=55432 -c max_worker_processes=8 -c fsync=on -c synchronous_commit=on" \
  -w start >"$pgq_artifacts/startup.log" 2>&1
export PGHOST="$pgq_socket" PGPORT=55432 PGDATABASE=postgres PGUSER="$(id -un)"
export PG_QDRANT_PSQL="$pgq_bin/psql" PG_QDRANT_DISPOSABLE_DATA="$pgq_data"
export PG_QDRANT_ARTIFACT_DIR="$pgq_artifacts"
python3 "$pgq_test_dir/verify_oom.py"

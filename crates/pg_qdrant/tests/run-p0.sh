#!/usr/bin/env bash
set -euo pipefail

# Run only against a cluster created by this script. The extension must already
# be installed into the selected PostgreSQL 17 installation.
if [[ "$(id -u)" == "0" ]]; then
  echo "P0 PostgreSQL runtime tests require a non-root operating-system user." >&2
  exit 2
fi

pgq_test_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pgq_config="${PGRX_PG_CONFIG_PATH:-pg_config}"
pgq_bin_dir="$("$pgq_config" --bindir)"
pgq_artifacts="${PG_QDRANT_ARTIFACT_DIR:-artifacts}"
mkdir -p "$pgq_artifacts"
pgq_artifacts="$(cd "$pgq_artifacts" && pwd)"
pgq_cluster="$(mktemp -d /tmp/pgq-p0.XXXXXXXX)"
pgq_data="$pgq_cluster/data"
pgq_socket="$pgq_cluster/socket"
mkdir "$pgq_socket"

cleanup() {
  local result=$?
  "$pgq_bin_dir/pg_ctl" -D "$pgq_data" -m immediate -w stop > /dev/null 2>&1 || true
  rm -rf -- "$pgq_cluster"
  exit "$result"
}
trap cleanup EXIT

"$pgq_bin_dir/initdb" -D "$pgq_data" --no-locale --encoding=UTF8 --auth=trust > "$pgq_artifacts/initdb.log" 2>&1
"$pgq_bin_dir/pg_ctl" -D "$pgq_data" -l "$pgq_artifacts/postgresql.log" \
  -o "-c listen_addresses='' -c unix_socket_directories='$pgq_socket' -c port=55432 -c max_worker_processes=8 -c fsync=on -c synchronous_commit=on" \
  -w start > "$pgq_artifacts/startup.log" 2>&1

export PGHOST="$pgq_socket" PGPORT=55432 PGDATABASE=postgres PGUSER="$(id -un)"
export PG_QDRANT_PSQL="$pgq_bin_dir/psql"
export PG_QDRANT_DISPOSABLE_DATA="$pgq_data"
export PG_QDRANT_ARTIFACT_DIR="$pgq_artifacts"
python3 "$pgq_test_dir/verify_p0.py"

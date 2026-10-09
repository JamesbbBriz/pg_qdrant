#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" != 10001 ]]; then echo 'Disposable preview user required' >&2; exit 2; fi
pgq_cluster="$(mktemp -d /tmp/pgq-package-preview.XXXXXXXX)"
mkdir "$pgq_cluster/socket"
pgq_artifacts="${PG_QDRANT_ARTIFACT_DIR:?}"
cleanup() {
  code=$?
  trap - EXIT
  set +e
  pg_ctl -D "$pgq_cluster/data" -m immediate -w stop >/dev/null 2>&1
  stop_code=$?
  if [[ "$code" == 0 && "$stop_code" == 0 ]]; then
    rm -rf -- "$pgq_cluster"
  else
    if [[ "$code" == 0 ]]; then code=1; fi
    printf 'Failed preview cluster retained at %s\n' "$pgq_cluster" >&2
    printf '%s\n' "$pgq_cluster" >"$pgq_artifacts/failed-cluster.txt"
  fi
  exit "$code"
}
trap cleanup EXIT
initdb -D "$pgq_cluster/data" --no-locale --encoding=UTF8 --auth=trust >"$pgq_artifacts/init.log" 2>&1
pg_ctl -D "$pgq_cluster/data" -l "$pgq_artifacts/postgres.log" \
  -o "-c listen_addresses='' -c unix_socket_directories='$pgq_cluster/socket' -c port=55435 -c fsync=on -c synchronous_commit=on" -w start
export PGHOST="$pgq_cluster/socket" PGPORT=55435 PGDATABASE=postgres PGUSER=builder
export PG_QDRANT_DISPOSABLE_DATA="$pgq_cluster/data"
python3 /package-tools/verify_preview.py

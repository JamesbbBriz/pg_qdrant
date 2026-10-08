#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" == 0 ]]; then echo 'Run as non-root' >&2; exit 2; fi
pgq_bin="$(pg_config --bindir)"
pgq_artifacts="${PG_QDRANT_ARTIFACT_DIR:-/src/artifacts}"
mkdir -p "$pgq_artifacts"
pgq_cluster="$(mktemp -d /tmp/pgq-p1-product.XXXXXXXX)"
mkdir "$pgq_cluster/socket"
cleanup() {
  code=$?
  if [[ "$code" != 0 ]]; then tail -n 100 "$pgq_artifacts/p1-product-postgres.log" >&2 || true; fi
  "$pgq_bin/pg_ctl" -D "$pgq_cluster/data" -m immediate -w stop >/dev/null 2>&1 || true
  rm -rf -- "$pgq_cluster"
  exit "$code"
}
trap cleanup EXIT
"$pgq_bin/initdb" -D "$pgq_cluster/data" --no-locale --encoding=UTF8 --auth=trust >"$pgq_artifacts/p1-product-init.log" 2>&1
"$pgq_bin/pg_ctl" -D "$pgq_cluster/data" -l "$pgq_artifacts/p1-product-postgres.log" \
  -o "-c listen_addresses='' -c unix_socket_directories='$pgq_cluster/socket' -c port=55434 -c fsync=on -c synchronous_commit=on" -w start
export PGHOST="$pgq_cluster/socket" PGPORT=55434 PGDATABASE=postgres PGUSER="$(id -un)"
export PG_QDRANT_DISPOSABLE_DATA="$pgq_cluster/data"
if [[ "${PG_QDRANT_P1_STORAGE_TEST:-0}" == 1 ]]; then
  python3 /src/crates/pg_qdrant/tests/verify_p1_storage.py | tee "$pgq_artifacts/p1-storage-results.json"
else
  python3 /src/crates/pg_qdrant/tests/verify_p1.py | tee "$pgq_artifacts/p1-product-results.json"
fi

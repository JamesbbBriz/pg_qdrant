#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" == 0 ]]; then echo 'Run as non-root' >&2; exit 2; fi
pgq_bin="$(pg_config --bindir)"
pgq_artifacts="${PG_QDRANT_ARTIFACT_DIR:-/src/artifacts}"
mkdir -p "$pgq_artifacts"
if [[ "${PG_QDRANT_P1_STORAGE_TEST:-0}" == 1 ]]; then
  pgq_cluster=/tmp/pgq-p1-product.storage
  [[ -d "$pgq_cluster/data/pg_qdrant_p0/db-5.indexes" && ! -e "$pgq_cluster/bootstrap" ]]
else
  pgq_cluster="$(mktemp -d /tmp/pgq-p1-product.XXXXXXXX)"
fi
mkdir "$pgq_cluster/socket"
cleanup() {
  code=$?
  trap - EXIT
  set +e
  if [[ "$code" != 0 ]]; then tail -n 100 "$pgq_artifacts/p1-product-postgres.log" >&2 || true; fi
  "$pgq_bin/pg_ctl" -D "$pgq_cluster/data" -m immediate -w stop >/dev/null 2>&1
  stop_code=$?
  if [[ "$code" == 0 && "$stop_code" == 0 ]]; then
    # The storage fixture contains a Docker-managed mount. Its successful
    # disposable container is removed by the outer runner, never recursively here.
    if [[ "${PG_QDRANT_P1_STORAGE_TEST:-0}" != 1 ]]; then rm -rf -- "$pgq_cluster"; fi
  else
    if [[ "$code" == 0 ]]; then code=1; fi
    python3 -c 'import json,sys; print(json.dumps({"status":"failed","cluster":sys.argv[1],"exit_code":int(sys.argv[2]),"stop_exit_code":int(sys.argv[3]),"release_supported":False}))' \
      "$pgq_cluster" "$code" "$stop_code" >"$pgq_artifacts/p1-failed-cluster.json"
    printf 'Failed product cluster retained at %s\n' "$pgq_cluster" >&2
  fi
  exit "$code"
}
trap cleanup EXIT
pgq_init="$pgq_cluster/data"
if [[ "${PG_QDRANT_P1_STORAGE_TEST:-0}" == 1 ]]; then pgq_init="$pgq_cluster/bootstrap"; fi
"$pgq_bin/initdb" -D "$pgq_init" --no-locale --encoding=UTF8 --auth=trust >"$pgq_artifacts/p1-product-init.log" 2>&1
if [[ "$pgq_init" != "$pgq_cluster/data" ]]; then
  # Docker mounts the exact engine root before startup. Initialize beside that
  # mount and copy the fresh cluster without replacing the mounted directory.
  cp -a -- "$pgq_init/." "$pgq_cluster/data/"
fi
"$pgq_bin/pg_ctl" -D "$pgq_cluster/data" -l "$pgq_artifacts/p1-product-postgres.log" \
  -o "-c listen_addresses='' -c unix_socket_directories='$pgq_cluster/socket' -c port=55434 -c fsync=on -c synchronous_commit=on" -w start
export PGHOST="$pgq_cluster/socket" PGPORT=55434 PGDATABASE=postgres PGUSER="$(id -un)"
export PG_QDRANT_DISPOSABLE_DATA="$pgq_cluster/data"
if [[ "${PG_QDRANT_QUALITY_TEST:-0}" == 1 ]]; then
  python3 /src/scripts/run_quality.py --fixtures "${PG_QDRANT_QUALITY_FIXTURES:?}" \
    --output "$pgq_artifacts/quality-results.json" | tee "$pgq_artifacts/quality-summary.json"
elif [[ "${PG_QDRANT_P1_STORAGE_TEST:-0}" == 1 ]]; then
  python3 /src/crates/pg_qdrant/tests/verify_p1_storage.py | tee "$pgq_artifacts/p1-storage-results.json"
else
  python3 /src/crates/pg_qdrant/tests/verify_p1.py | tee "$pgq_artifacts/p1-product-results.json"
fi

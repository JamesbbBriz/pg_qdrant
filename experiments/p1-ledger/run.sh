#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" == 0 ]]; then echo "Run as non-root" >&2; exit 2; fi
pgq_bin="$(pg_config --bindir)"
artifacts=/src/artifacts
mkdir -p "$artifacts"
cluster="$(mktemp -d /tmp/pgq-p1-ledger.XXXXXXXX)"
mkdir "$cluster/socket"
cleanup() {
  code=$?
  "$pgq_bin/pg_ctl" -D "$cluster/data" -m immediate -w stop >/dev/null 2>&1 || true
  rm -rf -- "$cluster"
  exit "$code"
}
trap cleanup EXIT
"$pgq_bin/initdb" -D "$cluster/data" --no-locale --encoding=UTF8 --auth=trust >"$artifacts/p1-initdb.log" 2>&1
"$pgq_bin/pg_ctl" -D "$cluster/data" -l "$artifacts/p1-postgresql.log" \
 -o "-c listen_addresses='' -c unix_socket_directories='$cluster/socket' -c port=55433 -c fsync=on -c synchronous_commit=on" \
 -w start >"$artifacts/p1-startup.log" 2>&1
export PGHOST="$cluster/socket" PGPORT=55433 PGDATABASE=postgres PGUSER="$(id -un)"
"$pgq_bin/psql" -X -v ON_ERROR_STOP=1 -c "CREATE EXTENSION pg_qdrant" >"$artifacts/p1-extension.log"
"$pgq_bin/psql" -X -v ON_ERROR_STOP=1 -f /src/experiments/p1-ledger/p1-ledger.sql >"$artifacts/p1-install.log" 2>&1
"$pgq_bin/psql" -X -v ON_ERROR_STOP=1 -f /src/experiments/p1-ledger/test.sql >"$artifacts/p1-assertions.log" 2>&1
python3 /src/experiments/p1-ledger/parallel.py >"$artifacts/p1-parallel.json" 2>"$artifacts/p1-parallel.log"
echo "P1 ledger checks passed"

#!/usr/bin/env bash
set -euo pipefail
# This entry point prepares only a fresh, private container's fixed mount parents.
# PostgreSQL and every fault operation still run as the non-root builder account.
[[ "$(id -u)" == 0 && "${PG_QDRANT_P1_STORAGE_TEST:-0}" == 1 ]]
pgq_cluster=/tmp/pgq-p1-product.storage
pgq_disk="$pgq_cluster/data/pg_qdrant_p0/db-5.indexes"
[[ "${PG_QDRANT_P1_DISK_DIR:-}" == "$pgq_disk" ]]
python3 - <<'PY'
from pathlib import Path
import os
root=Path('/tmp/pgq-p1-product.storage')
disk=root/'data/pg_qdrant_p0/db-5.indexes'
assert root.resolve()==root and disk.resolve()==disk
assert set(root.iterdir())=={root/'data'}
assert set((root/'data').iterdir())=={root/'data/pg_qdrant_p0'}
assert set((root/'data/pg_qdrant_p0').iterdir())=={disk}
rows=[line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
mount=next(row for row in rows if row[4]==str(disk))
assert mount[mount.index('-')+1]=='tmpfs'
assert not list(disk.iterdir()) and disk.stat().st_uid==10001
capacity=os.statvfs(disk)
assert capacity.f_blocks*capacity.f_frsize==384*1024*1024
assert root.stat().st_dev!=disk.stat().st_dev
PY
chown -- 10001:10001 "$pgq_cluster" "$pgq_cluster/data" "$pgq_cluster/data/pg_qdrant_p0"
chmod -- 0700 "$pgq_cluster" "$pgq_cluster/data" "$pgq_cluster/data/pg_qdrant_p0"
exec runuser -u builder -- bash /src/crates/pg_qdrant/tests/run-p1.sh

#!/usr/bin/env bash
set -euo pipefail
[[ "$(id -u)" != 0 ]]
mkdir -p "$PG_QDRANT_ARTIFACT_DIR"
export PG_QDRANT_QUALITY_ENCODED="$PG_QDRANT_ARTIFACT_DIR/encoding"
PYTHONPATH=/opt/quality-runtime python3 /src/scripts/encode_quality.py \
  --root /opt/quality-model --fixtures "$PG_QDRANT_QUALITY_FIXTURES" \
  --output "$PG_QDRANT_QUALITY_ENCODED"
exec bash /src/crates/pg_qdrant/tests/run-p1.sh

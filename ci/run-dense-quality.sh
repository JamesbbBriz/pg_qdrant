#!/usr/bin/env bash
set -euo pipefail
[[ "$(id -u)" != 0 ]]
mkdir -p "$PG_QDRANT_ARTIFACT_DIR"
export PG_QDRANT_QUALITY_ENCODED="$PG_QDRANT_ARTIFACT_DIR/encoding"
PYTHONPATH=/opt/quality-runtime python3 /src/scripts/encode_quality.py \
  --root /opt/quality-model --fixtures "$PG_QDRANT_QUALITY_FIXTURES" \
  --output "$PG_QDRANT_QUALITY_ENCODED"
if [[ "${PG_QDRANT_QUALITY_TOKENS:-0}" == 1 ]]; then
  export PYTHONPATH=/opt/quality-runtime OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2
  python3 /src/scripts/quality_tokens.py --root /opt/quality-model \
    --fixtures "$PG_QDRANT_QUALITY_FIXTURES" --output "$PG_QDRANT_QUALITY_ENCODED/tokens"
fi
exec bash /src/crates/pg_qdrant/tests/run-p1.sh

#!/usr/bin/env bash
set -euo pipefail

# The installed binary must have been built explicitly with
# --no-default-features --features pg17,p0-fault-injection.
# This script creates its own disposable cluster through run-p0.sh.
export PG_QDRANT_FAULT_TESTS=1
pgq_test_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$pgq_test_dir/run-p0.sh"

#!/usr/bin/env bash
set -euo pipefail

# The installed extension and selected helper must explicitly enable
# p0-fault-injection; PG_QDRANT_MANAGED_HELPER must match the installed profile.
# This script creates its own disposable cluster through run-p0.sh.
export PG_QDRANT_FAULT_TESTS=1
pgq_test_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$pgq_test_dir/run-p0.sh"

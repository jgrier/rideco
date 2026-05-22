#!/usr/bin/env bash
# Register the Python deployment with Restate. Idempotent — re-run any time
# to pick up code changes (uses --force).

set -euo pipefail
DEPLOYMENT="${DEPLOYMENT:-http://host.docker.internal:9080}"

echo "→ register Python deployment ($DEPLOYMENT)"
restate -y deployments register --force "$DEPLOYMENT" | tail -n +3

#!/usr/bin/env bash
# Register each of the 12 per-service deployments with Restate. Each one
# is a separate HTTP endpoint inside the host network; restate-server
# (in Docker) reaches them via host.docker.internal.
#
# Usage: ./scripts/register-all.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

HOST="${HOST:-http://host.docker.internal}"
PORTS=(9080 9081 9082 9083 9084 9085 9086 9087 9088 9089 9090 9091)

for port in "${PORTS[@]}"; do
  echo "── registering ${HOST}:${port} ──"
  restate -y deployments register --force "${HOST}:${port}" 2>&1 | tail -3
done
echo
echo "── all 12 deployments registered ──"
restate -y services list

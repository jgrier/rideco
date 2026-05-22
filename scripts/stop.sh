#!/usr/bin/env bash
# Stop hypercorn reliably (pkill misses worker forks on macOS).

set -euo pipefail

PIDS=$(lsof -nP -iTCP:9080 -sTCP:LISTEN -t 2>/dev/null || true)
if [ -z "$PIDS" ]; then
  echo "9080 already free"
else
  echo "killing PIDs on 9080: $PIDS"
  echo "$PIDS" | xargs kill -9
fi

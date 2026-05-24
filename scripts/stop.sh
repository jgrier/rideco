#!/usr/bin/env bash
# Stop every per-service hypercorn (9080-9091) reliably. pkill misses
# worker forks on macOS, so we go port-by-port via lsof.

set -euo pipefail

KILLED=0
for port in $(seq 9080 9091); do
  PIDS=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    echo "killing PIDs on $port: $PIDS"
    echo "$PIDS" | xargs kill -9
    KILLED=$((KILLED + 1))
  fi
done
[ "$KILLED" -eq 0 ] && echo "9080-9091 already free"

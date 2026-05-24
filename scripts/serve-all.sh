#!/usr/bin/env bash
# Launch all 12 services as separate hypercorn processes.
# Each line of every service's log is prefixed with [service-name] so the
# interleaved output is readable in one terminal.
#
# Foreground; Ctrl+C tears all 12 children down via the trap.
# Usage: ./scripts/serve-all.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

PY="${PY:-.venv/bin/python}"

# Ordered list of (module:port) — port assignments match register-all.sh.
SERVICES=(
  "trip:9080"
  "offers:9081"
  "eta:9082"
  "pricing:9083"
  "locations:9084"
  "features:9085"
  "dispatch:9086"
  "region_safety_agent:9087"
  "sim_rider:9088"
  "sim_driver:9089"
  "sim_mapping:9090"
  "sim_control:9091"
)

PIDS=()
cleanup() {
  echo
  echo "── stopping all services ──"
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  # belt-and-braces: kill anything still listening on our port range
  for port in $(seq 9080 9091); do
    p=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)
    [ -n "$p" ] && echo "$p" | xargs kill -9 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

echo "── starting 12 services ──"
for entry in "${SERVICES[@]}"; do
  svc="${entry%:*}"
  port="${entry#*:}"
  # Each process: hypercorn binding ${port}, output piped through sed to tag lines.
  # Using "stdbuf -oL" if available would force line-buffering, but Python's
  # default print is line-buffered for terminals — and we redirect to a pipe,
  # so we set PYTHONUNBUFFERED to keep logs prompt.
  PYTHONUNBUFFERED=1 "$PY" -m hypercorn \
    "rideco.services.${svc}:app" --bind "0.0.0.0:${port}" 2>&1 \
    | sed -u "s/^/[${svc}] /" &
  PIDS+=($!)
  printf "  %-22s :%-6s pid=%s\n" "$svc" "$port" "$!"
done
echo
echo "── all up; Ctrl+C to stop ──"
wait

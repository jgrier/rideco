#!/usr/bin/env bash
# Pretty-print a SafetyAgent VO's current state.
# Look for `pending_awakeable` — if non-null, the agent is suspended waiting
# for a human verdict and you can resolve it with scripts/approve.sh.
# Usage: scripts/show-agent.sh <trip_id>

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"

if [ $# -lt 1 ]; then
  echo "usage: $0 <trip_id>"; exit 1
fi

echo "→ SafetyAgent[$1] state (shared handler):"
echo
curl -s -X POST "$INGRESS/SafetyAgent/$1/get" -H 'Content-Type: application/json' -d '{}' \
  | python3 -m json.tool
echo
echo "  pending_awakeable  — if non-null, this is the awakeable id to resolve."
echo "                       Resolve with: scripts/approve.sh <id>"
echo "  ticks              — number of safety checks performed"
echo "  escalations        — number of times the agent has handed off to a human"

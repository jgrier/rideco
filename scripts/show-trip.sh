#!/usr/bin/env bash
# Pretty-print a Trip VO's current state.
# Usage: scripts/show-trip.sh <trip_id>

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"

if [ $# -lt 1 ]; then
  echo "usage: $0 <trip_id>"; exit 1
fi

echo "→ Trip[$1] state (shared handler, doesn't block exclusive writers):"
echo
curl -s -X POST "$INGRESS/Trip/$1/get" -H 'Content-Type: application/json' -d '{}' \
  | python3 -m json.tool
echo
echo "  status meanings:"
echo "    requested    — Trip just started, before Offers.generate completed"
echo "    quoted       — Offer built, awaiting rider confirm"
echo "    dispatching  — rider confirmed, enqueued to Dispatch[region]"
echo "    assigned     — Dispatch matched a driver to this trip"
echo "    completed    — terminal"
echo "    cancelled    — terminal"

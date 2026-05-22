#!/usr/bin/env bash
# Cancel a trip. Trip.cancel is a terminal state transition; like complete,
# it fires Bifrost send → SafetyAgent.stop_monitoring.
# Usage: ./scripts/cancel-trip.sh <trip_id>

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"

if [ $# -lt 1 ]; then
  echo "usage: $0 <trip_id>"; exit 1
fi
TRIP_ID="$1"

echo "════════════════════════════════════════════════════════════════"
echo " CANCEL TRIP — $TRIP_ID"
echo "════════════════════════════════════════════════════════════════"
echo
curl -s -X POST "$INGRESS/Trip/$TRIP_ID/cancel" -H 'Content-Type: application/json' -d '{}' \
  | python3 -m json.tool
echo
echo " ✓ Trip cancelled. SafetyAgent.stop_monitoring fired (Bifrost)."
echo
echo " Restate UI: http://localhost:9070 → State → Trip → $TRIP_ID"

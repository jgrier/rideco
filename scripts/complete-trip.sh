#!/usr/bin/env bash
# Mark a trip as completed. Trip.complete is the terminal state transition;
# it fires a durable async send to SafetyAgent.stop_monitoring so the per-trip
# agent stops ticking.
# Usage: ./scripts/complete-trip.sh <trip_id>

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"

if [ $# -lt 1 ]; then
  echo "usage: $0 <trip_id>"; exit 1
fi
TRIP_ID="$1"

echo "════════════════════════════════════════════════════════════════"
echo " COMPLETE TRIP — $TRIP_ID"
echo "════════════════════════════════════════════════════════════════"
echo
echo " Trip.complete — terminal state transition for the trip."
echo " Fires durable async send → SafetyAgent.stop_monitoring, which marks"
echo " the per-trip agent inactive. The agent's next tick will see"
echo " active=false and return without scheduling another tick."
echo
curl -s -X POST "$INGRESS/Trip/$TRIP_ID/complete" -H 'Content-Type: application/json' -d '{}' \
  | python3 -m json.tool
echo
echo " ✓ Trip marked completed."
echo
echo " In Terminal 1 you'll see:"
echo "   • [send→] Trip → SafetyAgent.stop_monitoring"
echo "   • Trip completed trip=$TRIP_ID"
echo "   • SafetyAgent stopped trip=$TRIP_ID  (on its next tick)"
echo
echo " Restate UI: http://localhost:9070 → State → Trip → $TRIP_ID"
echo "   status: completed"
echo
echo " Verify:"
echo "   ./scripts/show-trip.sh $TRIP_ID    # status=completed"
echo "   ./scripts/show-trip.sh $TRIP_ID   # active=false (after next tick)"

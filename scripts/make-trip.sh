#!/usr/bin/env bash
# Fire one rider request, end-to-end: request_ride (sync, awaits offer) + confirm.
# Usage: scripts/make-trip.sh <trip_id> <region>

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"

if [ $# -lt 2 ]; then
  echo "usage: $0 <trip_id> <region>"; exit 1
fi
TRIP_ID="$1"
REGION="$2"

case "$REGION" in
  SF)  LAT=37.78; LNG=-122.42 ;;
  NYC) LAT=40.72; LNG=-74.00 ;;
  LA)  LAT=34.05; LNG=-118.24 ;;
  SEA) LAT=47.61; LNG=-122.33 ;;
  *) echo "unknown region: $REGION"; exit 1 ;;
esac
DEST_LAT=$(echo "$LAT - 0.02" | bc -l)
DEST_LNG=$(echo "$LNG + 0.02" | bc -l)

echo "════════════════════════════════════════════════════════════════"
echo " MAKE TRIP — $TRIP_ID in $REGION"
echo "════════════════════════════════════════════════════════════════"
echo
echo " (1/2) Trip.request_ride (sync RPC — rider awaits the offer)"
echo "       Trip → Offers → ETA + Pricing all run synchronously,"
echo "       the offer comes back below:"
echo
curl -s -X POST "$INGRESS/Trip/$TRIP_ID/request_ride" \
  -H 'Content-Type: application/json' \
  -d "{\"rider_id\":\"r-$TRIP_ID\",\"origin\":{\"lat\":$LAT,\"lng\":$LNG},\"destination\":{\"lat\":$DEST_LAT,\"lng\":$DEST_LNG},\"region\":\"$REGION\"}" \
  | python3 -m json.tool

echo
echo " (2/2) Trip.confirm (Bifrost send — fire-and-forget)"
echo "       Trip enqueues into Dispatch[$REGION] for the next matching round."
echo
curl -s -X POST "$INGRESS/Trip/$TRIP_ID/confirm" -H 'Content-Type: application/json' -d '{}'
echo

echo
echo " ✓ Trip $TRIP_ID dispatching. Dispatch closes its epoch every 5s."
echo
echo " In Terminal 1 you should have seen:"
echo "   • [sync→] Trip → Offers.generate"
echo "   • [sync→] Offers → ETA.estimate"
echo "   • [sync→] Offers → Pricing.quote"
echo "   • [send→] Trip → Pricing.note_demand"
echo "   • [send→] Trip → Dispatch.enqueue_trip"
echo "   • [self→] Dispatch → close_epoch in 5s"
echo
echo " Within ~5s you'll see:"
echo "   • Dispatch close-epoch matched=1"
echo "   • [send→] Dispatch → Trip.assign_driver"
echo "   • [send→] Trip → SafetyAgent.start_monitoring"
echo "   • [self→] SafetyAgent → tick in 8s (start)"
echo
echo " Then check:   scripts/show-trip.sh $TRIP_ID"

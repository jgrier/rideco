#!/usr/bin/env bash
# Fire a rider request via /send (fire-and-forget). Use this when ETA might jam —
# you don't want the rider to hang waiting for a stuck retry.
# Usage: scripts/make-trip-send.sh <trip_id> <region>

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
echo " MAKE TRIP (fire-and-forget) — $TRIP_ID in $REGION"
echo "════════════════════════════════════════════════════════════════"
echo
echo " Trip.request_ride/send (fire-and-forget — rider doesn't wait)"
echo " The invocation is accepted into Bifrost. If ETA is currently"
echo " stuck on a poison-pill, that's fine — Restate will keep retrying"
echo " in the background without holding the rider's connection open."
echo
curl -s -X POST "$INGRESS/Trip/$TRIP_ID/request_ride/send" \
  -H 'Content-Type: application/json' \
  -d "{\"rider_id\":\"r-$TRIP_ID\",\"origin\":{\"lat\":$LAT,\"lng\":$LNG},\"destination\":{\"lat\":$DEST_LAT,\"lng\":$DEST_LNG},\"region\":\"$REGION\"}" \
  | python3 -m json.tool
echo
echo " ✓ Invocation accepted."
echo
echo " To see if it's stuck or progressing:"
echo "   scripts/show-invocations.sh"
echo "   scripts/show-trip.sh $TRIP_ID"

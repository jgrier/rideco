#!/usr/bin/env bash
# Initialize a region: publish baseline features (clear weather, low accidents)
# and put one driver online (idle, registered with the region's Dispatch pool).
# Usage: scripts/setup-region.sh <region>

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-$REPO_DIR/.venv/bin/python}"
INGRESS="${INGRESS:-http://localhost:8080}"

if [ $# -lt 1 ]; then
  echo "usage: $0 <region>"; exit 1
fi
REGION="$1"
DRIVER_ID="${DRIVER_ID:-d-$REGION-$(date +%s)}"

case "$REGION" in
  SF)  LAT=37.7749; LNG=-122.4194 ;;
  NYC) LAT=40.7128; LNG=-74.0060 ;;
  LA)  LAT=34.0522; LNG=-118.2437 ;;
  SEA) LAT=47.6062; LNG=-122.3321 ;;
  *) echo "unknown region: $REGION (expected SF, NYC, LA, SEA)"; exit 1 ;;
esac

echo "════════════════════════════════════════════════════════════════"
echo " SETUP REGION — $REGION"
echo "════════════════════════════════════════════════════════════════"
echo
echo " (1/3) Publishing baseline weather=clear via Kafka"
"$SCRIPT_DIR/set-feature.sh" "$REGION" weather clear
echo
echo " (2/3) Publishing baseline accident_density=0.05 via Kafka"
"$SCRIPT_DIR/set-feature.sh" "$REGION" accident_density 0.05
echo
echo " (3/3) Driver $DRIVER_ID going online in $REGION (sync HTTP)"
curl -s -X POST "$INGRESS/Locations/$DRIVER_ID/set_status" \
  -H 'Content-Type: application/json' \
  -d "{\"status\":\"idle\",\"region\":\"$REGION\"}"
echo
curl -s -X POST "$INGRESS/Locations/$DRIVER_ID/ping" \
  -H 'Content-Type: application/json' \
  -d "{\"lat\":$LAT,\"lng\":$LNG}" > /dev/null

echo
echo " ✓ Region $REGION ready."
echo "   weather:           clear"
echo "   accident_density:  0.05"
echo "   driver:            $DRIVER_ID (idle, registered with Dispatch[$REGION])"
echo
echo " In Terminal 1 you should have seen:"
echo "   • Features set key=region:$REGION:weather value=clear"
echo "   • Features set key=region:$REGION:accident_density value=0.05"
echo "   • [send→] Locations → Dispatch.register_driver"
echo "   • Dispatch driver+ pool=N"

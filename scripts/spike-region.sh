#!/usr/bin/env bash
# Force a region into unsafe territory until the RegionSafetyAgent halts it.
#
# We sustain the spike for ~25s by writing the unsafe values every few seconds
# in a loop. This wins against the mapping-sim's live emits — by the time the
# agent's next tick fires (10s cycle), at least one of the recent writes is
# still in place, so the composite risk score crosses threshold.
#
# Once halted, the demo continues without needing the spike to be maintained
# (the region stays halted until a human verdict comes in).
#
# Usage: ./scripts/spike-region.sh [region]   (default: SF)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INGRESS="${INGRESS:-http://localhost:8080}"
REGION="${1:-SF}"

echo "════════════════════════════════════════════════════════════════"
echo " SPIKE — pushing region=$REGION into unsafe territory"
echo "════════════════════════════════════════════════════════════════"
echo
echo " Setting accident_density=0.85 and weather=rain_heavy. Composite"
echo " risk threshold is 0.6. The RegionSafetyAgent[$REGION] ticks every"
echo " 10s — sustaining the spike for ~25s so the agent definitely sees"
echo " it on at least one tick (we're racing the live mapping-sim's"
echo " random emits)."
echo

END=$((SECONDS + 25))
while [ $SECONDS -lt $END ]; do
  curl -s -X POST "$INGRESS/Features/region:$REGION:accident_density/set" \
    -H 'Content-Type: application/json' -d '{"value":0.85}' > /dev/null
  curl -s -X POST "$INGRESS/Features/region:$REGION:weather/set" \
    -H 'Content-Type: application/json' -d '{"value":"rain_heavy"}' > /dev/null
  printf "."
  sleep 3
done
echo
echo
echo " ✓ Spike sustained. The agent should have halted dispatch by now."
echo
echo " Watch:"
echo "   ./scripts/show-region.sh $REGION         # region_active=false, pending_awakeable=sign_..."
echo "   ./scripts/show-invocations.sh            # the suspended agent"
echo
echo " Resume:"
echo "   ./scripts/approve.sh <awakeable> approve"

#!/usr/bin/env bash
# Push a region's accident_density past the SafetyAgent threshold (0.6).
# Any SafetyAgent ticking against a trip in this region will, on its next tick,
# read this feature, score risk ≥ 0.6, open an Awakeable, and suspend until
# a human operator resolves it via HTTP.
# Usage: scripts/escalate.sh [region]   (default: SF)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="${1:-SF}"

echo "════════════════════════════════════════════════════════════════"
echo " ESCALATE — region=$REGION"
echo "════════════════════════════════════════════════════════════════"
echo
echo " Setting region:$REGION:accident_density = 0.8"
echo
echo " This represents external accident reports / city traffic feeds"
echo " telling us conditions just got dangerous in $REGION."
echo
echo " Any SafetyAgent monitoring a trip in $REGION will, on its next"
echo " tick (~8s cycle), read this value, score risk ≥ 0.6, open an"
echo " Awakeable, and suspend pending a human operator's verdict."
echo

"$SCRIPT_DIR/set-feature.sh" "$REGION" accident_density 0.8

echo
echo " Next steps (wait ~10s for the next tick to fire):"
echo "   sleep 10"
echo "   scripts/show-agent.sh <trip_id>   # look for pending_awakeable"
echo "   scripts/show-invocations.sh       # the suspended tick is visible here"
echo
echo " In Terminal 1 (serve log) you'll see:"
echo "   SafetyAgent ESCALATE (suspending for human verdict) ..."
echo "   and a ready-to-paste awakeable id."
echo
echo " Resolve with:"
echo "   scripts/approve.sh <awakeable_id>"

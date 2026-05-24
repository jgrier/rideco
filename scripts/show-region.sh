#!/usr/bin/env bash
# Show a region's RegionSafetyAgent state + Dispatch state.
# Usage: ./scripts/show-region.sh <region>

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"
REGION="${1:-SF}"

echo "── RegionSafetyAgent[$REGION] ──"
curl -s -X POST "$INGRESS/RegionSafetyAgent/$REGION/get" \
  -H 'Content-Type: application/json' -d '{}' | python3 -m json.tool
echo
echo "── Dispatch[$REGION] ──"
curl -s -X POST "$INGRESS/Dispatch/$REGION/get" \
  -H 'Content-Type: application/json' -d '{}' | python3 -m json.tool
echo
echo "  Key fields:"
echo "    region_active / active   — false means region is HALTED (matching paused)"
echo "    last_score               — most recent risk score from the agent (>= 0.6 triggers halt)"
echo "    pending_trips_count      — trips queued in Dispatch (drains when active flips back true)"
echo "    pending_awakeable        — when set, the agent is suspended waiting for a verdict"

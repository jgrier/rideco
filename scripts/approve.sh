#!/usr/bin/env bash
# Resolve a SafetyAgent's suspended Awakeable as if you were the human operator.
# The agent resumes execution from exactly where it suspended.
# Usage: scripts/approve.sh <awakeable_id> [verdict]   (default verdict: approve)

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"

if [ $# -lt 1 ]; then
  echo "usage: $0 <awakeable_id> [verdict]"
  echo
  echo " find the awakeable id with:"
  echo "   scripts/show-region.sh <region>   # look for 'pending_awakeable'"
  echo " or look for 'ESCALATE (suspending for human verdict)' in the serve log"
  exit 1
fi

AID="$1"
VERDICT="${2:-approve}"

echo "════════════════════════════════════════════════════════════════"
echo " APPROVE — awakeable=$AID  verdict=$VERDICT"
echo "════════════════════════════════════════════════════════════════"
echo
echo " POSTing to Restate's awakeable ingress endpoint:"
echo "   $INGRESS/restate/awakeables/$AID/resolve"
echo
echo " Restate journals the resolution. On the next replay of the suspended"
echo " SafetyAgent.tick invocation, the awaited future returns with this"
echo " verdict, and the agent resumes from exactly where it left off."
echo
RESP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$INGRESS/restate/awakeables/$AID/resolve" \
  -H 'Content-Type: application/json' \
  -d "{\"verdict\":\"$VERDICT\",\"reviewer\":\"operator\"}")
echo " → HTTP $RESP_STATUS"
echo
echo " ✓ Awakeable resolved."
echo
echo " In Terminal 1 you should see (within ~1s):"
echo "   RegionSafety RESUMED region=<id> verdict=$VERDICT"
echo "   [send→] RegionSafety → Dispatch.set_active(true)  (if approve)"
echo "   [self→] RegionSafety → tick in 10s"
echo
echo " Verify with:"
echo "   scripts/show-region.sh <region>      # pending_awakeable should be null"
echo "   scripts/show-invocations.sh          # no more running invocations"

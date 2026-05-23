#!/usr/bin/env bash
# Poison-pill: publish a sentinel value the Features service can't process.
# Uses the /send endpoint so the publisher fires-and-forgets — same shape
# as a real upstream emitter. The Features.set invocation gets stuck in
# Restate's retry queue. Subsequent set() calls for the same key queue up
# behind it (per-key exclusive serialization). Other keys are unaffected.
#
# Usage: ./scripts/poison.sh [region]   (default: LA)

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"
REGION="${1:-LA}"
KEY="region:$REGION:weather"

echo "════════════════════════════════════════════════════════════════"
echo " POISON-PILL — region=$REGION  feature=weather"
echo "════════════════════════════════════════════════════════════════"
echo
echo " Publishing {\"value\": \"POISON\"} to Features/$KEY/set/send"
echo
echo " Features.set is wired to raise a non-Terminal ValueError on the"
echo " POISON sentinel. Restate retries the invocation forever with"
echo " exponential backoff. Because Features is a Virtual Object,"
echo " subsequent set() calls for the SAME key queue up behind the"
echo " stuck one — that's per-key failure isolation."
echo

curl -s -X POST "$INGRESS/Features/$KEY/set/send" \
  -H 'Content-Type: application/json' \
  -d '{"value":"POISON"}' \
  | python3 -m json.tool

echo
echo " ✓ Send accepted into the Restate log. The set() invocation is now"
echo "   stuck retrying."
echo
echo " Next steps:"
echo "   ./scripts/show-invocations.sh                  # see the stuck Features.set"
echo "   ./scripts/set-feature.sh $REGION weather clear  # this will queue behind it"
echo
echo " To fix: edit rideco/services/features.py → HANDLE_POISON_GRACEFULLY = True"
echo "   Then in Terminal 1: Ctrl+C → make serve"
echo "   Then in Terminal 2: ./scripts/register.sh"
echo "   Stuck invocation drains on next retry; the queued good message lands."

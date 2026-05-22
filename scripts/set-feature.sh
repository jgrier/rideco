#!/usr/bin/env bash
# Publish a feature update to Kafka, then wait until Restate's subscription
# has actually propagated the value into the Features VO. Returns only once
# the value is visible to readers — so calling code doesn't race ETA / Pricing.
#
# Usage: scripts/set-feature.sh <region> <feature_name> <value>

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-$REPO_DIR/.venv/bin/python}"
INGRESS="${INGRESS:-http://localhost:8080}"
KAFKA="${KAFKA:-localhost:29092}"

if [ $# -lt 3 ]; then
  echo "usage: $0 <region> <feature_name> <value>"
  echo "examples:"
  echo "  $0 SF weather rain_heavy"
  echo "  $0 SF accident_density 0.8"
  echo "  $0 LA weather BAD"
  exit 1
fi

REGION="$1"
NAME="$2"
RAW="$3"
KEY="region:$REGION:$NAME"

echo "→ Kafka publish: topic=mapping_events  key=$KEY  value=$RAW"
echo "  (representing an external mapping provider — weather/traffic/accidents — emitting an event)"

"$PYTHON" - "$KEY" "$RAW" "$KAFKA" <<'PYEOF'
import asyncio, json, sys
from aiokafka import AIOKafkaProducer
key, raw, bootstrap = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    value = float(raw) if "." in raw else int(raw)
except ValueError:
    value = raw
async def main():
    p = AIOKafkaProducer(bootstrap_servers=bootstrap); await p.start()
    await p.send_and_wait("mapping_events", key=key.encode(), value=json.dumps({"value": value}).encode())
    await p.stop()
asyncio.run(main())
PYEOF

# Poll Features VO until it reflects the published value. This is the bit that
# makes the demo deterministic — Kafka → Restate-subscription → Features.set
# is async; without polling, downstream callers can race ahead and read stale
# state.
echo "→ Restate subscription routes the record to Features(\"$KEY\").set()"
echo -n "→ Waiting for Features VO to reflect the new value"
ATTEMPT=0
MAX_ATTEMPTS=30
while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
  ATTEMPT=$((ATTEMPT+1))
  RESP=$(curl -s -X POST "$INGRESS/Features/$KEY/get" -H 'Content-Type: application/json' -d '{}' || true)
  MATCH=$("$PYTHON" - "$RESP" "$RAW" <<'PYEOF' || echo "no"
import sys, json
resp_raw, expected_raw = sys.argv[1], sys.argv[2]
try:
    expected = float(expected_raw) if "." in expected_raw else int(expected_raw)
except ValueError:
    expected = expected_raw
try:
    d = json.loads(resp_raw)
except Exception:
    print("no"); sys.exit()
if d.get("is_default", True):
    print("no"); sys.exit()
print("yes" if d.get("value") == expected else "no")
PYEOF
)
  if [ "$MATCH" = "yes" ]; then
    echo "  ✓ ($((ATTEMPT * 200))ms)"
    break
  fi
  echo -n "."
  sleep 0.2
done

if [ "$MATCH" != "yes" ]; then
  echo
  echo "  ⚠ timed out — Features VO did not reflect $KEY=$RAW within $((MAX_ATTEMPTS * 200))ms"
  echo "    (subscription might be broken; check 'scripts/register.sh' ran successfully)"
  exit 2
fi

echo "  See: http://localhost:9070  →  State  →  Features  →  $KEY"

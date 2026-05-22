#!/usr/bin/env bash
# Write a feature update directly to the Features VO via Restate's HTTP ingress.
# Sync POST so the call returns once the durable write is committed — no race
# against downstream readers.
#
# Usage: ./scripts/set-feature.sh <region> <feature_name> <value>

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"

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

# Detect numeric vs string for JSON encoding.
if [[ "$RAW" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
  JSON_VALUE="$RAW"
else
  JSON_VALUE="\"$RAW\""
fi

echo "→ Sync HTTP POST to Restate ingress  Features/$KEY/set"
echo "  body: {\"value\": $JSON_VALUE}"
echo "  (representing an external mapping provider — weather/traffic/accidents — emitting an event)"

curl -s -X POST "$INGRESS/Features/$KEY/set" \
  -H 'Content-Type: application/json' \
  -d "{\"value\": $JSON_VALUE}" \
  | python3 -m json.tool

echo "→ Durable write committed. The value is journaled on the Restate log"
echo "  and the Features VO state is now visible to ETA / Pricing / Dispatch /"
echo "  SafetyAgent on their next read."
echo
echo "  See: http://localhost:9070  →  State  →  Features  →  $KEY"

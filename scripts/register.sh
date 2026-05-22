#!/usr/bin/env bash
# Register the Python deployment AND the Kafka subscription with Restate.
# Idempotent — re-run any time to pick up code changes (uses --force).

set -euo pipefail
DEPLOYMENT="${DEPLOYMENT:-http://host.docker.internal:9080}"
ADMIN="${ADMIN:-http://localhost:9070}"

echo "→ register Python deployment ($DEPLOYMENT)"
restate -y deployments register --force "$DEPLOYMENT" | tail -n +3

echo
echo "→ register Kafka subscription: mapping_events → Features.set"
# Check if it already exists; if so, skip.
EXISTING=$(curl -s "$ADMIN/subscriptions" | python3 -c "
import sys, json
subs = json.load(sys.stdin).get('subscriptions', [])
for s in subs:
    if s.get('source','').endswith('/mapping_events') and s.get('sink','') == 'service://Features/set':
        print(s['id']); break
" || true)

if [ -n "$EXISTING" ]; then
  echo "  (already registered: $EXISTING)"
else
  curl -s -X POST "$ADMIN/subscriptions" \
    -H 'Content-Type: application/json' \
    --data '{"source":"kafka://rideco/mapping_events","sink":"service://Features/set","options":{"auto.offset.reset":"earliest"}}' \
    | python3 -c "import sys,json; print('  subscription:', json.load(sys.stdin)['id'])"
fi

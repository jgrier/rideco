#!/usr/bin/env bash
# Full state wipe: stop hypercorn, restart Restate (loses all state),
# wait for it, register the deployment.
# After this, you still need to `make serve` (in Terminal 1) before `register`.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "→ stopping hypercorn (if running)"
"$SCRIPT_DIR/stop.sh"

echo
echo "→ docker compose down (wipes all Restate state)"
(cd "$REPO_DIR" && docker compose down)

echo
echo "→ docker compose up -d"
(cd "$REPO_DIR" && docker compose up -d)

echo
echo "→ waiting for Restate admin to be ready..."
until curl -s -o /dev/null -m 1 http://localhost:9070/health; do sleep 0.5; done
echo "  ready"

echo
echo "✓ clean state. Now:"
echo "  Terminal 1:  make serve"
echo "  Terminal 2:  ./scripts/register.sh"

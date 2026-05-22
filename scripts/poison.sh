#!/usr/bin/env bash
# Poison-pill: write weather=BAD to a region's Features VO via the Restate log.
# ETA's _weather_penalty raises ValueError on "BAD" — non-Terminal exception
# → Restate retries forever with exponential backoff.
# Usage: scripts/poison.sh [region]   (default: LA)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGION="${1:-LA}"

echo "════════════════════════════════════════════════════════════════"
echo " POISON-PILL — region=$REGION"
echo "════════════════════════════════════════════════════════════════"
echo
echo " Setting region:$REGION:weather = \"BAD\""
echo
echo " ETA's _weather_penalty() doesn't know how to handle this sentinel."
echo " It raises a regular Python ValueError — NOT a TerminalError — so"
echo " Restate retries the invocation forever with exponential backoff."
echo " Other regions are unaffected (per-key isolation)."
echo

"$SCRIPT_DIR/set-feature.sh" "$REGION" weather BAD

echo
echo " Next steps:"
echo "   scripts/make-trip-send.sh t-poison-$REGION $REGION   # will jam in ETA"
echo "   scripts/show-invocations.sh                          # see the stuck retries"
echo
echo " To fix: edit rideco/services/eta.py → HANDLE_BAD_WEATHER_GRACEFULLY = True"
echo "   Then in Terminal 1: Ctrl+C → make serve"
echo "   Then in Terminal 2: scripts/register.sh"
echo "   Stuck invocations will drain on next retry."

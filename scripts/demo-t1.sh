#!/usr/bin/env bash
# T1 (THE SHOW) driver — handles fresh-start or post-edit restart, then
# hands the terminal over to hypercorn whose log scrolls during the demo.
#
# Usage:
#   ./scripts/demo-t1.sh          # interactive choice
#   ./scripts/demo-t1.sh fresh    # wipe state + start
#   ./scripts/demo-t1.sh restart  # just start hypercorn (after editing eta.py)
#
# This script is meant to live in Terminal 1. Terminal 2 runs ./scripts/demo-t2.sh.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

pause() { echo; echo "─── press ENTER to continue ───"; read -r; }

clear
echo "════════════════════════════════════════════════════════════════"
echo " RideCo demo — TERMINAL 1 (the show)"
echo "════════════════════════════════════════════════════════════════"
echo
echo " This terminal will host hypercorn — the Python process serving"
echo " all 8 Restate services. Its log scrolls below as the demo runs;"
echo " every cross-service hop is tagged [sync→] / [send→] / [self→]"
echo " so the audience can see sync RPC vs durable async send vs cadence"
echo " self-send at a glance."
echo
echo " Restate UI for visual confirmation:  http://localhost:9070"
echo

# Choose mode
MODE="${1:-}"
if [ -z "$MODE" ]; then
  echo " Pick a mode:"
  echo "   [f]  fresh start — wipe Restate + Kafka, start hypercorn"
  echo "   [r]  restart only — keep state, just (re)start hypercorn"
  echo "        (use this after editing rideco/services/eta.py in Phase 3)"
  echo
  read -p " Choice [f/r]: " choice
  case "$choice" in
    f|F|fresh)   MODE=fresh ;;
    r|R|restart) MODE=restart ;;
    *) echo "unknown choice"; exit 1 ;;
  esac
fi

if [ "$MODE" = "fresh" ]; then
  echo
  echo " ── (1/3) docker compose down — wiping Restate + Kafka state ──"
  pause
  docker compose down

  echo
  echo " ── (2/3) docker compose up -d ──"
  pause
  docker compose up -d
  echo "    waiting for Restate admin to be ready..."
  until curl -s -m 1 -o /dev/null http://localhost:9070/health; do sleep 0.5; done
  echo "    ready."

  echo
  echo " ── (3/3) start hypercorn — your log will scroll below ──"
  echo "    (in Terminal 2 now: ./scripts/demo-t2.sh)"
  pause
fi

if [ "$MODE" = "restart" ]; then
  echo
  echo " ── kill any lingering hypercorn, then start fresh ──"
  pause
  "$SCRIPT_DIR/stop.sh"
fi

# Hand the terminal over to hypercorn.
exec make serve

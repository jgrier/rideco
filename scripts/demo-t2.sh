#!/usr/bin/env bash
# T2 (COMMANDS) driver — guided demo walkthrough.
#
# Drives the demo against a LIVE system: rider, driver, and mapping-events
# sims run in the background while you step through the phases. The
# RegionSafetyAgent is the central character — it watches per-region
# features, halts dispatch when conditions get unsafe, and suspends on an
# awakeable until a human approves resume.
#
# Prereq: T1 is running (./scripts/demo-t1.sh fresh) and ideally T3 is
# running (./scripts/watch-regions.sh) so you can watch state flip live.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

pause() { echo; echo "─── press ENTER to continue ───"; read -r; }

run() {
  echo; echo "\$ $*"; echo
  "$@"
}

section() {
  echo
  echo "════════════════════════════════════════════════════════════════"
  echo " $1"
  echo "════════════════════════════════════════════════════════════════"
}

countdown() {
  local secs=$1
  while [ "$secs" -gt 0 ]; do
    printf "\r   waiting %2ds... " "$secs"; sleep 1; secs=$((secs - 1))
  done
  printf "\r   done           \n"
}

# Background sim PIDs (killed on exit)
RIDER_PID=""; DRIVER_PID=""; MAPPING_PID=""
cleanup_sims() {
  for pid in "$RIDER_PID" "$DRIVER_PID" "$MAPPING_PID"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup_sims EXIT

# ───── intro ─────────────────────────────────────────────────────────
clear
cat <<'EOF'
════════════════════════════════════════════════════════════════
 RideCo demo — TERMINAL 2 (commands)
════════════════════════════════════════════════════════════════

 Three phases, ENTER between steps:

   1. System alive — sims keep traffic flowing across four regions
   2. SF goes unsafe — RegionSafetyAgent halts dispatch + suspends
   3. Human approves — agent resumes, backlog drains

 Watch T1 (the log) and T3 (the dashboard) as we go.
EOF
pause

# ───── boot ──────────────────────────────────────────────────────────
section "BOOTSTRAP — register Python services with Restate"
echo
echo " Restate's ingress (:8080) routes every external call into the right"
echo " handler. register.sh tells Restate where to find the eight services."
pause
run ./scripts/register.sh
pause

# ───── PHASE 1 ───────────────────────────────────────────────────────
section "PHASE 1 — system is alive"
echo
echo " Starting three background sims that play the role of external"
echo " actors:"
echo "   driver-sim   — drivers online + pinging GPS"
echo "   mapping-sim  — weather + accident feeds (also kicks off the"
echo "                  Pricing.refresh and RegionSafetyAgent.tick loops)"
echo "   rider-sim    — trip requests across all regions"
pause

echo " starting sims..."
.venv/bin/python -m rideco.sim.driver --drivers 16 > /tmp/rideco-driver-sim.log 2>&1 &
DRIVER_PID=$!
.venv/bin/python -m rideco.sim.mapping_events --interval 12 > /tmp/rideco-mapping-sim.log 2>&1 &
MAPPING_PID=$!
sleep 3
.venv/bin/python -m rideco.sim.rider --riders 3 --rate 0.1 > /tmp/rideco-rider-sim.log 2>&1 &
RIDER_PID=$!

echo " letting the system settle..."
countdown 15
echo
echo " T3 should show all four regions active, low risk, Done climbing."
pause

run ./scripts/show-invocations.sh
echo
echo " Per-region scheduled invocations (Dispatch.close_epoch, Pricing.refresh,"
echo " RegionSafetyAgent.tick) are the cadence loops — all delayed self-sends."
pause

# ───── PHASE 2 ───────────────────────────────────────────────────────
section "PHASE 2 — SF goes unsafe"
echo
echo " RegionSafetyAgent[SF] ticks every 10s, scores its features, and"
echo " halts dispatch when the composite risk crosses the threshold. We'll"
echo " force the score by spiking SF's weather + accident_density:"
pause

run ./scripts/spike-region.sh SF
echo
echo " Waiting for the next agent tick..."
countdown 12
pause

run ./scripts/show-region.sh SF
echo
echo " region_active=false, halts=1, awakeable populated, Dispatch active=false."
echo " On T3: SF row is HALTED in red; NYC/LA/SEA keep working."
pause

# ───── PHASE 3 ───────────────────────────────────────────────────────
section "PHASE 3 — human approves resume"
echo
echo " You're the safety operator. Approve to resume dispatch. We read"
echo " the awakeable id straight off the agent's state — the same value"
echo " T3 is showing in the AWAKEABLE column."
AID=$(curl -s -X POST http://localhost:8080/RegionSafetyAgent/SF/get \
  -H 'Content-Type: application/json' -d '{}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin).get('pending_awakeable') or '')")
if [ -z "$AID" ]; then
  echo "   no pending awakeable — SF may have already resumed."
else
  echo "   $AID"
fi
pause

run ./scripts/approve.sh "$AID" approve
echo
echo " The agent resumes from suspend, sets Dispatch[SF].active=true, and"
echo " continues its tick loop. Next close_epoch (≤5s) drains the backlog."
countdown 8

run ./scripts/show-region.sh SF
echo
echo " active=true, pending draining. T3 shows SF green, Pending falling."
pause

# ───── wrap ──────────────────────────────────────────────────────────
section "DONE"
cat <<'EOF'

 One region went unsafe; the agent halted it and suspended on an
 awakeable. The other three regions kept matching the whole time. A
 human resolved the awakeable; the agent resumed; the backlog drained.

 All of it on three primitives: call(), send(), and awakeable. Plus
 delayed self-sends for cadence (Dispatch close_epoch every 5s, Pricing
 refresh every 10s, RegionSafetyAgent tick every 10s).

 No Kafka, no Redis, no workflow engine, no agent framework. Just
 Restate plus stateless application code.

 Reset:
   T1:  Ctrl+C, then ./scripts/demo-t1.sh fresh
   T2:  ./scripts/demo-t2.sh
EOF
echo
echo " (background sims will be killed when this terminal exits)"

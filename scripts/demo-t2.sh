#!/usr/bin/env bash
# T2 (COMMANDS) driver — guided demo walkthrough.
#
# This version drives the demo against a LIVE system: rider, driver, and
# mapping-events sims run in the background for the full walkthrough, so
# traffic keeps flowing as you step through the phases. The RegionSafetyAgent
# is the central character — it watches per-region features, halts dispatch
# when conditions get unsafe, and suspends on an awakeable until a human
# approves resume.
#
# Prereq: Terminal 1 is running ./scripts/demo-t1.sh fresh (or make serve).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

# ───── helpers ───────────────────────────────────────────────────────
pause() { echo; echo "─── press ENTER to continue ───"; read -r; }

run() {
  echo
  echo "\$ $*"
  echo
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
    printf "\r   waiting %2ds... " "$secs"
    sleep 1
    secs=$((secs - 1))
  done
  printf "\r   done           \n"
}

# Background sim PIDs (set when sims start; killed on exit)
RIDER_PID=""
DRIVER_PID=""
MAPPING_PID=""
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

 The system runs LIVE for the whole walkthrough. Three sims keep
 traffic flowing in the background: riders requesting trips,
 drivers pinging GPS, mapping providers publishing weather and
 accident data. The Restate log handles every call.

 What you're about to see, in 3 phases:

   1. System is alive — sims drive constant traffic across regions
   2. A region goes unsafe → RegionSafetyAgent halts dispatch for
      that region → suspends on an awakeable for a human verdict
   3. Human approves → dispatch resumes → backlog drains

 Every step pauses for ENTER. Watch Terminal 1 (the log) and
 http://localhost:9070 (Restate Web UI) between steps.

 Two SDK primitives + one awakeable:

   ▸ call()      synchronous — caller awaits the response
   ▸ send()      asynchronous — fire-and-forget into the Restate log;
                 delayed sends use the same primitive
   ▸ awakeable   pause an invocation on a token; resume when
                 another handler or HTTP POST resolves it

 Every handler supports both call() and send() — choice is at the
 call site.
EOF
pause

# ───── boot ──────────────────────────────────────────────────────────
section "BOOTSTRAP — register Python services with Restate"
echo
echo " Tells Restate where to find the eight services. Restate's ingress"
echo " on :8080 routes every external HTTP request (call() or send()) to"
echo " the right handler, with the Restate log doing the durable buffering."
pause
run ./scripts/register.sh
pause

# ───── PHASE 1 ───────────────────────────────────────────────────────
section "PHASE 1 — system is alive"
echo
echo " About to start three background sims:"
echo "   ▸ rider-sim     — fires trip requests across all regions"
echo "   ▸ driver-sim    — keeps drivers online + pinging GPS"
echo "   ▸ mapping-sim   — publishes weather + accident features"
echo "                     (also bootstraps Pricing.refresh and"
echo "                     RegionSafetyAgent.start_monitoring per region)"
echo
echo " Their output is sent to log files in /tmp/rideco-*-sim.log to"
echo " keep this terminal clean. Watch Terminal 1 for the live"
echo " inter-service activity."
pause

echo " starting sims..."
.venv/bin/python -m rideco.sim.driver --drivers 16 > /tmp/rideco-driver-sim.log 2>&1 &
DRIVER_PID=$!
.venv/bin/python -m rideco.sim.mapping_events --interval 12 > /tmp/rideco-mapping-sim.log 2>&1 &
MAPPING_PID=$!
sleep 3   # let drivers come online + mapping bootstrap RegionSafetyAgents
.venv/bin/python -m rideco.sim.rider --rate 0.3 > /tmp/rideco-rider-sim.log 2>&1 &
RIDER_PID=$!
echo "   driver-sim PID:  $DRIVER_PID"
echo "   mapping-sim PID: $MAPPING_PID"
echo "   rider-sim PID:   $RIDER_PID"
echo
echo " letting the system settle for ~15s — drivers register, mapping"
echo " bootstraps RegionSafetyAgent per region, riders begin requesting trips"
countdown 15
pause

run ./scripts/show-invocations.sh
echo
echo " You should see:"
echo "   ▸ scheduled  — Dispatch close_epoch loops, Pricing refresh loops,"
echo "                  RegionSafetyAgent tick loops, all per region"
echo "   ▸ running    — current handler invocations (trips, matches, etc.)"
pause

echo " snapshot of each region's safety agent + dispatch state:"
for region in SF NYC LA SEA; do
  echo
  echo "  $region:"
  curl -s -X POST "http://localhost:8080/RegionSafetyAgent/$region/get" \
    -H 'Content-Type: application/json' -d '{}' \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"    active={d.get('active')} region_active={d.get('region_active')} ticks={d.get('ticks')} halts={d.get('halts')} last_score={d.get('last_score')}\")
" || true
done
echo
echo " All regions should be active=true, region_active=true, low scores."
pause

# ───── PHASE 2 ───────────────────────────────────────────────────────
section "PHASE 2 — a region goes unsafe"
echo
echo " The RegionSafetyAgent ticks every 10s, reads its region's features"
echo " (weather, accident_density), computes a composite risk score, and"
echo " halts dispatch when the score crosses the threshold."
echo
echo " mapping-sim emits stay in safe ranges on their own, so we'll force"
echo " SF into unsafe territory deterministically:"
pause

run ./scripts/spike-region.sh SF
echo
echo " The RegionSafetyAgent[SF] ticks every 10s. Waiting for its next"
echo " evaluation, which will see the spiked features and halt dispatch."
countdown 12
pause

run ./scripts/show-region.sh SF
echo
echo " Expected:"
echo "   • RegionSafetyAgent: region_active=false, halts=1, pending_awakeable=sign_..."
echo "   • Dispatch:           active=false  (halted)"
echo "   • pending_trips_count > 0  (riders queuing — backlog)"
echo
echo " Copy the pending_awakeable value above."
pause

run ./scripts/show-invocations.sh
echo
echo " In Restate UI → Invocations: RegionSafetyAgent[SF]/tick is the"
echo " agent's suspended invocation. Notice other regions (NYC/LA/SEA)"
echo " keep matching normally — their Dispatch loops are still firing."
pause

# ───── PHASE 3 ───────────────────────────────────────────────────────
section "PHASE 3 — human approves the resume"
echo
echo " You're the human safety operator. You've reviewed the situation"
echo " and decide SF is OK to resume. POST a verdict to the awakeable."
echo
read -p " Paste the pending_awakeable id: " AID
pause

run ./scripts/approve.sh "$AID" approve
echo
countdown 3
echo
echo " The agent's tick handler resumed from suspend, set Dispatch[SF].active"
echo " back to true, and continues its monitoring loop. Within 5 seconds"
echo " Dispatch's next close_epoch will fire and drain the backlog."
countdown 6

run ./scripts/show-region.sh SF
echo
echo " region_active should be true again; pending_trips_count should be"
echo " decreasing as the matcher catches up on the backlog."
pause

# ───── wrap ──────────────────────────────────────────────────────────
section "DONE — what we just saw"
cat <<'EOF'

 Live traffic flowing across four regions the entire time. One region went
 unsafe, the RegionSafetyAgent picked it up on its next tick, halted dispatch,
 and suspended on an awakeable. Three other regions kept matching trips,
 completely unaffected. A human resolved the awakeable; the agent resumed,
 dispatch came back online, the queued backlog drained.

 Two SDK primitives plus one awakeable did all of it:

   ✓ call()  — synchronous, caller awaits
       External:  rider request_ride/confirm, driver set_status
       Internal:  Trip → Offers → ETA + Pricing
                  Dispatch → Locations.get_position
                  RegionSafetyAgent → Locations + Features

   ✓ send()  — asynchronous, fire-and-forget into the Restate log
       External:  mapping providers → Features.set
                  driver app → Locations.ping (high-frequency)
                  human operator → awakeable resolve
       Internal:  Trip → Dispatch.enqueue_trip (with awakeable token)
                  Trip → Pricing.note_demand / Locations.accept_trip
                  Locations → Dispatch.register/deregister_driver
                  RegionSafetyAgent → Dispatch.set_active
       Self:      Dispatch close_epoch (5s), Pricing refresh (10s),
                  RegionSafetyAgent tick (10s) — same primitive, just to
                  self with a send_delay.

   ✓ awakeable — suspend an invocation on a token
       RegionSafetyAgent suspends when it halts a region; the same
       invocation resumes when the operator POSTs the verdict.

 Stateless workers, no Kafka, no Redis, no separate workflow engine, no
 agent framework. Just Restate.

 Reset for another run:
   T1:  Ctrl+C, then ./scripts/demo-t1.sh fresh
   T2:  ./scripts/demo-t2.sh

 Background sims will be killed when this terminal closes.
EOF
echo
echo " (sims still running in background; they'll be killed when you exit)"

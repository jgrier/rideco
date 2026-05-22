#!/usr/bin/env bash
# T2 (COMMANDS) driver — the guided demo walkthrough. Pauses between every
# step so you can flip to Terminal 1 (the log), the Restate UI, or just talk.
#
# Prereq: Terminal 1 is running ./scripts/demo-t1.sh fresh (or make serve).
#
# Usage: ./scripts/demo-t2.sh

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

# ───── intro ─────────────────────────────────────────────────────────
clear
cat <<EOF
════════════════════════════════════════════════════════════════
 RideCo demo — TERMINAL 2 (commands)
════════════════════════════════════════════════════════════════

 What you're about to see, in 5 phases:

   1. One quiet trip end-to-end (the architecture in 30 log lines)
   2. Poison-pill in LA — failure isolates per-key
   3. Fix the code — watch the stuck retries drain
   4. Human-in-the-loop — SafetyAgent suspends on Awakeable,
      operator approves, agent resumes
   5. Trip completes — agent shuts down, lifecycle closes

 Every step pauses for ENTER. Flip to Terminal 1 or
 http://localhost:9070 (Restate Web UI) between steps if you want
 to poke around.
EOF
pause

# ───── boot ──────────────────────────────────────────────────────────
section "BOOTSTRAP — register Python services + Kafka subscription"
echo
echo " Tells Restate where to find the Python services and sets up the"
echo " mapping_events Kafka subscription that routes external feature"
echo " events into Features.set."
pause
run ./scripts/register.sh
pause

# ───── PHASE 1 ───────────────────────────────────────────────────────
section "PHASE 1 — one quiet trip, top to bottom"
echo
echo " Going to (a) seed SF with baseline features via Kafka, (b) put"
echo " one driver online, (c) fire one rider request + confirm, (d) wait"
echo " for the 5s dispatch round to match, (e) read final state."
echo
echo " Watch Terminal 1: the full sync chain + Bifrost sends + self-sends"
echo " will scroll. Watch Restate UI → Invocations: live invocation graph."
pause

run ./scripts/setup-region.sh SF
pause

run ./scripts/make-trip.sh t-1 SF
echo
echo " Dispatch closes its epoch every 5s. Waiting for the match..."
countdown 7
pause

run ./scripts/show-trip.sh t-1
echo
echo " status should be 'assigned', assigned_driver_id non-null."
echo " Restate UI → State → Trip → t-1 shows this same state."
echo
echo " Behind the scenes, a SafetyAgent has started monitoring t-1."
run ./scripts/show-agent.sh t-1
pause

# ───── PHASE 2 ───────────────────────────────────────────────────────
section "PHASE 2 — poison-pill in LA, SF stays healthy"
echo
echo " Inject weather=BAD into LA. ETA can't parse 'BAD' → ValueError →"
echo " Restate retries forever with exponential backoff (per-key)."
echo " Other regions are unaffected — that's the slide."
pause

run ./scripts/setup-region.sh LA
pause

run ./scripts/poison.sh LA
pause

echo
echo " Fire an LA trip (fire-and-forget — rider doesn't wait for a stuck offer)"
run ./scripts/make-trip-send.sh t-poison-LA LA
pause

echo
echo " Now an SF trip — same code path, healthy region. Should sail through."
run ./scripts/make-trip.sh t-healthy-SF SF
echo
countdown 3
pause

echo
echo " Running invocations — LA is jammed, SF was never blocked."
run ./scripts/show-invocations.sh
echo
echo " Restate UI → Invocations: you'll see Trip/t-poison-LA/request_ride"
echo " and Offers/generate climbing in duration. Failure isolated per-key."
pause

# ───── PHASE 3 ───────────────────────────────────────────────────────
section "PHASE 3 — fix the code, watch the drain"
echo
echo " Now you fix the bug. Three things in order:"
echo
echo "   1. In your editor: open  rideco/services/eta.py"
echo "      Find:  HANDLE_BAD_WEATHER_GRACEFULLY = False"
echo "      Change to: True"
echo "      Save."
echo
echo "   2. In Terminal 1:  Ctrl+C to stop hypercorn"
echo "      Then run:  ./scripts/demo-t1.sh restart"
echo "      (or just: make serve)"
echo
echo "   3. Return here and press ENTER."
pause

run ./scripts/register.sh
echo
echo " Now wait for Restate to retry the stuck invocation. Exponential"
echo " backoff: first retry ~1s, 2s, 4s, 8s, 16s, ... give it ~15s."
countdown 15
pause

run ./scripts/show-invocations.sh
echo
echo " Should be 0 running. The stuck invocation hit the fixed code on"
echo " its next retry, succeeded, and completed."
pause

run ./scripts/show-trip.sh t-poison-LA
echo
echo " Trip should now be status=quoted with a real offer. Drained."
pause

# ───── PHASE 4 ───────────────────────────────────────────────────────
section "PHASE 4 — human-in-the-loop (AI agent escalation)"
echo
echo " t-1's SafetyAgent has been ticking quietly every 8s at risk≈0.2."
echo " We're going to bump SF's accident_density to 0.8 via Kafka. On its"
echo " next tick, the agent reads the new feature, scores risk ≥ 0.6,"
echo " creates an Awakeable, and suspends. No process is held in memory."
pause

run ./scripts/escalate.sh SF
pause

echo
echo " Wait for the SafetyAgent's next tick (8s cycle)..."
countdown 10
pause

run ./scripts/show-agent.sh t-1
echo
echo " pending_awakeable should be a 'sign_...' id. Copy it."
echo
echo " Restate UI → Invocations: SafetyAgent/t-1/tick will show as"
echo " 'running' — but it's actually suspended on the awakeable."
echo
read -p " Paste the pending_awakeable id: " AID
pause

run ./scripts/approve.sh "$AID"
echo
countdown 2

run ./scripts/show-agent.sh t-1
echo
echo " pending_awakeable=null, escalations=1. Agent resumed and scheduled"
echo " its next tick. T1 should have shown: SafetyAgent RESUMED verdict=approve"
pause

# ───── PHASE 5 ───────────────────────────────────────────────────────
section "PHASE 5 — complete the trip"
echo
echo " The ride ends. Trip.complete is a terminal state transition that"
echo " also fires Bifrost send to SafetyAgent.stop_monitoring."
pause

run ./scripts/complete-trip.sh t-1
echo
countdown 9

run ./scripts/show-trip.sh t-1
run ./scripts/show-agent.sh t-1
echo
echo " Trip status=completed. Agent active=false."
pause

# ───── wrap ──────────────────────────────────────────────────────────
section "DONE — what we just saw"
cat <<EOF

 Four primitives, demonstrated end-to-end:

   • Virtual Objects as per-key durable state
       Trip, Locations, Pricing, Features, SafetyAgent — all single-writer,
       all durable, all live in the same runtime.

   • Function-shaped composition with Bifrost durability under the hood
       [sync→] looks like RPC, [send→] looks like fire-and-forget, but
       both are journaled, retryable, and observable.

   • Self-scheduled cadence
       Dispatch's 5s epoch, Pricing's 10s refresh, SafetyAgent's 8s tick.
       No cron, no Airflow, no separate scheduler.

   • Awakeables for human-in-the-loop
       Agent suspended for the human's verdict — no process held in
       memory, deterministic replay.

 One Kafka topic at the trust-boundary edge. Everything else on Bifrost.

 Reset for another run:
   T1:  Ctrl+C, then ./scripts/demo-t1.sh fresh
   T2:  ./scripts/demo-t2.sh
   Also: flip HANDLE_BAD_WEATHER_GRACEFULLY back to False
EOF

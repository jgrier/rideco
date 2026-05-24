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

 What you're about to see, in 3 phases:

   1. One quiet trip end-to-end (the architecture in ~30 log lines)
   2. Human-in-the-loop — SafetyAgent suspends on an awakeable,
      operator approves, the same invocation resumes
   3. Trip completes — agent shuts down, lifecycle closes

 Every step pauses for ENTER. Flip to Terminal 1 or
 http://localhost:9070 (Restate Web UI) between steps if you want
 to poke around.

 Every interaction is one of two SDK primitives plus an awakeable
 for human-in-the-loop:

   ▸ call()                     synchronous — caller awaits the response
                                ([sync→] in T1). Writes to the Restate log
                                first, then blocks until the worker
                                completes.
   ▸ send()                     asynchronous — fire-and-forget into the
                                Restate log ([send→] in T1). Returns
                                immediately. Delayed sends ([self→] when
                                targeting self) replace external schedulers.
   ▸ awakeable                  pause an invocation on a token; resume
                                when another handler — or an external HTTP
                                POST — resolves it. No process held while
                                waiting.

 Every handler supports both call() and send() — the choice is at
 the call site.
EOF
pause

# ───── boot ──────────────────────────────────────────────────────────
section "BOOTSTRAP — register Python services with Restate"
echo
echo " Tells Restate where to find the Python services. Restate's ingress"
echo " on :8080 then routes every external HTTP request (call() or send())"
echo " to the right handler, with the Restate log doing the durable buffering."
pause
run ./scripts/register.sh
pause

# ───── PHASE 1 ───────────────────────────────────────────────────────
section "PHASE 1 — one quiet trip, top to bottom"
echo
echo " Channels in this phase:"
echo "   ▸ call()                   rider request_ride/confirm (Trip VO)"
echo "                              driver set_status (Locations VO)"
echo "                              Trip → Offers → ETA + Pricing"
echo "   ▸ send()                   mapping providers → Features.set"
echo "                              Trip → Pricing.note_demand, Dispatch.enqueue_trip,"
echo "                              Locations.accept_trip, SafetyAgent.start_monitoring"
echo "                              Locations → Dispatch.register_driver"
echo "   ▸ delayed send()           Dispatch close_epoch (every 5s),"
echo "                              SafetyAgent tick (every 8s)"
echo
echo " Going to (a) seed SF with baseline features (durable HTTP sends), (b) put"
echo " one driver online, (c) fire one rider request + confirm, (d) wait"
echo " for the 5s dispatch round to match, (e) read final state."
echo
echo " Watch Terminal 1: the full sync chain + durable async sends + self-sends"
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
section "PHASE 2 — human-in-the-loop (AI agent escalation)"
echo
echo " Channels in this phase:"
echo "   ▸ send()                   publish accident_density=0.8 into Features"
echo "   ▸ ctx.run                  mocked LLM risk score, journaled for replay"
echo "   ▸ awakeable                agent suspends cleanly; no process held;"
echo "                              operator POSTs verdict to resume the same"
echo "                              invocation from exactly where it stopped"
echo
echo " t-1's SafetyAgent has been ticking quietly every 8s at risk≈0.2."
echo " We're going to bump SF's accident_density to 0.8. On its next tick,"
echo " the agent reads the new feature, scores risk ≥ 0.6, creates an"
echo " awakeable, and suspends. No process is held in memory."
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

# ───── PHASE 3 ───────────────────────────────────────────────────────
section "PHASE 3 — complete the trip"
echo
echo " Channels in this phase:"
echo "   ▸ call()                   Trip.complete"
echo "   ▸ send()                   Trip → SafetyAgent.stop_monitoring"
echo
echo " The ride ends. Trip.complete is the terminal state transition; it"
echo " also send()s to SafetyAgent.stop_monitoring so the agent shuts down."
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

 Two SDK primitives exercised across the three phases:

   ✓ call() — caller awaits the response
       External:  rider request_ride/confirm/cancel, driver set_status,
                  app complete, operator awakeable resolve
       Internal:  Trip → Offers → ETA + Pricing
                  Dispatch → Locations.get_position
                  SafetyAgent → Locations + Features

   ✓ send() — fire-and-forget into the Restate log
       External:  mapping providers → Features.set
                  driver app → Locations.ping
       Internal:  Trip → Pricing.note_demand / Dispatch.enqueue_trip
                  Trip → Locations.accept_trip
                  Trip → SafetyAgent.start_monitoring / stop_monitoring
                  Locations → Dispatch.register_driver / deregister_driver
       Self:      Dispatch close_epoch (5s), Pricing refresh (10s),
                  SafetyAgent tick (8s) — same primitive, just to self
                  with a send_delay; replaces external schedulers.

       (No Kafka — the Restate log handles the durable input queue job
       for every handler entry point, transparently.)

 One awakeable for human-in-the-loop:

   ✓ SafetyAgent suspends on a token; operator POSTs the verdict; the
     same invocation resumes from exactly where it stopped. No Python
     process held during the wait.

 The Restate primitives underneath:

   • Virtual Objects as per-key durable state
       Trip, Locations, Pricing, Features, SafetyAgent — single-writer,
       durable, live in the same runtime.

   • Function-shaped composition with Restate-log durability
       [sync→] looks like RPC, [send→] looks like fire-and-forget — both
       are journaled, retryable, observable.

   • ctx.run for non-deterministic side effects
       Mocked LLM call inside SafetyAgent. Replays deterministically
       because the result is journaled.

   • Awakeables for human-in-the-loop
       Same invocation suspends and resumes; no Python process held.

 No Kafka. Every external write and every internal hop is on the Restate log.

 Reset for another run:
   T1:  Ctrl+C, then ./scripts/demo-t1.sh fresh
   T2:  ./scripts/demo-t2.sh
   Also: flip HANDLE_BAD_WEATHER_GRACEFULLY back to False in rideco/services/eta.py
EOF

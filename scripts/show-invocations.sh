#!/usr/bin/env bash
# Show all in-flight Restate invocations across every status:
#   - running     handler is currently executing on a worker
#   - scheduled   waiting for a delayed send (cadence loop) to fire
#   - pending     queued behind an exclusive handler holding the key
#   - backing-off in retry backoff (poison-pill case — handler raised non-Terminal)
#   - suspended   waiting on an awakeable (human-in-the-loop case)

set -euo pipefail
echo "→ Restate invocations (all in-flight statuses):"
echo
restate -y invocations list
echo
echo "  Status guide:"
echo "    running       handler executing right now"
echo "    scheduled     delayed send awaiting its fire time (cadence loops)"
echo "    pending       queued behind exclusive handler that's holding the key"
echo "    backing-off   retrying after a non-Terminal error (POISON-pill case)"
echo "    suspended     waiting on an awakeable (SafetyAgent → human verdict)"
echo
echo "  Also visible: http://localhost:9070  →  Invocations"

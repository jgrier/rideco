#!/usr/bin/env bash
# Show currently-running invocations in Restate. This is where you see:
#   - Stuck poison-pill invocations (Target: Trip/.../request_ride, Offers/generate, ETA/estimate)
#   - Suspended SafetyAgent ticks waiting on an awakeable
#   - Scheduled cadence loops (Dispatch/close_epoch, Pricing/refresh, SafetyAgent/tick)

set -euo pipefail
echo "→ Restate invocations (running + scheduled):"
echo
restate -y invocations list --status running
echo
echo "  A 'running' invocation that's been running for many seconds is either:"
echo "    1. Stuck in a retry loop (poison-pill case — ETA can't parse the input)"
echo "    2. Suspended on an awakeable (SafetyAgent case — waiting for a human)"
echo
echo "  Also visible: http://localhost:9070  →  Invocations"

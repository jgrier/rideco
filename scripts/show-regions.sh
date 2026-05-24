#!/usr/bin/env bash
# At-a-glance view of all four regions' safety + dispatch state.
# Usage: ./scripts/show-regions.sh

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"

printf "%-6s | %-12s | %-7s | %-12s | %-8s | %-14s | %s\n" \
  REGION ACTIVE? HALTS RISK_SCORE EPOCH DRIVERS/PENDING AWAKEABLE
printf "%s\n" "------+--------------+---------+--------------+----------+----------------+------------------------------------------"

for r in SF NYC LA SEA; do
  AGENT=$(curl -s -X POST "$INGRESS/RegionSafetyAgent/$r/get" -H 'Content-Type: application/json' -d '{}')
  DISP=$(curl -s -X POST "$INGRESS/Dispatch/$r/get" -H 'Content-Type: application/json' -d '{}')
  python3 - "$r" "$AGENT" "$DISP" <<'PYEOF'
import sys, json
region, agent_raw, disp_raw = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    a = json.loads(agent_raw)
    d = json.loads(disp_raw)
except Exception:
    print(f"{region:<6} | ERROR")
    sys.exit()
region_active = a.get("region_active")
active_str = "HALTED" if region_active is False else ("active" if region_active else "—")
halts = a.get("halts", 0)
score = a.get("last_score")
score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "—"
epoch = d.get("epoch_id", 0)
drv_pend = f"{d.get('active_driver_count',0)} / {d.get('pending_trips_count',0)}"
awk = a.get("pending_awakeable") or "—"
if len(awk) > 40: awk = awk[:37] + "..."
print(f"{region:<6} | {active_str:<12} | {halts:<7} | {score_str:<12} | {epoch:<8} | {drv_pend:<14} | {awk}")
PYEOF
done
echo
echo "  ACTIVE? — 'HALTED' means dispatch is paused for that region (trips queue, no matching)."
echo "  HALTS   — how many times the RegionSafetyAgent has halted this region since start."
echo "  RISK    — most recent composite risk score (>= 0.6 triggers a halt)."
echo "  DRIVERS/PENDING — registered idle drivers / trips queued waiting for a match."
echo "  AWAKEABLE — when non-empty, the agent is suspended waiting for a human verdict."

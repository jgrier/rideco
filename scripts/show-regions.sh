#!/usr/bin/env bash
# At-a-glance view of all four regions' safety + dispatch state.
# Usage: ./scripts/show-regions.sh

set -euo pipefail
INGRESS="${INGRESS:-http://localhost:8080}"

printf "%-6s | %-7s | %-5s | %-5s | %-5s | %-4s | %-7s | %-9s | %-5s | %s\n" \
  REGION ACTIVE? HALTS RISK EPOCH IDLE PENDING IN-FLIGHT DONE AWAKEABLE
printf "%s\n" "------+---------+-------+-------+-------+------+---------+-----------+-------+------------------------------------------"

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
idle = d.get("active_driver_count", 0)
pending = d.get("pending_trips_count", 0)
in_flight = d.get("in_flight", 0)
done = d.get("total_completed", 0)
awk = a.get("pending_awakeable") or "—"
if len(awk) > 40: awk = awk[:37] + "..."
print(f"{region:<6} | {active_str:<7} | {halts:<5} | {score_str:<5} | {epoch:<5} | {idle:<4} | {pending:<7} | {in_flight:<9} | {done:<5} | {awk}")
PYEOF
done
echo
echo "  ACTIVE?    — 'HALTED' means dispatch is paused (trips queue, no matching)."
echo "  HALTS      — cumulative halts by the RegionSafetyAgent for this region."
echo "  RISK       — most recent composite risk score (>= 0.6 triggers a halt)."
echo "  IDLE       — registered idle drivers ready to be matched."
echo "  PENDING    — trips queued waiting for a match."
echo "  IN-FLIGHT  — trips matched and currently being driven."
echo "  DONE       — trips completed since start (cumulative)."
echo "  AWAKEABLE  — non-empty when the agent is suspended waiting for a human verdict."

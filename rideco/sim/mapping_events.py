"""Mapping-events injector — writes directly to the Restate log.

Stands in for external feeds: weather APIs, traffic providers, accident
reports. Each emit is a `send()` to `Features.set` via Restate's HTTP
ingress. Every record is durable from the moment Restate acks.

On startup, also bootstraps two per-region background loops:
- `Pricing.refresh` — periodic multiplier recompute
- `RegionSafetyAgent.start_monitoring` — the per-region monitor that
  halts dispatch when conditions become unsafe

The sim occasionally produces a "fault drift" — a high accident_density
+ severe weather emit for one region — so the RegionSafetyAgent has
something to react to during a live demo.
"""

import argparse
import asyncio
import random

from rideco.shared.log import log
from rideco.shared.regions import all_regions
from rideco.shared.types import ENTITY_REGION, feature_key
from rideco.sim._ingress import send_object


WEATHER_OPTIONS = ["clear", "clear", "clear", "rain_light", "rain_heavy", "fog"]

# Probability per emit that this region's reading is a "fault drift" — pushed
# into unsafe territory. With ~4 regions emitting every interval, gives
# roughly one organic halt every few minutes.
FAULT_DRIFT_PROB = 0.05


async def _publish(key: str, value) -> None:
    """Fire-and-forget write into the Features VO via the Restate log."""
    await send_object("Features", key, "set", {"value": value})
    log("mapping", f"send → Features.set", flow="send", key=key, value=value)


async def _bootstrap(regions: list[str]) -> None:
    """Per-region one-time initialization on sim startup."""
    for region in regions:
        await send_object("Pricing", region, "refresh", {})
        log("mapping", "bootstrapped Pricing.refresh", flow="send", region=region)
        await send_object("RegionSafetyAgent", region, "start_monitoring", {})
        log("mapping", "bootstrapped RegionSafetyAgent.start_monitoring", flow="send", region=region)


async def _emit_region(region: str) -> None:
    if random.random() < FAULT_DRIFT_PROB:
        # Fault drift — conditions pushed into the agent's halt range.
        weather = random.choice(["snow", "rain_heavy"])
        accidents = round(random.uniform(0.65, 0.9), 2)
        log("mapping", "FAULT DRIFT", region=region, weather=weather, accidents=accidents)
    else:
        weather = random.choice(WEATHER_OPTIONS)
        accidents = round(random.betavariate(2, 8), 2)
    await _publish(feature_key(ENTITY_REGION, region, "weather"), weather)
    await _publish(feature_key(ENTITY_REGION, region, "accident_density"), accidents)


async def _amain(regions: list[str], interval: float) -> None:
    log("mapping", "starting", regions=",".join(regions), interval=interval)
    await _bootstrap(regions)
    while True:
        for region in regions:
            try:
                await _emit_region(region)
            except Exception as e:
                log("mapping", f"emit error: {type(e).__name__}: {e}", region=region)
        await asyncio.sleep(interval)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--regions", default=",".join(all_regions()))
    p.add_argument("--interval", type=float, default=15.0,
                   help="seconds between feature refresh sweeps")
    args = p.parse_args()
    asyncio.run(_amain(args.regions.split(","), args.interval))


if __name__ == "__main__":
    main()

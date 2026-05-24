"""Mapping-events injector — writes directly to the Restate log.

Stands in for external feeds: weather APIs, traffic providers, accident
reports. Each emit is a `send()` to `Features.set` via Restate's HTTP
ingress. Every record is durable from the moment Restate acks.

On startup, also bootstraps two per-region background loops:
- `Pricing.refresh` — periodic multiplier recompute
- `RegionSafetyAgent.start_monitoring` — the per-region monitor that
  halts dispatch when conditions become unsafe

By default emits stay within safe ranges (low accident_density, mild
weather). Use `./scripts/spike-region.sh <region>` to deterministically
push one region into the halt zone. The previous organic-drift feature
was removed because, given enough demo time, it caused every region to
halt, leaving the system fully jammed.
"""

import argparse
import asyncio
import random

from rideco.shared.log import log
from rideco.shared.regions import all_regions
from rideco.shared.types import ENTITY_REGION, feature_key
from rideco.sim._ingress import send_object


# Safe-only weather options. The composite risk scorer only adds points for
# "snow", "rain_heavy", or "fog"; we keep those out of the rotation (except
# light fog, which alone can't cross the halt threshold) so organic emits
# never trigger a halt on their own.
WEATHER_OPTIONS = ["clear", "clear", "clear", "clear", "clear", "rain_light"]

# Hard cap on accident_density. The agent's risk scorer adds 0.5 for
# accidents >= 0.5; we stay strictly below that.
MAX_ACCIDENTS = 0.35


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
    # Safe ranges only. Use spike-region.sh to push a region unsafe on demand.
    weather = random.choice(WEATHER_OPTIONS)
    accidents = round(random.uniform(0.0, MAX_ACCIDENTS), 2)
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

"""Mapping-events injector — writes directly to the Restate log.

In a typical streaming stack, external feeds (weather APIs, traffic
providers, accident reports) would publish to Kafka and a stream processor
would consume + write to a feature store. Here we skip Kafka entirely:
external feeds just `POST` to Restate's ingress, which writes durably to
the Restate log on the way to the `Features` Virtual Object.

The fire-and-forget shape is preserved (`/send` endpoint — caller doesn't
wait for the write to complete). Every record is durable from the moment
Restate acknowledges receipt.

Also kicks off `Pricing.refresh` per region on startup so the periodic
multiplier refresh loop is running.
"""

import argparse
import asyncio
import random

from rideco.shared.log import log
from rideco.shared.regions import all_regions
from rideco.shared.types import ENTITY_REGION, feature_key
from rideco.sim._ingress import send_object


WEATHER_OPTIONS = ["clear", "clear", "clear", "rain_light", "rain_heavy", "fog"]


async def _publish(key: str, value) -> None:
    """Fire-and-forget write into the Features VO via the Restate log."""
    await send_object("Features", key, "set", {"value": value})
    log("mapping", f"durable async send → Features.set", flow="send", key=key, value=value)


async def _bootstrap_pricing(regions: list[str]) -> None:
    for region in regions:
        await send_object("Pricing", region, "refresh", {})
        log("mapping", "bootstrapped Pricing.refresh", flow="send", region=region)


async def _emit_region(region: str) -> None:
    weather = random.choice(WEATHER_OPTIONS)
    accidents = round(random.betavariate(2, 8), 2)
    await _publish(feature_key(ENTITY_REGION, region, "weather"), weather)
    await _publish(feature_key(ENTITY_REGION, region, "accident_density"), accidents)


async def _poison_region(region: str) -> None:
    log("mapping", "POISONING — weather=BAD will jam ETA", flow="send", region=region)
    await _publish(feature_key(ENTITY_REGION, region, "weather"), "BAD")


async def _amain(regions: list[str], interval: float, poison: str | None) -> None:
    log("mapping", "starting", regions=",".join(regions), interval=interval)
    await _bootstrap_pricing(regions)
    if poison:
        await _poison_region(poison)
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
    p.add_argument("--poison", default=None,
                   help="region code to inject the BAD weather sentinel (e.g. SF)")
    args = p.parse_args()
    asyncio.run(_amain(args.regions.split(","), args.interval, args.poison))


if __name__ == "__main__":
    main()

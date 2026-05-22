"""Driver simulator.

Each driver registers as idle in one region, then pings GPS at a steady cadence.
Periodically toggles status to mimic ride completion so the matched-pool churns
visibly during the demo.
"""

import argparse
import asyncio
import random

from rideco.shared.log import log
from rideco.shared.regions import REGIONS, all_regions
from rideco.shared.types import DRIVER_IDLE
from rideco.sim._ingress import call_object, send_object


def _jitter(center: dict, radius: float = 0.05) -> dict:
    return {
        "lat": center["lat"] + random.uniform(-radius, radius),
        "lng": center["lng"] + random.uniform(-radius, radius),
    }


async def _driver_loop(driver_id: str, region: str, ping_interval: float) -> None:
    center = REGIONS[region]["center"]
    pos = _jitter(center, 0.04)

    # Initial registration as idle in the region.
    log("driver-sim", "Locations.set_status", flow="sync", driver=driver_id, region=region)
    await call_object("Locations", driver_id, "set_status", {
        "status": DRIVER_IDLE,
        "region": region,
    })
    # Bump the region's supply counter (drives Pricing's multiplier).
    log("driver-sim", "Pricing.note_supply", flow="send", driver=driver_id, region=region)
    await send_object("Pricing", region, "note_supply", {"delta": 1})
    log("driver-sim", "online", driver=driver_id, region=region)

    while True:
        # GPS drift.
        pos = {
            "lat": pos["lat"] + random.uniform(-0.0008, 0.0008),
            "lng": pos["lng"] + random.uniform(-0.0008, 0.0008),
        }
        try:
            await send_object("Locations", driver_id, "ping",
                              {"lat": pos["lat"], "lng": pos["lng"]})
        except Exception as e:
            log("driver-sim", f"ping error: {type(e).__name__}: {e}", driver=driver_id)
        await asyncio.sleep(ping_interval)


async def _amain(num_drivers: int, regions: list[str], ping_interval: float) -> None:
    log("driver-sim", "starting", drivers=num_drivers,
        regions=",".join(regions), ping_interval=ping_interval)
    tasks = []
    for i in range(num_drivers):
        region = random.choice(regions)
        tasks.append(_driver_loop(f"driver-{i:03d}", region, ping_interval))
    await asyncio.gather(*tasks)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--drivers", type=int, default=12)
    p.add_argument("--regions", default=",".join(all_regions()))
    p.add_argument("--ping-interval", type=float, default=2.0)
    args = p.parse_args()
    asyncio.run(_amain(args.drivers, args.regions.split(","), args.ping_interval))


if __name__ == "__main__":
    main()

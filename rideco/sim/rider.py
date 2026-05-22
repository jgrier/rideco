"""Rider simulator.

Picks a region at random, fires a Trip.request_ride to a synthetic trip_id,
then a Trip.confirm. Restate's runtime carries the rest. Each rider runs on
its own cadence so the dispatch round has multiple trips to chew on.
"""

import argparse
import asyncio
import random
import uuid

from rideco.shared.log import log
from rideco.shared.regions import REGIONS, all_regions
from rideco.sim._ingress import call_object, send_object


def _jitter(center: dict, radius: float = 0.03) -> dict:
    return {
        "lat": center["lat"] + random.uniform(-radius, radius),
        "lng": center["lng"] + random.uniform(-radius, radius),
    }


async def _one_request(rider_id: str, region: str) -> None:
    center = REGIONS[region]["center"]
    origin = _jitter(center, 0.05)
    destination = _jitter(center, 0.06)
    trip_id = f"trip-{uuid.uuid4().hex[:8]}"

    log("rider-sim", "Trip.request_ride", flow="sync", rider=rider_id, region=region, trip=trip_id)
    offer = await call_object("Trip", trip_id, "request_ride", {
        "rider_id": rider_id,
        "origin": origin,
        "destination": destination,
        "region": region,
    })
    log("rider-sim", "offer", trip=trip_id,
        eta=offer.get("eta_seconds"), price=offer.get("price_cents"),
        mult=offer.get("multiplier"), rel=offer.get("reliability_score"))

    await asyncio.sleep(0.3)
    log("rider-sim", "Trip.confirm", flow="send", trip=trip_id)
    await send_object("Trip", trip_id, "confirm", {})


async def _rider_loop(rider_id: str, regions: list[str], rate: float) -> None:
    while True:
        region = random.choice(regions)
        try:
            await _one_request(rider_id, region)
        except Exception as e:
            log("rider-sim", f"error: {type(e).__name__}: {e}", rider=rider_id)
        # Poisson-ish inter-arrival
        await asyncio.sleep(random.expovariate(rate))


async def _amain(num_riders: int, regions: list[str], rate: float) -> None:
    log("rider-sim", "starting", riders=num_riders, regions=",".join(regions), rate=rate)
    await asyncio.gather(*[
        _rider_loop(f"rider-{i:03d}", regions, rate)
        for i in range(num_riders)
    ])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--riders", type=int, default=6)
    p.add_argument("--regions", default=",".join(all_regions()))
    p.add_argument("--rate", type=float, default=0.4,
                   help="per-rider request rate (requests/sec)")
    args = p.parse_args()
    asyncio.run(_amain(args.riders, args.regions.split(","), args.rate))


if __name__ == "__main__":
    main()

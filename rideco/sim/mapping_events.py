"""Mapping-events injector — publishes to Kafka.

This is the **one** place RideCo uses Kafka. It models the external feeds
(third-party weather services, government traffic feeds, city accident
reports) that cross a trust/security boundary into the platform. In
production those feeds genuinely live on Kafka and fan out to multiple
internal consumers.

Restate has a subscription registered against the `mapping_events` topic
that routes each record to `Features.set`. The Kafka record's KEY becomes
the Virtual Object KEY (e.g. `region:SF:weather`), and the JSON VALUE
becomes the `set` handler's payload.

Everything else in RideCo (driver pings, trip lifecycle, dispatch
internals, pricing refresh, safety ticks) is Restate log — see the README's
"Sync vs async" table.

Also kicks off Pricing.refresh per region on startup via direct HTTP —
that bootstrap isn't an event, just a one-time enable of the periodic
refresh loop.
"""

import argparse
import asyncio
import json
import os
import random

from aiokafka import AIOKafkaProducer

from rideco.shared.log import log
from rideco.shared.regions import all_regions
from rideco.shared.types import ENTITY_REGION, feature_key
from rideco.sim._ingress import send_object


KAFKA_BOOTSTRAP = os.environ.get("RIDECO_KAFKA_BOOTSTRAP", "localhost:29092")
TOPIC = "mapping_events"

WEATHER_OPTIONS = ["clear", "clear", "clear", "rain_light", "rain_heavy", "fog"]


async def _publish(producer: AIOKafkaProducer, key: str, value: dict) -> None:
    await producer.send_and_wait(
        TOPIC,
        key=key.encode("utf-8"),
        value=json.dumps(value).encode("utf-8"),
    )
    log("mapping", f"published {TOPIC}", flow="kafka", key=key, value=value)


async def _bootstrap_pricing(regions: list[str]) -> None:
    for region in regions:
        await send_object("Pricing", region, "refresh", {})
        log("mapping", f"bootstrapped Pricing.refresh", flow="send", region=region)


async def _emit_region(producer: AIOKafkaProducer, region: str) -> None:
    weather = random.choice(WEATHER_OPTIONS)
    accidents = round(random.betavariate(2, 8), 2)
    await _publish(producer, feature_key(ENTITY_REGION, region, "weather"), {"value": weather})
    await _publish(producer, feature_key(ENTITY_REGION, region, "accident_density"), {"value": accidents})


async def _poison_region(producer: AIOKafkaProducer, region: str) -> None:
    log("mapping", "POISONING — weather=BAD will jam ETA", flow="kafka", region=region)
    await _publish(producer, feature_key(ENTITY_REGION, region, "weather"), {"value": "BAD"})


async def _amain(regions: list[str], interval: float, poison: str | None) -> None:
    log("mapping", "starting", regions=",".join(regions), interval=interval, kafka=KAFKA_BOOTSTRAP)

    producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP)
    await producer.start()
    try:
        await _bootstrap_pricing(regions)
        if poison:
            await _poison_region(producer, poison)
        while True:
            for region in regions:
                try:
                    await _emit_region(producer, region)
                except Exception as e:
                    log("mapping", f"emit error: {type(e).__name__}: {e}", region=region)
            await asyncio.sleep(interval)
    finally:
        await producer.stop()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--regions", default=",".join(all_regions()))
    p.add_argument("--interval", type=float, default=15.0,
                   help="seconds between feature refresh sweeps")
    p.add_argument("--poison", default=None,
                   help="region code to publish the BAD weather sentinel (e.g. SF)")
    args = p.parse_args()
    asyncio.run(_amain(args.regions.split(","), args.interval, args.poison))


if __name__ == "__main__":
    main()

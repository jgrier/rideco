"""MappingSim — durable per-region weather + accident feed.

VirtualObject keyed by `region`. Each region VO emits a fresh
(weather, accident_density) pair every `interval_s` seconds by sending
to Features. Values stay in safe ranges by default so organic emits
never trigger a halt; use `spike-region.sh` (or any direct Features.set)
to force a region unsafe on demand.

On first start, also bootstraps the region's Pricing.refresh and
RegionSafetyAgent.start_monitoring cadence loops.
"""

from datetime import timedelta
import random

import restate

from rideco.shared.log import log
from rideco.shared.types import ENTITY_REGION, feature_key
from rideco.services import features as features_svc
from rideco.services import pricing as pricing_svc
from rideco.services import region_safety_agent as rsa_svc


mapping_sim = restate.VirtualObject("MappingSim")


WEATHER_OPTIONS = ["clear", "clear", "clear", "clear", "clear", "rain_light"]
MAX_ACCIDENTS = 0.35


def _pick_weather() -> str:
    return random.choice(WEATHER_OPTIONS)


def _pick_accidents() -> float:
    return round(random.uniform(0.0, MAX_ACCIDENTS), 2)


@mapping_sim.handler("start")
async def start(ctx: restate.ObjectContext, payload: dict) -> dict:
    region = ctx.key()
    interval_s = float(payload.get("interval_s", 12.0))
    already = (await ctx.get("active", type_hint=bool)) or False
    ctx.set("interval_s", interval_s)
    ctx.set("active", True)
    if not already:
        ctx.set("emits", 0)
        log("MappingSim", "→ Pricing.refresh (bootstrap)", flow="send", region=region)
        ctx.object_send(pricing_svc.refresh, key=region, arg={})
        log("MappingSim", "→ RegionSafetyAgent.start_monitoring (bootstrap)",
            flow="send", region=region)
        ctx.object_send(rsa_svc.start_monitoring, key=region, arg={})
        ctx.object_send(tick, key=region, arg={},
                        send_delay=timedelta(seconds=interval_s))
        log("MappingSim", "started", region=region, interval_s=interval_s)
    else:
        log("MappingSim", "config updated", region=region, interval_s=interval_s)
    return {"region": region, "active": True, "interval_s": interval_s}


@mapping_sim.handler("pause")
async def pause(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    ctx.set("active", False)
    log("MappingSim", "paused", region=ctx.key())
    return {"region": ctx.key(), "active": False}


@mapping_sim.handler("resume")
async def resume(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    region = ctx.key()
    was_active = (await ctx.get("active", type_hint=bool)) or False
    ctx.set("active", True)
    if not was_active:
        interval_s = (await ctx.get("interval_s", type_hint=float)) or 12.0
        log("MappingSim", "resumed", region=region)
        ctx.object_send(tick, key=region, arg={},
                        send_delay=timedelta(seconds=interval_s))
    return {"region": region, "active": True}


@mapping_sim.handler("set_interval")
async def set_interval(ctx: restate.ObjectContext, payload: dict) -> dict:
    interval_s = float(payload["interval_s"])
    ctx.set("interval_s", interval_s)
    log("MappingSim", "set_interval", region=ctx.key(), interval_s=interval_s)
    return {"region": ctx.key(), "interval_s": interval_s}


@mapping_sim.handler("tick")
async def tick(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    region = ctx.key()
    if not ((await ctx.get("active", type_hint=bool)) or False):
        log("MappingSim", "tick-stopped (paused)", region=region)
        return {"region": region, "action": "stopped"}

    emits = ((await ctx.get("emits", type_hint=int)) or 0) + 1

    weather = await ctx.run_typed(f"weather_{emits}", _pick_weather)
    accidents = await ctx.run_typed(f"accidents_{emits}", _pick_accidents)

    log("MappingSim", "→ Features.set", flow="send",
        region=region, weather=weather, accidents=accidents)
    ctx.object_send(
        features_svc.set_value,
        key=feature_key(ENTITY_REGION, region, "weather"),
        arg={"value": weather},
    )
    ctx.object_send(
        features_svc.set_value,
        key=feature_key(ENTITY_REGION, region, "accident_density"),
        arg={"value": accidents},
    )

    ctx.set("emits", emits)
    ctx.set("last_weather", weather)
    ctx.set("last_accidents", accidents)

    interval_s = (await ctx.get("interval_s", type_hint=float)) or 12.0
    ctx.object_send(tick, key=region, arg={},
                    send_delay=timedelta(seconds=interval_s))
    return {"region": region, "emits": emits, "weather": weather, "accidents": accidents}


@mapping_sim.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext, _: dict | None = None) -> dict:
    return {
        "region": ctx.key(),
        "active": (await ctx.get("active", type_hint=bool)) or False,
        "interval_s": (await ctx.get("interval_s", type_hint=float)) or 12.0,
        "emits": (await ctx.get("emits", type_hint=int)) or 0,
        "last_weather": await ctx.get("last_weather", type_hint=str),
        "last_accidents": await ctx.get("last_accidents", type_hint=float),
    }


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[mapping_sim])

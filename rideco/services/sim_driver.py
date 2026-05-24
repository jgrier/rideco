"""DriverSim — durable load generator simulating one driver.

VirtualObject keyed by `driver_id`. Each driver VO holds its current
(lat, lng) and a ping cadence. The `tick` handler:
  - jitters its position
  - sends Locations.ping(lat, lng)
  - self-sends `tick` after ping_interval_s

On first start the VO also registers itself as IDLE with Locations
(which in turn registers the driver in the regional Dispatch pool)
and bumps Pricing.note_supply for the region.
"""

from datetime import timedelta
import random

import restate

from rideco.shared.log import log
from rideco.shared.regions import REGIONS
from rideco.shared.types import DRIVER_IDLE
from rideco.services import locations as locations_svc
from rideco.services import pricing as pricing_svc


driver_sim = restate.VirtualObject("DriverSim")


def _initial_pos(*, center: dict, radius: float) -> dict:
    return {
        "lat": center["lat"] + random.uniform(-radius, radius),
        "lng": center["lng"] + random.uniform(-radius, radius),
    }


def _jitter_pos(*, lat: float, lng: float, radius: float) -> dict:
    return {
        "lat": lat + random.uniform(-radius, radius),
        "lng": lng + random.uniform(-radius, radius),
    }


@driver_sim.handler("start")
async def start(ctx: restate.ObjectContext, payload: dict) -> dict:
    driver_id = ctx.key()
    region = payload.get("region")
    ping_interval_s = float(payload.get("ping_interval_s", 2.0))
    if not region:
        raise restate.exceptions.TerminalError("region required")

    already = (await ctx.get("active", type_hint=bool)) or False
    ctx.set("region", region)
    ctx.set("ping_interval_s", ping_interval_s)
    ctx.set("active", True)

    if not already:
        center = REGIONS[region]["center"]
        pos = await ctx.run_typed("initial_pos", _initial_pos, center=center, radius=0.04)
        ctx.set("lat", pos["lat"])
        ctx.set("lng", pos["lng"])
        ctx.set("pings_sent", 0)

        log("DriverSim", "→ Locations.set_status(idle)", flow="sync",
            driver=driver_id, region=region)
        await ctx.object_call(
            locations_svc.set_status,
            key=driver_id,
            arg={"status": DRIVER_IDLE, "region": region},
        )
        log("DriverSim", "→ Pricing.note_supply", flow="send", driver=driver_id, region=region)
        ctx.object_send(pricing_svc.note_supply, key=region, arg={"delta": 1})

        log("DriverSim", "online", driver=driver_id, region=region)
        ctx.object_send(tick, key=driver_id, arg={},
                        send_delay=timedelta(seconds=ping_interval_s))
    return {"driver_id": driver_id, "active": True, "region": region}


@driver_sim.handler("pause")
async def pause(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    ctx.set("active", False)
    log("DriverSim", "paused", driver=ctx.key())
    return {"driver_id": ctx.key(), "active": False}


@driver_sim.handler("resume")
async def resume(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    driver_id = ctx.key()
    was_active = (await ctx.get("active", type_hint=bool)) or False
    ctx.set("active", True)
    if not was_active:
        ping_interval_s = (await ctx.get("ping_interval_s", type_hint=float)) or 2.0
        log("DriverSim", "resumed", driver=driver_id)
        ctx.object_send(tick, key=driver_id, arg={},
                        send_delay=timedelta(seconds=ping_interval_s))
    return {"driver_id": driver_id, "active": True}


@driver_sim.handler("tick")
async def tick(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    driver_id = ctx.key()
    if not ((await ctx.get("active", type_hint=bool)) or False):
        log("DriverSim", "tick-stopped (paused)", driver=driver_id)
        return {"driver_id": driver_id, "action": "stopped"}

    lat = await ctx.get("lat", type_hint=float)
    lng = await ctx.get("lng", type_hint=float)
    pings_sent = ((await ctx.get("pings_sent", type_hint=int)) or 0) + 1

    new_pos = await ctx.run_typed(
        f"drift_{pings_sent}", _jitter_pos, lat=lat, lng=lng, radius=0.0008
    )
    ctx.set("lat", new_pos["lat"])
    ctx.set("lng", new_pos["lng"])

    ctx.object_send(
        locations_svc.ping,
        key=driver_id,
        arg={"lat": new_pos["lat"], "lng": new_pos["lng"]},
    )

    ctx.set("pings_sent", pings_sent)
    ping_interval_s = (await ctx.get("ping_interval_s", type_hint=float)) or 2.0
    ctx.object_send(tick, key=driver_id, arg={},
                    send_delay=timedelta(seconds=ping_interval_s))
    return {"driver_id": driver_id, "pings_sent": pings_sent}


@driver_sim.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext, _: dict | None = None) -> dict:
    return {
        "driver_id": ctx.key(),
        "active": (await ctx.get("active", type_hint=bool)) or False,
        "region": await ctx.get("region", type_hint=str),
        "lat": await ctx.get("lat", type_hint=float),
        "lng": await ctx.get("lng", type_hint=float),
        "pings_sent": (await ctx.get("pings_sent", type_hint=int)) or 0,
    }

"""RiderSim — durable load generator simulating one rider.

VirtualObject keyed by `rider_id`. Each rider VO holds its own cadence
(rate, in trips/sec) and a paused flag. The `tick` handler:
  - calls Trip.request_ride (sync)
  - sends Trip.confirm (async)
  - bumps its own trip counter
  - self-sends `tick` with a jittered Poisson-ish delay

Pause/resume/rate-change are just normal handler calls — no SIGUSR1,
no side-channel control plane. Same primitives the app services use.
"""

from datetime import timedelta
import random
import uuid

import restate

from rideco.shared.log import log
from rideco.shared.regions import REGIONS, all_regions
from rideco.services import trip as trip_svc


rider_sim = restate.VirtualObject("RiderSim")


def _jitter(*, center: dict, radius: float) -> dict:
    return {
        "lat": center["lat"] + random.uniform(-radius, radius),
        "lng": center["lng"] + random.uniform(-radius, radius),
    }


def _next_delay(*, rate: float) -> float:
    """Exponential inter-arrival; floored so we never busy-loop."""
    return random.expovariate(max(rate, 0.001))


def _new_trip_id() -> str:
    return f"trip-{uuid.uuid4().hex[:8]}"


def _pick_region() -> str:
    """Riders move freely between regions — pick one fresh per request."""
    return random.choice(all_regions())


@rider_sim.handler("start")
async def start(ctx: restate.ObjectContext, payload: dict | None = None) -> dict:
    """Initialize and (idempotently) kick off the tick loop.

    Riders don't pin to a region — each request picks a fresh region, so
    even a small rider pool covers every region over time.
    """
    p = payload or {}
    rider_id = ctx.key()
    rate = float(p.get("rate", 0.1))

    already = (await ctx.get("active", type_hint=bool)) or False
    ctx.set("rate", rate)
    ctx.set("active", True)
    if not already:
        ctx.set("trips_started", 0)
        log("RiderSim", "start", rider=rider_id, rate=rate)
        ctx.object_send(tick, key=rider_id, arg={}, send_delay=timedelta(seconds=2))
    else:
        log("RiderSim", "config updated", rider=rider_id, rate=rate)
    return {"rider_id": rider_id, "active": True, "rate": rate}


@rider_sim.handler("pause")
async def pause(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    ctx.set("active", False)
    log("RiderSim", "paused", rider=ctx.key())
    return {"rider_id": ctx.key(), "active": False}


@rider_sim.handler("resume")
async def resume(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    rider_id = ctx.key()
    was_active = (await ctx.get("active", type_hint=bool)) or False
    ctx.set("active", True)
    if not was_active:
        log("RiderSim", "resumed", rider=rider_id)
        ctx.object_send(tick, key=rider_id, arg={}, send_delay=timedelta(seconds=1))
    return {"rider_id": rider_id, "active": True}


@rider_sim.handler("set_rate")
async def set_rate(ctx: restate.ObjectContext, payload: dict) -> dict:
    rate = float(payload["rate"])
    ctx.set("rate", rate)
    log("RiderSim", "set_rate", rider=ctx.key(), rate=rate)
    return {"rider_id": ctx.key(), "rate": rate}


@rider_sim.handler("tick")
async def tick(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    rider_id = ctx.key()
    if not ((await ctx.get("active", type_hint=bool)) or False):
        log("RiderSim", "tick-stopped (paused)", rider=rider_id)
        return {"rider_id": rider_id, "action": "stopped"}

    rate = (await ctx.get("rate", type_hint=float)) or 0.1
    trips_started = ((await ctx.get("trips_started", type_hint=int)) or 0) + 1

    region = await ctx.run_typed(f"region_{trips_started}", _pick_region)
    center = REGIONS[region]["center"]
    origin = await ctx.run_typed(f"origin_{trips_started}", _jitter, center=center, radius=0.05)
    destination = await ctx.run_typed(f"dest_{trips_started}", _jitter, center=center, radius=0.06)
    trip_id = await ctx.run_typed(f"trip_id_{trips_started}", _new_trip_id)

    log("RiderSim", "→ Trip.request_ride", flow="sync", rider=rider_id, trip=trip_id, region=region)
    await ctx.object_call(
        trip_svc.request_ride,
        key=trip_id,
        arg={"rider_id": rider_id, "origin": origin, "destination": destination, "region": region},
    )
    log("RiderSim", "→ Trip.confirm", flow="send", rider=rider_id, trip=trip_id)
    ctx.object_send(trip_svc.confirm, key=trip_id, arg={})

    ctx.set("trips_started", trips_started)
    ctx.set("last_trip_id", trip_id)
    ctx.set("last_region", region)

    delay_s = await ctx.run_typed(f"delay_{trips_started}", _next_delay, rate=rate)
    delay_s = max(0.5, min(delay_s, 120.0))  # sanity caps
    log("RiderSim", f"→ self.tick in {delay_s:.1f}s", flow="self", rider=rider_id)
    ctx.object_send(tick, key=rider_id, arg={}, send_delay=timedelta(seconds=delay_s))

    return {"rider_id": rider_id, "trips_started": trips_started, "trip": trip_id}


@rider_sim.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext, _: dict | None = None) -> dict:
    return {
        "rider_id": ctx.key(),
        "active": (await ctx.get("active", type_hint=bool)) or False,
        "rate": await ctx.get("rate", type_hint=float),
        "trips_started": (await ctx.get("trips_started", type_hint=int)) or 0,
        "last_trip_id": await ctx.get("last_trip_id", type_hint=str),
        "last_region": await ctx.get("last_region", type_hint=str),
    }


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[rider_sim])

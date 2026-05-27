"""Locations — per-driver state, GPS ingest, and the "matched" position downstream.

Real-Time Map-Matching (RTMM) in production systems typically uses a
Marginalized Particle Filter with Unscented Kalman updates. Here the
smoothing is mocked (one-step exponential average) — the architectural
shape is what's interesting: every driver is a Virtual Object holding
their own durable state, GPS pings land on `ping`, matched position is
read by Dispatch and ETA via `get_position`.

`set_status` is also where drivers register/deregister with the regional
Dispatch pool when they come online or go offline.
"""

import restate

from rideco.shared.log import log, log_in, log_out
from rideco.shared.types import (
    DRIVER_EN_ROUTE,
    DRIVER_IDLE,
    DRIVER_OFFLINE,
    DRIVER_ON_TRIP,
)
from rideco.services import dispatch as dispatch_svc

locations = restate.VirtualObject("Locations")


@locations.handler("ping")
async def ping(ctx: restate.ObjectContext, payload: dict) -> dict:
    """Receive a raw GPS observation. Apply mocked smoothing. Store result."""
    log_in("ping", driver=ctx.key())
    raw_lat = float(payload["lat"])
    raw_lng = float(payload["lng"])
    prev_lat = await ctx.get("matched_lat", type_hint=float)
    prev_lng = await ctx.get("matched_lng", type_hint=float)

    # Exponential moving average as a stand-in for the Marginalized Particle Filter.
    alpha = 0.6
    matched_lat = raw_lat if prev_lat is None else (alpha * raw_lat + (1 - alpha) * prev_lat)
    matched_lng = raw_lng if prev_lng is None else (alpha * raw_lng + (1 - alpha) * prev_lng)

    ctx.set("matched_lat", matched_lat)
    ctx.set("matched_lng", matched_lng)
    ctx.set("last_ping_ms", await ctx.time())
    return {"driver_id": ctx.key(), "lat": matched_lat, "lng": matched_lng}


@locations.handler("set_status")
async def set_status(ctx: restate.ObjectContext, payload: dict) -> dict:
    """Driver going online/offline updates status and registers with the regional pool."""
    status = payload["status"]
    region = payload.get("region")
    log_in("set_status", driver=ctx.key(), status=status, region=region)
    prev_status = await ctx.get("status", type_hint=str)
    prev_region = await ctx.get("region", type_hint=str)
    ctx.set("status", status)
    if region:
        ctx.set("region", region)

    # Register/deregister with the region's Dispatch pool when transitioning to/from idle.
    going_idle = status == DRIVER_IDLE and prev_status != DRIVER_IDLE
    leaving_idle = status != DRIVER_IDLE and prev_status == DRIVER_IDLE
    if going_idle and region:
        log_out("send", "Dispatch.register_driver", driver=ctx.key(), region=region)
        ctx.object_send(dispatch_svc.register_driver, key=region, arg={"driver_id": ctx.key()})
    elif leaving_idle and prev_region:
        log_out("send", "Dispatch.deregister_driver", driver=ctx.key(), region=prev_region)
        ctx.object_send(dispatch_svc.deregister_driver, key=prev_region, arg={"driver_id": ctx.key()})

    log("status", driver=ctx.key(), status=status, region=region or prev_region)
    return {"driver_id": ctx.key(), "status": status}


@locations.handler("accept_trip")
async def accept_trip(ctx: restate.ObjectContext, payload: dict) -> dict:
    trip_id = payload["trip_id"]
    log_in("accept_trip", driver=ctx.key(), trip=trip_id)
    region = await ctx.get("region", type_hint=str)
    ctx.set("current_trip_id", trip_id)
    ctx.set("status", DRIVER_EN_ROUTE)
    # Pull the driver out of the region's idle pool while they're driving.
    # `set_status(idle, region)` from Trip.complete re-registers them.
    if region:
        log_out("send", "Dispatch.deregister_driver", driver=ctx.key(), region=region)
        ctx.object_send(dispatch_svc.deregister_driver, key=region, arg={"driver_id": ctx.key()})
    log("accepted", driver=ctx.key(), trip=trip_id)
    return {"driver_id": ctx.key(), "trip_id": trip_id}


@locations.handler(kind="shared")
async def get_position(ctx: restate.ObjectSharedContext, _: dict | None = None) -> dict:
    return {
        "driver_id": ctx.key(),
        "lat": await ctx.get("matched_lat", type_hint=float),
        "lng": await ctx.get("matched_lng", type_hint=float),
        "status": (await ctx.get("status", type_hint=str)) or DRIVER_OFFLINE,
        "region": await ctx.get("region", type_hint=str),
    }


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[locations])

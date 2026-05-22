"""Trip — lifecycle state machine, request entry point.

Domain on the Restate map: **stateful microservices**.

The rider's first hop. Trip orchestrates the synchronous path by calling
Offers (which fans in ETA + Pricing), then on confirmation enqueues into
the region's Dispatch round. When Dispatch later calls `assign_driver`,
Trip notifies the driver via Locations and starts a SafetyAgent for the
ride. On completion the SafetyAgent is stopped.

What this service shows off: a stateful microservice that owns its
entity's lifecycle as a Virtual Object — durable state per `trip_id`,
single-writer, no external database or queue. The flow reads as ordinary
function calls.
"""

import restate

from rideco.shared.log import log
from rideco.shared.types import (
    TRIP_ASSIGNED,
    TRIP_CANCELLED,
    TRIP_COMPLETED,
    TRIP_DISPATCHING,
    TRIP_QUOTED,
    TRIP_REQUESTED,
)
from rideco.services import dispatch as dispatch_svc
from rideco.services import locations as locations_svc
from rideco.services import offers as offers_svc
from rideco.services import pricing as pricing_svc
from rideco.services import safety_agent as safety_svc

trip = restate.VirtualObject("Trip")


@trip.handler("request_ride")
async def request_ride(ctx: restate.ObjectContext, payload: dict) -> dict:
    """Build a quoted offer via Offers (which fans in ETA + Pricing)."""
    trip_id = ctx.key()
    rider_id = payload["rider_id"]
    origin = payload["origin"]
    destination = payload["destination"]
    region = payload["region"]

    ctx.set("rider_id", rider_id)
    ctx.set("origin", origin)
    ctx.set("destination", destination)
    ctx.set("region", region)
    ctx.set("status", TRIP_REQUESTED)

    log("Trip", "→ Offers.generate", flow="sync", trip=trip_id, region=region)
    bundle = await ctx.service_call(
        offers_svc.generate,
        arg={"trip_id": trip_id, "origin": origin, "destination": destination, "region": region},
    )
    selected = bundle["selected"]
    ctx.set("offer", selected)
    ctx.set("multiplier", bundle["multiplier"])
    ctx.set("status", TRIP_QUOTED)

    log("Trip", "→ Pricing.note_demand", flow="send", region=region)
    ctx.object_send(pricing_svc.note_demand, key=region, arg={})

    log("Trip", "quoted", trip=trip_id, region=region,
        eta=selected["eta_seconds"], price=selected["price_cents"],
        car_class=selected["car_class"], mult=bundle["multiplier"])
    return {
        "trip_id": trip_id,
        "eta_seconds": selected["eta_seconds"],
        "reliability_score": selected["reliability_score"],
        "price_cents": selected["price_cents"],
        "car_class": selected["car_class"],
        "multiplier": bundle["multiplier"],
        "region": region,
    }


@trip.handler("confirm")
async def confirm(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    """Rider accepted the offer. Enqueue into the region's Dispatch round."""
    trip_id = ctx.key()
    region = await ctx.get("region", type_hint=str)
    origin = await ctx.get("origin", type_hint=dict)
    if not region or not origin:
        raise restate.exceptions.TerminalError(
            "cannot confirm a trip that wasn't quoted",
        )
    ctx.set("status", TRIP_DISPATCHING)
    log("Trip", "→ Dispatch.enqueue_trip", flow="send", region=region, trip=trip_id)
    ctx.object_send(
        dispatch_svc.enqueue_trip,
        key=region,
        arg={"trip_id": trip_id, "origin": origin},
    )
    log("Trip", "confirmed", trip=trip_id, region=region)
    return {"trip_id": trip_id, "status": TRIP_DISPATCHING}


@trip.handler("assign_driver")
async def assign_driver(ctx: restate.ObjectContext, payload: dict) -> dict:
    """Called by Dispatch when a match is found. Also boots the SafetyAgent."""
    driver_id = payload["driver_id"]
    region = payload.get("region") or (await ctx.get("region", type_hint=str))
    trip_id = ctx.key()
    ctx.set("assigned_driver_id", driver_id)
    ctx.set("epoch_id", payload.get("epoch_id"))
    ctx.set("status", TRIP_ASSIGNED)

    log("Trip", "→ Locations.accept_trip", flow="send", trip=trip_id, driver=driver_id)
    ctx.object_send(
        locations_svc.accept_trip,
        key=driver_id,
        arg={"trip_id": trip_id},
    )
    log("Trip", "→ SafetyAgent.start_monitoring", flow="send", trip=trip_id)
    ctx.object_send(
        safety_svc.start_monitoring,
        key=trip_id,
        arg={"driver_id": driver_id, "region": region},
    )
    log("Trip", "assigned", trip=trip_id, driver=driver_id, epoch=payload.get("epoch_id"))
    return {"trip_id": trip_id, "driver_id": driver_id}


@trip.handler("complete")
async def complete(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    trip_id = ctx.key()
    ctx.set("status", TRIP_COMPLETED)
    log("Trip", "→ SafetyAgent.stop_monitoring", flow="send", trip=trip_id)
    ctx.object_send(safety_svc.stop_monitoring, key=trip_id, arg={})
    log("Trip", "completed", trip=trip_id)
    return {"trip_id": trip_id, "status": TRIP_COMPLETED}


@trip.handler("cancel")
async def cancel(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    trip_id = ctx.key()
    ctx.set("status", TRIP_CANCELLED)
    log("Trip", "→ SafetyAgent.stop_monitoring", flow="send", trip=trip_id)
    ctx.object_send(safety_svc.stop_monitoring, key=trip_id, arg={})
    log("Trip", "cancelled", trip=trip_id)
    return {"trip_id": trip_id, "status": TRIP_CANCELLED}


@trip.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext, _: dict | None = None) -> dict:
    return {
        "trip_id": ctx.key(),
        "status": await ctx.get("status", type_hint=str),
        "rider_id": await ctx.get("rider_id", type_hint=str),
        "region": await ctx.get("region", type_hint=str),
        "offer": await ctx.get("offer", type_hint=dict),
        "multiplier": await ctx.get("multiplier", type_hint=float),
        "assigned_driver_id": await ctx.get("assigned_driver_id", type_hint=str),
        "epoch_id": await ctx.get("epoch_id", type_hint=int),
    }

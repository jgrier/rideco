"""Trip — lifecycle state machine, request entry point.

The rider's first hop. Trip owns the entire trip lifecycle. `request_ride`
builds a quoted offer (synchronously fans out to Offers → ETA + Pricing).
`confirm` enqueues the trip into the region's Dispatch round and then
SUSPENDS on an Awakeable that Dispatch will eventually resolve with a
driver_id. When the match arrives, the same `confirm` invocation resumes,
records the assignment, and fans out to Locations.

The awakeable pattern makes the dependency 1-way: Trip calls Dispatch,
Dispatch resolves a token Trip handed it. Dispatch never imports Trip.
"""

from datetime import timedelta

import restate

from rideco.shared.log import log
from rideco.shared.types import (
    DRIVER_IDLE,
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

trip = restate.VirtualObject("Trip")

# Simulated ride duration. After a Trip is assigned, the same Trip VO
# schedules a self-send to `complete` this many seconds later, which
# closes out the trip and flips the driver back to idle. This keeps the
# driver pool turning over during a live demo.
RIDE_DURATION = timedelta(seconds=20)


@trip.handler("request_ride")
async def request_ride(ctx: restate.ObjectContext, payload: dict) -> dict:
    """Build a quoted offer. Sync fan-out to Offers (which fans into ETA + Pricing)."""
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
    """Rider accepted the offer. Enqueue into Dispatch and suspend until matched.

    Flow:
      1. Create an awakeable; pass its name to Dispatch in the enqueue payload.
      2. Suspend on the awakeable. No Python process held during the wait.
      3. When Dispatch's next matching round resolves the awakeable, this same
         invocation resumes with the driver_id.
      4. Record assignment, fan out to Locations.
    """
    trip_id = ctx.key()
    region = await ctx.get("region", type_hint=str)
    origin = await ctx.get("origin", type_hint=dict)
    if not region or not origin:
        raise restate.exceptions.TerminalError(
            "cannot confirm a trip that wasn't quoted",
        )

    ctx.set("status", TRIP_DISPATCHING)

    awakeable_name, driver_future = ctx.awakeable(type_hint=dict)
    ctx.set("pending_match_awakeable", awakeable_name)

    log("Trip", "→ Dispatch.enqueue_trip (one-way)", flow="send",
        region=region, trip=trip_id, awakeable=awakeable_name)
    ctx.object_send(
        dispatch_svc.enqueue_trip,
        key=region,
        arg={"trip_id": trip_id, "origin": origin, "awakeable": awakeable_name},
    )

    log("Trip", "awaiting match (suspended)", trip=trip_id)
    match = await driver_future  # SUSPENDS — same invocation resumes when Dispatch resolves
    driver_id = match["driver_id"]
    epoch_id = match.get("epoch_id")

    ctx.set("assigned_driver_id", driver_id)
    ctx.set("epoch_id", epoch_id)
    ctx.set("status", TRIP_ASSIGNED)
    ctx.clear("pending_match_awakeable")

    log("Trip", "→ Locations.accept_trip", flow="send", trip=trip_id, driver=driver_id)
    ctx.object_send(
        locations_svc.accept_trip,
        key=driver_id,
        arg={"trip_id": trip_id},
    )

    # Schedule auto-completion so the driver returns to the pool. Real
    # systems would mark complete from the driver's app on dropoff; for the
    # demo, a delayed self-send to `complete` simulates ride duration.
    log("Trip", f"→ self.complete in {int(RIDE_DURATION.total_seconds())}s", flow="self", trip=trip_id)
    ctx.object_send(complete, key=trip_id, arg={}, send_delay=RIDE_DURATION)

    log("Trip", "assigned", trip=trip_id, driver=driver_id, epoch=epoch_id)
    return {"trip_id": trip_id, "status": TRIP_ASSIGNED, "driver_id": driver_id, "epoch_id": epoch_id}


@trip.handler("complete")
async def complete(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    trip_id = ctx.key()
    status = await ctx.get("status", type_hint=str)
    if status == TRIP_COMPLETED or status == TRIP_CANCELLED:
        return {"trip_id": trip_id, "status": status}

    driver_id = await ctx.get("assigned_driver_id", type_hint=str)
    region = await ctx.get("region", type_hint=str)
    ctx.set("status", TRIP_COMPLETED)

    # Flip the driver back to idle so they can be matched again.
    if driver_id and region:
        log("Trip", "→ Locations.set_status(idle)", flow="send",
            trip=trip_id, driver=driver_id)
        ctx.object_send(
            locations_svc.set_status,
            key=driver_id,
            arg={"status": DRIVER_IDLE, "region": region},
        )

    # Bump the region's completion counter so the dashboard can show it.
    if region:
        ctx.object_send(dispatch_svc.note_completion, key=region, arg={})

    log("Trip", "completed", trip=trip_id, driver=driver_id)
    return {"trip_id": trip_id, "status": TRIP_COMPLETED}


@trip.handler("cancel")
async def cancel(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    trip_id = ctx.key()
    ctx.set("status", TRIP_CANCELLED)
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
        "pending_match_awakeable": await ctx.get("pending_match_awakeable", type_hint=str),
    }


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[trip])

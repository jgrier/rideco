"""Dispatch — batched matching round per region.

Real shape: every few seconds, build a weighted bipartite graph (riders × drivers
in the region), solve via LP relaxation → Hungarian / commercial solver,
emit assignments, carry unmatched riders forward to the next batch. Demo
shape: same cadence, greedy nearest-driver instead of Hungarian (good enough
to show the architecture).

Key design choices worth pointing at on stage:
- One Virtual Object per region. State is the active driver pool and the
  pending trip queue. Per-key serialization is free.
- Epoch cadence advances itself via a delayed `close_epoch` send to the same
  key. No external scheduler, no cron service.
- Trip enqueue, driver register/deregister, and `close_epoch` are all
  serialized by region so the matcher always sees a consistent snapshot.
"""

from datetime import timedelta

import restate

from rideco.shared.log import log
from rideco.shared.types import (
    DRIVER_EN_ROUTE,
    DRIVER_IDLE,
    feature_key,
    ENTITY_REGION,
)
from rideco.services import features as features_svc
from rideco.services import locations as locations_svc

dispatch = restate.VirtualObject("Dispatch")

EPOCH_INTERVAL = timedelta(seconds=5)


@dispatch.handler("register_driver")
async def register_driver(ctx: restate.ObjectContext, payload: dict) -> dict:
    driver_id = payload["driver_id"]
    drivers = (await ctx.get("active_driver_ids", type_hint=list)) or []
    if driver_id not in drivers:
        drivers.append(driver_id)
        ctx.set("active_driver_ids", drivers)
    log("Dispatch", "driver+", region=ctx.key(), driver=driver_id, pool=len(drivers))
    return {"region": ctx.key(), "pool_size": len(drivers)}


@dispatch.handler("deregister_driver")
async def deregister_driver(ctx: restate.ObjectContext, payload: dict) -> dict:
    driver_id = payload["driver_id"]
    drivers = (await ctx.get("active_driver_ids", type_hint=list)) or []
    drivers = [d for d in drivers if d != driver_id]
    ctx.set("active_driver_ids", drivers)
    log("Dispatch", "driver-", region=ctx.key(), driver=driver_id, pool=len(drivers))
    return {"region": ctx.key(), "pool_size": len(drivers)}


@dispatch.handler("enqueue_trip")
async def enqueue_trip(ctx: restate.ObjectContext, payload: dict) -> dict:
    """A Trip joins the next dispatch round for this region."""
    trip_id = payload["trip_id"]
    origin = payload["origin"]
    region = ctx.key()

    pending = (await ctx.get("pending_trips", type_hint=list)) or []
    pending.append({"trip_id": trip_id, "origin": origin})
    ctx.set("pending_trips", pending)

    # Kick off the round loop on first enqueue.
    already_running = (await ctx.get("loop_running", type_hint=bool)) or False
    if not already_running:
        ctx.set("loop_running", True)
        ctx.set("epoch_id", 1)
        log("Dispatch", "→ close_epoch in 5s (loop start)", flow="self", region=region)
        ctx.object_send(close_epoch, key=region, arg={}, send_delay=EPOCH_INTERVAL)

    log("Dispatch", "enqueue", region=region, trip=trip_id, pending=len(pending))
    return {"region": region, "pending": len(pending)}


def _euclid(a: dict, b: dict) -> float:
    return ((a["lat"] - b["lat"]) ** 2 + (a["lng"] - b["lng"]) ** 2) ** 0.5


@dispatch.handler("close_epoch")
async def close_epoch(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    """Snapshot pending trips + active drivers, run matching, emit assignments.

    Greedy by nearest driver. A real implementation would build edge weights from
    ETA, driver value-function (the reinforcement-learning approach common in
    the ride-hailing literature), and market conditions, then solve via
    Hungarian / commercial LP.
    """
    region = ctx.key()
    epoch_id = (await ctx.get("epoch_id", type_hint=int)) or 1
    pending = (await ctx.get("pending_trips", type_hint=list)) or []
    drivers = list((await ctx.get("active_driver_ids", type_hint=list)) or [])

    # Optional read: accident_density nudges the demo logging — could be folded
    # into edge weights in a real matcher.
    accident_res = await ctx.object_call(
        features_svc.get,
        key=feature_key(ENTITY_REGION, region, "accident_density"),
        arg={"default": 0.0},
    )

    # Fetch driver positions for the snapshot.
    driver_positions: list[tuple[str, dict]] = []
    for driver_id in drivers:
        pos = await ctx.object_call(locations_svc.get_position, key=driver_id, arg={})
        if pos.get("lat") is not None and pos.get("status") == DRIVER_IDLE:
            driver_positions.append((driver_id, pos))

    # Greedy nearest-driver match.
    assignments: list[dict] = []
    used_drivers: set[str] = set()
    leftover: list[dict] = []
    for trip in pending:
        best: tuple[float, str] | None = None
        for driver_id, pos in driver_positions:
            if driver_id in used_drivers:
                continue
            d = _euclid(trip["origin"], pos)
            if best is None or d < best[0]:
                best = (d, driver_id)
        if best is not None:
            used_drivers.add(best[1])
            assignments.append({"trip_id": trip["trip_id"], "driver_id": best[1]})
        else:
            leftover.append(trip)

    log("Dispatch", "close-epoch", region=region, epoch=epoch_id,
        pending=len(pending), drivers=len(driver_positions),
        matched=len(assignments), carried_over=len(leftover),
        accident_density=accident_res.get("value"))

    # Notify Trips of their assignment (one-way so the matcher doesn't block).
    # NB: importing trip here to avoid a circular import at module load.
    from rideco.services import trip as trip_svc
    for a in assignments:
        log("Dispatch", "→ Trip.assign_driver", flow="send",
            trip=a["trip_id"], driver=a["driver_id"])
        ctx.object_send(trip_svc.assign_driver, key=a["trip_id"],
                        arg={"driver_id": a["driver_id"], "region": region, "epoch_id": epoch_id})

    # Carry unmatched trips into the next epoch and schedule the next close.
    ctx.set("pending_trips", leftover)
    ctx.set("epoch_id", epoch_id + 1)
    has_more_work = bool(leftover) or bool(driver_positions)
    if has_more_work:
        log("Dispatch", "→ close_epoch in 5s", flow="self", region=region)
        ctx.object_send(close_epoch, key=region, arg={}, send_delay=EPOCH_INTERVAL)
    else:
        ctx.set("loop_running", False)
        log("Dispatch", "loop-idle", region=region)

    return {
        "region": region,
        "epoch_id": epoch_id,
        "matched": len(assignments),
        "carried_over": len(leftover),
    }

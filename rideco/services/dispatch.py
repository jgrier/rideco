"""Dispatch — batched matching round per region.

Every few seconds, build a weighted bipartite graph (riders × drivers in
the region), solve via LP relaxation → Hungarian / commercial solver in a
real system; greedy nearest-driver here for the demo. Emit assignments
by resolving the awakeable each Trip handed in on enqueue. Carry
unmatched riders forward to the next batch.

Dependency is one-way: Trip calls Dispatch with an awakeable token;
Dispatch resolves it when a match is found. Dispatch never imports Trip.

Each region's Dispatch VO has an `active` flag (default true). When the
RegionSafetyAgent halts a region, it sets active=false; close_epoch sees
that and skips matching for that epoch (trips stay queued). When the
human approves resume, active flips back to true and the backlog drains
on the next epoch.
"""

from datetime import timedelta

import restate

from rideco.shared.log import log
from rideco.shared.types import (
    DRIVER_IDLE,
    feature_key,
    ENTITY_REGION,
)
from rideco.services import features as features_svc
from rideco.services import locations as locations_svc

dispatch = restate.VirtualObject("Dispatch")

EPOCH_INTERVAL = timedelta(seconds=5)


@dispatch.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext, _: dict | None = None) -> dict:
    """Read-only inspector for a region's dispatch state."""
    active = await ctx.get("active", type_hint=bool)
    if active is None:
        active = True
    matched = (await ctx.get("total_matched", type_hint=int)) or 0
    completed = (await ctx.get("total_completed", type_hint=int)) or 0
    return {
        "region": ctx.key(),
        "active": active,
        "epoch_id": (await ctx.get("epoch_id", type_hint=int)) or 0,
        "loop_running": (await ctx.get("loop_running", type_hint=bool)) or False,
        "active_driver_count": len((await ctx.get("active_driver_ids", type_hint=list)) or []),
        "pending_trips_count": len((await ctx.get("pending_trips", type_hint=list)) or []),
        "total_matched": matched,
        "total_completed": completed,
        "in_flight": max(0, matched - completed),
    }


@dispatch.handler("note_completion")
async def note_completion(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    """Trip.complete fires this to bump the per-region completion counter."""
    n = ((await ctx.get("total_completed", type_hint=int)) or 0) + 1
    ctx.set("total_completed", n)
    return {"region": ctx.key(), "total_completed": n}


@dispatch.handler("set_active")
async def set_active(ctx: restate.ObjectContext, payload: dict) -> dict:
    """Turn matching on or off for this region. Called by RegionSafetyAgent."""
    region = ctx.key()
    active = bool(payload.get("active", True))
    prev = (await ctx.get("active", type_hint=bool))
    if prev is None:
        prev = True
    ctx.set("active", active)
    log("Dispatch", f"set_active={active} (was {prev})", region=region)
    return {"region": region, "active": active}


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
    """A Trip joins the next dispatch round for this region. Payload carries
    the trip's awakeable token — we resolve it when matching succeeds."""
    trip_id = payload["trip_id"]
    origin = payload["origin"]
    awakeable = payload["awakeable"]
    region = ctx.key()

    pending = (await ctx.get("pending_trips", type_hint=list)) or []
    pending.append({"trip_id": trip_id, "origin": origin, "awakeable": awakeable})
    ctx.set("pending_trips", pending)

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
    """Snapshot pending trips + active drivers, run matching, resolve awakeables.

    If the region's `active` flag is false (set by RegionSafetyAgent on a halt),
    skip matching this epoch — trips stay queued, drivers stay registered, the
    next epoch is still scheduled. When the agent flips active back to true,
    the backlog drains.
    """
    region = ctx.key()
    epoch_id = (await ctx.get("epoch_id", type_hint=int)) or 1
    pending = (await ctx.get("pending_trips", type_hint=list)) or []
    drivers = list((await ctx.get("active_driver_ids", type_hint=list)) or [])
    active = await ctx.get("active", type_hint=bool)
    if active is None:
        active = True

    # If region is halted, skip matching but keep the cadence loop running so
    # we resume immediately on un-halt.
    if not active:
        log("Dispatch", "close-epoch (HALTED, no matching)", region=region,
            epoch=epoch_id, pending=len(pending), drivers=len(drivers))
        ctx.set("epoch_id", epoch_id + 1)
        log("Dispatch", "→ close_epoch in 5s", flow="self", region=region)
        ctx.object_send(close_epoch, key=region, arg={}, send_delay=EPOCH_INTERVAL)
        return {"region": region, "epoch_id": epoch_id, "halted": True,
                "matched": 0, "pending": len(pending)}

    # Optional read: accident_density nudges the demo logging.
    accident_res = await ctx.object_call(
        features_svc.get,
        key=feature_key(ENTITY_REGION, region, "accident_density"),
        arg={"default": 0.0},
    )

    driver_positions: list[tuple[str, dict]] = []
    for driver_id in drivers:
        pos = await ctx.object_call(locations_svc.get_position, key=driver_id, arg={})
        if pos.get("lat") is not None and pos.get("status") == DRIVER_IDLE:
            driver_positions.append((driver_id, pos))

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
            assignments.append({
                "trip_id": trip["trip_id"],
                "driver_id": best[1],
                "awakeable": trip["awakeable"],
            })
        else:
            leftover.append(trip)

    log("Dispatch", "close-epoch", region=region, epoch=epoch_id,
        pending=len(pending), drivers=len(driver_positions),
        matched=len(assignments), carried_over=len(leftover),
        accident_density=accident_res.get("value"))

    for a in assignments:
        log("Dispatch", "resolve_awakeable", trip=a["trip_id"], driver=a["driver_id"])
        ctx.resolve_awakeable(a["awakeable"], {
            "driver_id": a["driver_id"],
            "epoch_id": epoch_id,
        })

    if assignments:
        prior_matched = (await ctx.get("total_matched", type_hint=int)) or 0
        ctx.set("total_matched", prior_matched + len(assignments))

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


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[dispatch])

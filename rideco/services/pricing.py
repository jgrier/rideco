"""Pricing — surge multiplier per region.

Surge pricing has evolved in the industry from "M/M/c queue throttle" through
"constrained optimization across a network topology matrix". Here we sketch
the spirit: per-region state holds a current multiplier that's refreshed
periodically off supply, demand, and feature inputs. `quote` returns
base × multiplier.

The refresh loop is scheduled via a self-targeted delayed send — no external
cron, no scheduler service. Restate's per-key serialization plus delayed sends
*are* the cadence primitive.
"""

from datetime import timedelta

import restate

from rideco.shared.log import log
from rideco.shared.regions import region_base_fare_cents
from rideco.shared.types import ENTITY_REGION, feature_key
from rideco.services import features as features_svc

pricing = restate.VirtualObject("Pricing")

REFRESH_INTERVAL = timedelta(seconds=10)


def _multiplier_from_signals(supply: int, demand: int, weather: str, accidents: float) -> float:
    """Sketch of surge pricing: supply/demand ratio with weather + accident
    adjustments.

    Reality is constrained optimization across a network topology matrix. The
    point here is to show the shape — features in, multiplier out, refreshed
    in place.
    """
    base = 1.0
    if supply > 0:
        base = max(1.0, demand / max(supply, 1))
    if weather in ("rain_heavy", "snow"):
        base *= 1.25
    if accidents > 0.5:
        base *= 1.15
    return round(min(base, 3.0), 2)


@pricing.handler("quote")
async def quote(ctx: restate.ObjectContext, payload: dict) -> dict:
    region = ctx.key()
    distance_m = float(payload.get("distance_m", 3000))
    multiplier = (await ctx.get("multiplier", type_hint=float)) or 1.0
    base = region_base_fare_cents(region)
    distance_cents = int(distance_m / 1000 * 150)
    total = int((base + distance_cents) * multiplier)
    return {
        "region": region,
        "base_fare_cents": base,
        "distance_cents": distance_cents,
        "multiplier": multiplier,
        "total_cents": total,
    }


@pricing.handler("refresh")
async def refresh(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    """Recompute the multiplier from current features. Reschedules itself."""
    region = ctx.key()

    weather_res = await ctx.object_call(
        features_svc.get,
        key=feature_key(ENTITY_REGION, region, "weather"),
        arg={"default": "clear"},
    )
    accidents_res = await ctx.object_call(
        features_svc.get,
        key=feature_key(ENTITY_REGION, region, "accident_density"),
        arg={"default": 0.0},
    )
    supply = (await ctx.get("supply_count", type_hint=int)) or 0
    demand = (await ctx.get("demand_count", type_hint=int)) or 0

    multiplier = _multiplier_from_signals(
        supply=supply,
        demand=demand,
        weather=str(weather_res.get("value") or "clear"),
        accidents=float(accidents_res.get("value") or 0.0),
    )
    ctx.set("multiplier", multiplier)
    ctx.set("last_refresh_ms", await ctx.time())
    log("Pricing", "refresh", region=region, multiplier=multiplier,
        weather=weather_res.get("value"), accidents=accidents_res.get("value"))

    log("Pricing", "→ refresh in 10s", flow="self", region=region)
    ctx.object_send(refresh, key=region, arg={}, send_delay=REFRESH_INTERVAL)
    return {"region": region, "multiplier": multiplier}


@pricing.handler("note_demand")
async def note_demand(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    demand = ((await ctx.get("demand_count", type_hint=int)) or 0) + 1
    ctx.set("demand_count", demand)
    return {"region": ctx.key(), "demand_count": demand}


@pricing.handler("note_supply")
async def note_supply(ctx: restate.ObjectContext, payload: dict) -> dict:
    delta = int(payload.get("delta", 1))
    supply = ((await ctx.get("supply_count", type_hint=int)) or 0) + delta
    ctx.set("supply_count", max(supply, 0))
    return {"region": ctx.key(), "supply_count": supply}


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[pricing])

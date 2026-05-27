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

from rideco.shared.log import log, log_in, log_out
from rideco.shared.regions import region_base_fare_cents
from rideco.shared.types import ENTITY_REGION, feature_key
from rideco.services import features as features_svc

pricing = restate.VirtualObject("Pricing")

REFRESH_INTERVAL = timedelta(seconds=10)


def _multiplier_from_signals(
    supply: int, demand: int, weather: str, accidents: float,
    request_rate_per_s: float,
) -> float:
    """Sketch of surge pricing: supply/demand ratio with weather +
    accident adjustments + a rolling-window demand-intensity term.

    Reality is constrained optimization across a network topology matrix.
    The point here is to show the shape — features in, multiplier out,
    refreshed in place.

    `request_rate_per_s` comes from a windowed aggregate maintained by
    the Features service (see Features.record_event / event_rate). It
    decays naturally, unlike the cumulative `demand` counter, so the
    multiplier responds to *recent* demand instead of all-time demand.
    """
    base = 1.0
    if supply > 0:
        base = max(1.0, demand / max(supply, 1))
    # Rolling-window contribution. >0.5 req/s starts nudging the
    # multiplier; the contribution caps at 1.5x. With ~5 sims fanning
    # out across 4 regions at 0.10/s each, baseline is ~0.12/s/region
    # — well under the threshold, so this kicks in only on real bursts.
    if request_rate_per_s > 0.5:
        base *= min(1.5, 1.0 + (request_rate_per_s - 0.5) * 0.25)
    if weather in ("rain_heavy", "snow"):
        base *= 1.25
    if accidents > 0.5:
        base *= 1.15
    return round(min(base, 3.0), 2)


@pricing.handler("quote")
async def quote(ctx: restate.ObjectContext, payload: dict) -> dict:
    region = ctx.key()
    log_in("quote", region=region, distance_m=payload.get("distance_m"))
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
    log_in("refresh", region=region)

    weather_key = feature_key(ENTITY_REGION, region, "weather")
    accidents_key = feature_key(ENTITY_REGION, region, "accident_density")
    rate_key = f"events:region:{region}:ride_request"

    log_out("call", "Features.get", key=weather_key)
    weather_res = await ctx.object_call(
        features_svc.get, key=weather_key, arg={"default": "clear"},
    )
    log_out("call", "Features.get", key=accidents_key)
    accidents_res = await ctx.object_call(
        features_svc.get, key=accidents_key, arg={"default": 0.0},
    )
    # Windowed demand-intensity from the Features rolling aggregate.
    # Trip.request_ride fire-and-forgets a record_event per request;
    # this read returns events/sec over the last 60s.
    log_out("call", "Features.event_rate", key=rate_key)
    rate_res = await ctx.object_call(
        features_svc.event_rate, key=rate_key, arg={},
    )
    supply = (await ctx.get("supply_count", type_hint=int)) or 0
    demand = (await ctx.get("demand_count", type_hint=int)) or 0
    request_rate = float(rate_res.get("rate_per_s") or 0.0)

    multiplier = _multiplier_from_signals(
        supply=supply,
        demand=demand,
        weather=str(weather_res.get("value") or "clear"),
        accidents=float(accidents_res.get("value") or 0.0),
        request_rate_per_s=request_rate,
    )
    ctx.set("multiplier", multiplier)
    ctx.set("last_refresh_ms", await ctx.time())
    log("refreshed", region=region, multiplier=multiplier,
        weather=weather_res.get("value"), accidents=accidents_res.get("value"),
        request_rate_per_s=request_rate)

    delay_s = int(REFRESH_INTERVAL.total_seconds())
    log_out(f"send+delay({delay_s}s)", "Pricing.refresh", region=region)
    ctx.object_send(refresh, key=region, arg={}, send_delay=REFRESH_INTERVAL)
    return {"region": region, "multiplier": multiplier}


@pricing.handler("note_demand")
async def note_demand(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    log_in("note_demand", region=ctx.key())
    demand = ((await ctx.get("demand_count", type_hint=int)) or 0) + 1
    ctx.set("demand_count", demand)
    return {"region": ctx.key(), "demand_count": demand}


@pricing.handler("note_supply")
async def note_supply(ctx: restate.ObjectContext, payload: dict) -> dict:
    delta = int(payload.get("delta", 1))
    log_in("note_supply", region=ctx.key(), delta=delta)
    supply = ((await ctx.get("supply_count", type_hint=int)) or 0) + delta
    ctx.set("supply_count", max(supply, 0))
    return {"region": ctx.key(), "supply_count": supply}


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[pricing])

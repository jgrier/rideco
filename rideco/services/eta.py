"""ETA — reliable arrival time prediction.

Real production flow uses gradient-boosted-tree models with reliability SLAs
per ETA bracket — only ETAs meeting an SLA are returned. The demo mocks
that: a base estimate is adjusted by region-level features (weather,
accident_density), then a reliability score is attached.

**Poison-pill location.** When a region's weather feature is set to the sentinel
value `"BAD"`, this handler hits a code path that raises a *non-Terminal*
exception. Restate retries it forever with exponential backoff, isolated in the
runtime's retry queue. Good traffic in other regions keeps flowing. Fix is in
the marked branch below — flip the flag and re-register.
"""

import math

import restate

from rideco.shared.log import log
from rideco.shared.types import ENTITY_REGION, feature_key
from rideco.services import features as features_svc

eta = restate.Service("ETA")

# Flip this to True to "fix" the poison-pill scenario on stage. Re-register
# the deployment with --force after flipping. Stuck invocations drain.
HANDLE_BAD_WEATHER_GRACEFULLY = False


def _haversine_m(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    r = 6_371_000
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat))
         * math.sin(dlng / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(h))


def _weather_penalty(weather_value: str) -> float:
    """Map a weather feature value to a travel-time multiplier."""
    if weather_value == "BAD" and not HANDLE_BAD_WEATHER_GRACEFULLY:
        # The poison-pill branch. Real bug: somebody upstream emitted a sentinel
        # that this handler wasn't built to parse. Raised as a regular Exception
        # so Restate retries it (vs TerminalError which would propagate).
        return float(weather_value)  # raises ValueError
    return {
        "clear": 1.0,
        "rain_light": 1.1,
        "rain_heavy": 1.35,
        "snow": 1.5,
        "fog": 1.15,
    }.get(weather_value, 1.0)


@eta.handler("estimate")
async def estimate(ctx: restate.Context, payload: dict) -> dict:
    origin = payload["origin"]
    destination = payload["destination"]
    region = payload["region"]

    distance_m = _haversine_m(origin["lat"], origin["lng"], destination["lat"], destination["lng"])

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

    base_seconds = distance_m / 11.0  # ~40 km/h baseline
    weather_mult = _weather_penalty(str(weather_res.get("value") or "clear"))
    accident_mult = 1.0 + 0.4 * float(accidents_res.get("value") or 0.0)
    eta_seconds = int(base_seconds * weather_mult * accident_mult)

    # Reliability: higher when conditions are calm.
    reliability = round(max(0.5, 1.0 - 0.2 * (weather_mult - 1) - 0.3 * (accident_mult - 1)), 2)

    log("ETA", "estimate", region=region, dist_m=int(distance_m),
        eta_s=eta_seconds, reliability=reliability,
        weather=weather_res.get("value"), accidents=accidents_res.get("value"))

    return {
        "eta_seconds": eta_seconds,
        "distance_m": int(distance_m),
        "reliability_score": reliability,
        "route_summary": f"direct via {region}",
    }

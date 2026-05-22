"""Offers — generate + select ride offers.

Domain on the Restate map: **stateful microservices**.

Real ride-hailing platforms split this into "offer generation" (build the
candidate set of products + prices + ETAs for a request) and "offer selection"
(rank, dedup, decide which to show). Both teams sit between the rider-facing
request and the dispatchable trip. Here both jobs live in one stateless
service so the demo's "function-call-shaped composition" point lands without
a second hop.

`generate` is the synthesis layer: it fans out to ETA + Pricing in parallel,
then composes their results into a small set of candidate offers per car
class. The fan-out is plain `await ctx.service_call(...)` / `await ctx.object_call(...)` —
no orchestrator, no DAG framework, no queue plumbing. The call graph *is* the
workflow.
"""

import restate

from rideco.shared.log import log
from rideco.services import eta as eta_svc
from rideco.services import pricing as pricing_svc


offers = restate.Service("Offers")


CAR_CLASSES: list[dict] = [
    {"name": "Standard", "price_mult": 1.00, "eta_mult": 1.00},
    {"name": "XL",       "price_mult": 1.60, "eta_mult": 1.05},
    {"name": "Lux",      "price_mult": 2.40, "eta_mult": 0.85},
]


@offers.handler("generate")
async def generate(ctx: restate.Context, payload: dict) -> dict:
    """Build a ranked offer set for a trip request.

    Pure synthesis — no state of its own. Sync fan-out to ETA + Pricing,
    sync fan-in to a ranked list. Trip calls this once per request.
    """
    trip_id = payload["trip_id"]
    origin = payload["origin"]
    destination = payload["destination"]
    region = payload["region"]

    log("Offers", "→ ETA.estimate", flow="sync", trip=trip_id, region=region)
    eta_result = await ctx.service_call(
        eta_svc.estimate,
        arg={"origin": origin, "destination": destination, "region": region},
    )
    log("Offers", "→ Pricing.quote", flow="sync", trip=trip_id, region=region)
    price = await ctx.object_call(
        pricing_svc.quote,
        key=region,
        arg={"distance_m": eta_result["distance_m"]},
    )

    candidates: list[dict] = []
    for car in CAR_CLASSES:
        candidates.append({
            "car_class": car["name"],
            "eta_seconds": int(eta_result["eta_seconds"] * car["eta_mult"]),
            "price_cents": int(price["total_cents"] * car["price_mult"]),
            "reliability_score": eta_result["reliability_score"],
        })

    # Selection policy: default to Standard. Real platforms run an experiment-aware
    # selection step here that respects rider preferences, ETA SLAs, market signals,
    # etc.
    selected = candidates[0]

    log("Offers", "generated", trip=trip_id, region=region, candidates=len(candidates),
        selected_class=selected["car_class"], selected_eta=selected["eta_seconds"],
        selected_price=selected["price_cents"], multiplier=price["multiplier"])

    return {
        "trip_id": trip_id,
        "region": region,
        "candidates": candidates,
        "selected": selected,
        "multiplier": price["multiplier"],
    }

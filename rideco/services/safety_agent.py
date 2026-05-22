"""SafetyAgent — AI-agent orchestration domain.

Domain on the Restate map: **AI agent orchestration**.

A per-trip safety monitor that wakes on a cadence, gathers context (driver
position, region features), calls a mocked LLM risk scorer via `ctx.run`,
and — when risk is high — opens an Awakeable for a human reviewer and
suspends until the human responds. Trip starts the agent on driver
assignment; Trip stops it on completion.

This service exists specifically to showcase the AI-agent primitives Restate
provides natively:

- **Per-agent state** as a Virtual Object keyed by `trip_id` (single-writer,
  durable across crashes and restarts).
- **Suspend/resume across long external calls** — the mocked LLM call lives
  inside `ctx.run`; a real one would too. Handlers don't hold processes in
  memory while waiting.
- **Awakeables for human-in-the-loop** — when risk warrants escalation, the
  agent creates a named Awakeable and suspends until a human resolver POSTs
  to Restate's awakeable ingress endpoint.
- **Deterministic replay** — every step (LLM result, human verdict, action
  taken) is journaled, so re-running an agent's trace is reproducible.
- **Composable functions** — no DAG framework, no separate "agent runtime".
  The agent is just async functions calling other functions.

`ctx.run` deserves its own callout. The LLM call is exactly the kind of
non-deterministic external I/O Restate's `ctx.run` is designed for: it
journals the result on first execution so replays see the same value. In
Temporal's model this would have to be routed through an Activity in a
separate worker fleet. Here it's a closure.
"""

from datetime import timedelta

import restate

from rideco.shared.log import log
from rideco.shared.types import ENTITY_REGION, feature_key
from rideco.services import features as features_svc
from rideco.services import locations as locations_svc


safety_agent = restate.VirtualObject("SafetyAgent")

TICK_INTERVAL = timedelta(seconds=8)
RISK_THRESHOLD = 0.6


def _mock_llm_risk_score(
    accident_density: float,
    weather: str,
    driver_speed_proxy: float,
) -> dict:
    """Pretend to call an LLM. Returns a risk score and a one-line rationale.

    A real implementation would `await an_llm.score(...)` here; the inputs
    would be a structured prompt with the same fields. The result is
    journaled by `ctx.run` so replays are deterministic.
    """
    # Pure function of inputs so the demo behaviour is predictable on stage.
    score = 0.0
    rationale_parts: list[str] = []
    if accident_density > 0.5:
        score += 0.45
        rationale_parts.append(f"area accident_density={accident_density:.2f} (high)")
    if weather in ("rain_heavy", "snow", "fog"):
        score += 0.25
        rationale_parts.append(f"adverse weather: {weather}")
    if driver_speed_proxy > 0.02:
        score += 0.20
        rationale_parts.append(f"driver position jitter {driver_speed_proxy:.3f}")
    score = min(score, 1.0)
    rationale = "; ".join(rationale_parts) or "conditions nominal"
    return {"risk_score": round(score, 2), "rationale": rationale}


@safety_agent.handler("start_monitoring")
async def start_monitoring(ctx: restate.ObjectContext, payload: dict) -> dict:
    """Trip calls this on driver assignment. Initializes state, schedules first tick."""
    trip_id = ctx.key()
    driver_id = payload["driver_id"]
    region = payload["region"]

    already_running = (await ctx.get("active", type_hint=bool)) or False
    if already_running:
        log("SafetyAgent", "already-running", trip=trip_id)
        return {"trip_id": trip_id, "active": True}

    ctx.set("active", True)
    ctx.set("driver_id", driver_id)
    ctx.set("region", region)
    ctx.set("ticks", 0)
    ctx.set("escalations", 0)

    log("SafetyAgent", "→ tick in 8s (start)", flow="self", trip=trip_id)
    ctx.object_send(tick, key=trip_id, arg={}, send_delay=TICK_INTERVAL)
    log("SafetyAgent", "started", trip=trip_id, driver=driver_id, region=region)
    return {"trip_id": trip_id, "active": True}


@safety_agent.handler("tick")
async def tick(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    """One monitoring cycle: gather context, score risk, escalate if needed."""
    trip_id = ctx.key()
    active = (await ctx.get("active", type_hint=bool)) or False
    if not active:
        log("SafetyAgent", "tick-stopped (not active)", trip=trip_id)
        return {"trip_id": trip_id, "action": "stopped"}

    driver_id = await ctx.get("driver_id", type_hint=str)
    region = await ctx.get("region", type_hint=str)
    ticks = ((await ctx.get("ticks", type_hint=int)) or 0) + 1
    ctx.set("ticks", ticks)

    # Read the inputs the "LLM" will reason over.
    driver_pos = await ctx.object_call(locations_svc.get_position, key=driver_id, arg={})
    weather_res = await ctx.object_call(
        features_svc.get,
        key=feature_key(ENTITY_REGION, region, "weather"),
        arg={"default": "clear"},
    )
    accident_res = await ctx.object_call(
        features_svc.get,
        key=feature_key(ENTITY_REGION, region, "accident_density"),
        arg={"default": 0.0},
    )

    # `ctx.run` journals this so replays don't re-call the "LLM".
    score = await ctx.run_typed(
        f"llm_risk_{trip_id}_{ticks}",
        _mock_llm_risk_score,
        accident_density=float(accident_res.get("value") or 0.0),
        weather=str(weather_res.get("value") or "clear"),
        driver_speed_proxy=abs(float(driver_pos.get("lat") or 0.0)) % 0.05,
    )

    log("SafetyAgent", "tick", trip=trip_id, n=ticks, region=region,
        risk=score["risk_score"], rationale=score["rationale"])

    action = "ok"
    if score["risk_score"] >= RISK_THRESHOLD:
        action = await _escalate_to_human(ctx, trip_id, score)

    if (await ctx.get("active", type_hint=bool)):
        log("SafetyAgent", "→ tick in 8s", flow="self", trip=trip_id)
        ctx.object_send(tick, key=trip_id, arg={}, send_delay=TICK_INTERVAL)

    return {"trip_id": trip_id, "tick": ticks, "risk": score["risk_score"], "action": action}


async def _escalate_to_human(ctx: restate.ObjectContext, trip_id: str, score: dict) -> str:
    """Open an Awakeable, suspend the agent, resume on the human's verdict."""
    escalations = ((await ctx.get("escalations", type_hint=int)) or 0) + 1
    ctx.set("escalations", escalations)

    awakeable_name, verdict_future = ctx.awakeable(type_hint=dict)
    ctx.set("pending_awakeable", awakeable_name)

    log("SafetyAgent", "ESCALATE (suspending for human verdict)",
        trip=trip_id, awakeable=awakeable_name, risk=score["risk_score"],
        resolve_hint=f"curl -X POST http://localhost:8080/restate/awakeables/{awakeable_name}/resolve -d '{{\\\"verdict\\\":\\\"approve\\\"}}'",
    )

    # Agent suspends here until a human reviewer resolves the awakeable.
    # No process is held in memory while waiting.
    verdict = await verdict_future
    ctx.clear("pending_awakeable")

    decision = str(verdict.get("verdict", "approve"))
    log("SafetyAgent", "RESUMED", trip=trip_id, verdict=decision, awakeable=awakeable_name)
    return f"human:{decision}"


@safety_agent.handler("stop_monitoring")
async def stop_monitoring(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    ctx.set("active", False)
    trip_id = ctx.key()
    log("SafetyAgent", "stopped", trip=trip_id,
        ticks=await ctx.get("ticks", type_hint=int),
        escalations=await ctx.get("escalations", type_hint=int))
    return {"trip_id": trip_id, "active": False}


@safety_agent.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext, _: dict | None = None) -> dict:
    return {
        "trip_id": ctx.key(),
        "active": (await ctx.get("active", type_hint=bool)) or False,
        "driver_id": await ctx.get("driver_id", type_hint=str),
        "region": await ctx.get("region", type_hint=str),
        "ticks": (await ctx.get("ticks", type_hint=int)) or 0,
        "escalations": (await ctx.get("escalations", type_hint=int)) or 0,
        "pending_awakeable": await ctx.get("pending_awakeable", type_hint=str),
    }

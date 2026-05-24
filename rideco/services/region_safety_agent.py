"""RegionSafetyAgent — per-region monitor that halts dispatch when conditions
are unsafe and waits for a human to approve a resume.

VirtualObject keyed by `region`. Ticks every 10s. Each tick:
- reads region features (weather, accident_density)
- scores a composite risk via `ctx.run_typed` (mocked LLM)
- if risk crosses threshold AND dispatch is currently active for this
  region: halts dispatch, creates an awakeable, suspends pending human
  verdict
- on resume: approve → dispatch resumes; deny → dispatch stays halted

The agent never auto-resumes after a deny — humans gate the state
transition. Other regions are entirely unaffected (each region has its
own agent VO).

This is the AI-agent showcase: per-key state, mocked LLM via `ctx.run`,
awakeable for human-in-the-loop, all on Restate's durable runtime.
"""

from datetime import timedelta

import restate

from rideco.shared.log import log
from rideco.shared.types import ENTITY_REGION, feature_key
from rideco.services import dispatch as dispatch_svc
from rideco.services import features as features_svc


region_safety_agent = restate.VirtualObject("RegionSafetyAgent")

TICK_INTERVAL = timedelta(seconds=10)
RISK_THRESHOLD = 0.6


def _composite_risk(weather: str, accident_density: float) -> dict:
    """Mocked LLM risk scorer. Pure function of inputs so demos are
    predictable: replace with a real LLM call in production (still inside
    `ctx.run` so the result is journaled for replay)."""
    score = 0.0
    parts: list[str] = []
    if accident_density >= 0.5:
        score += 0.5
        parts.append(f"accident_density={accident_density:.2f}")
    elif accident_density >= 0.3:
        score += 0.2
        parts.append(f"accident_density={accident_density:.2f} (elevated)")
    if weather == "snow":
        score += 0.35
        parts.append(f"weather=snow")
    elif weather == "rain_heavy":
        score += 0.3
        parts.append(f"weather=rain_heavy")
    elif weather == "fog":
        score += 0.15
        parts.append(f"weather=fog")
    return {"score": round(min(score, 1.0), 2), "rationale": "; ".join(parts) or "nominal"}


@region_safety_agent.handler("start_monitoring")
async def start_monitoring(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    """Boot the per-region tick loop. Idempotent."""
    region = ctx.key()
    already = (await ctx.get("active", type_hint=bool)) or False
    if already:
        log("RegionSafety", "already-running", region=region)
        return {"region": region, "active": True}

    ctx.set("active", True)
    ctx.set("region_active", True)  # dispatch starts allowed
    ctx.set("ticks", 0)
    ctx.set("halts", 0)

    log("RegionSafety", "→ tick in 10s (start)", flow="self", region=region)
    ctx.object_send(tick, key=region, arg={}, send_delay=TICK_INTERVAL)
    log("RegionSafety", "started", region=region)
    return {"region": region, "active": True}


@region_safety_agent.handler("tick")
async def tick(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    """One safety check cycle."""
    region = ctx.key()
    if not ((await ctx.get("active", type_hint=bool)) or False):
        log("RegionSafety", "tick-stopped (not active)", region=region)
        return {"region": region, "action": "stopped"}

    ticks = ((await ctx.get("ticks", type_hint=int)) or 0) + 1
    ctx.set("ticks", ticks)

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
    weather = str(weather_res.get("value") or "clear")
    accidents = float(accident_res.get("value") or 0.0)

    # Composite risk via ctx.run — journaled so replays are deterministic.
    score = await ctx.run_typed(
        f"region_risk_{region}_{ticks}",
        _composite_risk,
        weather=weather,
        accident_density=accidents,
    )
    ctx.set("last_score", score["score"])
    ctx.set("last_rationale", score["rationale"])

    region_active = (await ctx.get("region_active", type_hint=bool))
    if region_active is None:
        region_active = True

    log("RegionSafety", "tick", region=region, n=ticks,
        risk=score["score"], rationale=score["rationale"],
        dispatch_active=region_active)

    if score["score"] >= RISK_THRESHOLD and region_active:
        await _halt_and_wait(ctx, region, score)
    elif score["score"] >= RISK_THRESHOLD and not region_active:
        log("RegionSafety", "still-unsafe (already halted, awaiting verdict)",
            region=region, risk=score["score"])

    if (await ctx.get("active", type_hint=bool)):
        log("RegionSafety", "→ tick in 10s", flow="self", region=region)
        ctx.object_send(tick, key=region, arg={}, send_delay=TICK_INTERVAL)

    return {"region": region, "tick": ticks, "risk": score["score"]}


async def _halt_and_wait(ctx: restate.ObjectContext, region: str, score: dict) -> None:
    """Halt dispatch for this region, suspend on awakeable, resume on verdict."""
    halts = ((await ctx.get("halts", type_hint=int)) or 0) + 1
    ctx.set("halts", halts)

    log("RegionSafety", "HALTING dispatch", region=region,
        risk=score["score"], rationale=score["rationale"])

    log("RegionSafety", "→ Dispatch.set_active(false)", flow="send", region=region)
    ctx.object_send(dispatch_svc.set_active, key=region, arg={"active": False})
    ctx.set("region_active", False)

    awakeable_name, verdict_future = ctx.awakeable(type_hint=dict)
    ctx.set("pending_awakeable", awakeable_name)

    log("RegionSafety", "ESCALATE (suspending for human verdict)",
        region=region, awakeable=awakeable_name,
        resolve_hint=f"./scripts/approve-region.sh {awakeable_name} approve   # or deny")

    verdict = await verdict_future  # SUSPENDS — no process held
    ctx.clear("pending_awakeable")
    decision = str(verdict.get("verdict", "approve"))
    ctx.set("last_verdict", decision)

    log("RegionSafety", "RESUMED", region=region, verdict=decision)

    if decision == "approve":
        log("RegionSafety", "→ Dispatch.set_active(true)", flow="send", region=region)
        ctx.object_send(dispatch_svc.set_active, key=region, arg={"active": True})
        ctx.set("region_active", True)
    # else: deny — region stays halted. Future ticks will log "still-unsafe"
    # until someone resumes it (out-of-band approve via force_resume below,
    # or by issuing a new halt event after conditions clear).


@region_safety_agent.handler("force_resume")
async def force_resume(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    """Manually resume dispatch for this region (out-of-band override —
    used when a previous halt was denied and conditions have since cleared)."""
    region = ctx.key()
    log("RegionSafety", "→ Dispatch.set_active(true) (force resume)", flow="send", region=region)
    ctx.object_send(dispatch_svc.set_active, key=region, arg={"active": True})
    ctx.set("region_active", True)
    return {"region": region, "region_active": True}


@region_safety_agent.handler("stop_monitoring")
async def stop_monitoring(ctx: restate.ObjectContext, _: dict | None = None) -> dict:
    region = ctx.key()
    ctx.set("active", False)
    log("RegionSafety", "stopped", region=region)
    return {"region": region, "active": False}


@region_safety_agent.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext, _: dict | None = None) -> dict:
    return {
        "region": ctx.key(),
        "active": (await ctx.get("active", type_hint=bool)) or False,
        "region_active": await ctx.get("region_active", type_hint=bool),
        "ticks": (await ctx.get("ticks", type_hint=int)) or 0,
        "halts": (await ctx.get("halts", type_hint=int)) or 0,
        "last_score": await ctx.get("last_score", type_hint=float),
        "last_rationale": await ctx.get("last_rationale", type_hint=str),
        "last_verdict": await ctx.get("last_verdict", type_hint=str),
        "pending_awakeable": await ctx.get("pending_awakeable", type_hint=str),
    }


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[region_safety_agent])

"""Features — production-style online feature store, unified with the platform.

Each Virtual Object key is a composite `{entity_type}:{entity_id}:{feature_name}`.
In a typical production path, features are computed in Flink (or another
streaming engine), written to an online feature store via a streaming sink,
and read by ML services at request time. Here that whole pipeline collapses
to a Virtual Object: the writer (mapping-events injector) calls `set`, the
readers (ETA, Pricing, Dispatch, SafetyAgent) call `get` — same per-key
durable state, no separate feature DB.
"""

import restate

from rideco.shared.log import log

features = restate.VirtualObject("Features")


@features.handler("set")
async def set_value(ctx: restate.ObjectContext, payload: dict) -> dict:
    value = payload.get("value")
    version = (await ctx.get("version", type_hint=int)) or 0
    ctx.set("value", value)
    ctx.set("version", version + 1)
    ctx.set("last_updated_ms", await ctx.time())
    log("Features", "set", key=ctx.key(), value=value, version=version + 1)
    return {"key": ctx.key(), "value": value, "version": version + 1}


@features.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext, payload: dict | None = None) -> dict:
    default = (payload or {}).get("default")
    value = await ctx.get("value")
    version = (await ctx.get("version", type_hint=int)) or 0
    if value is None:
        return {"key": ctx.key(), "value": default, "version": 0, "is_default": True}
    return {"key": ctx.key(), "value": value, "version": version, "is_default": False}

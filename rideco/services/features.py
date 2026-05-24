"""Features — online feature store. Ingests events, serves a derived view.

Each Virtual Object key is a composite `{entity_type}:{entity_id}:{feature_name}`.
Writers (the mapping-events injector, an upstream stream-processor in a real
system) POST to `/Features/<key>/set/send` and the value is durable on the
Restate log from the moment ingress acks. Readers (ETA, Pricing, Dispatch,
SafetyAgent) call `get` directly. One primitive, one place.

**Poison-pill target.** When a `set` arrives with the sentinel value
`"POISON"` and the gracefully-handle flag is off, the handler raises a
non-Terminal `ValueError` — Restate retries forever with exponential
backoff. Because Virtual Object exclusive handlers serialize per key,
subsequent `set` calls for the same key queue up behind the stuck one.
Other Features VOs (other keys) keep working — that's per-key failure
isolation.
"""

import restate

from rideco.shared.log import log

features = restate.VirtualObject("Features")

# Flip to True to "fix" the poison-pill on stage. After flipping, restart
# hypercorn and re-register; the stuck invocation drains on its next retry.
HANDLE_POISON_GRACEFULLY = False


@features.handler("set")
async def set_value(ctx: restate.ObjectContext, payload: dict) -> dict:
    value = payload.get("value")

    # The poison-pill branch. Real bug: somebody upstream emitted a sentinel
    # value this handler wasn't built to process. Raised as a plain Exception
    # so Restate retries (vs TerminalError which would just fail the
    # invocation immediately).
    if value == "POISON" and not HANDLE_POISON_GRACEFULLY:
        raise ValueError("cannot ingest POISON sentinel — bad message from upstream")

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


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[features])

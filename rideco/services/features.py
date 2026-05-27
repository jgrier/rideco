"""Features — online feature store. Ingests events, serves a derived view.

Each Virtual Object key is a composite `{entity_type}:{entity_id}:{feature_name}`.
Writers (the mapping-events injector, an upstream stream-processor in a real
system) POST to `/Features/<key>/set/send` and the value is durable on the
Restate log from the moment ingress acks. Readers (ETA, Pricing, Dispatch,
SafetyAgent) call `get` directly. One primitive, one place.

**Two shapes per VO key:**

- *Point-value features* (`set` / `get`) — last-write-wins. Used for
  things like `region:SF:weather` where the most recent reading is
  the one that matters.

- *Event streams* (`record_event` / `event_rate`) — append-only
  rolling window. Used for things like `events:region:SF:ride_request`
  where the *rate* of arrivals over the last minute is the signal,
  not any individual event. This is the canonical stream-processing
  shape — events in, windowed aggregate out, consumed by another
  service to make a decision. Pricing reads it to fold real-time
  demand intensity into the surge multiplier.

  The two shapes coexist on the same VO because they use different
  state slots (`value/version` vs `samples`); per-key serialization
  still applies — same key = same writer queue.

**Poison-pill target.** When a `set` arrives with the sentinel value
`"POISON"` and the gracefully-handle flag is off, the handler raises a
non-Terminal `ValueError` — Restate retries forever with exponential
backoff. Because Virtual Object exclusive handlers serialize per key,
subsequent `set` calls for the same key queue up behind the stuck one.
Other Features VOs (other keys) keep working — that's per-key failure
isolation.
"""

import restate

from rideco.shared.log import log, log_in

features = restate.VirtualObject("Features")

# Flip to True to "fix" the poison-pill on stage. After flipping, restart
# hypercorn and re-register; the stuck invocation drains on its next retry.
HANDLE_POISON_GRACEFULLY = False

# Rolling window for event-stream keys. 60s gives the surge loop (which
# refreshes every 10s) six full windows of recent samples to react to.
EVENT_WINDOW_MS = 60_000


@features.handler("set")
async def set_value(ctx: restate.ObjectContext, payload: dict) -> dict:
    value = payload.get("value")
    log_in("set", key=ctx.key(), value=value)

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
    log("set", key=ctx.key(), value=value, version=version + 1)
    return {"key": ctx.key(), "value": value, "version": version + 1}


@features.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext, payload: dict | None = None) -> dict:
    default = (payload or {}).get("default")
    value = await ctx.get("value")
    version = (await ctx.get("version", type_hint=int)) or 0
    if value is None:
        return {"key": ctx.key(), "value": default, "version": 0, "is_default": True}
    return {"key": ctx.key(), "value": value, "version": version, "is_default": False}


# ───── event streams (rolling-window aggregates) ─────────────────────


@features.handler("record_event")
async def record_event(
    ctx: restate.ObjectContext, _: dict | None = None,
) -> dict:
    """Append `now` to the per-key rolling window, trimming out samples
    older than EVENT_WINDOW_MS.

    Callers treat this as a fire-and-forget /send — the value of the
    handler is the durable append onto Restate's log; the response is
    only useful for debugging. Exclusive per-key handlers mean each
    stream has a single-writer queue without us writing any locks.
    """
    log_in("record_event", key=ctx.key())
    now_ms = await ctx.time()
    samples: list[int] = (await ctx.get("samples", type_hint=list)) or []
    cutoff = now_ms - EVENT_WINDOW_MS
    # Trim first so the stored list stays bounded even under bursty
    # ingest. With ride_request ingest at ~5/s and a 60s window that's
    # ~300 ints — tiny.
    samples = [t for t in samples if t >= cutoff]
    samples.append(now_ms)
    ctx.set("samples", samples)
    return {"key": ctx.key(), "count_in_window": len(samples)}


@features.handler(kind="shared")
async def event_rate(
    ctx: restate.ObjectSharedContext, _: dict | None = None,
) -> dict:
    """Returns events/second over the rolling window for this key.

    Shared handler — multiple concurrent readers don't block each
    other. The trim is read-only here (we filter into a local list
    without writing back); the next record_event will persist the
    pruned form."""
    now_ms = await ctx.time()
    samples: list[int] = (await ctx.get("samples", type_hint=list)) or []
    cutoff = now_ms - EVENT_WINDOW_MS
    fresh = [t for t in samples if t >= cutoff]
    window_s = EVENT_WINDOW_MS / 1000.0
    return {
        "key": ctx.key(),
        "window_s": window_s,
        "count": len(fresh),
        "rate_per_s": round(len(fresh) / window_s, 3) if window_s else 0.0,
    }


# Standalone ASGI app — one Restate deployment per service.
app = restate.app(services=[features])

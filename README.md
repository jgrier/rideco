# RideCo on Restate

RideCo is a working ride-hailing backend built on
[Restate](https://restate.dev). Eight services (Trip, Offers, ETA, Pricing,
Locations, Features, Dispatch, SafetyAgent) run as stateless Python workers;
all state, durability, retries, ordering, and scheduling live inside Restate.

The repo is a reproducible demo. Two terminals, a few scripts, no Kafka, no
Redis, no separate workflow engine, no agent framework. Just Restate plus
stateless application code.

![Architecture](architecture.svg)

## How Restate fits in

> **Every service/handler in Restate automatically has a durable log in front of it.** Any invocation — sync RPC, async send, scheduled timer, webhook — is journaled in Restate's log *before* it executes. Durability, retry, ordering, observability — properties of every handler, transparently.

External clients (rider, driver, mapping providers, human operator) talk to
Restate's HTTP ingress. Restate journals the call and invokes the appropriate
worker. **Workers never call each other directly.** When a worker uses
`ctx.call()` or `ctx.send()` from inside a handler, the call goes back
through Restate, which routes it onward. Restate is always the hub.

**`call()` vs `send()`.** Every handler supports both — the choice is at
the call site. `call()` is synchronous: the caller awaits the response.
`send()` is asynchronous: fire-and-forget, returns immediately. Both
journal the invocation in the Restate log first, so both are durable from
the moment Restate acks. A trip request that uses `call()` blocks waiting
for the result; a GPS ping that uses `send()` returns the moment Restate
has the message.

## All workers are stateless

Each of the eight services is an ordinary Python process behind hypercorn —
no local state, no RocksDB, no Redis sessions, no consumer-group offsets to
maintain. The workers can be killed and restarted at will, scaled
horizontally without coordination, run anywhere a regular HTTP service can
run. **Every operational concern that usually sticks to "stateful services"
lives in Restate.** That's a substantial simplification: deploy stateless
workers behind a load balancer, run a Restate cluster, done.

## Virtual Objects

Most of RideCo's services are **Virtual Objects** keyed by a domain
identifier — Trip per `trip_id`, Pricing per `region`, Locations per
`driver_id`, Features per `entity:id:feature_name`. Two properties matter:

- **Per-key state.** A VO instance "always exists" for its key; state
  survives across processes and restarts.
- **Per-key serialization.** Exclusive handlers for the same key run
  one-at-a-time — no locks, no coordination service. Shared handlers
  (read-only) can run concurrently. This is why Dispatch's `close_epoch`
  for SF can't race itself, and why Trip's lifecycle stays consistent
  under concurrent updates.

## Four domains, plus serving

Restate covers four backend domains that have historically required separate
substrates. RideCo exercises all four:

**Event-driven applications.** Services triggered by events on a log. The
caller doesn't wait for a result, only for the event to be durable. In
RideCo: mapping providers publishing into Features, driver apps publishing
GPS pings into Locations, Trip sending into Dispatch.

**Stateful microservices.** Regular request/response services that may keep
per-key state. Callers await results synchronously. In RideCo: Trip, Offers,
ETA, Pricing — the synchronous application path from rider tap to quoted
offer.

**Durable Execution.** Long-running operations with automatic retries, a
durable journal that avoids re-running successful steps, timers (delayed
calls), and awakeables for human-in-the-loop or async waits. In RideCo:
Dispatch's batched matching rounds and SafetyAgent's per-trip monitor.

**AI agent infrastructure.** Per-agent state, suspend across long LLM calls,
human-in-the-loop via awakeables, deterministic replay of agent decisions —
all the same primitives the other three domains use, applied to LLM-driven
agents. In RideCo: SafetyAgent.

**Serving** is a fifth concept layered on top of these: a service that
ingests events (event-driven app, write side) AND serves a derived view via
sync `get` reads (serving, read side). Locations and Features are both —
they receive fire-and-forget writes from external publishers and respond to
sync reads from internal consumers. Serving reads aren't drawn as request
arrows in the architecture diagram; they're implicit.

## Per-service breakdown

How each service is invoked, what state it owns, what it calls.

Two patterns underneath every interaction:

- **`call()`** (`[sync→]` in logs) — caller awaits. Writes to the Restate log first, then blocks until the worker completes.
- **`send()`** (`[send→]` in logs) — fire-and-forget into the Restate log. Returns immediately. Delayed sends (`[self→]` when targeting self) replace external schedulers.

### Trip — Stateful microservice

Virtual Object keyed by `trip_id`. One per ride.

**Receives:**
- Sync HTTP from rider/driver apps: `request_ride`, `confirm`, `cancel`, `complete`
- Shared HTTP read: `get`

**State:** `rider_id`, `origin`, `destination`, `region`, `status`, `offer`, `multiplier`, `assigned_driver_id`, `epoch_id`, `pending_match_awakeable`.

**Calls:**
- Sync RPC → `Offers.generate`
- Sends → `Pricing.note_demand`, `Dispatch.enqueue_trip` (carries an awakeable token), `Locations.accept_trip`, `SafetyAgent.start_monitoring` / `stop_monitoring`

Trip is the only orchestrator. `confirm` is a long-running operation: it creates an awakeable, sends one-way to `Dispatch.enqueue_trip` carrying the awakeable name, and **suspends** on the awakeable. When Dispatch's next matching round resolves the awakeable with a `driver_id`, the same `confirm` invocation resumes, records the assignment, and fans out to Locations and SafetyAgent. Trip → Dispatch is a one-way dependency; Dispatch never imports Trip.

### Offers — Stateful microservice (stateless service)

**Receives:** sync RPC from Trip during `request_ride`.

**State:** none.

**Calls:** Sync RPC → `ETA.estimate`, `Pricing.quote`.

Pure synthesis layer. Fans into ETA + Pricing in sequence, builds three candidate offers per car class (Standard, XL, Lux), selects Standard.

### ETA — Stateful microservice (stateless service)

**Receives:** sync RPC from Offers during `generate`.

**State:** none.

**Calls:** Sync RPC → `Features.get` for region weather + accident_density.

Reliable arrival prediction. Computes haversine distance, adjusts by region features, returns ETA + reliability score.

### Pricing — Stateful microservice

Virtual Object keyed by `region`.

**Receives:**
- Sync RPC from Offers: `quote`
- Sends from Trip (`note_demand`) and driver-sim (`note_supply`)
- Self-scheduled `refresh` (delayed call to self every 10s)

**State:** `multiplier`, `supply_count`, `demand_count`, `last_refresh_ms`.

**Calls:** Sync RPC → `Features.get`. Self-send → `refresh` in 10s.

Surge multiplier per region. The refresh loop is a delayed call to self — no external scheduler.

### Locations — Event-driven app + Serving

Virtual Object keyed by `driver_id`.

**Receives:**
- Driver app pings (one-way send): `ping`
- Driver app status changes via `call()`: `set_status`
- Send from Trip on assignment: `accept_trip`
- Sync shared read from Dispatch + SafetyAgent: `get_position`

**State:** `status`, `matched_lat`, `matched_lng`, `last_ping_ms`, `region`, `current_trip_id`.

**Calls:** Sends → `Dispatch.register_driver` / `deregister_driver` on status transitions.

Per-driver state. Pings are fire-and-forget; the smoothing (mocked here as an exponential moving average; production systems use a Marginalized Particle Filter) happens inside the handler. Position reads via the shared `get_position` handler are the serving path.

### Features — Event-driven app + Serving

Virtual Object keyed by `{entity_type}:{entity_id}:{feature_name}`.

**Receives:**
- `send()` from external mapping providers (and internal callers): `set`
- `call()` from ETA, Pricing, Dispatch, SafetyAgent (shared read): `get`

**State:** `value`, `version`, `last_updated_ms`.

**Calls:** nothing.

Online feature store. External providers `send()` events to Restate; Restate journals the write and invokes `Features.set` on the right key. Internal readers `call()` `get` to retrieve the current value.

### Dispatch — Durable Execution

Virtual Object keyed by `region`. Pure event-driven — every handler is a send.

**Receives (all one-way):**
- `enqueue_trip` from Trip (carries the trip's awakeable token)
- `register_driver` / `deregister_driver` from Locations
- Self-scheduled `close_epoch` every 5s

**State:** `active_driver_ids`, `pending_trips` (with each trip's awakeable token), `epoch_id`, `loop_running`.

**Calls:**
- Sync RPC → `Locations.get_position` (per active driver at epoch close)
- `ctx.resolve_awakeable` per matched trip — Dispatch's only outbound communication. It never calls Trip directly.
- Self-send → `close_epoch` in 5s

Long-running matcher. Each epoch: snapshot pending trips, snapshot driver positions, greedy nearest-driver match, resolve the awakeable token of each matched trip with the driver_id. Unmatched trips carry forward to the next epoch. Dispatch has no knowledge of Trip's state machine — it just resolves tokens it was handed.

### SafetyAgent — AI Agent infrastructure

Virtual Object keyed by `trip_id`. One agent per active ride.

**Receives:**
- Sends from Trip: `start_monitoring`, `stop_monitoring`
- Self-scheduled `tick` (delayed call every 8s)
- External HTTP awakeable resolve from human operator
- Sync shared read: `get`

**State:** `active`, `driver_id`, `region`, `ticks`, `escalations`, `pending_awakeable`.

**Calls:**
- Sync RPC → `Locations.get_position`, `Features.get` (per tick)
- `ctx.run_typed` for the mocked LLM risk score — journaled so replays are deterministic
- `ctx.awakeable()` to create a suspension token; `await future` to suspend; resumes when external HTTP resolves the awakeable
- Self-send → `tick` in 8s

The long-running AI agent. Three primitives the other services don't exercise: `ctx.run` for non-deterministic side effects (the LLM call), `ctx.awakeable()` for human-in-the-loop suspension, and per-agent state via the Virtual Object. Combined: a per-conversation agent with durable memory, deterministic replay, and clean human-in-the-loop — without LangGraph, Redis sessions, or an external scheduler.

## Why no Kafka

This demo doesn't use Kafka anywhere. Every external write and every internal
hop goes to the Restate log via Restate's HTTP ingress.

The argument is short. Every Restate handler is durable from the moment
ingress acks the request — the Restate log handles the durable-input-queue
job a Kafka topic would otherwise do, without the cluster. For sync calls,
`call()` is just durable function-call ergonomics. For async, `send()`
writes to the same log. External publishers use `send()` the same way
internal callers do. For cadence, delayed sends replace cron and Airflow.

**Restate and Kafka coexist peacefully.** Restate has first-class Kafka
subscriptions (inbound) and producer support (outbound). But for
async/event-driven microservice workloads, Restate is just a better log:
lower latency, no consumer-group bookkeeping, durability and ordering tuned
for this exact workload. A Restate Kafka subscription literally just copies
messages from Kafka into the Restate log before any processing happens —
that extra copy is unnecessary when your producers can speak HTTP directly
to Restate in the first place. Many architectures that have Kafka today
simply don't need it.

## How to run the demo

Two terminals plus a browser tab at `http://localhost:9070` (Restate Web UI).

Prereqs: Python 3.13 (or 3.11+), Docker, the
[`restate` CLI](https://docs.restate.dev/get_started/install), and `uv` (or
plain `pip`).

```bash
# Install once
uv venv --python 3.13 .venv
.venv/bin/python -m pip install -e .
```

**Terminal 1** — the show. Logs scroll here as the demo runs.

```bash
./scripts/demo-t1.sh fresh
```

Pauses for ENTER, wipes Restate state, brings up Restate, starts hypercorn.
When the Phase 3 fix step asks for it: `Ctrl+C` here, edit
`rideco/services/features.py`, then `./scripts/demo-t1.sh restart`.

**Terminal 2** — guided walkthrough.

```bash
./scripts/demo-t2.sh
```

Three phases, ENTER between every step. Each step explains what's happening,
what to look for in Terminal 1, and where to look in the Restate Web UI:

1. **Quiet trip** — one rider request end-to-end; the full architecture visible in the log.
2. **Human-in-the-loop** — escalate via the SafetyAgent; the agent suspends on an awakeable; you POST a verdict; the same invocation resumes.
3. **Complete the trip** — terminal state, agent shuts down.

## Scripts reference

| Script | Purpose |
|---|---|
| `./scripts/demo-t1.sh [fresh\|restart]` | Terminal 1 driver (fresh wipes state, restart picks up code changes) |
| `./scripts/demo-t2.sh` | Terminal 2 guided 5-phase walkthrough |
| `./scripts/register.sh` | Register the Python deployment with Restate |
| `./scripts/reset.sh` | Wipe Restate state and restart the container |
| `./scripts/stop.sh` | Stop hypercorn reliably |
| `./scripts/setup-region.sh <region>` | Initialize a region: clear features + one idle driver |
| `./scripts/make-trip.sh <trip_id> <region>` | Rider request + confirm (sync; confirm waits for match) |
| `./scripts/make-trip-send.sh <trip_id> <region>` | Same but fire-and-forget request_ride |
| `./scripts/complete-trip.sh <trip_id>` | Mark a trip complete; SafetyAgent stops |
| `./scripts/cancel-trip.sh <trip_id>` | Cancel a trip |
| `./scripts/set-feature.sh <region> <feature> <value>` | Write a feature directly via HTTP |
| `./scripts/poison.sh [region]` | Publish the POISON sentinel into Features for that region |
| `./scripts/escalate.sh [region]` | Push accident_density past 0.6 to trigger SafetyAgent escalation |
| `./scripts/approve.sh <awakeable_id> [verdict]` | Resolve a suspended SafetyAgent awakeable |
| `./scripts/show-trip.sh <trip_id>` | Pretty-print Trip state with a status legend |
| `./scripts/show-agent.sh <trip_id>` | Pretty-print SafetyAgent state |
| `./scripts/show-invocations.sh` | `restate invocations list` with a status legend |

## Versions

- **Restate server:** 1.6.2 (pinned in `docker-compose.yml`)
- **Restate Python SDK:** `restate_sdk[serde]` 0.18.0
- **Python:** 3.13
- **ASGI server:** Hypercorn (HTTP/2)

## Layout

```
rideco/
├── architecture.svg           # diagram, embedded above
├── docker-compose.yml         # restate-server 1.6.2 — the whole stack
├── hypercorn-config.toml      # ASGI binds :9080
├── pyproject.toml             # restate_sdk[serde], hypercorn, httpx, rich
├── Makefile                   # serve, register, stop
├── scripts/                   # demo scripts — see Scripts reference above
├── rideco/
│   ├── shared/                # types, region defs, color logging w/ flow tags
│   ├── services/              # trip, offers, dispatch, locations, pricing,
│   │                          #   eta, features, safety_agent, app
│   └── sim/                   # rider, driver, mapping_events, _ingress helper
```

All eight services run in one Python process for the demo. Each is
independent and could be split into its own process — Restate doesn't care.

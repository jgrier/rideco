# RideCo — ride dispatch on Restate

RideCo is a fictional ride-hailing company invented to make the architecture
concrete. The artifact is a working slice of how you'd build the **next new
system** at a ride-hailing platform if Restate were the runtime — eight
services running on one stateful application platform instead of the usual
mix of Kafka + stream-processor + workflow-engine + ad-hoc queues + agent
framework.

This is not a migration pitch.

## The punchline

> **Every service/handler in Restate automatically has a durable log in front of it.** Any invocation — sync RPC, async send, scheduled timer, webhook — is journaled in Restate's log *before* it executes. Durability, retry, ordering, observability — all the things Kafka was giving you for internal-bus use cases — are now a property of every handler, transparently.

You don't need Kafka for what Kafka was being used for most of the time.
External publishers, internal courier work between services, scheduled
cadence — all the same primitive: `ctx.send()` (or an HTTP POST to
Restate's ingress) writes to the Restate log, durable from
acknowledgement, no Kafka cluster to operate.

## The four domains this demonstrates

Restate is a stateful application platform. It covers four domains the
industry has historically solved with separate tools:

| Domain                       | What teams build there today                      | What Restate provides                                              |
|------------------------------|---------------------------------------------------|--------------------------------------------------------------------|
| **Event-driven applications**| Apache Kafka, Kafka Streams, custom stream procs  | Durable event processing with per-key state, server-side           |
| **Stateful microservices**   | Microservices + databases + queues + retry logic  | Durable execution + Virtual Objects out of the box                 |
| **Workflow orchestration**   | Temporal, AWS Step Functions, Airflow             | Workflows that emerge from the function-call graph                 |
| **AI agent orchestration**   | LangGraph, in-house agent frameworks              | Per-agent state, suspend/resume across long calls, awakeables      |

RideCo's eight services are deliberately spread across all four.

## Architecture

![Architecture](architecture.svg)

**Arrow legend.** Two line styles cover every interaction:

- `→` thin solid = **synchronous request** (`[sync→]` in logs) — caller awaits the response. Both external HTTP and internal RPC.
- `⇒` thick solid = **durable async send** (`[send→]` in logs) — fire-and-forget into the Restate log. Both external publishers (mapping providers, driver pings) and internal one-way hops between services.

**Not shown on the diagram, but in the system:**

- **Self-sends for cadence loops** (`[self→]` in logs): Dispatch's `close_epoch` every 5s, Pricing's `refresh` every 10s, SafetyAgent's `tick` every 8s. Each VO schedules its own next invocation via `ctx.object_send(handler, key=self.key(), send_delay=...)`. Same primitive as the durable async send arrow, just to self with a delay — no external scheduler.

**A trip from rider tap to dispatched ride, in order:**

```
1. Rider          → Trip.request_ride                              sync HTTP
2.  Trip          → Offers.generate                                sync RPC
3.   Offers       → ETA.estimate (reads Features)                  sync RPC
4.   Offers       → Pricing.quote (reads Features)                 sync RPC
5.  Trip          ⇒ Pricing.note_demand                            Restate log
6.  Trip          (returns offer to rider)
7. Rider          → Trip.confirm                                   sync HTTP
8.  Trip          ⇒ Dispatch.enqueue_trip                          Restate log
9.   Dispatch     ↻ close_epoch (delayed self-send, every 5s)      self
10.   Dispatch    → Locations.get_position (per active driver)     sync RPC
11.   Dispatch    ⇒ Trip.assign_driver (per match)                 Restate log
12.    Trip       ⇒ Locations.accept_trip                          Restate log
13.    Trip       ⇒ SafetyAgent.start_monitoring                   Restate log
14.     SafetyAgent ↻ tick (delayed self-send, every 8s)           self
```

## Services, grouped by domain

| Domain                    | Service          | Restate primitive                                       | Role                                                                                                |
|---------------------------|------------------|---------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| Event-driven applications | **Locations**    | `VirtualObject` keyed by `driver_id`                    | GPS ingestion + map-matched position + driver status                                                |
| Event-driven applications | **Features**     | `VirtualObject` keyed by `entity:id:feature`            | Online feature store. External feeds publish directly via HTTP send.                                |
| Stateful microservices    | **Trip**         | `VirtualObject` keyed by `trip_id`                      | Lifecycle state machine; entry point for ride requests                                              |
| Stateful microservices    | **Offers**       | stateless `Service`                                     | Fan-in over ETA + Pricing to produce ranked offer candidates per car class                          |
| Stateful microservices    | **Pricing**      | `VirtualObject` keyed by `region`                       | Surge multiplier; periodic refresh via delayed self-send                                            |
| Stateful microservices    | **ETA**          | stateless `Service`                                     | Reliable arrival prediction; reads Features at request time. **Poison-pill target.**                |
| Workflow orchestration    | **Dispatch**     | `VirtualObject` keyed by `region`                       | Batched matching round, every few seconds; epoch cadence is a delayed self-send                     |
| AI agent orchestration    | **SafetyAgent**  | `VirtualObject` keyed by `trip_id`                      | Per-trip monitor with mocked LLM via `ctx.run`, Awakeables for human-in-the-loop, suspend/resume    |

## Per-service breakdown

How each service uses Restate, how it receives requests, what state it owns,
and who it talks to. Three communication patterns recur, plus one special
primitive:

- **Sync request** (`[sync→]` tag in logs) — caller awaits a response. Covers both external HTTP from rider/driver/operator apps and internal RPC between services (`ctx.service_call` / `ctx.object_call`).
- **Durable async send** (`[send→]` tag) — fire-and-forget into the Restate log. Covers both external publishers (mapping providers, driver pings via HTTP `/send`) and internal one-way hops (`ctx.service_send` / `ctx.object_send`). Durable from acknowledgement.
- **Self-scheduled cadence** (`[self→]` tag) — same primitive as durable async send, just to self with a `send_delay`. Replaces external schedulers.
- **Awakeable** — pause an invocation on a token; resume when someone POSTs to its resolve URL. Used for the SafetyAgent's human-in-the-loop step.

### Trip — `VirtualObject` keyed by `trip_id`

**Domain:** Stateful microservices · **Lifetime:** one VO per ride.

**Requests arrive via:**
- Sync HTTP from rider/driver apps: `request_ride`, `confirm`, `cancel`, `complete`
- Durable async send from Dispatch (per match): `assign_driver`
- Shared HTTP read: `get`

**State owned:** `rider_id`, `origin`, `destination`, `region`, `status` (requested → quoted → dispatching → assigned → completed | cancelled), `offer`, `multiplier`, `assigned_driver_id`, `epoch_id`.

**Calls out to:**
- Sync RPC: `Offers.generate`
- Durable async sends: `Pricing.note_demand`, `Dispatch.enqueue_trip`, `Locations.accept_trip`, `SafetyAgent.start_monitoring`, `SafetyAgent.stop_monitoring`

Lifecycle owner. Single-writer-per-key means concurrent confirms/cancels for the same trip can't race.

### Offers — stateless `Service`

**Domain:** Stateful microservices · **Lifetime:** none — stateless.

**Requests arrive via:** Sync RPC from Trip during `request_ride` (handler `generate`).

**State owned:** None.

**Calls out to:** Sync RPC to `ETA.estimate` and `Pricing.quote`.

Synthesis layer — fans in ETA + Pricing into a ranked offer candidate set per car class (Standard, XL, Lux). Stateless because there's no per-trip-id concurrency story to enforce; the parent Trip VO already serializes.

### Pricing — `VirtualObject` keyed by `region`

**Domain:** Stateful microservices · **Lifetime:** one VO per region (SF, NYC, LA, SEA).

**Requests arrive via:**
- Sync RPC from Offers: `quote`
- Durable async sends from Trip (`note_demand`) and the driver-sim (`note_supply`)
- Self-scheduled `refresh` every 10s

**State owned:** `multiplier`, `supply_count`, `demand_count`, `last_refresh_ms`.

**Calls out to:**
- Sync RPC: `Features.get` (during refresh, reads weather + accident_density)
- Self-send: `refresh` to itself with `send_delay=10s` — the cadence loop

Per-region surge multiplier with periodic refresh. The self-send pattern means there's no cron, no Airflow, no external scheduler — the runtime is the scheduler.

### ETA — stateless `Service`

**Domain:** Stateful microservices · **Lifetime:** none — stateless.

**Requests arrive via:** Sync RPC from Offers during `generate` (handler `estimate`).

**State owned:** None.

**Calls out to:** Sync RPC to `Features.get` per request (reads weather + accident_density for the region).

Reliable arrival prediction. **The poison-pill target** — when weather is the sentinel value `"BAD"` and the gracefully-handle flag is off, the handler raises a plain `ValueError`. Restate retries forever with exponential backoff until either the input changes or the code is fixed.

### Dispatch — `VirtualObject` keyed by `region`

**Domain:** Workflow orchestration · **Lifetime:** one VO per region.

**Requests arrive via:**
- Durable async sends from Trip (`enqueue_trip`) and Locations (`register_driver` / `deregister_driver`)
- Self-scheduled `close_epoch` every 5s — the batched matching round

**State owned:** `active_driver_ids` (the regional driver pool), `pending_trips` (carried forward across epochs), `epoch_id`, `loop_running` flag.

**Calls out to:**
- Sync RPC: `Locations.get_position` (per active driver at epoch close)
- Durable async sends: `Trip.assign_driver` (per match)
- Self-send: `close_epoch` to itself with `send_delay=5s`

The long-running workflow. Each epoch is one round of the matching algorithm — snapshot pending trips, snapshot driver positions, greedy nearest-driver match (LP/Hungarian in a real system), carry unmatched trips into the next epoch. The "workflow" emerges from the call graph plus the self-send cadence; there's no DAG declaration.

### Locations — `VirtualObject` keyed by `driver_id`

**Domain:** Event-driven applications · **Lifetime:** one VO per driver.

**Requests arrive via:**
- Sync HTTP from driver app: `set_status`, `ping` (GPS update)
- Durable async send from Trip: `accept_trip`
- Sync RPC reads from Dispatch and SafetyAgent: `get_position` (shared handler)

**State owned:** `status` (offline / idle / en_route / on_trip), `matched_lat`, `matched_lng`, `last_ping_ms`, `region`, `current_trip_id`.

**Calls out to:**
- Durable async sends: `Dispatch.register_driver` (on transition to idle), `Dispatch.deregister_driver` (on transition away from idle)

High-volume GPS path. The map-matching smoothing (mocked here as an exponential moving average; a real system uses a Marginalized Particle Filter) happens inside the handler. The driver app POSTs pings as fire-and-forget HTTP sends — they're durable on the Restate log from the moment ingress acks them.

### Features — `VirtualObject` keyed by `entity_type:entity_id:feature_name`

**Domain:** Event-driven applications · **Lifetime:** one VO per feature key (e.g. `region:SF:weather`).

**Requests arrive via:**
- **Durable async HTTP send** from external mapping providers — they POST to `/Features/<key>/set/send`. Durable on the Restate log from the moment ingress acks. Same job a Kafka topic would do, without the cluster.
- Sync RPC reads from ETA, Pricing, Dispatch, SafetyAgent: `get` (shared handler)

**State owned:** `value`, `version`, `last_updated_ms`.

**Calls out to:** Nothing.

Online feature store. The single biggest architectural collapse in the demo: in a typical stack this would be Kafka topics + a Flink job + a separate feature-store service + an SDK to query it. Here external providers POST directly to Restate's ingress, the write is journaled on the Restate log, and downstream readers query the same VO that holds the value. Four pieces of infrastructure collapse into one primitive.

### SafetyAgent — `VirtualObject` keyed by `trip_id`

**Domain:** AI agent orchestration · **Lifetime:** one agent per active ride; lives for the duration of the trip.

**Requests arrive via:**
- Durable async sends from Trip: `start_monitoring` (on driver assignment), `stop_monitoring` (on trip complete / cancel)
- Self-scheduled `tick` every 8s
- **HTTP awakeable resolve** from human operator: external POST to `/restate/awakeables/{name}/resolve` (resumes a suspended tick)
- Shared HTTP read: `get`

**State owned:** `active` flag, `driver_id`, `region`, `ticks` counter, `escalations` counter, `pending_awakeable` (set while suspended on a human verdict).

**Calls out to:**
- Sync RPC per tick: `Locations.get_position`, `Features.get` (weather + accident_density)
- `ctx.run` for the mocked LLM risk score (journaled — replays are deterministic)
- `ctx.awakeable()` to create a suspension token + `await future` to suspend on it
- Self-send: `tick` to itself with `send_delay=8s`

The long-running AI agent. Three Restate primitives the other domains don't exercise:
- **`ctx.run`** journals a non-deterministic side effect (in a real system, the LLM call) so replays are reproducible.
- **Awakeable + `await future`** suspends the invocation cleanly. No Python process is held in memory while waiting; the runtime resumes the same invocation when the awakeable is resolved.
- Combined with the per-agent VO state, the result is a per-conversation agent with durable memory, deterministic replay, and human-in-the-loop — without LangGraph, without Redis-backed session state, without an external scheduler.

## Sync vs async — every interaction classified

Each edge is one of three flavors. The live log tags them so the audience can
see the distinction without narration:

- `[sync→]` synchronous request (caller awaits the response — external HTTP or internal RPC)
- `[send→]` durable async send (one-way; the Restate log handles the durable-input-queue job)
- `[self→]` delayed self-send (cadence loops; same primitive as `[send→]`, just to self with a delay)

| From | To | Flavor | Why |
|---|---|---|---|
| Rider app | `Trip.request_ride` | sync HTTP | Rider awaits the offer |
| Rider app | `Trip.confirm` / `cancel` | sync HTTP | App holds for ack |
| Driver app | `Trip.complete` | sync HTTP | App holds for ack |
| Driver app | `Locations.set_status` | sync HTTP | State transition with immediate ack |
| Driver app | `Locations.ping` (GPS) | HTTP `/send` (durable async) | Highest-volume external path. The Restate log handles the durable-input-queue job. |
| Mapping providers (external) | `Features.set` | HTTP `/send` (durable async) | External feed, fire-and-forget. **No Kafka.** Durable on the Restate log from acknowledgement. |
| `Trip` | `Offers.generate` | `[sync→]` | Trip needs the offer to respond to rider |
| `Trip` | `Pricing.note_demand` | `[send→]` | 1:1, fire-and-forget counter bump |
| `Trip` | `Dispatch.enqueue_trip` | `[send→]` | 1:1 into the region's matching round |
| `Trip` | `Locations.accept_trip` | `[send→]` | 1:1 to that specific driver VO |
| `Trip` | `SafetyAgent.start_monitoring` / `stop_monitoring` | `[send→]` | 1:1, agent lifecycle |
| `Offers` | `ETA.estimate` | `[sync→]` | Offers needs ETA to build candidates |
| `Offers` | `Pricing.quote` | `[sync→]` | Offers needs price to build candidates |
| `Dispatch` | `Locations.get_position` (per match) | `[sync→]` | Matcher needs current positions |
| `Dispatch` | `Trip.assign_driver` (per match) | `[send→]` | 1:1 per matched trip |
| `Locations` | `Dispatch.register_driver` / `deregister_driver` | `[send→]` | 1:1 to the region's pool |
| `Pricing` | `Features.get` (per refresh) | `[sync→]` | Reads features at refresh time |
| `ETA` | `Features.get` (per request) | `[sync→]` | Reads features at request time |
| `SafetyAgent` | `Locations.get_position` (per tick) | `[sync→]` | Agent reads driver position |
| `SafetyAgent` | `Features.get` (per tick) | `[sync→]` | Agent reads region features |
| `Dispatch` | itself (`close_epoch`) | `[self→]` every 5s | No external scheduler; Restate is the cadence |
| `Pricing` | itself (`refresh`) | `[self→]` every 10s | Same |
| `SafetyAgent` | itself (`tick`) | `[self→]` every 8s | Same |
| Human safety operator | Restate awakeable ingress | sync HTTP | POST resolves the agent's awakeable; agent resumes |

**Everything async — external publishers and internal hops — runs on the
Restate log.** No Kafka in the system. The audience sees two log-prefix
shapes for async work: `[send→]` (durable async via the Restate log) and
`[self→]` (cadence — a delayed send to self).

## Why no Kafka

This architecture doesn't use Kafka anywhere. Every external write — mapping
events, GPS pings, rider requests — and every internal hop writes directly
to the Restate log via Restate's HTTP ingress on `:8080`.

The argument:

- **Every Restate handler is durable from the moment ingress acks the
  request.** The Restate log handles the durable-input-queue job a Kafka
  topic would otherwise do — without the cluster to operate.
- **For internal RPC**, the sync path is just function calls
  (`ctx.service_call` / `ctx.object_call`). No bus needed.
- **For internal one-way / async decoupling between services**, `ctx.send()`
  writes to the same log. Same durability, retry, ordering, and
  observability you got from Kafka, with fewer moving parts.
- **For external publishers**, the HTTP `/send` endpoint gives the same
  "I publish and it's durable from here" guarantee. No subscription
  pipeline to operate.
- **For cadence loops**, delayed self-sends replace cron / Airflow.

**Where Kafka would still belong** (none of which is in this demo):

- Multi-consumer pub-sub with independent consumer groups and offsets
- Retention measured in days for analytical replay
- Cross-system flow beyond your Restate cluster
- Integration with an existing Kafka ecosystem you don't own

If any of those become real requirements, Restate has first-class Kafka
integration both inbound (subscriptions) and outbound (`ctx.run` producing
to a topic). They coexist cleanly. The point of this demo is that for the
vast majority of internal architectures, you don't *need* Kafka — and
removing it is one less cluster to operate, one less consumer-group story
to debug, one less integration test surface.

## How to run the demo

You need Python 3.13 (or 3.11+), Docker, and the
[`restate` CLI](https://docs.restate.dev/get_started/install).

### One-time setup

```bash
uv venv --python 3.13 .venv
.venv/bin/python -m pip install -e .
```

### Drive the demo (two terminals)

**Terminal 1** — the show. Hypercorn log scrolls here as the demo runs.

```bash
./scripts/demo-t1.sh fresh
```

Pauses for ENTER, wipes Restate state, starts hypercorn. Stays parked
on the running server. When Phase 3 of the demo asks you to fix the code:
`Ctrl+C` here, edit `rideco/services/eta.py`, then run
`./scripts/demo-t1.sh restart`.

**Terminal 2** — the guided walkthrough.

```bash
./scripts/demo-t2.sh
```

Walks through five phases, pausing for ENTER between every step. Each step
tells you what's about to happen, what to look for in Terminal 1's log, and
where to look in the Restate Web UI (`http://localhost:9070`).

### What the five phases show

1. **Quiet trip** — one rider request end-to-end. The full architecture
   visible in ~30 log lines: `[sync→]` RPC chain, `[send→]` Restate log hops,
   `[self→]` cadence loops.
2. **Poison-pill in LA** — inject `weather=BAD` via HTTP send. ETA can't parse it,
   raises a non-Terminal exception, Restate retries forever. SF takes the same
   code path and sails through. Per-key failure isolation in action.
3. **Fix the code** — flip a flag in `eta.py`, restart Terminal 1, watch the
   stuck retries drain on Restate's next backoff retry.
4. **Human-in-the-loop** — bump SF accident_density above the SafetyAgent's
   threshold. The agent reads the new feature on its next tick, opens an
   Awakeable, and suspends. You resolve the Awakeable as the operator via an
   HTTP POST; the agent resumes from exactly where it left off.
5. **Complete the trip** — terminal state transition. Trip fires a Restate log
   send to `SafetyAgent.stop_monitoring`; the agent shuts down.

### Scripts reference

| Script | Purpose |
|---|---|
| `./scripts/demo-t1.sh [fresh\|restart]` | Terminal 1 driver (fresh wipes state) |
| `./scripts/demo-t2.sh` | Terminal 2 guided 5-phase walkthrough |
| `./scripts/register.sh` | Register Python deployment with Restate |
| `./scripts/reset.sh` | Wipe Restate state |
| `./scripts/stop.sh` | Reliably stop hypercorn (handles macOS pkill gotcha) |
| `./scripts/setup-region.sh <region>` | Init a region: features + one idle driver |
| `./scripts/make-trip.sh <trip_id> <region>` | Rider request + confirm (sync, awaits offer) |
| `./scripts/make-trip-send.sh <trip_id> <region>` | Rider request fire-and-forget (use when poisoned) |
| `./scripts/complete-trip.sh <trip_id>` | Mark trip completed; agent shuts down |
| `./scripts/cancel-trip.sh <trip_id>` | Cancel trip |
| `./scripts/set-feature.sh <region> <feature> <value>` | Write a feature directly to the Features VO (sync HTTP) |
| `./scripts/poison.sh [region]` | Inject `weather=BAD` (default region LA) |
| `./scripts/escalate.sh [region]` | Push `accident_density=0.8` (default region SF) |
| `./scripts/approve.sh <awakeable_id> [verdict]` | Resolve a suspended SafetyAgent Awakeable |
| `./scripts/show-trip.sh <trip_id>` | Pretty-print Trip state + status legend |
| `./scripts/show-agent.sh <trip_id>` | Pretty-print SafetyAgent state |
| `./scripts/show-invocations.sh` | `restate invocations list --status running` with annotations |

Every script that writes a feature uses a sync HTTP POST to the Features
VO — the call returns once the durable write is committed, so the demo
flow is deterministic.

## Talking points, grouped by domain

These are speaker notes — substantive engineering, not marketing.

### Event-driven applications (Locations, Features)

- **In the usual Kafka land.** GPS pings hit a topic. A stream processor
  smooths and publishes back to another topic. A consumer writes a Redis
  geohash index. Driver state changes (online/off/en-route) live in a
  separate store with its own consistency story. Feature serving is a
  separate service backed by another KV store. Five moving pieces.
- **In RideCo.** A driver is a Virtual Object. Pings are handler calls.
  Map-matching smoothing happens in the handler (mocked here). A feature is
  also a Virtual Object — same primitive, different key namespace. Writers
  (external mapping providers, driver apps) call `.set` directly via HTTP
  `/send`; readers call `.get`. Every write is durable on the Restate log
  from the moment ingress acks. No Kafka, no Flink, no separate feature
  store.

### Stateful microservices (Trip, Offers, Pricing, ETA)

- **In the usual microservices land.** Trip state lives in a relational DB;
  state-machine transitions cross services via outbox + Kafka + consumer.
  Pricing's per-region multiplier is computed by a streaming job and
  stuffed into an online KV the pricing service polls. ETA is a service
  behind a feature-store-server with its own deployment story. Offer
  synthesis fans in via direct HTTP — failures show up as cascading
  timeouts, retries are client-coded.
- **In RideCo.** Trip is a Virtual Object — durable state per `trip_id`,
  no outbox, no consumer. Pricing is a Virtual Object per region with a
  delayed self-send for refresh. ETA reads features as ordinary handler
  calls. Offers is a stateless service that fans in via
  `await ctx.service_call(...)` / `await ctx.object_call(...)`. Retries are
  server-side and uniform across all of them. The poison-pill demo lives
  inside this domain.

### Workflow orchestration (Dispatch)

- **In the usual Temporal / Step Functions / Airflow land.** A batched
  matching round is a Workflow. A scheduler triggers it. Pending rider
  requests get pushed to a queue, drained inside the workflow, matched via
  an Activity. Carry-over re-publishes. Workflow code must be
  deterministic; any I/O has to be in an Activity in a separate worker
  fleet.
- **In RideCo.** The matching round is the body of `close_epoch` on a
  per-region Virtual Object. The cadence is a delayed self-send to the
  same key — the scheduler is the runtime. Pending trips and active
  drivers are state on the same object. There's no DAG, no separate
  worker fleet, no Activity-vs-Workflow split. The workflow emerges from
  the call graph.

### AI agent orchestration (SafetyAgent)

- **In the usual LangGraph / agent-framework land.** Per-agent state is in
  Redis with TTLs and lock dances. Long LLM calls hold a process. Human-in-
  the-loop is a separate orchestrator the agent polls. Deterministic replay
  is "good luck."
- **In RideCo.** The agent is a Virtual Object keyed by `trip_id` —
  durable, single-writer-per-key state. The LLM call goes through
  `ctx.run`, so the result is journaled and replays are deterministic.
  Human-in-the-loop is a one-line `(name, future) = ctx.awakeable()`; the
  agent suspends with `await future` and resumes when someone POSTs to
  `/restate/awakeables/{name}/resolve`. No process held in memory.

This is the domain Temporal does not address at all.

## Restate primitives the demo actually uses

- **Virtual Objects as per-key durable state.** Every entity that owns
  state is a Virtual Object. Trips, Drivers, Regions (Pricing + Dispatch),
  Features, per-trip SafetyAgents. Single-writer-per-key, durable across
  process / host / cluster.
- **Function-shaped service composition.** `await ctx.service_call(...)`
  and `await ctx.object_call(...)` look like ordinary function calls.
  Durable, retried, observable, cross-process. The call graph *is* the
  workflow.
- **Delayed self-sends as cadence.** Dispatch's epoch loop, Pricing's
  refresh loop, SafetyAgent's tick loop — all
  `ctx.object_send(handler, key=..., send_delay=timedelta(...))` to
  themselves. No external scheduler.
- **`ctx.run` for non-deterministic side effects.** SafetyAgent's mocked
  LLM call lives inside `ctx.run_typed(...)`. Result is journaled;
  replays are deterministic.
- **Awakeables for human-in-the-loop.** `(name, future) = ctx.awakeable()`
  produces a token; `await future` suspends. Resolved via Restate's
  awakeable ingress endpoint.
- **Retry vs `TerminalError`.** Regular `Exception` retries forever with
  backoff (poison-pill). `restate.exceptions.TerminalError` ends the
  invocation immediately.
- **Observability.** Restate UI at `:9070` shows every invocation, every
  retry, every state read and write.

## Versions used here

- **Restate server:** 1.6.2 (pinned in `docker-compose.yml`)
- **Restate Python SDK:** `restate_sdk[serde]` 0.18.0
- **Python:** 3.13
- **ASGI server:** Hypercorn (HTTP/2)

## Layout

```
rideco/
├── architecture.svg           # dark-slate architecture diagram, embedded above
├── docker-compose.yml         # restate-server 1.6.2 — that's the whole stack
├── hypercorn-config.toml      # ASGI binds :9080
├── pyproject.toml             # restate_sdk[serde], hypercorn, httpx, rich
├── Makefile                   # entry-point verbs (serve, register, stop, …)
├── scripts/                   # demo scripts — see "Scripts reference" above
├── rideco/
│   ├── shared/                # types, region defs, color logging w/ flow tags
│   ├── services/              # trip, offers, dispatch, locations, pricing,
│   │                          #   eta, features, safety_agent, app
│   └── sim/                   # rider, driver, mapping_events, _ingress helper
```

All eight services run in one process for the demo. Each is independent
and could be split into its own process — Restate doesn't care.

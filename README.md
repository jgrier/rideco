# RideCo — ride dispatch on Restate

RideCo is a fictional ride-hailing company invented to make the architecture
concrete. The artifact is a working slice of how you'd build the **next new
system** at a ride-hailing platform if Restate were the runtime — eight
services running on one stateful application platform instead of the usual
mix of Kafka + stream-processor + workflow-engine + ad-hoc queues + agent
framework.

This is not a migration pitch.

## The punchline

> **Every service/handler in Restate automatically has a durable log in front of it.** Any invocation — sync RPC, async send, Kafka subscription, scheduled timer, webhook — is journaled in Restate's log *before* it executes. Durability, retry, ordering, observability — all the things Kafka was giving you for internal-bus use cases — are now a property of every handler, transparently.

You don't need Kafka for what Kafka was being used for most of the time. You
need it where it actually earns its keep: a durable log at trust boundaries,
with multiple independent push consumers, with retention measured in days.
For everything else — internal courier work between two services that already
know each other — Restate's `ctx.send()` writes to the Restate log and you get the
same durability without operating the Kafka cluster.

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

**Arrow legend.** Three line styles cover every async/sync flavor in the demo:

- `→` thin solid = **synchronous RPC** (`[sync→]` in logs) — caller awaits the response.
- `⇒` thick solid = **durable async send via the Restate log** (`[send→]` in logs) — fire-and-forget, journaled, retryable.
- `⇢` dashed = **Kafka subscription** (`[kafka→]` in the publisher's logs) — used in exactly one place, the external mapping-events edge.

**Not shown on the diagram, but in the system:**

- **Self-sends for cadence loops** (`[self→]` in logs): Dispatch's `close_epoch` every 5s, Pricing's `refresh` every 10s, SafetyAgent's `tick` every 8s. Each VO schedules its own next invocation via `ctx.object_send(handler, key=self.key(), send_delay=...)`. No external scheduler.
- **Driver pings to Locations.ping**: same `[send→]`-shaped HTTP path as `set_status` but high-frequency. Could move to Kafka if multi-consumer ever emerges, but in our 8 services only Locations is the direct consumer — so it stays on the same path the rider app uses, just at higher rate.

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
| Event-driven applications | **Features**     | `VirtualObject` keyed by `entity:id:feature`            | Online feature store. Receives one Kafka topic externally.                                          |
| Stateful microservices    | **Trip**         | `VirtualObject` keyed by `trip_id`                      | Lifecycle state machine; entry point for ride requests                                              |
| Stateful microservices    | **Offers**       | stateless `Service`                                     | Fan-in over ETA + Pricing to produce ranked offer candidates per car class                          |
| Stateful microservices    | **Pricing**      | `VirtualObject` keyed by `region`                       | Surge multiplier; periodic refresh via delayed self-send                                            |
| Stateful microservices    | **ETA**          | stateless `Service`                                     | Reliable arrival prediction; reads Features at request time. **Poison-pill target.**                |
| Workflow orchestration    | **Dispatch**     | `VirtualObject` keyed by `region`                       | Batched matching round, every few seconds; epoch cadence is a delayed self-send                     |
| AI agent orchestration    | **SafetyAgent**  | `VirtualObject` keyed by `trip_id`                      | Per-trip monitor with mocked LLM via `ctx.run`, Awakeables for human-in-the-loop, suspend/resume    |

## Per-service breakdown

How each service uses Restate, how it receives requests, what state it owns,
and who it talks to. Five communication patterns recur:

- **Sync HTTP from external** — rider/driver/operator apps; Restate's ingress on `:8080`
- **Kafka subscription** — Restate consumes from a topic and invokes a handler; used in exactly one place
- **Sync RPC between services** — `ctx.service_call` / `ctx.object_call` (the `[sync→]` tag in logs)
- **Durable async send between services** — `ctx.service_send` / `ctx.object_send` via the Restate log (the `[send→]` tag)
- **Self-scheduled cadence** — same as durable async send but to self with `send_delay`; replaces an external scheduler (the `[self→]` tag)

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

High-volume GPS path. The map-matching smoothing (mocked here as an exponential moving average; a real system uses a Marginalized Particle Filter) happens inside the handler. Could be Kafka-fed in production if multi-consumer fan-out emerged, but in this demo only Locations is the direct consumer — so it stays on the same HTTP path the rider app uses.

### Features — `VirtualObject` keyed by `entity_type:entity_id:feature_name`

**Domain:** Event-driven applications · **Lifetime:** one VO per feature key (e.g. `region:SF:weather`).

**Requests arrive via:**
- **Kafka subscription** from `mapping_events` topic — the **only** Kafka path in the system. Restate routes each Kafka record by its key into `Features("<key>").set(<value>)`.
- Sync RPC reads from ETA, Pricing, Dispatch, SafetyAgent: `get` (shared handler)

**State owned:** `value`, `version`, `last_updated_ms`.

**Calls out to:** Nothing.

Online feature store. The single biggest architectural collapse in the demo: in a typical stack this would be Kafka topics + a Flink job + a separate feature-store service + an SDK to query it. Here the writer publishes to one Kafka topic (because external mapping providers live outside our trust boundary), Restate's subscription delivers the record, and downstream readers query the same VO that holds the value. Three pieces of infrastructure collapse into one primitive.

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

Each edge is one of four flavors. The live log tags them so the audience can
see the distinction without narration:

- `[sync→]` synchronous RPC (caller awaits the response)
- `[send→]` durable one-way send on Restate's log
- `[self→]` delayed self-send (cadence loops; no external scheduler)
- `[kafka→]` publish to a Kafka topic (used in **one** place — the external feed boundary)

| From | To | Flavor | Why |
|---|---|---|---|
| Rider app | `Trip.request_ride` | sync HTTP | Rider awaits the offer |
| Rider app | `Trip.confirm` / `cancel` | sync HTTP | App holds for ack |
| Driver app | `Trip.complete` | sync HTTP | App holds for ack |
| Driver app | `Locations.set_status` | sync HTTP | State transition with immediate ack |
| Driver app | `Locations.ping` (GPS) | sync HTTP `/send` | Highest-volume external path. **Restate log handles the durable-input-queue job that a Kafka topic would otherwise do.** |
| Mapping providers (external) | `Features.set` | **Kafka** → Restate subscription | The one Kafka use in the demo — see "Why Kafka here" below |
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

**Everything async in the system, except the one Kafka topic, runs on the
Restate log.** It replaces what Kafka used to be doing as an internal RPC
bus. The audience will see exactly two log-prefix shapes for async work:
`[send→]` (durable async via the Restate log) and `[self→]` (cadence — a
delayed send to self via the Restate log). Plus one `[kafka→]` shape at the
external-feed boundary.

## Why Kafka in exactly one place

External mapping providers (weather APIs, traffic feeds, government accident
reports) sit outside the platform's trust boundary. In real ride-hailing
stacks they genuinely land on Kafka because (a) the producers aren't yours,
(b) the same events fan out to many internal consumers, and (c) the log
needs retention measured in days for replay. That's where Kafka still earns
its keep, so we left it there:

```
external mapping providers → Kafka topic `mapping_events` → Restate subscription → Features.set
```

Restate's Kafka subscription routes each record: the Kafka **key** becomes
the Virtual Object key (e.g. `region:SF:weather`), the JSON **value**
becomes the `set` handler's payload. From the moment Restate accepts the
record, it's journaled in the Restate log — so even the Kafka-sourced invocations
benefit from the same durability/retry semantics as everything else.

**Where Kafka would still belong** (none of which is in our 8 services):

- Analytics consuming every Trip state transition
- ML training pipelines consuming dispatch assignments
- Real-time UI dashboards needing push-based fan-out
- Cross-system flow beyond Restate-land

If any of those got added, Kafka would slot in alongside Restate — typically
as an outbound `ctx.run` that publishes Trip lifecycle events to a topic.
Restate has first-class Kafka producer support for that pattern.

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

Pauses for ENTER, wipes Restate + Kafka state, starts hypercorn. Stays parked
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
   `[self→]` cadence loops, `Kafka` ingest.
2. **Poison-pill in LA** — inject `weather=BAD` via Kafka. ETA can't parse it,
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
| `./scripts/register.sh` | Register Python deployment + Kafka subscription |
| `./scripts/reset.sh` | Wipe Restate + Kafka state |
| `./scripts/stop.sh` | Reliably stop hypercorn (handles macOS pkill gotcha) |
| `./scripts/setup-region.sh <region>` | Init a region: features + one idle driver |
| `./scripts/make-trip.sh <trip_id> <region>` | Rider request + confirm (sync, awaits offer) |
| `./scripts/make-trip-send.sh <trip_id> <region>` | Rider request fire-and-forget (use when poisoned) |
| `./scripts/complete-trip.sh <trip_id>` | Mark trip completed; agent shuts down |
| `./scripts/cancel-trip.sh <trip_id>` | Cancel trip |
| `./scripts/set-feature.sh <region> <feature> <value>` | Publish feature via Kafka; polls until visible |
| `./scripts/poison.sh [region]` | Inject `weather=BAD` (default region LA) |
| `./scripts/escalate.sh [region]` | Push `accident_density=0.8` (default region SF) |
| `./scripts/approve.sh <awakeable_id> [verdict]` | Resolve a suspended SafetyAgent Awakeable |
| `./scripts/show-trip.sh <trip_id>` | Pretty-print Trip state + status legend |
| `./scripts/show-agent.sh <trip_id>` | Pretty-print SafetyAgent state |
| `./scripts/show-invocations.sh` | `restate invocations list --status running` with annotations |

Every script that writes to Kafka polls the Features VO until the value
actually lands — so the demo flow is deterministic, no race against the
async Kafka subscription path.

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
  call `.set`, readers call `.get`. The Kafka topic we kept (`mapping_events`)
  is there because external providers genuinely sit outside our trust
  boundary; everything else collapses into VOs.

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
- **Kafka subscription at the trust-boundary edge.** One topic
  (`mapping_events`), routed to `Features.set`. Restate's Kafka integration
  in action; everything internal stays on the Restate log.
- **Retry vs `TerminalError`.** Regular `Exception` retries forever with
  backoff (poison-pill). `restate.exceptions.TerminalError` ends the
  invocation immediately.
- **Observability.** Restate UI at `:9070` shows every invocation, every
  retry, every state read and write.

## Versions used here

- **Restate server:** 1.6.2 (pinned in `docker-compose.yml`)
- **Restate Python SDK:** `restate_sdk[serde]` 0.18.0
- **Kafka:** Apache Kafka 3.8.0, single broker, KRaft mode
- **Python:** 3.13
- **ASGI server:** Hypercorn (HTTP/2)

## Layout

```
rideco/
├── architecture.svg           # dark-slate architecture diagram, embedded above
├── docker-compose.yml         # restate-server 1.6.2 + kafka 3.8.0
├── restate.toml               # Restate config: declares the Kafka cluster
├── hypercorn-config.toml      # ASGI binds :9080
├── pyproject.toml             # restate_sdk[serde], hypercorn, httpx, aiokafka, rich
├── Makefile                   # entry-point verbs (serve, register, stop, …)
├── scripts/                   # 17 demo scripts — see "Scripts reference" above
├── rideco/
│   ├── shared/                # types, region defs, color logging w/ flow tags
│   ├── services/              # trip, offers, dispatch, locations, pricing,
│   │                          #   eta, features, safety_agent, app
│   └── sim/                   # rider, driver, mapping_events, _ingress helper
```

All eight services run in one process for the demo. Each is independent
and could be split into its own process — Restate doesn't care.

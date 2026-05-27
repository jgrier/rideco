"""RideCo TUI — single-window playground.

Owns the full demo lifecycle. On launch it boots Restate, spawns the
twelve service processes, registers each deployment, and starts the
sim fleet. On quit it tears everything back down.

Layout (top to bottom):
  Header
  Regions table        — live state of all four regions, 1s poll
  Services table       — process status of all twelve services, 1s poll
  Bottom pane          — boot progress / help / selected-service log tail
  Footer               — key hints

Run: ./scripts/tui.sh   (or python -m rideco.tui.app)
"""

from __future__ import annotations

import asyncio
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from rich.console import Group
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static

from rideco.shared.regions import REGIONS, all_regions
from rideco.tui.processes import (
    SERVICES,
    STATUS_DEAD,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPED,
    ProcessManager,
    ServiceProc,
)


# Bottom pane modes
MODE_HELP = "help"
MODE_BOOT = "boot"
MODE_TEARDOWN = "teardown"
MODE_LOG = "log"
MODE_REGION = "region"
MODE_DEMO = "demo"


INGRESS = "http://localhost:8080"
RISK_THRESHOLD = 0.6
REPO_DIR = Path(__file__).resolve().parent.parent.parent


# ───── data fetching ─────────────────────────────────────────────────


async def _fetch(client: httpx.AsyncClient, service: str, key: str) -> dict:
    try:
        r = await client.post(
            f"{INGRESS}/{service}/{key}/get", json={}, timeout=1.0,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


async def fetch_region_snapshot(
    client: httpx.AsyncClient, region: str,
) -> dict[str, dict]:
    agent, dispatch = await asyncio.gather(
        _fetch(client, "RegionSafetyAgent", region),
        _fetch(client, "Dispatch", region),
    )
    return {"agent": agent, "dispatch": dispatch}


async def fetch_sim_state(client: httpx.AsyncClient) -> dict:
    return await _fetch(client, "SimControl", "global")


# Rate-tuning bounds for the [ / ] keys. The SimControl default at boot
# is 0.10 trips/sec/rider. Floor/cap keep the demo from drifting into
# either silence or thousands of trips/sec.
RATE_STEP = 0.05
RATE_MIN = 0.02
RATE_MAX = 2.0


# ───── cell formatting ───────────────────────────────────────────────


def _risk_text(score: Any) -> Text:
    if not isinstance(score, (int, float)):
        return Text("—", style="dim")
    s = f"{score:.2f}"
    if score >= RISK_THRESHOLD:
        return Text(s, style="bold red")
    if score >= RISK_THRESHOLD * 0.66:
        return Text(s, style="yellow")
    return Text(s, style="green")


def _active_text(region_active: Any) -> Text:
    if region_active is False:
        return Text("HALTED", style="bold red")
    if region_active is True:
        return Text("active", style="green")
    return Text("—", style="dim")


def _count_text(n: int, style_pos: str) -> Text:
    return Text(str(n), style=style_pos) if n else Text("0", style="dim")


def _status_text(status: str) -> Text:
    if status == STATUS_RUNNING:
        return Text("● running", style="green")
    if status == STATUS_STARTING:
        return Text("◐ starting", style="yellow")
    if status == STATUS_DEAD:
        return Text("✕ dead", style="bold red")
    return Text("○ stopped", style="dim")


@dataclass
class RegionCells:
    active: Text
    halts: Text
    risk: Text
    epoch: str
    idle: Text
    pending: Text
    in_flight: Text
    done: Text
    awakeable: Text


def _build_region_cells(snap: dict) -> RegionCells:
    a = snap.get("agent") or {}
    d = snap.get("dispatch") or {}
    halts = a.get("halts", 0)
    pending = d.get("pending_trips_count", 0)
    idle = d.get("active_driver_count", 0)
    in_flight = d.get("in_flight", 0)
    done = d.get("total_completed", 0)
    awk = a.get("pending_awakeable") or ""
    if len(awk) > 36:
        awk = awk[:33] + "..."
    awk_text = Text(awk, style="cyan") if awk else Text("—", style="dim")
    return RegionCells(
        active=_active_text(a.get("region_active")),
        halts=Text(str(halts), style="bold red") if halts else Text("0", style="dim"),
        risk=_risk_text(a.get("last_score")),
        epoch=str(d.get("epoch_id", 0)),
        idle=_count_text(idle, "white") if idle else Text("0", style="red"),
        pending=_count_text(pending, "yellow"),
        in_flight=_count_text(in_flight, "cyan"),
        done=_count_text(done, "bold green"),
        awakeable=awk_text,
    )


# ───── widgets ───────────────────────────────────────────────────────


class RegionsTable(DataTable):
    # (key, label, width). width=None auto-sizes; explicit widths keep
    # rendered cells from getting truncated when the cell content (Rich
    # Text with markup) is wider than the header.
    COLS = [
        ("region", "Region", 7),
        ("active", "Active?", 9),
        ("halts", "Halts", 6),
        ("risk", "Risk", 6),
        ("epoch", "Epoch", 6),
        ("idle", "Idle", 5),
        ("pending", "Pending", 8),
        ("in_flight", "In-flight", 10),
        ("done", "Done", 6),
        ("awakeable", "Awakeable", None),
    ]

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        for key, label, width in self.COLS:
            self.add_column(label, key=key, width=width)
        for region in all_regions():
            self.add_row(region, "", "", "", "", "", "", "", "", "", key=region)

    def apply(self, region: str, snap: dict) -> None:
        c = _build_region_cells(snap)
        self.update_cell(region, "active", c.active)
        self.update_cell(region, "halts", c.halts)
        self.update_cell(region, "risk", c.risk)
        self.update_cell(region, "epoch", c.epoch)
        self.update_cell(region, "idle", c.idle)
        self.update_cell(region, "pending", c.pending)
        self.update_cell(region, "in_flight", c.in_flight)
        self.update_cell(region, "done", c.done)
        self.update_cell(region, "awakeable", c.awakeable)


class ServicesTable(DataTable):
    # (key, label, width). region_safety_agent is the longest service
    # name at 19 chars; "● running" is 9. Explicit widths so neither gets
    # truncated.
    COLS = [
        ("name", "Service", 22),
        ("port", "Port", 6),
        ("status", "Status", 12),
        ("pid", "PID", 8),
        ("last_log", "Last log line", None),
    ]

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        for key, label, width in self.COLS:
            self.add_column(label, key=key, width=width)
        for name, port in SERVICES:
            self.add_row(name, str(port), "", "", "", key=name)

    # Width budgets used by `apply` to compute available cell width for
    # the last_log column at render time. Adapts to terminal resize on
    # the next poll cycle. See _max_log_width below.
    _FIXED_COL_WIDTH = 22 + 6 + 12 + 8   # name + port + status + pid
    _TABLE_CHROME = 6                     # borders / cell padding / scrollbar

    def _max_log_width(self) -> int:
        """Available chars for the last_log cell at current terminal width."""
        return max(20, (self.size.width or 120)
                   - self._FIXED_COL_WIDTH - self._TABLE_CHROME)

    def apply(self, svc: ServiceProc) -> None:
        self.update_cell(svc.name, "status", _status_text(svc.status))
        self.update_cell(svc.name, "pid", str(svc.pid) if svc.pid else "—")
        # Pre-truncate to the dynamic budget so the auto-sized column
        # never expands beyond the terminal. no_wrap + overflow stays as
        # belt-and-suspenders against Rich/Textual rendering quirks.
        line = svc.last_log or ""
        budget = self._max_log_width()
        if len(line) > budget:
            line = line[:budget - 1] + "…"
        self.update_cell(
            svc.name, "last_log",
            Text(line, no_wrap=True, overflow="ellipsis"),
        )


HELP_TEXT = """[bold]RideCo TUI[/bold]

[bold cyan]Navigation[/bold cyan]
  [yellow]Tab[/yellow]    move focus between the Regions and Services tables
         (the row cursor follows whichever table has focus)
  [yellow]↑/↓[/yellow]   select rows in the focused table

[bold cyan]Quit / help / refresh[/bold cyan]
  [yellow]q[/yellow]        quit (tears everything down)
  [yellow]?[/yellow]        show this help
  [yellow]r[/yellow]        refresh now
  [yellow]ctrl+r[/yellow]   [red]reset[/red] — wipe ALL Restate state and reboot the stack

[bold cyan]On the Regions table  ([italic]bottom pane shows live region detail[/italic])[/bold cyan]
  [yellow]s[/yellow]    spike the selected region (25s of unsafe features
        — the RegionSafetyAgent will halt it)
  [yellow]a[/yellow]    approve the selected region's pending awakeable
        (resumes dispatch)
  [yellow]m[/yellow]    make an ad-hoc trip in the selected region
        (id stored for the next 'c' cancel)
  [yellow]c[/yellow]    cancel the last trip made via 'm'
  [yellow]p[/yellow]    poison-pill the selected region's weather key
        (stuck Features.set, queues per-key, others unaffected)

[bold cyan]On the Services table  ([italic]bottom pane tails the selected service's log[/italic])[/bold cyan]
  [yellow]k[/yellow]    kill the selected service process
  [yellow]b[/yellow]    boot the selected service (re-registers with Restate)

[bold cyan]Sims  ([italic]single-line panel above the bottom pane[/italic])[/bold cyan]
  [yellow]\\[[/yellow] / [yellow]\\][/yellow]   nudge the per-rider trip rate down / up (step 0.05/s,
            clamped to [0.02, 2.00])
  [yellow]shift+p[/yellow]   pause / resume riders + drivers + mapping

[bold cyan]Trip inspection[/bold cyan]
  [yellow]t[/yellow]    open the trip-detail modal (pre-filled with the last
        trip made via 'm'; type any trip id + Enter to retarget)

[bold cyan]Demo walkthrough[/bold cyan]
  [yellow]d[/yellow]    toggle demo mode — guided 8-step walkthrough of the
        whole halt/approve/drain narrative + Restate UI callouts
  [yellow]n[/yellow] / [yellow]N[/yellow]   next / previous demo step (while demo is on)
  [yellow]o[/yellow]    open Restate UI in the browser (jumps to the page
        the current demo step references, or root otherwise)

[bold cyan]What's running[/bold cyan]
  The TUI owns restate-server, the twelve service hypercorns, and
  the sim fleet. Tables above are live (1s poll).
"""


RESTATE_UI = "http://localhost:9070"


SERVICE_NARRATIVES: dict[str, str] = {
    "trip": (
        "[bold]Per-trip Virtual Object — lifecycle state machine.[/bold]\n\n"
        "Handlers:\n"
        "  · [yellow]request_ride[/yellow]  sync entry — fans out to Offers (which fans into ETA + Pricing)\n"
        "  · [yellow]confirm[/yellow]       enqueues into Dispatch, then SUSPENDS on an awakeable\n"
        "  · [yellow]complete[/yellow]      auto-fired by a delayed self-send after ride duration\n"
        "  · [yellow]cancel[/yellow]        terminal\n"
        "  · [yellow]get[/yellow]           shared read\n\n"
        "The dispatch dependency is one-way: Trip hands Dispatch an "
        "awakeable token; Dispatch resolves it from the other side. "
        "Trip never imports Dispatch's matching logic."
    ),
    "offers": (
        "[bold]Stateless service — pure synthesis.[/bold]\n\n"
        "  · [yellow]generate[/yellow]  fans out to ETA.estimate and Pricing.quote "
        "in parallel, composes a ranked offer set across car classes, "
        "selects one. Called once per request_ride; no state of its own.\n\n"
        "The call graph IS the workflow — no orchestrator, no DAG "
        "framework, no queue plumbing. Just await."
    ),
    "eta": (
        "[bold]Stateless service — arrival-time prediction.[/bold]\n\n"
        "  · [yellow]estimate[/yellow]  haversine distance × weather multiplier × "
        "accident-density multiplier; reliability score on the side.\n\n"
        "Production uses GBDT models with reliability SLAs per ETA "
        "bracket. The mocked formula here takes the same inputs and "
        "returns the same shape so the surrounding architecture lands."
    ),
    "pricing": (
        "[bold]Per-region Virtual Object — surge multiplier.[/bold]\n\n"
        "  · [yellow]refresh[/yellow]  every 10s via delayed self-send. Reads "
        "weather / accident_density / windowed ride-request rate from "
        "Features, recomputes multiplier.\n"
        "  · [yellow]quote[/yellow]    returns base × multiplier for a distance.\n"
        "  · [yellow]note_demand[/yellow] / [yellow]note_supply[/yellow]  bump cumulative counters.\n\n"
        "Surge has BOTH a cumulative supply/demand ratio AND a rolling "
        "60s request-rate signal from the Features aggregate — recent "
        "demand intensity beats all-time demand."
    ),
    "locations": (
        "[bold]Per-driver Virtual Object — GPS + status state.[/bold]\n\n"
        "  · [yellow]ping[/yellow]        smoothed position (EMA here; MPF + UKF in production)\n"
        "  · [yellow]set_status[/yellow]  OFFLINE/IDLE/EN_ROUTE/ON_TRIP transitions\n"
        "  · [yellow]accept_trip[/yellow] driver took an assignment\n"
        "  · [yellow]get_position[/yellow] shared read\n\n"
        "Going IDLE registers the driver in the regional Dispatch pool; "
        "leaving IDLE deregisters. That's how supply availability flows "
        "into matching without a separate registry service."
    ),
    "features": (
        "[bold]Composite-key Virtual Object — online feature store.[/bold]\n\n"
        "Two shapes on the same VO, on different state slots:\n\n"
        "  [yellow]set[/yellow] / [yellow]get[/yellow]                 point-value features "
        "(last-write-wins). Used for region:SF:weather, region:SF:accident_density.\n"
        "  [yellow]record_event[/yellow] / [yellow]event_rate[/yellow]  rolling 60s aggregate over an "
        "event stream. Used for events:region:SF:ride_request.\n\n"
        "Per-key serialization means a stuck handler on one key "
        "(poison-pill demo) blocks only that key — every other Features "
        "VO keeps working."
    ),
    "dispatch": (
        "[bold]Per-region Virtual Object — batched matching round.[/bold]\n\n"
        "  · [yellow]close_epoch[/yellow]  every 5s via delayed self-send. Snapshots "
        "pending trips + idle drivers, runs nearest-driver match, "
        "resolves each trip's awakeable with the assignment.\n"
        "  · [yellow]enqueue_trip[/yellow] · [yellow]register_driver[/yellow] · [yellow]deregister_driver[/yellow]\n"
        "  · [yellow]set_active[/yellow]   RegionSafetyAgent flips this false on halt.\n\n"
        "When active=false, close_epoch skips matching but keeps "
        "ticking — so resume after a halt is immediate."
    ),
    "region_safety_agent": (
        "[bold]Per-region Virtual Object — AI safety monitor.[/bold]\n\n"
        "  · [yellow]tick[/yellow]  every 10s via delayed self-send. Reads region "
        "features, scores composite risk via [yellow]ctx.run_typed[/yellow] "
        "(a mocked LLM — journaled for replay determinism).\n"
        "  · When risk ≥ 0.6 and dispatch active: sends "
        "Dispatch.set_active(false), creates an awakeable, SUSPENDS "
        "until a human verdict arrives.\n\n"
        "Human-in-the-loop, durable suspend — the showcase for "
        "awakeables. No process held while waiting; hours can pass; "
        "host can restart and the awakeable survives."
    ),
    "sim_rider": (
        "[bold]Per-rider Virtual Object — durable load generator.[/bold]\n\n"
        "  · [yellow]tick[/yellow]   pick a region, call Trip.request_ride (sync), send "
        "Trip.confirm (async), self-tick at Poisson-jittered cadence.\n"
        "  · [yellow]start[/yellow] / [yellow]pause[/yellow] / [yellow]resume[/yellow] / [yellow]set_rate[/yellow]\n\n"
        "Each rider VO holds its own rate and pause flag. Pause/resume "
        "is just a normal handler call — no SIGUSR1, no side-channel "
        "control plane. Same primitives the app uses."
    ),
    "sim_driver": (
        "[bold]Per-driver Virtual Object — durable load generator.[/bold]\n\n"
        "  · [yellow]start[/yellow]  bootstraps the driver as IDLE via "
        "Locations.set_status (which registers it in the regional "
        "Dispatch pool) and bumps Pricing.note_supply.\n"
        "  · [yellow]tick[/yellow]   pings position to Locations every ~2s; self-tick.\n"
        "  · [yellow]pause[/yellow] / [yellow]resume[/yellow]\n\n"
        "Drivers pin to a region; riders don't (each request picks a "
        "fresh region) — that's why a small rider pool covers all "
        "regions over time."
    ),
    "sim_mapping": (
        "[bold]Per-region Virtual Object — weather + accident feed.[/bold]\n\n"
        "  · [yellow]start[/yellow]  on first start, BOOTSTRAPS the region's "
        "Pricing.refresh and RegionSafetyAgent.start_monitoring cadence "
        "loops (one-way kick).\n"
        "  · [yellow]tick[/yellow]   emits weather + accident_density to Features "
        "every 12s. Stays in safe ranges by default; "
        "[yellow]./scripts/spike-region.sh[/yellow] or the TUI's [yellow]s[/yellow] key forces it unsafe."
    ),
    "sim_control": (
        "[bold]Singleton Virtual Object (key='global') — sim fleet control plane.[/bold]\n\n"
        "Holds the configured drivers/riders count so the operator "
        "doesn't have to remember sizes.\n\n"
        "  · [yellow]start_all[/yellow] / [yellow]stop_all[/yellow]\n"
        "  · [yellow]pause_riders[/yellow] / [yellow]resume_riders[/yellow] / [yellow]set_rider_rate[/yellow]\n"
        "  · [yellow]pause_drivers[/yellow] / [yellow]resume_drivers[/yellow]\n"
        "  · [yellow]pause_mapping[/yellow] / [yellow]resume_mapping[/yellow]\n\n"
        "Fan-out to per-key sim VOs via ctx.object_send. Operators "
        "(TUI, scripts, curl) drive the fleet exactly the way they "
        "drive the app."
    ),
}


@dataclass(frozen=True)
class DemoStep:
    title: str
    body: str       # background / what's happening
    action: str     # what the operator should press in the TUI
    watch: str      # what to look for here in the TUI
    restate: str    # what to look at in the Restate UI
    # URL `o` opens for this step. Defaults to the UI root; specific
    # steps can override to land deeper if we know the route.
    restate_url: str = "http://localhost:9070"


DEMO_STEPS: list[DemoStep] = [
    DemoStep(
        title="Welcome to the RideCo demo",
        body=(
            "RideCo is a ride-dispatch system built on Restate. Four city "
            "regions, twelve services, simulators generating live load.\n\n"
            "This walkthrough takes you through the central narrative: "
            "an AI safety agent watching each region, halting dispatch "
            "when conditions get unsafe, suspending on a durable awakeable, "
            "and resuming when a human approves. All on three primitives: "
            "call(), send(), and awakeable."
        ),
        action="Press [yellow]n[/yellow] to advance, [yellow]N[/yellow] to go back, [yellow]d[/yellow] to exit demo.",
        watch="The four regions in the Regions table — SF / NYC / LA / SEA — should all show 'active' with low risk (green).",
        restate=f"Open [cyan]{RESTATE_UI}[/cyan] in a browser. We'll keep coming back to it.",
    ),
    DemoStep(
        title="Phase 1 · The system is alive",
        body=(
            "The TUI booted everything: a Restate container, twelve service "
            "hypercorns, and a sim fleet (rider/driver/mapping VOs driven "
            "by SimControl). Riders generate ride_request → quote → confirm "
            "flows continuously. Drivers ping their position. The mapping "
            "feed publishes weather + accident_density per region every 12s.\n\n"
            "Everything is a Restate service — including the sims. Same "
            "primitives the app uses, same ingress; they just play external "
            "roles."
        ),
        action="No action — just observe for ~30s.",
        watch="Regions table: 'Done' climbs in all four regions; 'In-flight' hovers around the matching cadence; 'Risk' stays green.",
        restate=f"In the Restate UI ([cyan]{RESTATE_UI}[/cyan]) open the [bold]Invocations[/bold] view. You'll see lots of activity: Trip / Offers / Pricing / Dispatch invocations completing, plus durable scheduled ones for the cadence loops (next step).",
    ),
    DemoStep(
        title="The cadence loops",
        body=(
            "Three loops drive the system, all implemented as delayed "
            "self-sends — Restate IS the scheduler:\n"
            "  · Dispatch.close_epoch       every 5s  per region\n"
            "  · Pricing.refresh            every 10s per region\n"
            "  · RegionSafetyAgent.tick     every 10s per region\n\n"
            "No cron, no scheduler service. Just ctx.object_send(..., "
            "send_delay=...). The next firing is durable on the Restate "
            "log — if the host crashes, the send still fires."
        ),
        action="In the Services table, arrow down to [yellow]dispatch[/yellow] (or [yellow]region_safety_agent[/yellow]) to tail its log.",
        watch=(
            "In the bottom-pane log tail you'll see lines like "
            "[blue]→ send+delay(5s) Dispatch.close_epoch[/blue] and "
            "[blue]→ send+delay(10s) RegionSafetyAgent.tick[/blue]. "
            "Press [yellow]Tab[/yellow] to flip back to the Regions table when ready."
        ),
        restate=f"In the Restate UI Invocations view, filter by service = [bold]Dispatch[/bold] or [bold]RegionSafetyAgent[/bold] — you'll see the scheduled future invocations queued (not yet executed).",
    ),
    DemoStep(
        title="Phase 2 · Spike SF unsafe",
        body=(
            "RegionSafetyAgent[SF] reads weather + accident_density from "
            "Features on every tick and scores composite risk via "
            "ctx.run_typed (a mocked LLM). Threshold is 0.6. We're about "
            "to force SF over the line by writing accident_density=0.85 "
            "and weather=rain_heavy continuously for 25s — the agent will "
            "see it on its next tick (within 10s)."
        ),
        action="Make sure SF is selected in the Regions table (arrow up to the SF row), then press [yellow]s[/yellow] to spike.",
        watch="A 'spiking SF (25s sustained)…' notification appears at the top. The bottom pane (in region-detail mode for SF) shows risk climbing as the agent ticks.",
        restate="In the Restate UI Invocations view, watch for [bold]RegionSafetyAgent.tick[/bold] firings on key=SF — each scores higher as the spike lands.",
    ),
    DemoStep(
        title="The halt",
        body=(
            "On the tick where risk crosses 0.6, the agent:\n"
            "  1. Sends Dispatch.set_active(false) — matching pauses\n"
            "  2. Creates an awakeable (a durable named promise)\n"
            "  3. Awaits the awakeable — SUSPENDS the invocation\n\n"
            "While suspended, NO Python process is held. The invocation "
            "is parked durably on Restate's log. Hours could pass; if the "
            "host restarts, the awakeable survives. SF's pending trips "
            "queue up but stay durably enqueued; the other three regions "
            "keep matching."
        ),
        action="Wait ~10s for the next agent tick. Stay on SF in the Regions table to watch.",
        watch="SF row goes [red]HALTED[/red]; the Halts column ticks up; the Awakeable column shows a [cyan]sign_…[/cyan] id; pending trips grow. NYC/LA/SEA keep working.",
        restate=f"In the Restate UI ([cyan]{RESTATE_UI}[/cyan]) → Invocations: find the [bold]RegionSafetyAgent[/bold] key=SF invocation in [bold]Suspended[/bold] state. Click into State → see region_active=false, halts=1, pending_awakeable=sign_…. That's the same id you see in the TUI.",
    ),
    DemoStep(
        title="Phase 3 · Human approves resume",
        body=(
            "You're the safety operator. Approving resolves the awakeable "
            "with verdict=approve. That resumes the suspended invocation, "
            "which then sends Dispatch.set_active(true). On the next "
            "Dispatch.close_epoch (≤5s) the backlog drains."
        ),
        action="With SF still selected, press [yellow]a[/yellow] to approve.",
        watch="Bottom pane shows 'approved SF — agent resuming'. Within ~5s SF flips back to [green]active[/green], 'pending' falls toward 0, 'in-flight' and 'done' climb again.",
        restate="Back in Invocations, the previously-suspended RegionSafetyAgent invocation transitions to Completed. The next RegionSafetyAgent.tick on SF resumes the normal scoring cycle (still ticking every 10s).",
    ),
    DemoStep(
        title="Stream processing — windowed demand",
        body=(
            "Pricing surge has a real-time component now: a rolling 60s "
            "rate of ride requests per region. Every Trip.request_ride "
            "fire-and-forget sends an event to Features (key prefix "
            "'events:region:SF:ride_request'). Features keeps a rolling "
            "window per key (record_event handler). Pricing.refresh "
            "reads the rate every 10s (event_rate shared handler) and "
            "folds it into the multiplier. Same VO holds both shapes — "
            "point-value features (set/get) AND aggregates over event "
            "streams (record_event/event_rate)."
        ),
        action="Arrow down to [yellow]pricing[/yellow] in the Services table to tail its log.",
        watch="Each refresh line shows [dim]request_rate_per_s=X.XXX[/dim] — that's the windowed signal. Spike the rider rate with [yellow]][/yellow] to push it higher; surge multiplier responds within a refresh tick.",
        restate=f"In Restate UI → Services → [bold]Features[/bold] → State, browse keys starting with [cyan]events:region:[/cyan]. Their 'samples' is the rolling timestamp list — the durable backing of the windowed aggregate.",
    ),
    DemoStep(
        title="Now try the rest",
        body=(
            "You've seen the core. Other things to play with from this TUI:\n"
            "  · [yellow]m[/yellow] make an ad-hoc trip in the selected region\n"
            "  · [yellow]c[/yellow] cancel the trip you just made\n"
            "  · [yellow]t[/yellow] open the trip-detail modal — drill into any trip\n"
            "  · [yellow]p[/yellow] poison a region's weather feature — per-key fault isolation\n"
            "  · [yellow]k[/yellow] / [yellow]b[/yellow] kill / boot a service — Restate keeps invocations durable across the gap\n"
            "  · [yellow][/yellow] / [yellow]][/yellow] tune the rider rate · [yellow]shift+p[/yellow] pause sims\n"
            "  · [yellow]ctrl+r[/yellow] full reset — wipe state, reboot everything\n\n"
            "Press [yellow]d[/yellow] to exit demo mode. The whole demo loop runs on the same three primitives — call(), send(), awakeable — plus delayed self-sends for cadence. No Kafka, no Redis, no workflow engine, no agent framework."
        ),
        action="Press [yellow]d[/yellow] to exit demo mode and explore freely.",
        watch="Anything you want.",
        restate=f"Keep [cyan]{RESTATE_UI}[/cyan] open on a second monitor — watching invocations land while you trigger TUI actions is the best way to internalize the model.",
    ),
]


TRIP_STATUS_STYLES = {
    "requested":   ("dim",            "requested"),
    "quoted":      ("yellow",         "quoted"),
    "dispatching": ("yellow",         "dispatching"),
    "assigned":    ("green",          "assigned"),
    "in_progress": ("green",          "in_progress"),
    "completed":   ("bold green",     "completed"),
    "cancelled":   ("red",            "cancelled"),
}


def _styled_status(status: Any) -> str:
    if not isinstance(status, str) or not status:
        return "[dim]—[/dim]"
    style, label = TRIP_STATUS_STYLES.get(status, ("white", status))
    return f"[{style}]{label}[/{style}]"


def _render_trip(data: dict) -> str:
    """Render a Trip.get response into Rich-markup text."""
    if not data or not data.get("trip_id"):
        return "[red]no such trip (state empty)[/red]"
    trip_id = data["trip_id"]
    status = _styled_status(data.get("status"))
    region = data.get("region") or "—"
    rider = data.get("rider_id") or "—"
    driver = data.get("assigned_driver_id") or "[dim]—[/dim]"
    epoch = data.get("epoch_id")
    epoch_s = str(epoch) if epoch is not None else "[dim]—[/dim]"
    offer = data.get("offer") or {}
    if offer:
        eta = offer.get("eta_seconds")
        eta_s = f"{eta // 60}m {eta % 60}s" if isinstance(eta, int) else "—"
        price = offer.get("price_cents")
        price_s = f"${price/100:.2f}" if isinstance(price, (int, float)) else "—"
        car = offer.get("car_class") or "—"
        rel = offer.get("reliability_score")
        rel_s = f"{rel:.2f}" if isinstance(rel, (int, float)) else "—"
        offer_block = (
            "[bold]Offer[/bold]\n"
            f"  ETA:        [cyan]{eta_s}[/cyan]\n"
            f"  Price:      [cyan]{price_s}[/cyan]\n"
            f"  Car class:  [cyan]{car}[/cyan]\n"
            f"  Reliability: [cyan]{rel_s}[/cyan]\n"
        )
    else:
        offer_block = "[dim]No offer yet (still in 'requested' state?)[/dim]"
    mult = data.get("multiplier")
    mult_s = f"{mult:.2f}x" if isinstance(mult, (int, float)) else "—"
    awk = data.get("pending_match_awakeable") or ""
    if awk:
        awk_block = (
            f"[bold]Awaiting match[/bold] — awakeable: [cyan]{awk}[/cyan]\n"
            "  [dim]Trip is suspended; Dispatch will resolve this on the next round[/dim]"
        )
    else:
        awk_block = "[dim]Not currently awaiting a match.[/dim]"
    return (
        f"[bold cyan]{trip_id}[/bold cyan]    status: {status}    region: [white]{region}[/white]\n\n"
        f"rider:  [white]{rider}[/white]\n"
        f"driver: [white]{driver}[/white]    epoch: [white]{epoch_s}[/white]    multiplier: [white]{mult_s}[/white]\n\n"
        f"{offer_block}\n"
        f"{awk_block}"
    )


class TripDetailScreen(ModalScreen):
    """Modal that polls Trip.get for a given trip id and renders detail.

    Pre-fills with the last manually-made trip id if any, but the user
    can type any trip id and press Enter to retarget.
    """

    CSS = """
    TripDetailScreen { align: center middle; }
    #trip_modal {
        width: 90; height: 24; padding: 1 2;
        border: thick $accent; background: $surface;
    }
    #trip_id_input { margin-bottom: 1; }
    #trip_body { height: 1fr; }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+c", "close", "Close"),
    ]

    def __init__(self, client: httpx.AsyncClient, initial_trip_id: str = "") -> None:
        super().__init__()
        self._client = client
        self._trip_id = initial_trip_id

    def compose(self) -> ComposeResult:
        with Vertical(id="trip_modal"):
            yield Static(
                "[bold]Trip detail[/bold]   "
                "[dim](type a trip id and press Enter; esc to close)[/dim]",
            )
            yield Input(
                value=self._trip_id, placeholder="trip-xxxxxxxx",
                id="trip_id_input",
            )
            yield Static(id="trip_body")

    async def on_mount(self) -> None:
        self.set_interval(1.0, self._poll)
        if self._trip_id:
            await self._poll()
        else:
            self.query_one("#trip_body", Static).update(
                "[dim]enter a trip id above and press Enter[/dim]",
            )

    @on(Input.Submitted, "#trip_id_input")
    async def _on_submitted(self, event: Input.Submitted) -> None:
        self._trip_id = event.value.strip()
        await self._poll()

    @on(Input.Changed, "#trip_id_input")
    def _on_changed(self, event: Input.Changed) -> None:
        self._trip_id = event.value.strip()

    async def _poll(self) -> None:
        if not self._trip_id:
            return
        try:
            r = await self._client.post(
                f"{INGRESS}/Trip/{self._trip_id}/get",
                json={}, timeout=2.0,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            try:
                self.query_one("#trip_body", Static).update(
                    f"[red]error fetching {self._trip_id}: {e}[/red]",
                )
            except Exception:
                pass
            return
        try:
            self.query_one("#trip_body", Static).update(_render_trip(data))
        except Exception:
            pass

    def action_close(self) -> None:
        self.dismiss()


class SimPanel(Static):
    """Single-line live summary of the sim fleet, with key hints."""

    DEFAULT = "[dim]sims: connecting...[/dim]"

    def on_mount(self) -> None:
        self.update(self.DEFAULT)

    def show(
        self, *, riders: int, drivers: int, rate: Any,
        mapping_s: Any, paused: bool,
    ) -> None:
        rate_s = f"{rate:.2f}/s" if isinstance(rate, (int, float)) else "—"
        map_s = f"{mapping_s:.1f}s" if isinstance(mapping_s, (int, float)) else "—"
        state = (
            "[bold red]PAUSED[/bold red]" if paused
            else "[green]running[/green]"
        )
        self.update(
            f"[bold]Sims[/bold]   "
            f"riders [yellow]{riders}[/yellow] @ [cyan]{rate_s}[/cyan]   "
            f"drivers [yellow]{drivers}[/yellow]   "
            f"mapping [yellow]{map_s}[/yellow]   "
            f"state: {state}   "
            f"[dim]press '\\[' / ']' to nudge rate · shift+p to pause[/dim]"
        )


class LatestTripPanel(Static):
    """Single-line ticker for the most recently enqueued trip across
    regions. Sourced from Dispatch.get.last_enqueued_trip_id every poll;
    enriched with status/multiplier from a Trip.get poll on the winner."""

    DEFAULT = "[dim]Latest trip: (none yet — sims are starting up)[/dim]"

    def on_mount(self) -> None:
        self.update(self.DEFAULT)

    def show(self, trip_id: Optional[str], snap: dict) -> None:
        if not trip_id:
            self.update(self.DEFAULT)
            return
        status = snap.get("status")
        status_s = _styled_status(status) if status else "[dim]—[/dim]"
        region = snap.get("region") or "—"
        driver = snap.get("assigned_driver_id") or ""
        mult = snap.get("multiplier")
        mult_s = f"{mult:.2f}x" if isinstance(mult, (int, float)) else "—"
        driver_s = f"   driver=[white]{driver}[/white]" if driver else ""
        self.update(
            f"[bold]Latest trip[/bold] [cyan]{trip_id}[/cyan]   "
            f"region=[white]{region}[/white]   "
            f"status={status_s}   "
            f"mult=[white]{mult_s}[/white]"
            f"{driver_s}   "
            f"[dim]press[/dim] [yellow]t[/yellow] [dim]for full detail[/dim]"
        )


class RightPane(Static):
    """Right side of the bottom row. One of:
      · demo step       (when demo mode is on — always wins)
      · service narrative  (when a service row is selected, demo off)
      · default hint     (otherwise)
    """

    DEFAULT = (
        "[bold]Side pane[/bold]\n\n"
        "[dim]Press[/dim] [yellow]d[/yellow] [dim]for the guided demo walkthrough.[/dim]\n"
        "[dim]Or select a service in the Services table to see what it does.[/dim]"
    )

    def on_mount(self) -> None:
        self.update(self.DEFAULT)

    def show_default(self) -> None:
        self.update(self.DEFAULT)

    def show_narrative(self, svc_name: str) -> None:
        body = SERVICE_NARRATIVES.get(svc_name)
        if body is None:
            self.update(f"[bold cyan]{svc_name}[/bold cyan]\n\n[dim]No description.[/dim]")
            return
        self.update(f"[bold cyan]{svc_name}[/bold cyan]\n\n{body}")

    def show_demo(self, idx: int, step: "DemoStep") -> None:
        header = (
            f"[bold cyan]Demo · Step {idx + 1} of {len(DEMO_STEPS)}[/bold cyan]"
            f"   [dim]([yellow]n[/yellow] next · [yellow]N[/yellow] back "
            f"· [yellow]d[/yellow] exit)[/dim]"
        )
        title = f"[bold]{step.title}[/bold]"
        body = step.body
        action = f"[bold yellow]Action:[/bold yellow]    {step.action}"
        watch = f"[bold green]Watch here:[/bold green]   {step.watch}"
        restate = f"[bold magenta]Restate UI:[/bold magenta]   {step.restate}"
        self.update(
            f"{header}\n\n{title}\n\n{body}\n\n{action}\n\n{watch}\n\n{restate}",
        )


class BottomPane(Static):
    """Multi-mode bottom area: help, boot progress, teardown progress, or
    a tailing log view of the currently-selected service."""

    def on_mount(self) -> None:
        self.update(HELP_TEXT)

    def show_help(self) -> None:
        self.update(HELP_TEXT)

    def show_boot(self, lines: list[str]) -> None:
        body = "[bold]Booting RideCo...[/bold]\n\n" + "\n".join(
            f"  {line}" for line in lines
        )
        self.update(body)

    def show_teardown(self, lines: list[str]) -> None:
        body = "[bold]Tearing down...[/bold]\n\n" + "\n".join(
            f"  {line}" for line in lines
        )
        self.update(body)

    def show_log(self, svc: ServiceProc, height_hint: int = 14) -> None:
        """Render the most-recent log lines for the given service.

        Lines are pre-truncated to `self.size.width - padding` and then
        wrapped in a Rich Text with no_wrap=True so long lines get
        cropped at the pane boundary instead of wrapping. Pre-truncating
        in app code is the guaranteed fix — relying solely on
        no_wrap/overflow turns out not to survive Textual's Static
        rendering path for multi-line content, and a wrapped traceback
        was the original cause of UI hangs.
        """
        header = Text.from_markup(
            f"[bold cyan]{svc.name}[/bold cyan]"
            f"   port=[yellow]{svc.port}[/yellow]"
            f"   status=[white]{svc.status}[/white]"
            f"   pid=[white]{svc.pid or '—'}[/white]"
            f"   [dim](? for help)[/dim]",
        )
        lines = list(svc.log)[-height_hint:]
        if not lines:
            body: Any = Text.from_markup("[dim]no log output yet[/dim]")
        else:
            # 4 = border (2) + padding (2). Adapts to terminal resize.
            budget = max(20, (self.size.width or 120) - 4)
            trimmed = [
                (line if len(line) <= budget else line[:budget - 1] + "…")
                for line in lines
            ]
            body = Text("\n".join(trimmed), no_wrap=True, overflow="crop")
        self.update(Group(header, Text(""), body))

    def show_region(self, region: str, snap: dict) -> None:
        """Render a focused detail view for the highlighted region."""
        a = snap.get("agent") or {}
        d = snap.get("dispatch") or {}
        active = a.get("region_active")
        if active is False:
            status = "[bold red]HALTED[/bold red]"
        elif active is True:
            status = "[green]active[/green]"
        else:
            status = "[dim]—[/dim]"
        score = a.get("last_score")
        if isinstance(score, (int, float)):
            if score >= RISK_THRESHOLD:
                score_s = f"[bold red]{score:.2f}[/bold red]"
            elif score >= RISK_THRESHOLD * 0.66:
                score_s = f"[yellow]{score:.2f}[/yellow]"
            else:
                score_s = f"[green]{score:.2f}[/green]"
        else:
            score_s = "[dim]—[/dim]"
        halts = a.get("halts", 0)
        halts_s = f"[bold red]{halts}[/bold red]" if halts else "[dim]0[/dim]"
        awk = a.get("pending_awakeable") or ""
        if awk:
            awk_block = (
                f"[bold]Pending awakeable:[/bold] [cyan]{awk}[/cyan]\n"
                "  [dim]press[/dim] [yellow]a[/yellow] [dim]to approve and resume dispatch[/dim]"
            )
        else:
            awk_block = "[dim]No pending awakeable (region not suspended).[/dim]"
        header = (
            f"[bold cyan]{region}[/bold cyan]   status={status}   "
            f"halts={halts_s}   risk={score_s} [dim](thresh {RISK_THRESHOLD:.2f})[/dim]   "
            f"epoch=[white]{d.get('epoch_id', 0)}[/white]"
        )
        counts = (
            f"  pending=[yellow]{d.get('pending_trips_count', 0)}[/yellow]   "
            f"in-flight=[cyan]{d.get('in_flight', 0)}[/cyan]   "
            f"done=[bold green]{d.get('total_completed', 0)}[/bold green]   "
            f"idle drivers=[white]{d.get('active_driver_count', 0)}[/white]"
        )
        footer = (
            "  [dim]press[/dim] [yellow]s[/yellow] [dim]to spike this region "
            "(25s sustained unsafe features)   ·   [yellow]?[/yellow] for help[/dim]"
        )
        self.update(
            f"{header}\n\n{counts}\n\n{awk_block}\n\n{footer}"
        )


# ───── app ───────────────────────────────────────────────────────────


class RidecoApp(App):
    CSS = """
    Screen { layout: vertical; }
    #regions { height: 9; }
    #services { height: 15; }
    #sim_panel { height: 1; padding: 0 2; }
    #latest_trip { height: 1; padding: 0 2; }
    #bottom_row { height: 1fr; }
    #bottom_left { width: 1fr; padding: 1 2; border: round $accent; }
    #bottom_right { width: 1fr; padding: 1 2; border: round $accent; }
    """

    BINDINGS = [
        Binding("q", "quit_clean", "Quit"),
        Binding("?", "help", "Help"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("s", "spike_region", "Spike"),
        Binding("a", "approve_region", "Approve"),
        Binding("m", "make_trip", "Make trip"),
        Binding("c", "cancel_trip", "Cancel trip"),
        Binding("p", "poison_region", "Poison"),
        Binding("ctrl+r", "reset_all", "Reset"),
        Binding("k", "kill_service", "Kill svc"),
        Binding("b", "boot_service", "Boot svc"),
        Binding("[", "rate_down", "Rate−"),
        Binding("]", "rate_up", "Rate+"),
        Binding("shift+p", "sims_toggle", "Pause sims"),
        Binding("t", "trip_detail", "Trip detail"),
        Binding("d", "demo_toggle", "Demo"),
        Binding("n", "demo_next", "Next step", show=False),
        Binding("N", "demo_prev", "Prev step", show=False),
        Binding("o", "open_restate", "Open Restate UI"),
    ]

    def __init__(self, auto_boot: bool = True) -> None:
        super().__init__()
        self._auto_boot = auto_boot
        self._boot_lines: list[str] = []
        self._teardown_lines: list[str] = []
        self._tearing_down = False
        self._mode: str = MODE_HELP
        self._selected_service: Optional[str] = None
        self._selected_region: Optional[str] = None
        # Both DataTables auto-emit a RowHighlighted for row 0 on mount.
        # We swallow each table's first event so the bottom pane stays on
        # the help screen until the user actually arrows into a row.
        self._initial_region_highlight_consumed = False
        self._initial_service_highlight_consumed = False
        # Per-region spike guard — pressing 's' twice on the same region
        # would otherwise stack two 25s write loops and double the rate.
        self._spiking: set[str] = set()
        # Last trip id the user manually made via the 'm' binding. The 'c'
        # binding cancels this one; demo-style ad-hoc trip flow.
        self._last_manual_trip: Optional[str] = None
        # Reset is heavy and global — guard to avoid two concurrent resets.
        self._resetting: bool = False
        # Sim pause state. SimControl has no central paused flag (each
        # rider/driver/mapping VO holds its own), so we track what we
        # last told the fleet and trust it.
        self._sims_paused: bool = False
        self._last_sim_state: dict = {}
        # Polling-in-progress flags. set_interval fires the callback at a
        # fixed cadence; if a previous refresh is still in flight (e.g.
        # Restate has slowed because a killed service is causing retries
        # to back up), we'd queue more in-flight requests on every tick
        # and saturate the httpx pool. Skip overlapping refreshes.
        self._refreshing_regions = False
        self._refreshing_services = False
        self._refreshing_sim = False
        # Demo walkthrough state. _demo_step is None when demo is off.
        self._demo_step: Optional[int] = None
        # Latest-trip ticker state. _latest_trip_id is whichever region
        # most-recently updated its dispatch.last_enqueued_trip_id.
        # _per_region_latest tracks the last-observed id per region so
        # we can detect changes; _latest_trip_snap is the most recent
        # Trip.get response so the ticker can render status + multiplier.
        self._latest_trip_id: Optional[str] = None
        self._per_region_latest: dict[str, str] = {}
        self._latest_trip_snap: dict = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RegionsTable(id="regions")
        yield ServicesTable(id="services")
        yield SimPanel(id="sim_panel")
        yield LatestTripPanel(id="latest_trip")
        with Horizontal(id="bottom_row"):
            yield BottomPane(id="bottom_left")
            yield RightPane(id="bottom_right")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "RideCo"
        self.sub_title = "owning the full stack · q quit · ? help"
        self._client = httpx.AsyncClient()
        self.pm = ProcessManager(REPO_DIR)
        # Start polling immediately so the user sees the (empty) tables.
        self.set_interval(1.0, self._refresh_regions)
        self.set_interval(0.8, self._refresh_services)
        self.set_interval(2.0, self._refresh_sim)
        self.set_interval(2.0, self._refresh_latest_trip)
        if self._auto_boot:
            asyncio.create_task(self._boot())

    async def on_unmount(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()

    # ───── boot / teardown ───────────────────────────────────────────

    def _boot_progress(self, line: str) -> None:
        self._boot_lines.append(line)
        if self._mode == MODE_BOOT:
            try:
                self.query_one(BottomPane).show_boot(self._boot_lines)
            except Exception:
                pass

    async def _boot(self) -> None:
        self._mode = MODE_BOOT
        try:
            await self.pm.full_boot(progress=self._boot_progress)
        except Exception as e:
            self._boot_progress(f"[red]boot failed: {e}[/red]")
            return
        # Boot complete — flip back to help (unless user has already
        # selected a service to tail).
        if self._mode == MODE_BOOT:
            self._mode = MODE_HELP
            self.query_one(BottomPane).show_help()

    async def _teardown(self) -> None:
        self._tearing_down = True
        self._mode = MODE_TEARDOWN
        self._teardown_lines = []

        def progress(msg: str) -> None:
            self._teardown_lines.append(msg)
            try:
                self.query_one(BottomPane).show_teardown(self._teardown_lines)
            except Exception:
                pass

        await self.pm.full_teardown(progress=progress, stop_restate=True)

    async def action_quit_clean(self) -> None:
        await self._teardown()
        self.exit()

    # ───── polling ───────────────────────────────────────────────────

    async def _refresh_regions(self) -> None:
        if self._refreshing_regions:
            return
        self._refreshing_regions = True
        try:
            regions = all_regions()
            snaps = await asyncio.gather(
                *(fetch_region_snapshot(self._client, r) for r in regions)
            )
            try:
                table = self.query_one(RegionsTable)
            except Exception:
                return
            for region, snap in zip(regions, snaps):
                table.apply(region, snap)
            # Track the most recently enqueued trip across regions for
            # the LatestTripPanel ticker. Each Dispatch.get returns its
            # last_enqueued_trip_id; whichever region's id has changed
            # since we last looked is the freshest activity.
            for region, snap in zip(regions, snaps):
                tid = (snap.get("dispatch") or {}).get("last_enqueued_trip_id")
                if tid and tid != self._per_region_latest.get(region):
                    self._per_region_latest[region] = tid
                    self._latest_trip_id = tid
            # If the bottom pane is in region-detail mode, repaint it
            # from the snapshot we just pulled so it streams at the
            # same cadence.
            if self._mode == MODE_REGION and self._selected_region:
                try:
                    idx = regions.index(self._selected_region)
                except ValueError:
                    return
                try:
                    self.query_one(BottomPane).show_region(
                        self._selected_region, snaps[idx],
                    )
                except Exception:
                    pass
        finally:
            self._refreshing_regions = False

    async def _refresh_latest_trip(self) -> None:
        """Poll Trip.get for the currently-tracked latest trip and update
        the ticker. Cheap: one request every 2s, only fires when we know
        a trip id."""
        try:
            panel = self.query_one(LatestTripPanel)
        except Exception:
            return
        trip_id = self._latest_trip_id
        if not trip_id:
            panel.show(None, {})
            return
        try:
            r = await self._client.post(
                f"{INGRESS}/Trip/{trip_id}/get", json={}, timeout=1.0,
            )
            r.raise_for_status()
            self._latest_trip_snap = r.json()
        except Exception:
            # Keep showing whatever we last had, just refresh the panel
            # with cached state. Don't blank — that flickers on transient
            # ingress hiccups.
            pass
        panel.show(trip_id, self._latest_trip_snap)

    async def _refresh_sim(self) -> None:
        if self._refreshing_sim:
            return
        self._refreshing_sim = True
        try:
            try:
                data = await fetch_sim_state(self._client)
            except Exception:
                return
            self._last_sim_state = data
            try:
                panel = self.query_one(SimPanel)
            except Exception:
                return
            panel.show(
                riders=data.get("riders", 0),
                drivers=data.get("drivers", 0),
                rate=data.get("rider_rate"),
                mapping_s=data.get("mapping_interval_s"),
                paused=self._sims_paused,
            )
        finally:
            self._refreshing_sim = False

    async def _refresh_services(self) -> None:
        if self._refreshing_services:
            return
        self._refreshing_services = True
        try:
            try:
                table = self.query_one(ServicesTable)
            except Exception:
                return
            for svc in self.pm.services.values():
                table.apply(svc)
            # If the bottom is in log-tail mode, repaint the selected
            # service's buffer so new lines stream at the same cadence.
            if self._mode == MODE_LOG and self._selected_service:
                svc = self.pm.services.get(self._selected_service)
                if svc is not None:
                    try:
                        self.query_one(BottomPane).show_log(svc)
                    except Exception:
                        pass
        finally:
            self._refreshing_services = False

    # ───── events ────────────────────────────────────────────────────

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Track which row in which table the user is on, and flip the
        bottom pane to match (region detail or service log tail).

        The very first auto-highlight from each table on mount is
        swallowed so the help screen survives until the user actually
        moves the cursor."""
        if event.row_key is None or event.row_key.value is None:
            return

        if isinstance(event.control, RegionsTable):
            region = event.row_key.value
            self._selected_region = region
            if not self._initial_region_highlight_consumed:
                self._initial_region_highlight_consumed = True
                return
            self._mode = MODE_REGION
            asyncio.create_task(self._paint_region_now(region))
            return

        if isinstance(event.control, ServicesTable):
            name = event.row_key.value
            self._selected_service = name
            if not self._initial_service_highlight_consumed:
                self._initial_service_highlight_consumed = True
                return
            svc = self.pm.services.get(name)
            if svc is None:
                return
            self._mode = MODE_LOG
            try:
                self.query_one(BottomPane).show_log(svc)
            except Exception:
                pass
            # Right pane: show the service narrative — unless the demo
            # is active, in which case the demo always wins.
            if self._demo_step is None:
                try:
                    self.query_one(RightPane).show_narrative(name)
                except Exception:
                    pass

    async def _paint_region_now(self, region: str) -> None:
        """One-shot fetch + paint so switching to a region row shows
        detail immediately instead of waiting for the next 1s poll."""
        snap = await fetch_region_snapshot(self._client, region)
        # Mode/selection may have changed while we were awaiting.
        if self._mode != MODE_REGION or self._selected_region != region:
            return
        try:
            self.query_one(BottomPane).show_region(region, snap)
        except Exception:
            pass

    # ───── actions ───────────────────────────────────────────────────

    def action_help(self) -> None:
        self._mode = MODE_HELP
        self.query_one(BottomPane).show_help()

    async def action_refresh_now(self) -> None:
        await self._refresh_regions()
        await self._refresh_services()

    # ───── command surface ───────────────────────────────────────────

    def _region_target(self) -> Optional[str]:
        return self._selected_region or "SF"

    def action_spike_region(self) -> None:
        region = self._region_target()
        if region is None:
            self.notify("no region selected", severity="warning")
            return
        if region in self._spiking:
            self.notify(f"{region} already spiking — ignored", severity="warning")
            return
        self.notify(f"spiking {region} (25s sustained)...")
        asyncio.create_task(self._spike(region))

    async def _spike(self, region: str) -> None:
        self._spiking.add(region)
        try:
            end = asyncio.get_event_loop().time() + 25.0
            wrote = 0
            while asyncio.get_event_loop().time() < end:
                try:
                    await self._client.post(
                        f"{INGRESS}/Features/region:{region}:accident_density/set",
                        json={"value": 0.85}, timeout=2.0,
                    )
                    await self._client.post(
                        f"{INGRESS}/Features/region:{region}:weather/set",
                        json={"value": "rain_heavy"}, timeout=2.0,
                    )
                    wrote += 1
                except Exception as e:
                    self.notify(f"spike write error: {e}", severity="error")
                    return
                await asyncio.sleep(3.0)
            self.notify(f"spike of {region} done ({wrote} writes)")
        finally:
            self._spiking.discard(region)

    async def action_approve_region(self) -> None:
        region = self._region_target()
        if region is None:
            self.notify("no region selected", severity="warning")
            return
        snap = await fetch_region_snapshot(self._client, region)
        aid = (snap.get("agent") or {}).get("pending_awakeable")
        if not aid:
            self.notify(f"{region} has no pending awakeable", severity="warning")
            return
        try:
            r = await self._client.post(
                f"{INGRESS}/restate/awakeables/{aid}/resolve",
                json={"verdict": "approve", "reviewer": "tui"},
                timeout=5.0,
            )
            r.raise_for_status()
            self.notify(f"approved {region} — agent resuming")
        except Exception as e:
            self.notify(f"approve failed: {e}", severity="error")

    def action_make_trip(self) -> None:
        region = self._region_target()
        if region is None:
            self.notify("no region selected", severity="warning")
            return
        self.notify(f"making trip in {region}...")
        asyncio.create_task(self._make_trip(region))

    async def _make_trip(self, region: str) -> None:
        center = REGIONS[region]["center"]
        lat, lng = center["lat"], center["lng"]
        trip_id = f"trip-{uuid.uuid4().hex[:8]}"
        try:
            r = await self._client.post(
                f"{INGRESS}/Trip/{trip_id}/request_ride",
                json={
                    "rider_id": f"r-{trip_id}",
                    "origin": {"lat": lat, "lng": lng},
                    "destination": {"lat": lat - 0.02, "lng": lng + 0.02},
                    "region": region,
                },
                timeout=10.0,
            )
            r.raise_for_status()
            r = await self._client.post(
                f"{INGRESS}/Trip/{trip_id}/confirm", json={}, timeout=5.0,
            )
            r.raise_for_status()
        except Exception as e:
            self.notify(f"make-trip failed: {e}", severity="error")
            return
        self._last_manual_trip = trip_id
        self.notify(f"trip {trip_id} created in {region} (saved for 'c')")

    async def action_cancel_trip(self) -> None:
        trip_id = self._last_manual_trip
        if trip_id is None:
            self.notify(
                "no manually-made trip to cancel — press 'm' first",
                severity="warning",
            )
            return
        try:
            r = await self._client.post(
                f"{INGRESS}/Trip/{trip_id}/cancel", json={}, timeout=5.0,
            )
            r.raise_for_status()
            self.notify(f"cancelled {trip_id}")
        except Exception as e:
            self.notify(f"cancel {trip_id} failed: {e}", severity="error")

    async def action_poison_region(self) -> None:
        region = self._region_target()
        if region is None:
            self.notify("no region selected", severity="warning")
            return
        try:
            r = await self._client.post(
                f"{INGRESS}/Features/region:{region}:weather/set/send",
                json={"value": "POISON"},
                timeout=5.0,
            )
            r.raise_for_status()
            self.notify(
                f"poison sent to Features[region:{region}:weather] "
                "— stuck invocation will queue same-key writes",
            )
        except Exception as e:
            self.notify(f"poison failed: {e}", severity="error")

    async def action_reset_all(self) -> None:
        if self._resetting:
            self.notify("reset already in progress", severity="warning")
            return
        self._resetting = True
        self._mode = MODE_BOOT
        self._boot_lines = [
            "[bold yellow]RESET — wiping all Restate state[/bold yellow]",
        ]
        try:
            self.query_one(BottomPane).show_boot(self._boot_lines)
        except Exception:
            pass
        try:
            await self.pm.full_teardown(
                progress=self._boot_progress, stop_restate=True,
            )
            await self.pm.full_boot(progress=self._boot_progress)
            # The trip we tracked no longer exists — its state was wiped.
            self._last_manual_trip = None
            self._boot_progress("[green]reset complete.[/green]")
        except Exception as e:
            self._boot_progress(f"[red]reset failed: {e}[/red]")
        finally:
            self._resetting = False

    def action_trip_detail(self) -> None:
        """Open the trip-detail modal. Defaults to the latest enqueued
        trip across all regions (sim-generated or manual) — falls back
        to the last manually-made one if we haven't observed any yet."""
        initial = self._latest_trip_id or self._last_manual_trip or ""
        self.push_screen(
            TripDetailScreen(client=self._client, initial_trip_id=initial),
        )

    # ───── demo walkthrough ──────────────────────────────────────────

    def action_demo_toggle(self) -> None:
        """Enter or exit demo mode. Demo lives in the right pane. The
        left pane stays free for region detail / service log / help, so
        you can read the step AND watch logs at the same time."""
        if self._demo_step is None:
            self._demo_step = 0
            self._show_demo_step()
            self.notify("demo mode on — n next · N back · d exit")
        else:
            self._demo_step = None
            # On exit: restore narrative if a service is selected,
            # else fall back to the default hint.
            try:
                right = self.query_one(RightPane)
            except Exception:
                return
            if self._selected_service:
                right.show_narrative(self._selected_service)
            else:
                right.show_default()
            self.notify("demo mode off")

    def action_demo_next(self) -> None:
        if self._demo_step is None:
            return
        if self._demo_step < len(DEMO_STEPS) - 1:
            self._demo_step += 1
            self._show_demo_step()
        else:
            self.notify("end of demo — press d to exit")

    def action_demo_prev(self) -> None:
        if self._demo_step is None:
            return
        if self._demo_step > 0:
            self._demo_step -= 1
            self._show_demo_step()
        else:
            self.notify("at first step")

    def _show_demo_step(self) -> None:
        if self._demo_step is None:
            return
        step = DEMO_STEPS[self._demo_step]
        try:
            self.query_one(RightPane).show_demo(self._demo_step, step)
        except Exception:
            pass

    def action_open_restate(self) -> None:
        """Open the Restate UI in the user's browser. If demo mode is
        active, jump to the URL the current step references; otherwise
        open the root."""
        if self._demo_step is not None:
            url = DEMO_STEPS[self._demo_step].restate_url
        else:
            url = RESTATE_UI
        try:
            webbrowser.open(url)
            self.notify(f"opened {url}")
        except Exception as e:
            self.notify(f"open failed: {e}", severity="error")

    # ───── sim control ───────────────────────────────────────────────

    def action_rate_down(self) -> None:
        asyncio.create_task(self._nudge_rate(-RATE_STEP))

    def action_rate_up(self) -> None:
        asyncio.create_task(self._nudge_rate(+RATE_STEP))

    async def _nudge_rate(self, delta: float) -> None:
        current = self._last_sim_state.get("rider_rate")
        if not isinstance(current, (int, float)):
            self.notify("rider_rate unknown yet — wait for sims to settle",
                        severity="warning")
            return
        new_rate = max(RATE_MIN, min(RATE_MAX, float(current) + delta))
        if abs(new_rate - float(current)) < 1e-6:
            self.notify(f"rider_rate already at {new_rate:.2f}/s "
                        f"({'min' if delta < 0 else 'max'})",
                        severity="warning")
            return
        try:
            r = await self._client.post(
                f"{INGRESS}/SimControl/global/set_rider_rate",
                json={"rate": new_rate}, timeout=5.0,
            )
            r.raise_for_status()
            # Update cached state immediately so the next keypress sees
            # the new value rather than waiting for the 2s poll.
            self._last_sim_state["rider_rate"] = new_rate
            self.notify(f"rider_rate → {new_rate:.2f}/s")
        except Exception as e:
            self.notify(f"set_rider_rate failed: {e}", severity="error")

    async def action_sims_toggle(self) -> None:
        if self._sims_paused:
            await self._resume_sims()
        else:
            await self._pause_sims()

    async def _pause_sims(self) -> None:
        try:
            r = await self._client.post(
                f"{INGRESS}/SimControl/global/stop_all",
                json={}, timeout=10.0,
            )
            r.raise_for_status()
            self._sims_paused = True
            self.notify("sims paused (riders + drivers + mapping)")
        except Exception as e:
            self.notify(f"pause sims failed: {e}", severity="error")

    async def _resume_sims(self) -> None:
        # SimControl has no single 'resume_all'; call the three resume
        # handlers in parallel. resume_riders/resume_drivers/resume_mapping
        # each re-kick the per-VO tick loops.
        async def _post(path: str) -> None:
            r = await self._client.post(
                f"{INGRESS}/SimControl/global/{path}",
                json={}, timeout=10.0,
            )
            r.raise_for_status()

        try:
            await asyncio.gather(
                _post("resume_riders"),
                _post("resume_drivers"),
                _post("resume_mapping"),
            )
            self._sims_paused = False
            self.notify("sims resumed")
        except Exception as e:
            self.notify(f"resume sims failed: {e}", severity="error")

    def action_kill_service(self) -> None:
        name = self._selected_service
        if not name:
            self.notify("select a service in the Services table first",
                        severity="warning")
            return
        self.notify(f"killing {name}...")
        # Fire-and-forget so the keypress handler returns immediately;
        # if stop_service ever wedges (asyncio watcher quirk on rapid
        # child kill, for example), the UI keeps responding.
        asyncio.create_task(self._kill_one(name))

    async def _kill_one(self, name: str) -> None:
        try:
            await asyncio.wait_for(self.pm.stop_service(name), timeout=8.0)
            self.notify(f"{name} stopped")
        except asyncio.TimeoutError:
            self.notify(
                f"{name}: stop timed out after 8s — process may be orphaned",
                severity="error",
            )
        except Exception as e:
            self.notify(f"stop {name} failed: {e}", severity="error")

    def action_boot_service(self) -> None:
        name = self._selected_service
        if not name:
            self.notify("select a service in the Services table first",
                        severity="warning")
            return
        self.notify(f"booting {name}...")
        asyncio.create_task(self._boot_one(name))

    async def _boot_one(self, name: str) -> None:
        try:
            await self.pm.start_service(name)
            # Give the hypercorn a moment to bind, then re-register.
            await asyncio.sleep(2.0)
            await self.pm.register_one(name)
            self.notify(f"{name} up + registered")
        except Exception as e:
            self.notify(f"boot {name} failed: {e}", severity="error")


def main() -> None:
    RidecoApp().run()


if __name__ == "__main__":
    main()

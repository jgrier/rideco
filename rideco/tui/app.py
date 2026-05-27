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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from rich.console import Group
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
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

[bold cyan]What's running[/bold cyan]
  The TUI owns restate-server, the twelve service hypercorns, and
  the sim fleet. Tables above are live (1s poll).
"""


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
    #bottom { padding: 1 2; border: round $accent; }
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RegionsTable(id="regions")
        yield ServicesTable(id="services")
        yield SimPanel(id="sim_panel")
        yield BottomPane(id="bottom")
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
        """Open the trip-detail modal, pre-filled with the last manual trip."""
        self.push_screen(
            TripDetailScreen(
                client=self._client,
                initial_trip_id=self._last_manual_trip or "",
            ),
        )

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

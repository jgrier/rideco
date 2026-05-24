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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static

from rideco.shared.regions import all_regions
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


INGRESS = "http://localhost:8080"
RISK_THRESHOLD = 0.6
REPO_DIR = Path(__file__).resolve().parent.parent.parent


# ───── data fetching ─────────────────────────────────────────────────


async def _fetch(client: httpx.AsyncClient, service: str, key: str) -> dict:
    try:
        r = await client.post(
            f"{INGRESS}/{service}/{key}/get", json={}, timeout=1.5,
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

    def apply(self, svc: ServiceProc) -> None:
        self.update_cell(svc.name, "status", _status_text(svc.status))
        self.update_cell(svc.name, "pid", str(svc.pid) if svc.pid else "—")
        tail = svc.last_log
        if len(tail) > 80:
            tail = tail[:77] + "..."
        self.update_cell(svc.name, "last_log", tail or "")


HELP_TEXT = """[bold]RideCo TUI[/bold]   [dim](phase 3d — full command surface)[/dim]

[bold cyan]Navigation[/bold cyan]
  [yellow]Tab[/yellow]    move focus between the Regions and Services tables
         (the row cursor follows whichever table has focus)
  [yellow]↑/↓[/yellow]   select rows in the focused table

[bold cyan]Quit / help / refresh[/bold cyan]
  [yellow]q[/yellow]    quit (tears everything down)
  [yellow]?[/yellow]    show this help
  [yellow]r[/yellow]    refresh now

[bold cyan]On the Regions table[/bold cyan]
  [yellow]s[/yellow]    spike the selected region (25s of unsafe features
        — the RegionSafetyAgent will halt it)
  [yellow]a[/yellow]    approve the selected region's pending awakeable
        (resumes dispatch)

[bold cyan]On the Services table  ([italic]bottom pane tails the selected service's log[/italic])[/bold cyan]
  [yellow]k[/yellow]    kill the selected service process
  [yellow]b[/yellow]    boot the selected service (re-registers with Restate)

[bold cyan]What's running[/bold cyan]
  The TUI owns restate-server, the twelve service hypercorns, and
  the sim fleet. Tables above are live (1s poll).
"""


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
        """Render the most-recent log lines for the given service."""
        header = (
            f"[bold cyan]{svc.name}[/bold cyan]"
            f"   port=[yellow]{svc.port}[/yellow]"
            f"   status=[white]{svc.status}[/white]"
            f"   pid=[white]{svc.pid or '—'}[/white]"
            f"   [dim](? for help)[/dim]"
        )
        lines = list(svc.log)[-height_hint:]
        if not lines:
            body_text = "[dim]no log output yet[/dim]"
        else:
            from rich.markup import escape
            body_text = "\n".join(escape(line) for line in lines)
        self.update(f"{header}\n\n{body_text}")


# ───── app ───────────────────────────────────────────────────────────


class RidecoApp(App):
    CSS = """
    Screen { layout: vertical; }
    #regions { height: 9; }
    #services { height: 15; }
    #bottom { padding: 1 2; border: round $accent; }
    """

    BINDINGS = [
        Binding("q", "quit_clean", "Quit"),
        Binding("?", "help", "Help"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("s", "spike_region", "Spike"),
        Binding("a", "approve_region", "Approve"),
        Binding("k", "kill_service", "Kill svc"),
        Binding("b", "boot_service", "Boot svc"),
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
        # The ServicesTable auto-emits a RowHighlighted for row 0 on mount.
        # We swallow that so the bottom pane stays on the help screen until
        # the user actually arrows into a service.
        self._initial_highlight_consumed = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RegionsTable(id="regions")
        yield ServicesTable(id="services")
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

    async def _refresh_services(self) -> None:
        try:
            table = self.query_one(ServicesTable)
        except Exception:
            return
        for svc in self.pm.services.values():
            table.apply(svc)
        # If the bottom is in log-tail mode, repaint the selected service's
        # buffer so new lines stream in at the same cadence.
        if self._mode == MODE_LOG and self._selected_service:
            svc = self.pm.services.get(self._selected_service)
            if svc is not None:
                try:
                    self.query_one(BottomPane).show_log(svc)
                except Exception:
                    pass

    # ───── events ────────────────────────────────────────────────────

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Track which row in which table the user is on.

        Services table: also flip the bottom pane to that service's live
        log tail. Regions table: just remember the selection so s/a
        target it (no bottom-pane change)."""
        if event.row_key is None or event.row_key.value is None:
            return

        if isinstance(event.control, RegionsTable):
            self._selected_region = event.row_key.value
            return

        if isinstance(event.control, ServicesTable):
            if not self._initial_highlight_consumed:
                self._initial_highlight_consumed = True
                return
            name = event.row_key.value
            svc = self.pm.services.get(name)
            if svc is None:
                return
            self._selected_service = name
            self._mode = MODE_LOG
            try:
                self.query_one(BottomPane).show_log(svc)
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
        self.notify(f"spiking {region} (25s sustained)...")
        asyncio.create_task(self._spike(region))

    async def _spike(self, region: str) -> None:
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

    async def action_kill_service(self) -> None:
        name = self._selected_service
        if not name:
            self.notify("select a service in the Services table first",
                        severity="warning")
            return
        self.notify(f"killing {name}...")
        await self.pm.stop_service(name)
        self.notify(f"{name} stopped")

    async def action_boot_service(self) -> None:
        name = self._selected_service
        if not name:
            self.notify("select a service in the Services table first",
                        severity="warning")
            return
        self.notify(f"booting {name}...")
        await self.pm.start_service(name)
        # Give the hypercorn a moment to bind, then re-register with restate.
        await asyncio.sleep(2.0)
        try:
            await self.pm.register_one(name)
            self.notify(f"{name} up + registered")
        except Exception as e:
            self.notify(f"register {name} failed: {e}", severity="error")


def main() -> None:
    RidecoApp().run()


if __name__ == "__main__":
    main()

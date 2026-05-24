"""RideCo TUI — single-window playground.

Phase 3a: read-only dashboard. Live per-region table polled from the
Restate ingress, plus a help pane at the bottom. Subprocess control,
spike/approve commands, log streaming, and demo mode land in later
passes.

Run: ./scripts/tui.sh   (or python -m rideco.tui.app)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static

from rideco.shared.regions import all_regions


INGRESS = "http://localhost:8080"
RISK_THRESHOLD = 0.6


# ───── data fetching ─────────────────────────────────────────────────


async def _fetch(client: httpx.AsyncClient, service: str, key: str) -> dict:
    """One inspector call. Returns {} on any failure — the TUI renders
    dashes for missing data rather than crashing."""
    try:
        r = await client.post(
            f"{INGRESS}/{service}/{key}/get", json={}, timeout=2.0,
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


def _build_cells(snap: dict) -> RegionCells:
    a = snap.get("agent") or {}
    d = snap.get("dispatch") or {}
    halts = a.get("halts", 0)
    pending = d.get("pending_trips_count", 0)
    idle = d.get("active_driver_count", 0)
    in_flight = d.get("in_flight", 0)
    done = d.get("total_completed", 0)
    awk = a.get("pending_awakeable") or ""
    if len(awk) > 38:
        awk = awk[:35] + "..."
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
    """Live per-region status, refreshed by the app's set_interval."""

    COLS = [
        ("region", "Region"),
        ("active", "Active?"),
        ("halts", "Halts"),
        ("risk", "Risk"),
        ("epoch", "Epoch"),
        ("idle", "Idle"),
        ("pending", "Pending"),
        ("in_flight", "In-flight"),
        ("done", "Done"),
        ("awakeable", "Awakeable"),
    ]

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.show_header = True
        for key, label in self.COLS:
            self.add_column(label, key=key)
        for region in all_regions():
            self.add_row(region, "", "", "", "", "", "", "", "", "", key=region)

    def apply_snapshot(self, region: str, snap: dict) -> None:
        cells = _build_cells(snap)
        self.update_cell(region, "active", cells.active)
        self.update_cell(region, "halts", cells.halts)
        self.update_cell(region, "risk", cells.risk)
        self.update_cell(region, "epoch", cells.epoch)
        self.update_cell(region, "idle", cells.idle)
        self.update_cell(region, "pending", cells.pending)
        self.update_cell(region, "in_flight", cells.in_flight)
        self.update_cell(region, "done", cells.done)
        self.update_cell(region, "awakeable", cells.awakeable)


HELP_TEXT = """[bold]RideCo TUI[/bold]   [dim](phase 3a — read-only dashboard)[/dim]

[bold cyan]Keys[/bold cyan]
  [yellow]q[/yellow]    quit
  [yellow]?[/yellow]    toggle help (this view)
  [yellow]r[/yellow]    refresh now (forces a poll cycle)

[bold cyan]What you're looking at[/bold cyan]
  The top table is live state for all four regions — the same data
  [italic]watch-regions.sh[/italic] shows, just inside the TUI. Polled once
  per second from the Restate ingress.

[bold cyan]Still to come[/bold cyan]
  • per-service status pane + log tail
  • subprocess control (start / stop / restart individual services)
  • command palette: spike, approve, set rider rate, pause sims
  • guided demo mode with scripted narrative

[dim]Until then, drive the system from another terminal — the regions table will reflect it live.[/dim]
"""


class BottomPane(Static):
    """Multi-mode bottom area. Phase 3a: always renders help text."""

    def on_mount(self) -> None:
        self.update(HELP_TEXT)


# ───── app ───────────────────────────────────────────────────────────


class RidecoApp(App):
    CSS = """
    Screen { layout: vertical; }
    #regions { height: 11; }
    #bottom { padding: 1 2; border: round $accent; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
        Binding("r", "refresh_now", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RegionsTable(id="regions")
        yield BottomPane(id="bottom")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "RideCo"
        self.sub_title = "live · q quit · ? help · r refresh"
        self._client = httpx.AsyncClient()
        # First refresh immediately, then on a 1s cadence.
        await self._refresh_regions()
        self.set_interval(1.0, self._refresh_regions)

    async def on_unmount(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()

    async def _refresh_regions(self) -> None:
        regions = all_regions()
        snaps = await asyncio.gather(
            *(fetch_region_snapshot(self._client, r) for r in regions)
        )
        table = self.query_one(RegionsTable)
        for region, snap in zip(regions, snaps):
            table.apply_snapshot(region, snap)

    def action_help(self) -> None:
        self.query_one(BottomPane).update(HELP_TEXT)

    async def action_refresh_now(self) -> None:
        await self._refresh_regions()


def main() -> None:
    RidecoApp().run()


if __name__ == "__main__":
    main()

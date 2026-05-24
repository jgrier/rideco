"""Live per-region dashboard.

Polls the RegionSafetyAgent and Dispatch VOs for every region and renders
a single table that updates in place. Same data as `show-regions.sh`,
just refreshed continuously.

Run: ./scripts/watch-regions.sh   (or python -m rideco.sim.watch_regions)
"""

import argparse
import asyncio
from datetime import datetime

import httpx
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from rideco.shared.regions import all_regions


INGRESS = "http://localhost:8080"
RISK_THRESHOLD = 0.6


async def _fetch(client: httpx.AsyncClient, service: str, key: str) -> dict:
    try:
        r = await client.post(f"{INGRESS}/{service}/{key}/get", json={}, timeout=2.0)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


async def _snapshot(client: httpx.AsyncClient, regions: list[str]) -> dict[str, dict]:
    calls = []
    for r in regions:
        calls.append(_fetch(client, "RegionSafetyAgent", r))
        calls.append(_fetch(client, "Dispatch", r))
    results = await asyncio.gather(*calls)
    out: dict[str, dict] = {}
    for i, r in enumerate(regions):
        out[r] = {"agent": results[i * 2], "dispatch": results[i * 2 + 1]}
    return out


def _risk_cell(score) -> Text:
    if not isinstance(score, (int, float)):
        return Text("—", style="dim")
    s = f"{score:.2f}"
    if score >= RISK_THRESHOLD:
        return Text(s, style="bold red")
    if score >= RISK_THRESHOLD * 0.66:
        return Text(s, style="yellow")
    return Text(s, style="green")


def _active_cell(region_active) -> Text:
    if region_active is False:
        return Text("HALTED", style="bold red")
    if region_active is True:
        return Text("active", style="green")
    return Text("—", style="dim")


def _awakeable_cell(value) -> Text:
    if not value:
        return Text("—", style="dim")
    s = str(value)
    if len(s) > 38:
        s = s[:35] + "..."
    return Text(s, style="cyan")


def _render(snapshot: dict[str, dict], regions: list[str]) -> Table:
    now = datetime.now().strftime("%H:%M:%S")
    table = Table(
        title=f"RideCo regions  ·  {now}",
        title_style="bold",
        caption="updates every second  ·  Ctrl+C to exit",
        caption_style="dim",
        expand=False,
    )
    table.add_column("Region", style="bold", no_wrap=True)
    table.add_column("Active?", no_wrap=True)
    table.add_column("Halts", justify="right")
    table.add_column("Risk", justify="right")
    table.add_column("Epoch", justify="right")
    table.add_column("Idle", justify="right")
    table.add_column("Pending", justify="right")
    table.add_column("In-flight", justify="right")
    table.add_column("Done", justify="right")
    table.add_column("Awakeable", no_wrap=True)

    totals = {"idle": 0, "pending": 0, "in_flight": 0, "done": 0, "halts": 0}

    for region in regions:
        a = snapshot[region].get("agent") or {}
        d = snapshot[region].get("dispatch") or {}
        if not a and not d:
            table.add_row(region, Text("offline", style="red"), *(["—"] * 8))
            continue
        halts = a.get("halts", 0)
        pending = d.get("pending_trips_count", 0)
        idle = d.get("active_driver_count", 0)
        in_flight = d.get("in_flight", 0)
        done = d.get("total_completed", 0)

        totals["idle"] += idle
        totals["pending"] += pending
        totals["in_flight"] += in_flight
        totals["done"] += done
        totals["halts"] += halts

        pending_text = Text(str(pending), style="yellow") if pending > 0 else Text("0", style="dim")
        idle_text = Text(str(idle), style="red") if idle == 0 else Text(str(idle))
        done_text = Text(str(done), style="green") if done > 0 else Text("0", style="dim")

        table.add_row(
            region,
            _active_cell(a.get("region_active")),
            Text(str(halts), style="red bold" if halts else "dim"),
            _risk_cell(a.get("last_score")),
            str(d.get("epoch_id", 0)),
            idle_text,
            pending_text,
            Text(str(in_flight), style="cyan") if in_flight else Text("0", style="dim"),
            done_text,
            _awakeable_cell(a.get("pending_awakeable")),
        )

    table.add_section()
    table.add_row(
        Text("TOTAL", style="bold"),
        "",
        Text(str(totals["halts"]), style="red bold" if totals["halts"] else "dim"),
        "",
        "",
        Text(str(totals["idle"])),
        Text(str(totals["pending"]), style="yellow") if totals["pending"] else Text("0", style="dim"),
        Text(str(totals["in_flight"]), style="cyan") if totals["in_flight"] else Text("0", style="dim"),
        Text(str(totals["done"]), style="bold green") if totals["done"] else Text("0", style="dim"),
        "",
    )
    return table


async def _amain(regions: list[str], interval: float) -> None:
    console = Console()
    async with httpx.AsyncClient() as client:
        with Live(_render({r: {} for r in regions}, regions), console=console,
                  refresh_per_second=4, screen=False) as live:
            while True:
                snap = await _snapshot(client, regions)
                live.update(_render(snap, regions))
                await asyncio.sleep(interval)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--regions", default=",".join(all_regions()))
    p.add_argument("--interval", type=float, default=1.0,
                   help="poll interval in seconds")
    args = p.parse_args()
    try:
        asyncio.run(_amain(args.regions.split(","), args.interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

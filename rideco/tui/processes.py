"""Process lifecycle management for the TUI.

Owns:
- The Restate container (via `docker compose up/down`)
- 12 hypercorn subprocesses (one per service)
- Deployment registration with the `restate` CLI
- Per-service log capture into bounded ring buffers

Everything is async so the TUI stays responsive while boot/teardown is
in flight.
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx


# (service_module, port) in launch order.
SERVICES: list[tuple[str, int]] = [
    ("trip", 9080),
    ("offers", 9081),
    ("eta", 9082),
    ("pricing", 9083),
    ("locations", 9084),
    ("features", 9085),
    ("dispatch", 9086),
    ("region_safety_agent", 9087),
    ("sim_rider", 9088),
    ("sim_driver", 9089),
    ("sim_mapping", 9090),
    ("sim_control", 9091),
]


# Process lifecycle states.
STATUS_STOPPED = "stopped"
STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_DEAD = "dead"


@dataclass
class ServiceProc:
    name: str
    port: int
    process: Optional[asyncio.subprocess.Process] = None
    status: str = STATUS_STOPPED
    log: deque = field(default_factory=lambda: deque(maxlen=400))
    _reader_task: Optional[asyncio.Task] = None

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid if self.process else None

    @property
    def last_log(self) -> str:
        return self.log[-1] if self.log else ""


class ProcessManager:
    """Spawns and supervises the per-service hypercorn fleet."""

    def __init__(self, repo_dir: Path, python: Optional[str] = None) -> None:
        self.repo_dir = Path(repo_dir)
        self.python = python or str(self.repo_dir / ".venv" / "bin" / "python")
        self.services: dict[str, ServiceProc] = {
            name: ServiceProc(name=name, port=port) for name, port in SERVICES
        }

    # ───── restate container ─────────────────────────────────────────

    async def restate_up(self) -> None:
        await self._run_cmd("docker", "compose", "up", "-d")
        await self._wait_for_restate_ready()

    async def restate_down(self) -> None:
        await self._run_cmd("docker", "compose", "down")

    async def _wait_for_restate_ready(self, timeout_s: float = 30.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout_s
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await client.get("http://localhost:9070/health", timeout=1.0)
                    if r.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.5)
        raise RuntimeError("restate-server admin (:9070) did not come up")

    # ───── services ──────────────────────────────────────────────────

    async def start_service(self, name: str) -> None:
        svc = self.services[name]
        if svc.status in (STATUS_STARTING, STATUS_RUNNING):
            return
        svc.status = STATUS_STARTING
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            proc = await asyncio.create_subprocess_exec(
                self.python, "-m", "hypercorn",
                f"rideco.services.{name}:app",
                "--bind", f"0.0.0.0:{svc.port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.repo_dir),
                env=env,
            )
        except Exception as e:
            svc.status = STATUS_DEAD
            svc.log.append(f"[spawn error: {e}]")
            return
        svc.process = proc
        svc.status = STATUS_RUNNING
        svc._reader_task = asyncio.create_task(self._reader(svc))

    async def _reader(self, svc: ServiceProc) -> None:
        assert svc.process and svc.process.stdout
        try:
            async for raw in svc.process.stdout:
                svc.log.append(raw.decode(errors="replace").rstrip())
        except Exception as e:
            svc.log.append(f"[reader error: {e}]")
        rc = await svc.process.wait()
        if svc.status != STATUS_STOPPED:
            svc.status = STATUS_DEAD
            svc.log.append(f"[exit {rc}]")

    async def stop_service(self, name: str) -> None:
        svc = self.services[name]
        proc = svc.process
        svc.status = STATUS_STOPPED  # set first so reader doesn't flip to dead
        if proc is None or proc.returncode is not None:
            svc.process = None
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        svc.process = None

    async def restart_service(self, name: str) -> None:
        await self.stop_service(name)
        await self.start_service(name)

    async def start_all(self) -> None:
        await asyncio.gather(*(self.start_service(n) for n, _ in SERVICES))

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop_service(n) for n in self.services))

    # ───── registration + sim bootstrap ──────────────────────────────

    async def register_all(self) -> None:
        for _, port in SERVICES:
            await self._run_cmd(
                "restate", "-y", "deployments", "register", "--force",
                f"http://host.docker.internal:{port}",
            )

    async def register_one(self, name: str) -> None:
        port = self.services[name].port
        await self._run_cmd(
            "restate", "-y", "deployments", "register", "--force",
            f"http://host.docker.internal:{port}",
        )

    async def sim_start_all(self, **kwargs) -> None:
        async with httpx.AsyncClient() as c:
            await c.post(
                "http://localhost:8080/SimControl/global/start_all",
                json=kwargs, timeout=10.0,
            )

    async def sim_stop_all(self) -> None:
        async with httpx.AsyncClient() as c:
            try:
                await c.post(
                    "http://localhost:8080/SimControl/global/stop_all",
                    json={}, timeout=5.0,
                )
            except Exception:
                pass  # best-effort

    # ───── helpers ───────────────────────────────────────────────────

    async def _run_cmd(self, *cmd: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.repo_dir),
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace")

    # ───── high-level orchestration ──────────────────────────────────

    async def full_boot(self, progress: Optional[Callable[[str], None]] = None) -> None:
        """Bring everything up in order. `progress` gets a status string
        at each phase so the TUI can render boot progress."""
        def _p(msg: str) -> None:
            if progress is not None:
                progress(msg)

        _p("starting restate-server (docker compose up)...")
        await self.restate_up()
        _p("starting 12 services...")
        await self.start_all()
        # Give hypercorns a beat to bind their ports.
        await asyncio.sleep(2.5)
        _p("registering deployments with restate...")
        await self.register_all()
        _p("starting sim fleet (SimControl.start_all)...")
        await self.sim_start_all()
        _p("ready.")

    async def full_teardown(self, progress: Optional[Callable[[str], None]] = None,
                            stop_restate: bool = True) -> None:
        def _p(msg: str) -> None:
            if progress is not None:
                progress(msg)

        _p("pausing sims...")
        await self.sim_stop_all()
        _p("stopping services...")
        await self.stop_all()
        if stop_restate:
            _p("stopping restate-server...")
            await self.restate_down()
        _p("done.")

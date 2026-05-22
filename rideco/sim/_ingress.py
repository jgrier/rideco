"""Thin wrapper around the Restate ingress at :8080.

Restate's ingress URL pattern (request/response):
  POST {ingress}/{ServiceName}/{handler}                 — services
  POST {ingress}/{ObjectName}/{key}/{handler}            — virtual objects
  POST {ingress}/{ServiceName}/{handler}/send            — one-way services
  POST {ingress}/{ObjectName}/{key}/{handler}/send       — one-way virtual objects

All bodies and responses are JSON.
"""

import os
from typing import Any

import httpx


INGRESS = os.environ.get("RIDECO_INGRESS", "http://localhost:8080")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30.0)


async def call_service(name: str, handler: str, body: Any) -> Any:
    async with _client() as c:
        r = await c.post(f"{INGRESS}/{name}/{handler}", json=body)
        r.raise_for_status()
        return r.json() if r.text else {}


async def call_object(name: str, key: str, handler: str, body: Any) -> Any:
    async with _client() as c:
        r = await c.post(f"{INGRESS}/{name}/{key}/{handler}", json=body)
        r.raise_for_status()
        return r.json() if r.text else {}


async def send_object(name: str, key: str, handler: str, body: Any) -> None:
    async with _client() as c:
        r = await c.post(f"{INGRESS}/{name}/{key}/{handler}/send", json=body)
        r.raise_for_status()

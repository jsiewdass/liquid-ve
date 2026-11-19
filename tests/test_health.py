"""Liveness endpoint test."""

from __future__ import annotations

import httpx


async def test_liveness_returns_ok(client: httpx.AsyncClient) -> None:
    """The liveness probe is cheap and always healthy."""
    response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers.get("X-Request-ID")


async def test_root_metadata(client: httpx.AsyncClient) -> None:
    """The root endpoint exposes service metadata."""
    response = await client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "local"

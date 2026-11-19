"""Health and readiness endpoints.

Policy: the database is the only critical dependency.  An unhealthy external
provider degrades the response (HTTP 200 with ``status="degraded"``) so the
orchestrator does not restart a process that can still serve traffic.  A
failed database returns HTTP 503.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request, Response

from app import __version__
from app.core.config import Settings
from app.domain.schemas import HealthComponent, HealthResponse
from app.infrastructure.database import check_database_health

router = APIRouter(prefix="/health", tags=["health"])

_CRITICAL_COMPONENTS = frozenset({"database"})
_DEFAULT_TIMEOUT_SECONDS = 5.0


async def _always_false() -> bool:
    """Return ``False`` for clients that were never initialized.

    Returns:
        ``False``.
    """
    return False


async def _probe(
    name: str,
    probe: Callable[[], Awaitable[bool]],
    timeout: float,
    missing_detail: str | None = None,
) -> HealthComponent:
    """Execute one probe with a hard timeout, never raising.

    Args:
        name: Component name.
        probe: Zero-argument callable returning an awaitable boolean.
        timeout: Maximum seconds to wait.
        missing_detail: Optional detail used when the client is absent.

    Returns:
        The component report.
    """
    start = time.perf_counter()
    if missing_detail is not None:
        return HealthComponent(
            name=name,
            healthy=False,
            latency_ms=0.0,
            detail=missing_detail,
        )
    try:
        healthy = bool(await asyncio.wait_for(probe(), timeout=timeout))
        detail = None if healthy else "unhealthy"
    except Exception as exc:  # noqa: BLE001 - probes must never raise
        healthy = False
        detail = type(exc).__name__
    latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
    return HealthComponent(name=name, healthy=healthy, latency_ms=latency_ms, detail=detail)


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Return a cheap liveness payload.

    Returns:
        A constant payload that never touches the database or providers.
    """
    return {"status": "ok", "version": __version__}


@router.get("/ready", response_model=HealthResponse)
async def readiness(request: Request, response: Response) -> HealthResponse:
    """Report dependency readiness.

    Args:
        request: Current request, used to read clients from ``app.state``.
        response: Response object used to set the status code.

    Returns:
        The aggregated health report.
    """
    app_settings: Settings = getattr(request.app.state, "settings", None)
    timeout = _DEFAULT_TIMEOUT_SECONDS
    binance = getattr(request.app.state, "binance", None)
    cachicamo = getattr(request.app.state, "cachicamo", None)
    sms = getattr(request.app.state, "sms", None)

    components = await asyncio.gather(
        _probe("database", check_database_health, timeout),
        _probe(
            "binance_c2c",
            binance.health_check if binance is not None else _always_false,
            timeout,
            missing_detail=None if binance is not None else "client not initialized",
        ),
        _probe(
            "cachicamo",
            cachicamo.health_check if cachicamo is not None else _always_false,
            timeout,
            missing_detail=None if cachicamo is not None else "client not initialized",
        ),
        _probe(
            "sms_gateway",
            sms.health_check if sms is not None else _always_false,
            timeout,
            missing_detail=None if sms is not None else "client not initialized",
        ),
        return_exceptions=True,
    )

    normalized: list[HealthComponent] = []
    for index, component in enumerate(components):
        if isinstance(component, HealthComponent):
            normalized.append(component)
        else:
            normalized.append(
                HealthComponent(
                    name=["database", "binance_c2c", "cachicamo", "sms_gateway"][index],
                    healthy=False,
                    latency_ms=0.0,
                    detail=type(component).__name__,
                )
            )

    critical_failed = any(
        component.name in _CRITICAL_COMPONENTS and not component.healthy
        for component in normalized
    )
    degraded = any(not component.healthy for component in normalized)
    if critical_failed:
        response.status_code = 503
    status: Any = "degraded" if (critical_failed or degraded) else "ok"
    return HealthResponse(
        status=status,
        version=__version__,
        environment=getattr(app_settings, "app_env", "unknown"),
        components=normalized,
    )


__all__ = ["router"]

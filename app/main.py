"""FastAPI application factory and process entrypoint."""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import ValidationError
from sqlalchemy import select

from app import __version__
from app.api.v1 import health, orders, webhooks
from app.core.config import Settings, get_settings
from app.core.exceptions import DuplicateOrderError, LiquidVEException
from app.core.logging import configure_logging, new_request_id, request_id_var
from app.domain.enums import OrderType
from app.infrastructure.clients.binance_browser import PlaywrightBinanceBrowser
from app.infrastructure.clients.binance_client import BinanceC2CClient
from app.infrastructure.clients.cachicamo_client import CachicamoClient
from app.infrastructure.clients.sms_gateway import SmsGatewayClient
from app.infrastructure.database import (
    create_all,
    dispose_engine,
    init_engine,
    session_scope,
)
from app.infrastructure.models import P2POrder
from app.services.buy_flow import BuyFlow

_PENDING_PAYMENT_STATUS = "PENDING_PAYMENT"


async def _run_sms_poller(app: FastAPI) -> None:
    """Poll the Android gateway inbox until cancelled.

    Every failure is logged and the loop continues; only cancellation stops
    it.  Nothing is left running after shutdown because the lifespan task is
    awaited.

    Args:
        app: Application holding settings and clients on ``state``.
    """
    app_settings: Settings = app.state.settings
    while True:
        await asyncio.sleep(app_settings.sms_gateway_poll_interval_seconds)
        try:
            sms_client = getattr(app.state, "sms", None)
            if sms_client is None:
                continue
            async with session_scope() as session:
                flow = BuyFlow(
                    session,
                    settings=app_settings,
                    binance=getattr(app.state, "binance", None),
                    cachicamo=getattr(app.state, "cachicamo", None),
                    sms=sms_client,
                    browser=getattr(app.state, "browser", None),
                )
                messages = await sms_client.fetch_inbox()
                for message in messages:
                    await flow.handle_incoming_sms(message)
        except asyncio.CancelledError:
            logger.info("SMS poller stopped")
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            logger.warning("SMS poller iteration failed: {}", type(exc).__name__)


async def _buy_worker_tick(app: FastAPI) -> None:
    """Run one BUY detection and timeout-sweep pass.

    The tick performs exactly one filtered history read, ignores anything that
    is not a ``BUY`` in ``PENDING_PAYMENT``, skips orders whose number is
    already persisted (the B1 idempotency anchor) and delegates the pipeline to
    :class:`BuyFlow`.  It never sleeps; the loop owns the interval.

    Args:
        app: Application holding settings and clients on ``state``.
    """
    app_settings: Settings = app.state.settings
    binance = getattr(app.state, "binance", None)
    if binance is None:
        return
    history = await binance.get_order_history(
        trade_type=OrderType.BUY,
        order_status=_PENDING_PAYMENT_STATUS,
    )
    async with session_scope() as session:
        flow = BuyFlow(
            session,
            settings=app_settings,
            binance=binance,
            cachicamo=getattr(app.state, "cachicamo", None),
            sms=getattr(app.state, "sms", None),
            browser=getattr(app.state, "browser", None),
        )
        for candidate in history:
            if candidate.trade_type is not OrderType.BUY:
                continue
            if candidate.status != _PENDING_PAYMENT_STATUS:
                continue
            known = await session.execute(
                select(P2POrder.id).where(
                    P2POrder.order_number == candidate.order_number
                )
            )
            if known.scalar_one_or_none() is not None:
                continue
            try:
                await flow.execute(candidate.order_number)
            except DuplicateOrderError:
                continue
            except LiquidVEException as exc:
                logger.warning(
                    "Buy worker skipped order {}: {}",
                    candidate.order_number,
                    type(exc).__name__,
                )
        await flow.sweep_timeouts()


async def _run_buy_worker(app: FastAPI) -> None:
    """Run the BUY detection loop until cancelled.

    Args:
        app: Application holding settings and clients on ``state``.
    """
    app_settings: Settings = app.state.settings
    while True:
        await asyncio.sleep(app_settings.buy_worker_poll_interval_seconds)
        try:
            await _buy_worker_tick(app)
        except asyncio.CancelledError:
            logger.info("Buy worker stopped")
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            logger.warning(
                "Buy worker iteration failed: {}", type(exc).__name__
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown resources.

    Args:
        app: Application being started.

    Yields:
        Control to the ASGI server while the application runs.
    """
    app_settings: Settings = app.state.settings
    configure_logging(app_settings)
    logger.info("Starting {} in {} mode", app_settings.app_name, app_settings.app_env)
    init_engine(app_settings)
    stack = AsyncExitStack()
    poller_task: asyncio.Task[None] | None = None
    worker_task: asyncio.Task[None] | None = None
    browser: PlaywrightBinanceBrowser | None = None
    try:
        if not app_settings.is_production:
            await create_all()
        app.state.binance = await stack.enter_async_context(
            BinanceC2CClient(app_settings)
        )
        app.state.cachicamo = await stack.enter_async_context(
            CachicamoClient(app_settings)
        )
        app.state.sms = await stack.enter_async_context(SmsGatewayClient(app_settings))
        if app_settings.binance_browser_enabled:
            browser = PlaywrightBinanceBrowser(app_settings)
        app.state.browser = browser
        if app_settings.sms_poller_enabled:
            poller_task = asyncio.create_task(_run_sms_poller(app), name="sms-poller")
        if app_settings.buy_worker_enabled:
            worker_task = asyncio.create_task(_run_buy_worker(app), name="buy-worker")
        yield
    finally:
        for task in (poller_task, worker_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if browser is not None:
            await browser.aclose()
        await stack.aclose()
        await dispose_engine()
        logger.info("Shutdown complete")


def _register_exception_handlers(application: FastAPI) -> None:
    """Register global exception handlers.

    Args:
        application: Application receiving the handlers.
    """

    @application.exception_handler(LiquidVEException)
    async def _handle_liquidve(_request: Request, exc: LiquidVEException) -> JSONResponse:
        """Render typed application errors as the canonical envelope."""
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @application.exception_handler(RequestValidationError)
    async def _handle_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Render validation failures as the canonical envelope."""
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder(
                {
                    "error": {
                        "code": "validation_error",
                        "message": "Request validation failed",
                        "details": {"errors": exc.errors()},
                    }
                }
            ),
        )

    @application.exception_handler(ValidationError)
    async def _handle_pydantic_validation(
        _request: Request, exc: ValidationError
    ) -> JSONResponse:
        """Render raw pydantic validation failures (for example webhook bodies)."""
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder(
                {
                    "error": {
                        "code": "validation_error",
                        "message": "Request body validation failed",
                        "details": {"errors": exc.errors(include_url=False)},
                    }
                }
            ),
        )

    @application.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        """Log unexpected failures and return a generic message."""
        logger.exception("Unhandled exception: {}", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred",
                    "details": {},
                }
            },
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: Optional settings override for tests.

    Returns:
        A fully wired application instance.
    """
    app_settings = settings or get_settings()
    application = FastAPI(
        title=app_settings.app_name,
        version=__version__,
        debug=app_settings.debug,
        lifespan=lifespan,
    )
    application.state.settings = app_settings

    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=app_settings.allowed_hosts,
    )

    @application.middleware("http")
    async def _request_id_middleware(request: Request, call_next: Any) -> Any:
        """Propagate or create a request identifier for logs and responses."""
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response

    _register_exception_handlers(application)
    application.include_router(health.router, prefix=app_settings.api_v1_prefix)
    application.include_router(webhooks.router, prefix=app_settings.api_v1_prefix)
    application.include_router(orders.router, prefix=app_settings.api_v1_prefix)

    @application.get("/", tags=["root"])
    async def root() -> dict[str, str]:
        """Return basic service metadata."""
        return {
            "name": app_settings.app_name,
            "version": __version__,
            "environment": app_settings.app_env,
            "status": "ok",
        }

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
    )

"""Shared pytest fixtures.

No test requires network access.  Environment variables are populated before
any application import so the cached module-level settings can be built even
when no ``.env`` file exists.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-api-key-0123456789abcdef")
os.environ.setdefault("BINANCE_API_KEY", "test-binance-api-key")
os.environ.setdefault("BINANCE_API_SECRET", "test-binance-api-secret")
os.environ.setdefault("CACHICAMO_BASE_URL", "http://localhost:9001")
os.environ.setdefault("CACHICAMO_API_KEY", "test-cachicamo-api-key")
os.environ.setdefault(
    "CACHICAMO_WEBHOOK_SECRET",
    "test-cachicamo-webhook-secret-0123456789abcdef",
)
os.environ.setdefault("SMS_GATEWAY_BASE_URL", "http://100.64.0.10:8080")
os.environ.setdefault("SMS_GATEWAY_TOKEN", "test-sms-gateway-token")
os.environ.setdefault("MAX_TX_AMOUNT_VES", "5000.00")
os.environ.setdefault("DAILY_LIMIT_VES", "20000.00")
os.environ.setdefault("MAX_USDT_PER_ORDER", "1000.00000000")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.infrastructure.database import create_all, dispose_engine, init_engine  # noqa: E402
from app.infrastructure.models import Base  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def test_settings(tmp_path: Path) -> Settings:
    """Build deterministic settings for the test suite.

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
        A fully populated settings object.
    """
    return Settings(
        app_name="liquidVE-test",
        app_env="local",
        debug=True,
        secret_key="test-secret-key-0123456789abcdef0123456789abcdef",
        internal_api_key="test-internal-api-key-0123456789abcdef",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        binance_api_key="test-binance-api-key",
        binance_api_secret="test-binance-api-secret",
        cachicamo_base_url="http://localhost:9001",
        cachicamo_api_key="test-cachicamo-api-key",
        cachicamo_webhook_secret="test-cachicamo-webhook-secret-0123456789abcdef",
        sms_gateway_base_url="http://100.64.0.10:8080",
        sms_gateway_token="test-sms-gateway-token",
        max_tx_amount_ves=Decimal("5000.00"),
        daily_limit_ves=Decimal("20000.00"),
        max_usdt_per_order=Decimal("1000.00000000"),
        log_json=False,
    )


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    """Create an in-memory engine with the schema applied.

    Yields:
        An engine bound to a single shared in-memory connection.
    """
    test_engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest.fixture()
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Provide a session bound to the in-memory test engine.

    Yields:
        An async session with expire-on-commit disabled.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as test_session:
        yield test_session


@pytest.fixture()
async def client(test_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Provide an HTTP client bound to the ASGI application.

    Note:
        ``respx`` can intercept adapter traffic in other tests; this fixture
        never performs real network calls.

    Yields:
        A client talking to the in-process application.
    """
    init_engine(test_settings)
    await create_all()
    application = create_app(test_settings)
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        await dispose_engine()


def pytest_configure(config: Any) -> None:
    """Register the asyncio marker for collections that need it.

    Args:
        config: Pytest configuration object.
    """
    config.addinivalue_line("markers", "integration: marks integration-style tests")

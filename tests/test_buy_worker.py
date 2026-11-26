"""S7 buy worker, timeout sweep and lifecycle tests.

Everything here is offline: the Binance history call is intercepted with
``respx``, the gateway and browser are scriptable doubles, and the worker is
driven with a single manual tick.  No test sleeps, opens a socket or imports
Playwright.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.main as main_module
from app.core.config import Settings
from app.core.exceptions import ExternalServiceError, UnauthorizedError
from app.domain.enums import AuditEvent, OrderStatus, OrderType
from app.domain.ports import PaymentInstruction
from app.infrastructure.clients.binance_client import (
    BinanceC2CClient,
    BinanceC2COrder,
)
from app.infrastructure.database import dispose_engine
from app.infrastructure.models import AuditLog, DailyVolumeLedger, P2POrder
from app.main import create_app

_HISTORY_REGEX = r".*/sapi/v1/c2c/orderMatch/listUserOrderHistory\?.*"
_TIMEOUT_REASON = "sms_confirmation_timeout"


def _buy_detail(
    order_number: str,
    *,
    status: str = "PENDING_PAYMENT",
    total_price: str = "1500.00",
) -> BinanceC2COrder:
    """Build a deterministic BUY order payload for the fake client."""
    return BinanceC2COrder(
        order_number=order_number,
        trade_type=OrderType.BUY,
        asset="USDT",
        fiat="VES",
        price=Decimal("150.00000000"),
        amount=Decimal("10.00000000"),
        total_price=Decimal(total_price),
        status=status,
        counterparty_nickname="counterparty",
        created_at=datetime.now(timezone.utc),
    )


def _instruction(order_number: str) -> PaymentInstruction:
    """Build a complete BDV payment instruction for the fake browser."""
    return PaymentInstruction(
        order_number=order_number,
        bank_numeric_code="0102",
        phone="04141234567",
        id_number="V-12345678",
        amount_ves=Decimal("1500.00"),
    )


def _scope_for(engine: AsyncEngine):
    """Build a ``session_scope`` replacement bound to ``engine``."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def _scope() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return _scope


@pytest.fixture()
def worker_app(
    test_settings: Settings,
    fake_binance: object,
    fake_cachicamo: object,
    fake_sms: object,
    fake_browser: object,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
):
    """Build a lifespan-free app wired to the in-memory engine for one tick."""
    application = create_app(test_settings)
    application.state.binance = fake_binance
    application.state.cachicamo = fake_cachicamo
    application.state.sms = fake_sms
    application.state.browser = fake_browser
    monkeypatch.setattr(main_module, "session_scope", _scope_for(engine))
    return application


@respx.mock
async def test_history_sends_trade_type_and_status(
    test_settings: Settings,
) -> None:
    """The signed query carries the BUY and PENDING_PAYMENT filters."""
    route = respx.post(url__regex=_HISTORY_REGEX).mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    async with BinanceC2CClient(test_settings) as client:
        orders = await client.get_order_history(
            trade_type=OrderType.BUY,
            order_status="PENDING_PAYMENT",
        )

    assert orders == []
    assert len(route.calls) == 1
    params = route.calls[0].request.url.params
    assert params["tradeType"] == "BUY"
    assert params["orderStatus"] == "PENDING_PAYMENT"


@respx.mock
async def test_history_401_raises_unauthorized(test_settings: Settings) -> None:
    """A credential rejection surfaces as a typed unauthorized error."""
    respx.post(url__regex=_HISTORY_REGEX).mock(return_value=httpx.Response(401))

    async with BinanceC2CClient(test_settings) as client:
        with pytest.raises(UnauthorizedError):
            await client.get_order_history(trade_type=OrderType.BUY)


@respx.mock
async def test_history_500_is_retried_because_read_is_idempotent(
    test_settings: Settings,
) -> None:
    """The history read is retried once after a server error."""
    test_settings.http_backoff_base_seconds = 0.0
    test_settings.http_backoff_max_seconds = 0.0
    route = respx.post(url__regex=_HISTORY_REGEX).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"data": []}),
        ]
    )

    async with BinanceC2CClient(test_settings) as client:
        orders = await client.get_order_history(trade_type=OrderType.BUY)

    assert orders == []
    assert len(route.calls) == 2


async def test_tick_persists_pending_payment_buy_order_once(
    worker_app,
    fake_binance: object,
    fake_browser: object,
    session: AsyncSession,
) -> None:
    """One tick fetches once, creates the order once and runs the pipeline."""
    fake_binance.register(_buy_detail("ORD-WORKER-1"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-WORKER-1")  # type: ignore[attr-defined]

    await main_module._buy_worker_tick(worker_app)

    assert fake_binance.history_calls == [  # type: ignore[attr-defined]
        {"trade_type": OrderType.BUY, "order_status": "PENDING_PAYMENT"}
    ]
    result = await session.execute(
        select(P2POrder).where(P2POrder.order_number == "ORD-WORKER-1")
    )
    order = result.scalar_one()
    assert order.order_type is OrderType.BUY
    audits = await session.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.order_id == order.id,
            AuditLog.event == AuditEvent.ORDER_CREATED,
        )
    )
    assert audits.scalar_one() == 1

    await main_module._buy_worker_tick(worker_app)

    audits = await session.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.order_id == order.id,
            AuditLog.event == AuditEvent.ORDER_CREATED,
        )
    )
    assert audits.scalar_one() == 1


async def test_tick_ignores_sell_orders(
    worker_app,
    fake_binance: object,
    session: AsyncSession,
) -> None:
    """A SELL order in the history is never persisted."""
    sell = _buy_detail("ORD-WORKER-SELL").model_copy(
        update={"trade_type": OrderType.SELL}
    )
    fake_binance.register(sell)  # type: ignore[attr-defined]

    await main_module._buy_worker_tick(worker_app)

    count = await session.execute(
        select(func.count()).select_from(P2POrder)
    )
    assert count.scalar_one() == 0
    assert fake_binance.detail_calls == []  # type: ignore[attr-defined]


async def test_tick_skips_known_order_numbers(
    worker_app,
    fake_binance: object,
    seed_order,
) -> None:
    """A re-detected order number is skipped before any detail call."""
    await seed_order(
        order_number="ORD-KNOWN",
        order_type=OrderType.BUY,
        status=OrderStatus.PENDING,
    )
    fake_binance.register(_buy_detail("ORD-KNOWN"))  # type: ignore[attr-defined]

    await main_module._buy_worker_tick(worker_app)

    assert fake_binance.detail_calls == []  # type: ignore[attr-defined]


async def test_tick_with_gate_off_parks_manual_review(
    worker_app,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
    session: AsyncSession,
) -> None:
    """The auto-pay gate off parks the detected order with zero SMS calls."""
    fake_binance.register(_buy_detail("ORD-WORKER-PARK"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-WORKER-PARK")  # type: ignore[attr-defined]

    await main_module._buy_worker_tick(worker_app)

    result = await session.execute(
        select(P2POrder).where(P2POrder.order_number == "ORD-WORKER-PARK")
    )
    order = result.scalar_one()
    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.manual_review_reason == "awaiting_operator_approval"
    assert fake_sms.calls == []  # type: ignore[attr-defined]


async def test_tick_never_sleeps(
    worker_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual tick completes without ever awaiting ``asyncio.sleep``."""
    async def _boom(_delay: float) -> None:
        raise AssertionError("the tick must not sleep")

    monkeypatch.setattr(main_module.asyncio, "sleep", _boom)

    await main_module._buy_worker_tick(worker_app)


async def test_sweep_flags_stale_sending_sms_without_resend_or_release(
    worker_app,
    fake_sms: object,
    session: AsyncSession,
    seed_order,
) -> None:
    """A stale window gets exactly one flag; money state is untouched."""
    with freeze_time("2026-11-23 12:00:00"):
        order = await seed_order(
            order_number="ORD-SWEEP",
            order_type=OrderType.BUY,
            status=OrderStatus.SENDING_PAYMENT,
            sms_sent_at=datetime(2026, 11, 23, 11, 54, 59, tzinfo=timezone.utc),
        )
        session.add(
            DailyVolumeLedger(
                ledger_date=datetime(2026, 11, 23, tzinfo=timezone.utc).date(),
                currency="VES_PAY",
                total_volume=Decimal("1500.00"),
                transaction_count=1,
            )
        )
        await session.commit()

        await main_module._buy_worker_tick(worker_app)

        flags = await session.execute(
            select(AuditLog).where(
                AuditLog.order_id == order.id,
                AuditLog.event == AuditEvent.EXTERNAL_ERROR,
            )
        )
        entries = list(flags.scalars().all())
        assert len(entries) == 1
        assert entries[0].payload["reason"] == _TIMEOUT_REASON
        assert order.status is OrderStatus.SENDING_PAYMENT

        ledger = await session.execute(
            select(DailyVolumeLedger).where(
                DailyVolumeLedger.currency == "VES_PAY"
            )
        )
        assert ledger.scalar_one().total_volume == Decimal("1500.00")
        assert fake_sms.calls == []  # type: ignore[attr-defined]


async def test_sweep_does_not_duplicate_flag(
    worker_app,
    session: AsyncSession,
    seed_order,
) -> None:
    """A second sweep over the same window writes no second flag."""
    with freeze_time("2026-11-23 12:00:00"):
        order = await seed_order(
            order_number="ORD-SWEEP-TWICE",
            order_type=OrderType.BUY,
            status=OrderStatus.SENDING_PAYMENT,
            sms_sent_at=datetime(2026, 11, 23, 11, 54, 59, tzinfo=timezone.utc),
        )

        await main_module._buy_worker_tick(worker_app)
        await main_module._buy_worker_tick(worker_app)

        flags = await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.order_id == order.id,
                AuditLog.event == AuditEvent.EXTERNAL_ERROR,
            )
        )
        assert flags.scalar_one() == 1


async def test_sweep_ignores_orders_inside_the_window(
    worker_app,
    session: AsyncSession,
    seed_order,
) -> None:
    """An order still inside the confirmation window is not flagged."""
    with freeze_time("2026-11-23 12:00:00"):
        order = await seed_order(
            order_number="ORD-SWEEP-FRESH",
            order_type=OrderType.BUY,
            status=OrderStatus.SENDING_PAYMENT,
            sms_sent_at=datetime(2026, 11, 23, 11, 59, 50, tzinfo=timezone.utc),
        )

        await main_module._buy_worker_tick(worker_app)

        flags = await session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.order_id == order.id,
                AuditLog.event == AuditEvent.EXTERNAL_ERROR,
            )
        )
        assert flags.scalar_one() == 0


async def test_lifespan_starts_and_cancels_buy_worker(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifespan starts the worker task and cancels it cleanly."""
    monkeypatch.setattr(main_module, "configure_logging", lambda _settings: None)
    test_settings.buy_worker_enabled = True
    test_settings.buy_worker_poll_interval_seconds = 3600
    test_settings.sms_poller_enabled = False
    application = create_app(test_settings)
    await dispose_engine()
    task: asyncio.Task[None] | None = None
    async with application.router.lifespan_context(application):
        task = next(
            (
                candidate
                for candidate in asyncio.all_tasks()
                if candidate.get_name() == "buy-worker"
            ),
            None,
        )
        assert task is not None
    assert task is not None
    assert task.done()


async def test_worker_disabled_starts_nothing(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the worker disabled no background task is created."""
    monkeypatch.setattr(main_module, "configure_logging", lambda _settings: None)
    test_settings.buy_worker_enabled = False
    test_settings.sms_poller_enabled = False
    application = create_app(test_settings)
    await dispose_engine()
    async with application.router.lifespan_context(application):
        names = {candidate.get_name() for candidate in asyncio.all_tasks()}
        assert "buy-worker" not in names


async def test_missing_browser_surfaces_typed_error_not_crash(
    test_settings: Settings,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled browser with a missing session state fails typed, not loudly."""
    monkeypatch.setattr(main_module, "configure_logging", lambda _settings: None)
    test_settings.binance_browser_enabled = True
    test_settings.binance_session_state_path = str(tmp_path / "missing-state.json")
    application = create_app(test_settings)
    await dispose_engine()
    async with application.router.lifespan_context(application):
        browser = application.state.browser
        assert browser is not None
        with pytest.raises(ExternalServiceError) as excinfo:
            await browser.extract_payment_instruction("ORD-NO-STATE")
        assert excinfo.value.service == "binance_browser"

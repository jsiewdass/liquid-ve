"""Operator pay endpoint tests for the BUY flow (S7).

The endpoint is exercised through ``app_client`` so requests and assertions
share the same in-memory engine.  No network, browser or real credential is
used.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AuditActor, AuditEvent, OrderStatus, OrderType
from app.domain.ports import PaymentInstruction
from app.infrastructure.clients.binance_client import BinanceC2COrder
from app.infrastructure.models import AuditLog

_HEADERS = {"X-API-Key": "test-internal-api-key-0123456789abcdef"}
_PAY_PATH = "/api/v1/orders/buy/{order_number}/pay"


def _app(client: httpx.AsyncClient) -> FastAPI:
    """Return the FastAPI application bound to the ASGI test client."""
    return client._transport.app  # type: ignore[attr-defined]


def _instruction(order_number: str) -> PaymentInstruction:
    """Build a complete BDV payment instruction for the fake browser."""
    return PaymentInstruction(
        order_number=order_number,
        bank_numeric_code="0102",
        phone="04141234567",
        id_number="V-12345678",
        amount_ves=Decimal("1500.00"),
    )


def _provider_buy_detail(
    order_number: str, *, status: str = "PENDING_PAYMENT"
) -> BinanceC2COrder:
    """Build the provider detail the operator path re-validates."""
    return BinanceC2COrder(
        order_number=order_number,
        trade_type=OrderType.BUY,
        asset="USDT",
        fiat="VES",
        price=Decimal("150.00000000"),
        amount=Decimal("10.00000000"),
        total_price=Decimal("1500.00"),
        status=status,
        counterparty_nickname="counterparty",
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture()
def api_setup(app_client: httpx.AsyncClient, fake_browser: object, fake_sms: object):
    """Wire the browser and gateway doubles onto the ASGI app."""
    application = _app(app_client)
    application.state.browser = fake_browser
    application.state.sms = fake_sms
    return app_client


async def test_missing_api_key_is_401(api_setup: httpx.AsyncClient) -> None:
    """The operator endpoint refuses anonymous callers."""
    response = await api_setup.post(_PAY_PATH.format(order_number="ORD-X"))

    assert response.status_code == 401


async def test_unauthenticated_attempt_emits_rejection_audit(
    api_setup: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    """A rejected attempt writes a system rejection audit row."""
    response = await api_setup.post(_PAY_PATH.format(order_number="ORD-X"))

    assert response.status_code == 401
    result = await session.execute(
        select(AuditLog).where(
            AuditLog.event == AuditEvent.WEBHOOK_REJECTED,
            AuditLog.actor == AuditActor.SYSTEM,
            AuditLog.order_id.is_(None),
        )
    )
    assert len(result.scalars().all()) == 1


async def test_unknown_order_is_404(api_setup: httpx.AsyncClient) -> None:
    """An unknown order number is a canonical 404."""
    response = await api_setup.post(
        _PAY_PATH.format(order_number="ORD-MISSING"), headers=_HEADERS
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_sell_order_is_409(
    api_setup: httpx.AsyncClient,
    seed_order,
) -> None:
    """A SELL order can never be paid through the BUY endpoint."""
    await seed_order(order_number="ORD-SELL-PAY", order_type=OrderType.SELL)

    response = await api_setup.post(
        _PAY_PATH.format(order_number="ORD-SELL-PAY"), headers=_HEADERS
    )

    assert response.status_code == 409


async def test_cancelled_provider_status_blocks_operator_dispatch(
    api_setup: httpx.AsyncClient,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
    seed_order,
) -> None:
    """The operator path re-validates the provider status before dispatching."""
    await seed_order(
        order_number="ORD-PAY-CANCELLED",
        order_type=OrderType.BUY,
        status=OrderStatus.PENDING,
    )
    fake_binance.register(  # type: ignore[attr-defined]
        _provider_buy_detail("ORD-PAY-CANCELLED", status="CANCELLED")
    )
    fake_browser.instruction = _instruction("ORD-PAY-CANCELLED")  # type: ignore[attr-defined]

    response = await api_setup.post(
        _PAY_PATH.format(order_number="ORD-PAY-CANCELLED"), headers=_HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "external_status_not_pending_payment"
    assert response.json()["error"]["details"]["status"] == "CANCELLED"
    assert fake_sms.calls == []  # type: ignore[attr-defined]


async def test_gate_off_park_resumes_and_dispatches(
    api_setup: httpx.AsyncClient,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
    session: AsyncSession,
    seed_order,
) -> None:
    """A parked approval resumes legally and dispatches exactly once."""
    order = await seed_order(
        order_number="ORD-PAY",
        order_type=OrderType.BUY,
        status=OrderStatus.MANUAL_REVIEW,
        manual_review_reason="awaiting_operator_approval",
    )
    fake_binance.register(_provider_buy_detail("ORD-PAY"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-PAY")  # type: ignore[attr-defined]
    _app(api_setup).state.settings.buy_auto_pay_enabled = True

    response = await api_setup.post(
        _PAY_PATH.format(order_number="ORD-PAY"), headers=_HEADERS
    )

    assert response.status_code == 202
    body = response.json()
    assert body["order_number"] == "ORD-PAY"
    assert body["status"] == OrderStatus.SENDING_PAYMENT.value
    assert body["outcome"] == "dispatched"
    assert len(fake_sms.calls) == 1  # type: ignore[attr-defined]

    approval = await session.execute(
        select(AuditLog).where(
            AuditLog.order_id == order.id,
            AuditLog.event == AuditEvent.STATUS_CHANGED,
            AuditLog.from_status == OrderStatus.MANUAL_REVIEW,
            AuditLog.to_status == OrderStatus.PENDING,
        )
    )
    rows = list(approval.scalars().all())
    assert len(rows) == 1
    assert rows[0].actor is AuditActor.OPERATOR


async def test_second_resume_is_noop(
    api_setup: httpx.AsyncClient,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
    seed_order,
) -> None:
    """Submitting the same approval twice dispatches only once."""
    await seed_order(
        order_number="ORD-PAY-TWICE",
        order_type=OrderType.BUY,
        status=OrderStatus.MANUAL_REVIEW,
        manual_review_reason="awaiting_operator_approval",
    )
    fake_binance.register(_provider_buy_detail("ORD-PAY-TWICE"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-PAY-TWICE")  # type: ignore[attr-defined]
    _app(api_setup).state.settings.buy_auto_pay_enabled = True

    first = await api_setup.post(
        _PAY_PATH.format(order_number="ORD-PAY-TWICE"), headers=_HEADERS
    )
    second = await api_setup.post(
        _PAY_PATH.format(order_number="ORD-PAY-TWICE"), headers=_HEADERS
    )

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["outcome"] == "noop"
    assert len(fake_sms.calls) == 1  # type: ignore[attr-defined]


async def test_sending_sms_is_noop_200(
    api_setup: httpx.AsyncClient,
    seed_order,
) -> None:
    """An order already waiting for its bank reply is a 200 no-op."""
    await seed_order(
        order_number="ORD-PAY-WAIT",
        order_type=OrderType.BUY,
        status=OrderStatus.SENDING_PAYMENT,
        sms_sent_at=datetime.now(timezone.utc),
    )

    response = await api_setup.post(
        _PAY_PATH.format(order_number="ORD-PAY-WAIT"), headers=_HEADERS
    )

    assert response.status_code == 200
    assert response.json()["outcome"] == "noop"


async def test_completed_is_409_terminal_status(
    api_setup: httpx.AsyncClient,
    seed_order,
) -> None:
    """A terminal order is refused with the typed ``terminal_status`` code."""
    await seed_order(
        order_number="ORD-PAY-DONE",
        order_type=OrderType.BUY,
        status=OrderStatus.COMPLETED,
    )

    response = await api_setup.post(
        _PAY_PATH.format(order_number="ORD-PAY-DONE"), headers=_HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "terminal_status"


async def test_response_contains_no_amount_phone_or_cedula(
    api_setup: httpx.AsyncClient,
    seed_order,
) -> None:
    """The response exposes lifecycle data only, never payment data."""
    await seed_order(
        order_number="ORD-PAY-SAFE",
        order_type=OrderType.BUY,
        status=OrderStatus.SENDING_PAYMENT,
        amount_fiat=Decimal("1500.00"),
        counterparty_phone="+584141234567",
        counterparty_id_number="12345678",
        sms_sent_at=datetime.now(timezone.utc),
    )

    response = await api_setup.post(
        _PAY_PATH.format(order_number="ORD-PAY-SAFE"), headers=_HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "order_number",
        "status",
        "outcome",
        "manual_review_reason",
        "bank_short_code",
        "sms_message_id",
    }
    text = response.text
    assert "1500" not in text
    assert "0414" not in text
    assert "12345678" not in text

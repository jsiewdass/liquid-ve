"""Direction guard: the sell-side inbound path must be SELL-only.

A Cachicamo payment webhook advances an order and can release crypto.  Before
the guard a BUY order with the same fiat amount could be selected, advanced and
released.  These tests pin the SQL predicate, the defence-in-depth refusal and
the flow-level direction assertions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import InvalidStateTransitionError
from app.domain.enums import AuditEvent, OrderStatus, OrderType
from app.infrastructure.clients.binance_client import BinanceC2COrder
from app.infrastructure.models import AuditLog, BankTransaction, P2POrder
from app.services.buy_flow import BuyFlow
from app.services.sell_flow import SellFlow


def _webhook_body(event_id: str, amount: str = "1500.00") -> bytes:
    """Build a valid Cachicamo webhook body.

    Args:
        event_id: Provider event identifier.
        amount: Fiat amount in VES.

    Returns:
        The JSON body encoded as bytes.
    """
    payload = {
        "event_id": event_id,
        "event_type": "PAYMENT_RECEIVED",
        "bank_reference": f"REF-{event_id}",
        "amount_ves": amount,
        "payer_phone": "+584141234567",
        "bank_code": "BANESCO",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(payload).encode("utf-8")


def _detail(
    order_number: str,
    trade_type: OrderType,
    *,
    amount: str = "10.00000000",
    price: str = "150.00000000",
    total: str = "1500.00",
) -> BinanceC2COrder:
    """Build one normalized Binance order detail.

    Args:
        order_number: Provider order number.
        trade_type: Trade direction.
        amount: Asset amount.
        price: Unit price.
        total: Fiat total.

    Returns:
        The normalized detail.
    """
    return BinanceC2COrder(
        order_number=order_number,
        trade_type=trade_type,
        asset="USDT",
        fiat="VES",
        price=Decimal(price),
        amount=Decimal(amount),
        total_price=Decimal(total),
        status="PENDING",
        counterparty_nickname="counterparty",
        created_at=datetime.now(timezone.utc),
    )


async def _reload(session: AsyncSession, order_number: str) -> P2POrder:
    """Re-read an order from the database after a request wrote it.

    Args:
        session: Assertion session.
        order_number: Provider order number.

    Returns:
        The fresh order row.
    """
    result = await session.execute(
        select(P2POrder).where(P2POrder.order_number == order_number)
    )
    order = result.scalar_one()
    await session.refresh(order)
    return order


async def test_equal_amounts_do_not_cross_directions(
    app_client: httpx.AsyncClient,
    fake_binance: object,
    seed_order: object,
    session: AsyncSession,
    signed_cachicamo_headers: object,
) -> None:
    """Only the SELL order advances when a BUY shares its exact fiat amount."""
    buy = await seed_order(  # type: ignore[operator]
        order_number="ORD-BUY-EQ",
        order_type=OrderType.BUY,
        amount_fiat=Decimal("1500.00"),
    )
    sell = await seed_order(  # type: ignore[operator]
        order_number="ORD-SELL-EQ",
        order_type=OrderType.SELL,
        amount_fiat=Decimal("1500.00"),
    )

    body = _webhook_body("evt-equal-amounts")
    headers = signed_cachicamo_headers(body, event_id="evt-equal-amounts")  # type: ignore[operator]
    response = await app_client.post(
        "/api/v1/webhooks/cachicamo", content=body, headers=headers
    )

    sell_number = sell.order_number
    buy_number = buy.order_number
    buy_id = buy.id

    assert response.status_code == 202
    assert response.json()["message"] == "Payment processed"

    fresh_sell = await _reload(session, sell_number)
    fresh_buy = await _reload(session, buy_number)
    assert fresh_sell.status is OrderStatus.COMPLETED
    assert fresh_buy.status is OrderStatus.PENDING

    buy_audits = await session.execute(
        select(AuditLog.event).where(AuditLog.order_id == buy_id)
    )
    assert AuditEvent.PAYMENT_VERIFIED not in set(buy_audits.scalars().all())

    assert fake_binance.release_calls == [sell_number]  # type: ignore[attr-defined]
    assert buy_number not in fake_binance.release_calls  # type: ignore[attr-defined]


async def test_only_buy_candidate_yields_no_match(
    app_client: httpx.AsyncClient,
    fake_binance: object,
    seed_order: object,
    session: AsyncSession,
    signed_cachicamo_headers: object,
) -> None:
    """A BUY-only database is invisible to the sell-side webhook."""
    buy = await seed_order(  # type: ignore[operator]
        order_number="ORD-BUY-ONLY",
        order_type=OrderType.BUY,
        amount_fiat=Decimal("1500.00"),
    )

    body = _webhook_body("evt-buy-only")
    headers = signed_cachicamo_headers(body, event_id="evt-buy-only")  # type: ignore[operator]
    response = await app_client.post(
        "/api/v1/webhooks/cachicamo", content=body, headers=headers
    )

    buy_number = buy.order_number
    buy_id = buy.id

    assert response.status_code == 202
    assert (
        response.json()["message"]
        == "Payment stored; no matching order is waiting for it"
    )

    fresh_buy = await _reload(session, buy_number)
    assert fresh_buy.status is OrderStatus.PENDING
    assert fake_binance.release_calls == []  # type: ignore[attr-defined]

    transactions = await session.execute(
        select(func.count())
        .select_from(BankTransaction)
        .where(BankTransaction.order_id == buy_id)
    )
    assert transactions.scalar_one() == 0


async def test_direction_mismatch_is_audited_after_load(
    session: AsyncSession,
    seed_order: object,
) -> None:
    """Defence in depth: a loaded non-SELL order gets a rejection audit."""
    from app.api.v1 import webhooks

    buy = await seed_order(  # type: ignore[operator]
        order_number="ORD-BUY-AUDIT",
        order_type=OrderType.BUY,
        amount_fiat=Decimal("1500.00"),
    )
    sell = await seed_order(  # type: ignore[operator]
        order_number="ORD-SELL-AUDIT",
        order_type=OrderType.SELL,
        amount_fiat=Decimal("1500.00"),
    )
    buy_id = buy.id

    message = await webhooks._reject_non_sell_order(session, buy)
    await session.commit()

    assert message == "Payment stored; no matching order is waiting for it"
    audits = await session.execute(
        select(AuditLog).where(
            AuditLog.order_id == buy_id,
            AuditLog.event == AuditEvent.WEBHOOK_REJECTED,
        )
    )
    audit = audits.scalar_one()
    assert audit.payload == {"reason": "direction_mismatch"}

    assert await webhooks._reject_non_sell_order(session, sell) is None


async def test_sell_flow_refuses_buy_order_before_bank_transaction(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_cachicamo: object,
    seed_order: object,
) -> None:
    """Defence in depth: a BUY order never gets a bank transaction or release."""
    buy = await seed_order(  # type: ignore[operator]
        order_number="ORD-BUY-FLOW",
        order_type=OrderType.BUY,
        amount_fiat=Decimal("1500.00"),
    )
    flow = SellFlow(
        session,
        settings=test_settings,
        binance=fake_binance,  # type: ignore[arg-type]
        cachicamo=fake_cachicamo,  # type: ignore[arg-type]
        sms=object(),
    )

    with pytest.raises(InvalidStateTransitionError):
        await flow.handle_incoming_payment(
            order_number=buy.order_number,
            bank_reference="REF-BUY-FLOW",
            amount_ves=Decimal("1500.00"),
        )

    transactions = await session.execute(
        select(func.count())
        .select_from(BankTransaction)
        .where(BankTransaction.order_id == buy.id)
    )
    assert transactions.scalar_one() == 0
    assert fake_binance.release_calls == []  # type: ignore[attr-defined]


async def test_sell_flow_execute_refuses_buy_detail(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_cachicamo: object,
) -> None:
    """SellFlow.execute rejects a BUY detail before persisting anything."""
    fake_binance.register(_detail("ORD-BUY-EXEC", OrderType.BUY))  # type: ignore[attr-defined]
    flow = SellFlow(
        session,
        settings=test_settings,
        binance=fake_binance,  # type: ignore[arg-type]
        cachicamo=fake_cachicamo,  # type: ignore[arg-type]
        sms=object(),
    )

    with pytest.raises(InvalidStateTransitionError):
        await flow.execute("ORD-BUY-EXEC")

    persisted = await session.execute(
        select(func.count())
        .select_from(P2POrder)
        .where(P2POrder.order_number == "ORD-BUY-EXEC")
    )
    assert persisted.scalar_one() == 0


async def test_buy_flow_refuses_sell_order(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_cachicamo: object,
) -> None:
    """BuyFlow.execute rejects a SELL detail before persisting anything."""
    fake_binance.register(_detail("ORD-SELL-EXEC", OrderType.SELL))  # type: ignore[attr-defined]
    flow = BuyFlow(
        session,
        settings=test_settings,
        binance=fake_binance,  # type: ignore[arg-type]
        cachicamo=fake_cachicamo,  # type: ignore[arg-type]
        sms=object(),
    )

    with pytest.raises(InvalidStateTransitionError):
        await flow.execute("ORD-SELL-EXEC")

    persisted = await session.execute(
        select(func.count())
        .select_from(P2POrder)
        .where(P2POrder.order_number == "ORD-SELL-EXEC")
    )
    assert persisted.scalar_one() == 0
    assert fake_binance.release_calls == []  # type: ignore[attr-defined]

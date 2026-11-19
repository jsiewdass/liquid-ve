"""Database-level idempotency guarantees."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    AuditActor,
    AuditEvent,
    BankCode,
    BankTransactionDirection,
    OrderStatus,
    OrderType,
    PaymentVerificationStatus,
)
from app.infrastructure.models import AuditLog, BankTransaction, P2POrder, WebhookEvent


def _order(order_number: str = "ORD-1") -> P2POrder:
    """Build a minimally valid order.

    Args:
        order_number: Unique order number.

    Returns:
        An unsaved order.
    """
    return P2POrder(
        order_number=order_number,
        order_type=OrderType.BUY,
        status=OrderStatus.PENDING,
        price=Decimal("1.00000000"),
        amount_asset=Decimal("10.00000000"),
        amount_fiat=Decimal("10.00"),
    )


async def test_duplicate_order_number_raises(session: AsyncSession) -> None:
    """The order number is the idempotency anchor."""
    session.add(_order("ORD-DUP"))
    await session.commit()
    session.add(_order("ORD-DUP"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_duplicate_bank_reference_raises(session: AsyncSession) -> None:
    """The bank reference is the financial idempotency anchor."""
    order = _order("ORD-BANK")
    session.add(order)
    await session.commit()
    session.add(
        BankTransaction(
            order_id=order.id,
            bank_reference="REF-123",
            direction=BankTransactionDirection.INCOMING,
            bank_code=BankCode.BANESCO,
            amount=Decimal("100.00"),
            verification_status=PaymentVerificationStatus.PENDING,
        )
    )
    await session.commit()
    session.add(
        BankTransaction(
            order_id=order.id,
            bank_reference="REF-123",
            direction=BankTransactionDirection.INCOMING,
            bank_code=BankCode.BANESCO,
            amount=Decimal("100.00"),
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_webhook_event_provider_and_event_id_are_unique(session: AsyncSession) -> None:
    """Webhook replay protection relies on the composite unique constraint."""
    session.add(
        WebhookEvent(
            provider="cachicamo",
            external_event_id="evt-1",
            payload_hash="a" * 64,
            signature_valid=True,
        )
    )
    await session.commit()
    session.add(
        WebhookEvent(
            provider="cachicamo",
            external_event_id="evt-1",
            payload_hash="b" * 64,
            signature_valid=True,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_audit_log_accepts_status_entries(session: AsyncSession) -> None:
    """Audit rows persist status changes with actor metadata."""
    order = _order("ORD-AUDIT")
    session.add(order)
    await session.commit()
    session.add(
        AuditLog(
            order_id=order.id,
            event=AuditEvent.STATUS_CHANGED,
            actor=AuditActor.WORKER,
            from_status=OrderStatus.PENDING,
            to_status=OrderStatus.PAYMENT_DETECTED,
            message="payment detected",
        )
    )
    await session.commit()
    assert order.id is not None

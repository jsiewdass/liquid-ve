"""Binance close and the ``COMPLETED`` boundary (S6).

The close is the only non-idempotent step after B6, so every test here pins
one half of R-BUY-009: the order reaches ``COMPLETED`` *only* after a
successful :meth:`BinanceBrowser.notify_paid`, a failing or uncertain close
parks it in ``MANUAL_REVIEW`` with ``binance_close_failed``, and a replay can
never produce a second notification.

All tests run offline.  The browser is the scriptable ``fake_browser`` port
double, so no Playwright import or browser launch happens in the default
``-q`` run.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ExternalServiceError
from app.core.security import mask_secret
from app.domain.enums import AuditEvent, BankCode, OrderStatus, OrderType
from app.infrastructure.clients.binance_browser import PlaywrightBinanceBrowser
from app.infrastructure.models import (
    AuditLog,
    BankTransaction,
    DailyVolumeLedger,
    P2POrder,
)
from app.services.buy_flow import BuyFlow

_FIXTURES = Path(__file__).parent / "fixtures" / "sms"
_REFERENCE = "90218473"
_CLOSE_FAILED = "binance_close_failed"


def _reply(name: str) -> str:
    """Read one synthetic bank-reply fixture.

    Args:
        name: Fixture file name inside ``tests/fixtures/sms``.

    Returns:
        The fixture body without a trailing newline.
    """
    return (_FIXTURES / name).read_text(encoding="utf-8").strip()


def _buy_order_kwargs(
    order_number: str,
    *,
    amount: str = "1500.00",
    bank: BankCode = BankCode.BANCO_DE_VENEZUELA,
    received_at: datetime,
) -> dict[str, object]:
    """Build the keyword arguments for a ``SENDING_PAYMENT`` BUY seed.

    Args:
        order_number: Provider order number.
        amount: Fiat amount.
        bank: Destination bank.
        received_at: Confirmation instant, also used as the SMS anchor.

    Returns:
        Keyword arguments accepted by ``seed_order``.
    """
    return {
        "order_number": order_number,
        "order_type": OrderType.BUY,
        "status": OrderStatus.SENDING_PAYMENT,
        "bank_code": bank,
        "amount_fiat": Decimal(amount),
        "amount_asset": Decimal("10.00000000"),
        "price": Decimal("150.00000000"),
        "sms_sent_at": received_at,
        "idempotency_key": f"{order_number}:payout",
    }


def _flow(
    session: AsyncSession,
    settings: Settings,
    *,
    browser: object | None,
    sms: object,
    binance: object,
) -> BuyFlow:
    """Build a ``BuyFlow`` wired to the offline doubles.

    Args:
        session: Active session shared with the assertions.
        settings: Active settings.
        browser: Browser port double, or ``None`` to model a missing adapter.
        sms: SMS gateway double.
        binance: Binance client double.

    Returns:
        The flow under test.
    """
    return BuyFlow(
        session,
        settings=settings,
        binance=binance,
        cachicamo=object(),
        sms=sms,
        browser=browser,
    )


async def _reload(session: AsyncSession, order_number: str) -> P2POrder:
    """Re-read an order after a confirmation wrote it.

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


async def _audit_rows(session: AsyncSession, order_id: int) -> list[AuditLog]:
    """Return the audit rows recorded for one order, oldest first.

    Args:
        session: Assertion session.
        order_id: Order primary key.

    Returns:
        The audit rows.
    """
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.order_id == order_id)
        .order_by(AuditLog.id.asc())
    )
    return list(result.scalars().all())


async def _count_transactions(session: AsyncSession, order_id: int) -> int:
    """Count the bank movements linked to an order.

    Args:
        session: Assertion session.
        order_id: Order primary key.

    Returns:
        The number of rows.
    """
    result = await session.execute(
        select(BankTransaction).where(BankTransaction.order_id == order_id)
    )
    return len(list(result.scalars().all()))


def _notify_events(fake_browser: object) -> list[tuple[Any, ...]]:
    """Return only the ``notify`` events recorded by the browser double.

    Args:
        fake_browser: Browser double exposing ``events``.

    Returns:
        The ordered notification events.
    """
    events = getattr(fake_browser, "events")
    return [event for event in events if event[0] == "notify"]


async def _confirm(
    flow: BuyFlow,
    *,
    message_id: str,
    body: str,
    received_at: datetime,
) -> object:
    """Deliver one bank reply through the shared confirmation entry point.

    The webhook is a thin adapter over this method, so driving it directly
    exercises the exact path a replayed delivery takes.

    Args:
        flow: Flow under test.
        message_id: Provider message identifier.
        body: Raw reply body.
        received_at: Provider timestamp.

    Returns:
        The confirmation outcome.
    """
    return await flow.confirm_bank_payment(
        message_id=message_id,
        sender="2661",
        body=body,
        received_at=received_at,
    )


# ---------------------------------------------------------------------------
# 9.1 — a successful close reaches COMPLETED
# ---------------------------------------------------------------------------


async def test_success_closes_order_with_masked_reference(
    session: AsyncSession,
    test_settings: Settings,
    seed_order: object,
    fake_browser: object,
    fake_sms: object,
    fake_binance: object,
) -> None:
    """A confirmed reply plus a successful notify reaches ``COMPLETED``."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-CLOSE-OK", received_at=received_at)
    )
    flow = _flow(
        session,
        test_settings,
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    confirmation = await _confirm(
        flow,
        message_id="msg-close-ok",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    assert confirmation.outcome == "confirmed"  # type: ignore[attr-defined]
    assert confirmation.order_number == "ORD-CLOSE-OK"  # type: ignore[attr-defined]
    assert confirmation.reason is None  # type: ignore[attr-defined]
    assert _notify_events(fake_browser) == [("notify", 1, _REFERENCE)]

    fresh = await _reload(session, "ORD-CLOSE-OK")
    assert fresh.status is OrderStatus.COMPLETED
    assert fresh.bank_confirmed_at is not None
    assert fresh.bank_reference == _REFERENCE
    assert await _count_transactions(session, order.id) == 1

    rows = await _audit_rows(session, order.id)
    completed = [
        row for row in rows if row.to_status is OrderStatus.COMPLETED
    ]
    assert len(completed) == 1
    entry = completed[0]
    assert entry.event is AuditEvent.STATUS_CHANGED
    assert entry.from_status is OrderStatus.AWAITING_RELEASE
    assert entry.payload["reference_masked"] == mask_secret(_REFERENCE, visible=4)
    assert _REFERENCE not in json.dumps([row.payload for row in rows], default=str)


async def test_close_is_attempted_only_after_confirmation(
    session: AsyncSession,
    test_settings: Settings,
    seed_order: object,
    fake_browser: object,
    fake_sms: object,
    fake_binance: object,
) -> None:
    """Non-success replies and unmatched deliveries never invoke the close."""
    received_at = datetime.now(timezone.utc)
    await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-CLOSE-FAILREPLY", received_at=received_at)
    )
    flow = _flow(
        session,
        test_settings,
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    await _confirm(
        flow,
        message_id="msg-close-failure-reply",
        body=_reply("bank_reply_failure_bdv.txt"),
        received_at=received_at,
    )
    assert _notify_events(fake_browser) == []

    # A success reply with no open candidate must also not notify.
    await _confirm(
        flow,
        message_id="msg-close-no-candidate",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at + timedelta(hours=1),
    )
    assert _notify_events(fake_browser) == []


# ---------------------------------------------------------------------------
# 9.2 — a failure or an unknown close never completes
# ---------------------------------------------------------------------------


async def test_success_close_uses_the_parsed_banesco_reference(
    session: AsyncSession,
    test_settings: Settings,
    seed_order: object,
    fake_browser: object,
    fake_sms: object,
    fake_binance: object,
) -> None:
    """The close posts the reference parsed from the reply, not a constant."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs(
            "ORD-CLOSE-BANESCO",
            bank=BankCode.BANESCO,
            received_at=received_at,
        )
    )
    flow = _flow(
        session,
        test_settings,
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    await flow.confirm_bank_payment(
        message_id="msg-close-banesco",
        sender="2846",
        body=_reply("bank_reply_success_banesco.txt"),
        received_at=received_at,
    )

    assert _notify_events(fake_browser) == [("notify", 1, "445120987")]
    fresh = await _reload(session, "ORD-CLOSE-BANESCO")
    assert fresh.status is OrderStatus.COMPLETED
    rows = await _audit_rows(session, order.id)
    completed = [row for row in rows if row.to_status is OrderStatus.COMPLETED]
    assert completed[0].payload["reference_masked"] == mask_secret(
        "445120987", visible=4
    )


async def test_failed_close_never_completes(
    session: AsyncSession,
    test_settings: Settings,
    seed_order: object,
    fake_browser: object,
    fake_sms: object,
    fake_binance: object,
) -> None:
    """A deterministic notify failure parks the order and keeps B6 evidence."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-CLOSE-FAIL", received_at=received_at)
    )
    session.add(
        DailyVolumeLedger(
            ledger_date=received_at.date(),
            currency="VES_PAY",
            total_volume=Decimal("1500.00"),
            transaction_count=1,
        )
    )
    await session.commit()
    fake_browser.notify_error = ExternalServiceError(  # type: ignore[attr-defined]
        "close failed",
        service="binance_browser",
        code="binance_close_unconfirmed",
    )
    flow = _flow(
        session,
        test_settings,
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    confirmation = await _confirm(
        flow,
        message_id="msg-close-fail",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    assert confirmation.outcome == "matched"  # type: ignore[attr-defined]
    assert confirmation.reason == _CLOSE_FAILED  # type: ignore[attr-defined]
    assert _notify_events(fake_browser) == [("notify", 1, _REFERENCE)]

    fresh = await _reload(session, "ORD-CLOSE-FAIL")
    assert fresh.status is OrderStatus.MANUAL_REVIEW
    assert fresh.manual_review_reason == _CLOSE_FAILED
    assert fresh.bank_confirmed_at is not None
    assert fresh.bank_reference == _REFERENCE
    assert await _count_transactions(session, order.id) == 1

    rows = await _audit_rows(session, order.id)
    assert all(row.to_status is not OrderStatus.COMPLETED for row in rows)

    ledger = await session.execute(
        select(DailyVolumeLedger).where(
            DailyVolumeLedger.ledger_date == received_at.date(),
            DailyVolumeLedger.currency == "VES_PAY",
        )
    )
    reserved = ledger.scalar_one()
    assert reserved.total_volume == Decimal("1500.00")
    assert reserved.transaction_count == 1


async def test_unknown_close_outcome_is_not_retried(
    session: AsyncSession,
    test_settings: Settings,
    seed_order: object,
    fake_browser: object,
    fake_sms: object,
    fake_binance: object,
) -> None:
    """An uncertain close is attempted exactly once and never re-invoked."""
    received_at = datetime.now(timezone.utc)
    await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-CLOSE-UNKNOWN", received_at=received_at)
    )
    fake_browser.notify_error = TimeoutError("close outcome unknown")  # type: ignore[attr-defined]
    flow = _flow(
        session,
        test_settings,
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    await _confirm(
        flow,
        message_id="msg-close-unknown",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )
    assert len(_notify_events(fake_browser)) == 1

    # A different delivery cannot re-open the closed order.
    await _confirm(
        flow,
        message_id="msg-close-unknown-retry",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )
    # A replay of the same delivery is deduplicated at the anchor.
    await _confirm(
        flow,
        message_id="msg-close-unknown",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )
    assert len(_notify_events(fake_browser)) == 1


async def test_confirmed_order_with_failed_close_is_queryable(
    session: AsyncSession,
    test_settings: Settings,
    seed_order: object,
    fake_browser: object,
    fake_sms: object,
    fake_binance: object,
) -> None:
    """A failed close leaves the confirmed order findable for an operator."""
    received_at = datetime.now(timezone.utc)
    await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-CLOSE-QUERY", received_at=received_at)
    )
    fake_browser.notify_error = RuntimeError("dom never confirmed")  # type: ignore[attr-defined]
    flow = _flow(
        session,
        test_settings,
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    await _confirm(
        flow,
        message_id="msg-close-query",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    result = await session.execute(
        select(P2POrder).where(
            P2POrder.bank_confirmed_at.is_not(None),
            P2POrder.bank_reference.is_not(None),
            P2POrder.status == OrderStatus.MANUAL_REVIEW,
        )
    )
    pending = list(result.scalars().all())
    assert [order.order_number for order in pending] == ["ORD-CLOSE-QUERY"]


async def test_missing_browser_leaves_confirmed_order_pending(
    session: AsyncSession,
    test_settings: Settings,
    seed_order: object,
    fake_browserless: None,
    fake_sms: object,
    fake_binance: object,
) -> None:
    """Without a wired browser the confirmed order stays close-pending."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-CLOSE-NOBROWSER", received_at=received_at)
    )
    flow = _flow(
        session,
        test_settings,
        browser=fake_browserless,
        sms=fake_sms,
        binance=fake_binance,
    )

    confirmation = await _confirm(
        flow,
        message_id="msg-close-no-browser",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    assert confirmation.outcome == "confirmed"  # type: ignore[attr-defined]
    fresh = await _reload(session, "ORD-CLOSE-NOBROWSER")
    assert fresh.status is OrderStatus.SENDING_PAYMENT
    assert fresh.bank_confirmed_at is not None
    assert await _count_transactions(session, order.id) == 1


# ---------------------------------------------------------------------------
# 9.3 — the close runs at most once and the adapter exposes the port method
# ---------------------------------------------------------------------------


async def test_close_called_at_most_once_for_a_confirmed_order(
    session: AsyncSession,
    test_settings: Settings,
    seed_order: object,
    fake_browser: object,
    fake_sms: object,
    fake_binance: object,
) -> None:
    """A replayed or repeated webhook can never add a second notify."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-CLOSE-ONCE", received_at=received_at)
    )
    flow = _flow(
        session,
        test_settings,
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    await _confirm(
        flow,
        message_id="msg-close-once",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )
    assert len(_notify_events(fake_browser)) == 1

    # 1. Replay of the same provider message id is deduplicated at B0′.
    replay = await _confirm(
        flow,
        message_id="msg-close-once",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )
    assert replay.duplicate is True  # type: ignore[attr-defined]

    # 2. A new delivery cannot match: status, bank_reference and
    #    bank_confirmed_at all independently exclude the completed order.
    await _confirm(
        flow,
        message_id="msg-close-once-second",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    assert len(_notify_events(fake_browser)) == 1
    fresh = await _reload(session, "ORD-CLOSE-ONCE")
    assert fresh.status is OrderStatus.COMPLETED
    rows = await _audit_rows(session, order.id)
    completions = [
        row for row in rows if row.to_status is OrderStatus.COMPLETED
    ]
    assert len(completions) == 1


def test_adapter_exposes_notify_paid() -> None:
    """The Playwright adapter implements the port's ``notify_paid`` coroutine."""
    assert hasattr(PlaywrightBinanceBrowser, "notify_paid")
    assert inspect.iscoroutinefunction(PlaywrightBinanceBrowser.notify_paid)

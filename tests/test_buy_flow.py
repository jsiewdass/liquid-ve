"""Buy dispatch, resume and outcome-classification tests.

Every test runs offline: the browser is a scriptable port double, the SMS
gateway is a recording double, and the settlement adapters only appear through
``respx`` in the contract test.  Money is always a ``Decimal``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    DuplicateOrderError,
    ExternalServiceResponseError,
    InvalidStateTransitionError,
    RetryableExternalServiceError,
    UnauthorizedError,
)
from app.domain.enums import AuditEvent, BankCode, OrderStatus, OrderType
from app.domain.ports import PaymentInstruction
from app.infrastructure.clients.binance_client import BinanceC2COrder
from app.infrastructure.clients.sms_gateway import SmsGatewayClient
from app.infrastructure.models import AuditLog, DailyVolumeLedger, P2POrder
from app.services.buy_flow import BuyFlow
from app.services.risk_policy import LEDGER_VES, LEDGER_VES_PAY, RiskPolicy
from app.services.sms_dispatch import DispatchFate, dispatch_bank_command


def _buy_detail(
    order_number: str,
    *,
    total_price: str = "1500.00",
    amount: str = "10.00000000",
    price: str = "150.00000000",
    status: str = "PENDING_PAYMENT",
) -> BinanceC2COrder:
    """Build a normalized BUY order detail.

    Args:
        order_number: Provider order number.
        total_price: Fiat total in VES.
        amount: Asset amount.
        price: Quoted unit price.
        status: External provider status.

    Returns:
        The normalized order detail.
    """
    return BinanceC2COrder(
        order_number=order_number,
        trade_type=OrderType.BUY,
        asset="USDT",
        fiat="VES",
        price=Decimal(price),
        amount=Decimal(amount),
        total_price=Decimal(total_price),
        status=status,
        counterparty_nickname="counterparty",
        created_at=datetime.now(timezone.utc),
    )


def _instruction(
    order_number: str,
    *,
    bank: str = "0102",
    phone: str = "+584141234567",
    id_number: str = "V-12345678",
    amount: str = "1500.00",
) -> PaymentInstruction:
    """Build a complete payment instruction.

    Args:
        order_number: Provider order number.
        bank: Scraped numeric bank code.
        phone: Scraped counterparty phone.
        id_number: Scraped identity document.
        amount: Scraped amount in VES.

    Returns:
        The payment instruction.
    """
    return PaymentInstruction(
        order_number=order_number,
        bank_numeric_code=bank,
        phone=phone,
        id_number=id_number,
        amount_ves=Decimal(amount),
    )


def _auto_pay_settings(settings: Settings) -> Settings:
    """Return settings with the auto-pay gate open.

    Args:
        settings: Base test settings.

    Returns:
        A copy with ``buy_auto_pay_enabled`` set.
    """
    return settings.model_copy(update={"buy_auto_pay_enabled": True})


def _flow(
    session: AsyncSession,
    settings: Settings,
    *,
    browser: object | None,
    sms: object,
    binance: object,
) -> BuyFlow:
    """Build a flow wired to doubles.

    Args:
        session: Active session.
        settings: Active settings.
        browser: Browser port double or ``None``.
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


async def _ledger_total(session: AsyncSession) -> Decimal:
    """Return the current ``VES_PAY`` ledger total.

    Args:
        session: Active session.

    Returns:
        The reserved total, or ``0`` when the row does not exist.
    """
    return await _channel_total(session, LEDGER_VES_PAY)


async def _channel_total(session: AsyncSession, currency: str) -> Decimal:
    """Return a channel's ledger total, tolerating a missing row.

    Args:
        session: Active session.
        currency: Ledger channel.

    Returns:
        The reserved total, or ``0`` when the row does not exist.
    """
    result = await session.execute(
        select(DailyVolumeLedger).where(DailyVolumeLedger.currency == currency)
    )
    row = result.scalar_one_or_none()
    return row.total_volume if row is not None else Decimal("0.00")


async def _ledger_count(session: AsyncSession) -> int:
    """Return the number of ledger rows created for any channel.

    Args:
        session: Active session.

    Returns:
        The total ledger row count.
    """
    result = await session.execute(select(func.count()).select_from(DailyVolumeLedger))
    return int(result.scalar_one())


async def _events(session: AsyncSession, order_id: int) -> list[AuditEvent]:
    """Return the audit events recorded for one order, oldest first.

    Args:
        session: Active session.
        order_id: Persisted order id.

    Returns:
        The ordered audit event list.
    """
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.order_id == order_id)
        .order_by(AuditLog.id)
    )
    return [entry.event for entry in result.scalars().all()]


# --------------------------------------------------------------------------
# 7.2 — dispatch with one classified fallback and a stable key
# --------------------------------------------------------------------------


async def test_primary_not_delivered_falls_back_once_with_same_key(
    fake_sms: object,
) -> None:
    """One not-delivered primary triggers exactly one same-key fallback."""
    fake_sms.errors.append(  # type: ignore[attr-defined]
        ExternalServiceResponseError(
            "rejected", service="sms_gateway", details={"status": 400}
        )
    )

    result = await dispatch_bank_command(
        fake_sms,  # type: ignore[arg-type]
        short_codes=("2661", "2662"),
        message="Pagar 0102 04141234567 12345678 1500,00",
        order_number="ORD-FALLBACK",
    )

    assert result.fate is DispatchFate.DELIVERED
    assert result.short_code == "2662"
    calls = fake_sms.calls  # type: ignore[attr-defined]
    assert [call[0] for call in calls] == ["2661", "2662"]
    assert {call[2] for call in calls} == {"ORD-FALLBACK:payout"}


async def test_second_not_delivered_is_permanent(fake_sms: object) -> None:
    """A second not-delivered result is permanent; never a third attempt."""
    fake_sms.errors.extend(  # type: ignore[attr-defined]
        [
            ExternalServiceResponseError(
                "rejected", service="sms_gateway", details={"status": 400}
            ),
            ExternalServiceResponseError(
                "rejected", service="sms_gateway", details={"status": 400}
            ),
        ]
    )

    result = await dispatch_bank_command(
        fake_sms,  # type: ignore[arg-type]
        short_codes=("2661", "2662"),
        message="Pagar 0102 04141234567 12345678 1500,00",
        order_number="ORD-PERM",
    )

    assert result.fate is DispatchFate.PERMANENT
    assert len(fake_sms.calls) == 2  # type: ignore[attr-defined]


async def test_unauthorized_does_not_fall_back(fake_sms: object) -> None:
    """Invalid credentials are permanent and make exactly one request."""
    fake_sms.errors.append(  # type: ignore[attr-defined]
        UnauthorizedError("nope", details={"status": 401})
    )

    result = await dispatch_bank_command(
        fake_sms,  # type: ignore[arg-type]
        short_codes=("2661", "2662"),
        message="Pagar 0102 04141234567 12345678 1500,00",
        order_number="ORD-AUTH",
    )

    assert result.fate is DispatchFate.PERMANENT
    assert len(fake_sms.calls) == 1  # type: ignore[attr-defined]


async def test_conflict_is_unknown_without_fallback(fake_sms: object) -> None:
    """A 409 means the message may already exist and must not be retried."""
    fake_sms.errors.append(  # type: ignore[attr-defined]
        ConflictError("duplicate key", details={"status": 409})
    )

    result = await dispatch_bank_command(
        fake_sms,  # type: ignore[arg-type]
        short_codes=("2661", "2662"),
        message="Pagar 0102 04141234567 12345678 1500,00",
        order_number="ORD-CONFLICT",
    )

    assert result.fate is DispatchFate.UNKNOWN
    assert len(fake_sms.calls) == 1  # type: ignore[attr-defined]


async def test_single_short_code_not_delivered_is_permanent(fake_sms: object) -> None:
    """A bank with one short code has no fallback and fails permanently."""
    fake_sms.errors.append(  # type: ignore[attr-defined]
        ExternalServiceResponseError(
            "rejected", service="sms_gateway", details={"status": 400}
        )
    )

    result = await dispatch_bank_command(
        fake_sms,  # type: ignore[arg-type]
        short_codes=("2846",),
        message="Pagar 0134 04141234567 12345678 1500,00",
        order_number="ORD-SINGLE",
    )

    assert result.fate is DispatchFate.PERMANENT
    assert result.short_code == "2846"
    assert len(fake_sms.calls) == 1  # type: ignore[attr-defined]


async def test_fallback_delivery_records_the_secondary_short_code(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """A successful fallback updates the persisted short code and telemetry."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-FALLBACK-FLOW"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-FALLBACK-FLOW")  # type: ignore[attr-defined]
    fake_sms.errors.append(  # type: ignore[attr-defined]
        ExternalServiceResponseError(
            "rejected", service="sms_gateway", details={"status": 400}
        )
    )
    flow = _flow(
        session, settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-FALLBACK-FLOW")

    assert order.status is OrderStatus.SENDING_PAYMENT
    assert order.bank_short_code == "2662"
    assert order.sms_message_id == "msg-2"
    assert [call[0] for call in fake_sms.calls] == ["2661", "2662"]  # type: ignore[attr-defined]
    assert {call[2] for call in fake_sms.calls} == {  # type: ignore[attr-defined]
        "ORD-FALLBACK-FLOW:payout"
    }


# --------------------------------------------------------------------------
# 7.3 — B1/B2/B4 durability, extraction and the attempt identity
# --------------------------------------------------------------------------


async def test_state_is_durable_before_the_gateway_call(
    session: AsyncSession,
    engine: AsyncEngine,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """B2 commits the attempt identity before the gateway sees the command."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-DURABLE"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-DURABLE")  # type: ignore[attr-defined]

    captured: dict[str, object] = {}
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _on_send(_phone: str) -> None:
        async with factory() as probe:
            result = await probe.execute(
                select(P2POrder).where(P2POrder.order_number == "ORD-DURABLE")
            )
            row = result.scalar_one()
            captured.update(
                status=row.status,
                idempotency_key=row.idempotency_key,
                bank_code=row.bank_code,
                bank_short_code=row.bank_short_code,
                counterparty_id_number=row.counterparty_id_number,
            )

    fake_sms.on_send = _on_send  # type: ignore[attr-defined]
    flow = _flow(
        session,
        settings,
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    await flow.execute("ORD-DURABLE")

    assert captured["status"] is OrderStatus.SENDING_PAYMENT
    assert captured["idempotency_key"] == "ORD-DURABLE:payout"
    assert captured["bank_code"] is BankCode.BANCO_DE_VENEZUELA
    assert captured["bank_short_code"] == "2661"
    assert captured["counterparty_id_number"] == "12345678"


async def test_idempotency_key_is_order_number_payout(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """The wire key is ``{order_number}:payout`` and the destination is the short code."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-KEY"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-KEY")  # type: ignore[attr-defined]
    flow = _flow(
        session, settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    await flow.execute("ORD-KEY")

    calls = fake_sms.calls  # type: ignore[attr-defined]
    assert len(calls) == 1
    phone_number, message, idempotency_key = calls[0]
    assert idempotency_key == "ORD-KEY:payout"
    assert phone_number == "2661"
    assert "04141234567" in message


async def test_extraction_persists_bank_phone_and_cedula(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """Extraction results are persisted on the order."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-EXTRACT"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-EXTRACT")  # type: ignore[attr-defined]
    flow = _flow(
        session, settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-EXTRACT")

    assert order.bank_code is BankCode.BANCO_DE_VENEZUELA
    assert order.bank_short_code == "2661"
    assert order.counterparty_phone == "04141234567"
    assert order.counterparty_id_number == "12345678"


async def test_delivered_send_commits_sms_telemetry_and_stays_sending_sms(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """B4 records telemetry without reaching ``COMPLETED``."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-TELEMETRY"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-TELEMETRY")  # type: ignore[attr-defined]
    flow = _flow(
        session, settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-TELEMETRY")

    assert order.status is OrderStatus.SENDING_PAYMENT
    assert order.sms_message_id == "msg-1"
    assert order.sms_sent_at is not None


async def test_successful_dispatch_reserves_both_daily_channels(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """A dispatched BUY consumes both the shared ``VES`` and the ``VES_PAY`` cap."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-DOUBLE-CAP"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-DOUBLE-CAP")  # type: ignore[attr-defined]
    flow = _flow(
        session, settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-DOUBLE-CAP")

    assert order.status is OrderStatus.SENDING_PAYMENT
    assert await _channel_total(session, LEDGER_VES) == Decimal("1500.00")
    assert await _channel_total(session, LEDGER_VES_PAY) == Decimal("1500.00")
    assert await _ledger_count(session) == 2


async def test_duplicate_order_number_raises_duplicate_order_error(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
    seed_order: object,
) -> None:
    """A second processing of the same order number is refused."""
    await seed_order(order_number="ORD-DUPLICATE")  # type: ignore[operator]
    fake_binance.register(_buy_detail("ORD-DUPLICATE"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-DUPLICATE")  # type: ignore[attr-defined]
    flow = _flow(
        session, test_settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    with pytest.raises(DuplicateOrderError):
        await flow.execute("ORD-DUPLICATE")


async def test_cancelled_detail_is_rejected_before_persistence_or_dispatch(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """A non-``PENDING_PAYMENT`` provider status is refused before any effect."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-CANCELLED", status="CANCELLED"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-CANCELLED")  # type: ignore[attr-defined]
    flow = _flow(
        session, settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    with pytest.raises(InvalidStateTransitionError) as excinfo:
        await flow.execute("ORD-CANCELLED")

    assert excinfo.value.code == "external_status_not_pending_payment"
    assert excinfo.value.details["status"] == "CANCELLED"
    assert fake_sms.calls == []  # type: ignore[attr-defined]
    result = await session.execute(
        select(P2POrder).where(P2POrder.order_number == "ORD-CANCELLED")
    )
    assert result.scalar_one_or_none() is None


# --------------------------------------------------------------------------
# 7.4 — extraction and breaker failures park in MANUAL_REVIEW
# --------------------------------------------------------------------------


async def test_incomplete_extraction_parks_payment_data_unavailable(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """A missing cedula blocks the dispatch."""
    fake_binance.register(_buy_detail("ORD-INCOMPLETE"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction(  # type: ignore[attr-defined]
        "ORD-INCOMPLETE", id_number=""
    )
    flow = _flow(
        session, test_settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-INCOMPLETE")

    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.manual_review_reason == "payment_data_unavailable"
    assert fake_sms.calls == []  # type: ignore[attr-defined]


async def test_no_browser_parks_payment_data_unavailable(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browserless: None,
    fake_sms: object,
) -> None:
    """Without a browser the order parks instead of crashing."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-NOBROWSER"))  # type: ignore[attr-defined]
    flow = _flow(
        session,
        settings,
        browser=fake_browserless,
        sms=fake_sms,
        binance=fake_binance,
    )

    order = await flow.execute("ORD-NOBROWSER")

    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.manual_review_reason == "payment_data_unavailable"
    assert fake_sms.calls == []  # type: ignore[attr-defined]
    assert await _channel_total(session, LEDGER_VES) == Decimal("0.00")
    assert await _channel_total(session, LEDGER_VES_PAY) == Decimal("0.00")
    assert await _ledger_count(session) == 0


async def test_unsupported_bank_parks_unsupported_destination_bank(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """An unsupported bank never dispatches."""
    fake_binance.register(_buy_detail("ORD-BANK", total_price="1500.00"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-BANK", bank="0105")  # type: ignore[attr-defined]
    flow = _flow(
        session, test_settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-BANK")

    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.manual_review_reason == "unsupported_destination_bank"
    assert fake_sms.calls == []  # type: ignore[attr-defined]


async def test_over_cap_parks_amount_over_cap(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """An amount above the per-order cap parks with ``amount_over_cap``."""
    settings = test_settings.model_copy(
        update={"max_buy_amount_ves": Decimal("1000.00")}
    )
    fake_binance.register(  # type: ignore[attr-defined]
        _buy_detail("ORD-CAP", total_price="2000.00", amount="10.00000000", price="200.00")
    )
    fake_browser.instruction = _instruction("ORD-CAP", amount="2000.00")  # type: ignore[attr-defined]
    flow = _flow(
        session, settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-CAP")

    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.manual_review_reason == "amount_over_cap"
    assert fake_sms.calls == []  # type: ignore[attr-defined]


async def test_rate_outside_tolerance_parks_rate_out_of_tolerance(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """A mangled scrape that implies a bad rate parks."""
    fake_binance.register(  # type: ignore[attr-defined]
        _buy_detail("ORD-RATE", total_price="1600.00", price="160.00")
    )
    fake_browser.instruction = _instruction("ORD-RATE", amount="1500.00")  # type: ignore[attr-defined]
    flow = _flow(
        session, test_settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-RATE")

    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.manual_review_reason == "rate_out_of_tolerance"
    assert fake_sms.calls == []  # type: ignore[attr-defined]


async def test_daily_pay_cap_parks_daily_pay_cap_exceeded(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """The daily outgoing cap blocks before any gateway call."""
    policy = RiskPolicy(session, test_settings)
    await policy.reserve(currency=LEDGER_VES_PAY, amount_ves=Decimal("19000.00"))
    fake_binance.register(_buy_detail("ORD-DAILY"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-DAILY")  # type: ignore[attr-defined]
    flow = _flow(
        session, test_settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-DAILY")

    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.manual_review_reason == "daily_pay_cap_exceeded"
    assert fake_sms.calls == []  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# 7.5 — unknown fate vs permanent failure (B3/B5)
# --------------------------------------------------------------------------


async def test_unknown_outcome_makes_exactly_one_request_and_stays_sending_sms(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """An unknown fate stays ``SENDING_PAYMENT`` with a single request."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-UNKNOWN"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-UNKNOWN")  # type: ignore[attr-defined]
    fake_sms.errors.append(  # type: ignore[attr-defined]
        RetryableExternalServiceError("boom", service="sms_gateway")
    )
    flow = _flow(
        session, settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-UNKNOWN")

    assert order.status is OrderStatus.SENDING_PAYMENT
    assert len(fake_sms.calls) == 1  # type: ignore[attr-defined]
    assert order.sms_message_id is None
    assert AuditEvent.EXTERNAL_ERROR in await _events(session, order.id)
    assert await _channel_total(session, LEDGER_VES) == Decimal("1500.00")
    assert await _ledger_total(session) == Decimal("1500.00")


async def test_permanent_failure_releases_reservation_and_parks_manual_review(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """A permanent failure releases the reservation in the same transaction."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-PERMANENT"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-PERMANENT")  # type: ignore[attr-defined]
    fake_sms.errors.append(  # type: ignore[attr-defined]
        UnauthorizedError("nope", details={"status": 401})
    )
    flow = _flow(
        session, settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-PERMANENT")

    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.manual_review_reason == "sms_dispatch_failed"
    assert len(fake_sms.calls) == 1  # type: ignore[attr-defined]
    assert await _channel_total(session, LEDGER_VES) == Decimal("0.00")
    assert await _channel_total(session, LEDGER_VES_PAY) == Decimal("0.00")
    ves = await session.execute(
        select(DailyVolumeLedger).where(DailyVolumeLedger.currency == LEDGER_VES)
    )
    sms = await session.execute(
        select(DailyVolumeLedger).where(DailyVolumeLedger.currency == LEDGER_VES_PAY)
    )
    assert ves.scalar_one().transaction_count == 0
    assert sms.scalar_one().transaction_count == 0


async def test_timeout_never_releases_the_reservation(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """A timeout keeps the reservation and the ``SENDING_PAYMENT`` evidence."""
    settings = _auto_pay_settings(test_settings)
    fake_binance.register(_buy_detail("ORD-TIMEOUT"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-TIMEOUT")  # type: ignore[attr-defined]
    fake_sms.errors.append(TimeoutError("gateway timed out"))  # type: ignore[attr-defined]
    flow = _flow(
        session, settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-TIMEOUT")

    assert order.status is OrderStatus.SENDING_PAYMENT
    assert len(fake_sms.calls) == 1  # type: ignore[attr-defined]
    assert await _channel_total(session, LEDGER_VES) == Decimal("1500.00")
    assert await _ledger_total(session) == Decimal("1500.00")


# --------------------------------------------------------------------------
# 7.6 — resume() idempotence and no premature COMPLETED
# --------------------------------------------------------------------------


async def _dispatch_delivered(
    session: AsyncSession,
    settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
    order_number: str,
) -> P2POrder:
    """Run a delivered dispatch and return the persisted order.

    Args:
        session: Active session.
        settings: Active settings.
        fake_binance: Binance double.
        fake_browser: Browser double.
        fake_sms: SMS double.
        order_number: Provider order number.

    Returns:
        The persisted order in ``SENDING_PAYMENT``.
    """
    auto = _auto_pay_settings(settings)
    fake_binance.register(_buy_detail(order_number))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction(order_number)  # type: ignore[attr-defined]
    flow = _flow(session, auto, browser=fake_browser, sms=fake_sms, binance=fake_binance)
    return await flow.execute(order_number)


async def test_dispatch_leaves_order_in_sending_sms(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """A delivered dispatch never reaches ``COMPLETED``."""
    order = await _dispatch_delivered(
        session, test_settings, fake_binance, fake_browser, fake_sms, "ORD-STAY"
    )

    assert order.status is OrderStatus.SENDING_PAYMENT
    assert AuditEvent.SMS_SENT in await _events(session, order.id)
    completed = await session.execute(
        select(AuditLog).where(
            AuditLog.order_id == order.id,
            AuditLog.to_status == OrderStatus.COMPLETED,
        )
    )
    assert completed.scalar_one_or_none() is None


async def test_resume_while_sending_sms_sends_nothing_and_reserves_nothing(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """Resuming a ``SENDING_PAYMENT`` order makes no gateway call and no reservation."""
    order = await _dispatch_delivered(
        session, test_settings, fake_binance, fake_browser, fake_sms, "ORD-RESUME"
    )
    calls_before = list(fake_sms.calls)  # type: ignore[attr-defined]
    reserved_before = await _ledger_total(session)
    status_changes_before = (await _events(session, order.id)).count(
        AuditEvent.STATUS_CHANGED
    )
    flow = _flow(
        session,
        _auto_pay_settings(test_settings),
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    resumed = await flow.resume("ORD-RESUME")

    assert resumed.status is OrderStatus.SENDING_PAYMENT
    assert fake_sms.calls == calls_before  # type: ignore[attr-defined]
    assert await _ledger_total(session) == reserved_before
    status_changes_after = (await _events(session, order.id)).count(
        AuditEvent.STATUS_CHANGED
    )
    assert status_changes_after - status_changes_before == 1


async def test_resume_while_sending_sms_does_not_reextract(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """Resuming does not re-run extraction."""
    await _dispatch_delivered(
        session, test_settings, fake_binance, fake_browser, fake_sms, "ORD-NOEXTRACT"
    )
    extracts_before = list(fake_browser.events)  # type: ignore[attr-defined]
    flow = _flow(
        session,
        _auto_pay_settings(test_settings),
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    await flow.resume("ORD-NOEXTRACT")

    assert fake_browser.events == extracts_before  # type: ignore[attr-defined]


async def test_resume_on_completed_order_raises_typed_error(
    session: AsyncSession,
    test_settings: Settings,
    fake_sms: object,
    seed_order: object,
) -> None:
    """Resuming a terminal order is a typed duplicate error."""
    order = await seed_order(  # type: ignore[operator]
        order_number="ORD-DONE",
        order_type=OrderType.BUY,
        status=OrderStatus.COMPLETED,
    )
    flow = _flow(
        session, test_settings, browser=None, sms=fake_sms, binance=object()
    )

    with pytest.raises(DuplicateOrderError) as excinfo:
        await flow.resume("ORD-DONE")

    assert excinfo.value.code == "duplicate_order"
    events = await _events(session, order.id)
    assert AuditEvent.IDEMPOTENCY_HIT in events
    assert AuditEvent.SMS_SENT not in events


async def test_resume_on_manual_review_without_approval_is_refused(
    session: AsyncSession,
    test_settings: Settings,
    fake_sms: object,
    seed_order: object,
) -> None:
    """An unapproved ``MANUAL_REVIEW`` order is refused."""
    await seed_order(  # type: ignore[operator]
        order_number="ORD-REVIEW",
        order_type=OrderType.BUY,
        status=OrderStatus.MANUAL_REVIEW,
    )
    flow = _flow(
        session, test_settings, browser=None, sms=fake_sms, binance=object()
    )

    with pytest.raises(DuplicateOrderError):
        await flow.resume("ORD-REVIEW")

    assert fake_sms.calls == []  # type: ignore[attr-defined]


async def test_dispatch_on_sending_sms_is_noop_without_second_reservation(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """A stale second dispatch cannot reserve again or call the gateway again."""
    order = await _dispatch_delivered(
        session, test_settings, fake_binance, fake_browser, fake_sms, "ORD-STALE"
    )
    calls_before = list(fake_sms.calls)  # type: ignore[attr-defined]
    ves_before = await _channel_total(session, LEDGER_VES)
    sms_before = await _channel_total(session, LEDGER_VES_PAY)
    status_changes_before = (await _events(session, order.id)).count(
        AuditEvent.STATUS_CHANGED
    )
    flow = _flow(
        session,
        _auto_pay_settings(test_settings),
        browser=fake_browser,
        sms=fake_sms,
        binance=fake_binance,
    )

    stale = await flow._dispatch(order, sms=fake_sms, operator_approved=True)

    assert stale.status is OrderStatus.SENDING_PAYMENT
    assert fake_sms.calls == calls_before  # type: ignore[attr-defined]
    assert await _channel_total(session, LEDGER_VES) == ves_before
    assert await _channel_total(session, LEDGER_VES_PAY) == sms_before
    status_changes_after = (await _events(session, order.id)).count(
        AuditEvent.STATUS_CHANGED
    )
    assert status_changes_after == status_changes_before


# --------------------------------------------------------------------------
# 7.7 — operator-approval gate
# --------------------------------------------------------------------------


async def test_gate_off_parks_manual_review_awaiting_operator_approval(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """With the gate closed a risk-approved order parks awaiting approval."""
    fake_binance.register(_buy_detail("ORD-GATE"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-GATE")  # type: ignore[attr-defined]
    flow = _flow(
        session, test_settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-GATE")

    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.manual_review_reason == "awaiting_operator_approval"
    assert AuditEvent.RISK_BLOCKED in await _events(session, order.id)


async def test_gate_off_reserves_nothing_and_calls_no_gateway(
    session: AsyncSession,
    test_settings: Settings,
    fake_binance: object,
    fake_browser: object,
    fake_sms: object,
) -> None:
    """The closed gate leaves the outgoing ledger untouched."""
    fake_binance.register(_buy_detail("ORD-GATE-QUIET"))  # type: ignore[attr-defined]
    fake_browser.instruction = _instruction("ORD-GATE-QUIET")  # type: ignore[attr-defined]
    flow = _flow(
        session, test_settings, browser=fake_browser, sms=fake_sms, binance=fake_binance
    )

    order = await flow.execute("ORD-GATE-QUIET")

    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.idempotency_key is None
    assert fake_sms.calls == []  # type: ignore[attr-defined]
    assert await _channel_total(session, LEDGER_VES) == Decimal("0.00")
    assert await _channel_total(session, LEDGER_VES_PAY) == Decimal("0.00")
    assert await _ledger_count(session) == 0


# --------------------------------------------------------------------------
# 7.8 — sms_gateway cleanup and the no-retry contract
# --------------------------------------------------------------------------


def test_build_payment_sms_is_removed() -> None:
    """The legacy sentence builder is gone."""
    assert not hasattr(SmsGatewayClient, "build_payment_sms")


@respx.mock
async def test_send_sms_contract_headers_and_no_retry(
    test_settings: Settings,
) -> None:
    """The real adapter sends once with the right headers and typed errors."""
    route = respx.post("http://100.64.0.10:8080/messages").mock(
        return_value=httpx.Response(500)
    )
    client = SmsGatewayClient(test_settings)

    with pytest.raises(RetryableExternalServiceError):
        await client.send_sms(
            phone_number="2661",
            message="Pagar 0102 04141234567 12345678 1500,00",
            idempotency_key="ORD-CONTRACT:payout",
        )

    assert len(route.calls) == 1
    request = route.calls[0].request
    assert request.headers["Idempotency-Key"] == "ORD-CONTRACT:payout"
    assert request.headers["Authorization"] == "Bearer test-sms-gateway-token"

    route.mock(return_value=httpx.Response(200, json={"unexpected": True}))

    with pytest.raises(ExternalServiceResponseError):
        await client.send_sms(
            phone_number="2661",
            message="Pagar 0102 04141234567 12345678 1500,00",
            idempotency_key="ORD-CONTRACT:payout",
        )

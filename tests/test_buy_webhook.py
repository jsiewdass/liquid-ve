"""SMS webhook confirmation, replay anchor and B6 tests.

Every webhook test uses the one-engine ``app_client`` harness so requests and
assertions bind to the same in-memory database.  The poller test drives the
service directly, which is the same entry point the webhook adapter uses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.enums import (
    AuditEvent,
    BankCode,
    BankTransactionDirection,
    OrderStatus,
    OrderType,
)
from app.domain.schemas import SmsConfirmation
from app.infrastructure.clients.sms_gateway import IncomingSms
from app.infrastructure.models import (
    AuditLog,
    BankTransaction,
    DailyVolumeLedger,
    P2POrder,
    WebhookEvent,
)
from app.services.buy_flow import BuyFlow

_FIXTURES = Path(__file__).parent / "fixtures" / "sms"
_BEARER = {"Authorization": "Bearer test-internal-api-key-0123456789abcdef"}
_BDV_SENDER = "2661"


def _reply(name: str) -> str:
    """Read one synthetic reply fixture.

    Args:
        name: Fixture file name inside ``tests/fixtures/sms``.

    Returns:
        The fixture body without a trailing newline.
    """
    return (_FIXTURES / name).read_text(encoding="utf-8").strip()


async def _post_sms(
    app_client: httpx.AsyncClient,
    *,
    message_id: str,
    body: str,
    sender: str = _BDV_SENDER,
    received_at: datetime | None = None,
) -> httpx.Response:
    """Deliver one SMS webhook.

    Args:
        app_client: ASGI client bound to the shared engine.
        message_id: Provider message identifier.
        body: Reply body.
        sender: Bank short code.
        received_at: Provider timestamp; defaults to now.

    Returns:
        The HTTP response.
    """
    payload = {
        "message_id": message_id,
        "sender": sender,
        "body": body,
        "received_at": (received_at or datetime.now(timezone.utc)).isoformat(),
    }
    return await app_client.post("/api/v1/webhooks/sms", json=payload, headers=_BEARER)


async def _reload(session: AsyncSession, order_number: str) -> P2POrder:
    """Re-read an order after a request wrote it.

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


async def _count_transactions(session: AsyncSession, order_id: int) -> int:
    """Count bank transactions linked to an order.

    Args:
        session: Assertion session.
        order_id: Order primary key.

    Returns:
        The number of rows.
    """
    result = await session.execute(
        select(func.count())
        .select_from(BankTransaction)
        .where(BankTransaction.order_id == order_id)
    )
    return int(result.scalar_one())


def _buy_order_kwargs(
    order_number: str,
    *,
    amount: str = "1500.00",
    bank: BankCode = BankCode.BANCO_DE_VENEZUELA,
    sms_sent_at: datetime | None = None,
) -> dict[str, object]:
    """Build the keyword arguments for a ``SENDING_PAYMENT`` BUY seed.

    Args:
        order_number: Provider order number.
        amount: Fiat amount.
        bank: Destination bank.
        sms_sent_at: Correlation anchor.

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
        "sms_sent_at": sms_sent_at or datetime.now(timezone.utc),
        "idempotency_key": f"{order_number}:payout",
    }


async def test_processed_replay_returns_duplicate_true_and_writes_nothing(
    app_client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    """A processed ``message_id`` short-circuits without writing anything."""
    session.add(
        WebhookEvent(
            provider="sms_gateway",
            external_event_id="msg-replay",
            payload_hash="a" * 64,
            signature_valid=True,
            processed=True,
        )
    )
    await session.commit()
    before = await session.execute(select(func.count()).select_from(AuditLog))

    response = await _post_sms(
        app_client,
        message_id="msg-replay",
        body=_reply("bank_reply_success_bdv.txt"),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["duplicate"] is True
    assert body["outcome"] == "no_match"
    assert body["reason"] == "already_processed"

    after = await session.execute(select(func.count()).select_from(AuditLog))
    assert after.scalar_one() == before.scalar_one()


async def test_unprocessed_row_is_reprocessed_once(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """A committed-but-unprocessed row is re-processed at-least-once."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-RETRY", sms_sent_at=received_at)
    )
    session.add(
        WebhookEvent(
            provider="sms_gateway",
            external_event_id="msg-retry",
            payload_hash="b" * 64,
            signature_valid=True,
            processed=False,
        )
    )
    await session.commit()

    response = await _post_sms(
        app_client,
        message_id="msg-retry",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    order_id = order.id
    order_number = order.order_number
    assert response.status_code == 202
    assert response.json()["outcome"] == "confirmed"

    fresh = await _reload(session, order_number)
    assert fresh.bank_confirmed_at is not None
    assert await _count_transactions(session, order_id) == 1

    result = await session.execute(
        select(WebhookEvent).where(WebhookEvent.external_event_id == "msg-retry")
    )
    assert result.scalar_one().processed is True


async def test_single_success_candidate_confirms(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """One matching ``SENDING_PAYMENT`` order is confirmed but not completed."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-CONFIRM", sms_sent_at=received_at)
    )

    response = await _post_sms(
        app_client,
        message_id="msg-confirm",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    order_number = order.order_number
    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "confirmed"
    assert body["order_number"] == order_number
    assert body["reason"] is None

    fresh = await _reload(session, order_number)
    assert fresh.status is OrderStatus.SENDING_PAYMENT
    assert fresh.bank_confirmed_at is not None
    assert fresh.bank_reference == "90218473"

    received = await session.execute(
        select(AuditLog).where(
            AuditLog.event == AuditEvent.WEBHOOK_RECEIVED,
            AuditLog.order_id.is_(None),
        )
    )
    assert received.scalar_one().payload["message_id"] == "msg-confirm"


async def test_confirmation_writes_one_outgoing_transaction_with_reference(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """B6 writes exactly one OUTGOING movement with the reply reference."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-B6", sms_sent_at=received_at)
    )

    response = await _post_sms(
        app_client,
        message_id="msg-b6",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    order_id = order.id
    assert response.status_code == 202
    result = await session.execute(
        select(BankTransaction).where(BankTransaction.order_id == order_id)
    )
    transactions = list(result.scalars().all())
    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction.direction is BankTransactionDirection.OUTGOING
    assert transaction.bank_reference == "90218473"
    assert transaction.amount == Decimal("1500.00")
    assert transaction.verified is True
    assert transaction.verification_status is None
    assert transaction.raw_payload["source"] == "sms_gateway"
    assert "sender_masked" in transaction.raw_payload
    assert "90218473" not in str(transaction.raw_payload)


async def test_failure_reply_moves_manual_review_and_retains_reservation(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """A bank rejection parks the order and keeps the reservation."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-FAIL", sms_sent_at=received_at)
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

    response = await _post_sms(
        app_client,
        message_id="msg-fail",
        body=_reply("bank_reply_failure_bdv.txt"),
        received_at=received_at,
    )

    order_number = order.order_number
    order_id = order.id
    assert response.status_code == 202
    assert response.json()["outcome"] == "matched"

    fresh = await _reload(session, order_number)
    assert fresh.status is OrderStatus.MANUAL_REVIEW
    assert fresh.manual_review_reason == "bank_rejected_payment"
    assert await _count_transactions(session, order_id) == 0

    ledger = await session.execute(
        select(DailyVolumeLedger).where(
            DailyVolumeLedger.ledger_date == received_at.date(),
            DailyVolumeLedger.currency == "VES_PAY",
        )
    )
    row = ledger.scalar_one()
    assert row.total_volume == Decimal("1500.00")
    assert row.transaction_count == 1


async def test_unparseable_reply_moves_manual_review(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """Unknown wording parks the single candidate for a human."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-UNPARSEABLE", sms_sent_at=received_at)
    )

    response = await _post_sms(
        app_client,
        message_id="msg-unparseable",
        body=_reply("bank_reply_unknown_bdv.txt"),
        received_at=received_at,
    )

    order_number = order.order_number
    assert response.status_code == 202
    assert response.json()["outcome"] == "matched"
    fresh = await _reload(session, order_number)
    assert fresh.status is OrderStatus.MANUAL_REVIEW
    assert fresh.manual_review_reason == "bank_reply_unparseable"


async def test_ambiguous_parse_moves_manual_review(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """A reply matching success and failure vocabularies parks the order."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-AMBIGUOUS-PARSE", sms_sent_at=received_at)
    )

    response = await _post_sms(
        app_client,
        message_id="msg-ambiguous-parse",
        body=_reply("bank_reply_ambiguous_bdv.txt"),
        received_at=received_at,
    )

    order_number = order.order_number
    assert response.status_code == 202
    assert response.json()["outcome"] == "matched"
    fresh = await _reload(session, order_number)
    assert fresh.status is OrderStatus.MANUAL_REVIEW
    assert fresh.manual_review_reason == "bank_reply_ambiguous"


async def test_zero_candidates_no_match(
    app_client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    """A success reply with no open order changes nothing."""
    response = await _post_sms(
        app_client,
        message_id="msg-zero",
        body=_reply("bank_reply_success_bdv.txt"),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "no_match"
    assert body["reason"] == "no_candidate"

    rejected = await session.execute(
        select(AuditLog).where(AuditLog.event == AuditEvent.WEBHOOK_REJECTED)
    )
    assert rejected.scalar_one().payload["reason"] == "no_candidate"


async def test_two_candidates_ambiguous_and_no_state_change(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """Two matching candidates raise an alert and change no state."""
    received_at = datetime.now(timezone.utc)
    first = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-AMB-1", sms_sent_at=received_at)
    )
    second = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-AMB-2", sms_sent_at=received_at)
    )

    response = await _post_sms(
        app_client,
        message_id="msg-two",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "ambiguous"
    assert body["reason"] == "multiple_candidates"

    for number in (first.order_number, second.order_number):
        fresh = await _reload(session, number)
        assert fresh.status is OrderStatus.SENDING_PAYMENT
        assert fresh.bank_confirmed_at is None

    rejected = await session.execute(
        select(AuditLog).where(AuditLog.event == AuditEvent.WEBHOOK_REJECTED)
    )
    entry = rejected.scalar_one()
    assert entry.payload["reason"] == "ambiguous"
    assert entry.payload["candidate_count"] == 2


async def test_foreign_sender_no_match(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """A sender outside the profile's short codes confirms nothing."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-FOREIGN", sms_sent_at=received_at)
    )

    response = await _post_sms(
        app_client,
        message_id="msg-foreign",
        body=_reply("bank_reply_success_bdv.txt"),
        sender="9999",
        received_at=received_at,
    )

    order_number = order.order_number
    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "no_match"
    assert body["reason"] == "unknown_sender"
    fresh = await _reload(session, order_number)
    assert fresh.status is OrderStatus.SENDING_PAYMENT


async def test_expired_window_no_match(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """A dispatch older than the confirmation window is not a candidate."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs(
            "ORD-EXPIRED",
            sms_sent_at=received_at - timedelta(seconds=400),
        )
    )

    response = await _post_sms(
        app_client,
        message_id="msg-expired",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    order_number = order.order_number
    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "no_match"
    assert body["reason"] == "no_candidate"
    fresh = await _reload(session, order_number)
    assert fresh.status is OrderStatus.SENDING_PAYMENT


async def test_amount_mismatch_no_match(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """A reply carrying a different amount never confirms the order."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-MISMATCH", sms_sent_at=received_at)
    )

    response = await _post_sms(
        app_client,
        message_id="msg-mismatch",
        body=_reply("bank_reply_amount_mismatch_bdv.txt"),
        received_at=received_at,
    )

    order_number = order.order_number
    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "no_match"
    assert body["reason"] == "amount_mismatch"
    fresh = await _reload(session, order_number)
    assert fresh.status is OrderStatus.SENDING_PAYMENT


async def test_duplicate_reference_moves_manual_review_and_returns_202(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """A UNIQUE reference collision becomes a review, never an HTTP 500."""
    received_at = datetime.now(timezone.utc)
    await seed_order(  # type: ignore[operator]
        order_number="ORD-PRIOR",
        order_type=OrderType.BUY,
        status=OrderStatus.COMPLETED,
        bank_code=BankCode.BANCO_DE_VENEZUELA,
        bank_reference="90218473",
        amount_fiat=Decimal("1000.00"),
    )
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-DUPLICATE-REF", sms_sent_at=received_at)
    )

    response = await _post_sms(
        app_client,
        message_id="msg-duplicate",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    order_number = order.order_number
    order_id = order.id
    assert response.status_code == 202
    body = response.json()
    assert body["outcome"] == "matched"
    assert body["reason"] == "duplicate_bank_reference"

    fresh = await _reload(session, order_number)
    assert fresh.status is OrderStatus.MANUAL_REVIEW
    assert fresh.manual_review_reason == "duplicate_bank_reference"
    assert fresh.bank_reference is None
    assert await _count_transactions(session, order_id) == 0


async def test_failure_reply_writes_no_bank_transaction(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """A failure reply has no reference, so it must not write a movement."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-FAIL-NO-TX", sms_sent_at=received_at)
    )

    response = await _post_sms(
        app_client,
        message_id="msg-fail-no-tx",
        body=_reply("bank_reply_failure_bdv.txt"),
        received_at=received_at,
    )

    order_id = order.id
    assert response.status_code == 202
    assert await _count_transactions(session, order_id) == 0


async def test_sms_webhook_response_carries_outcome_and_order_number(
    app_client: httpx.AsyncClient,
    seed_order: object,
) -> None:
    """The enriched response keeps the acceptance envelope and adds the outcome."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-RESPONSE", sms_sent_at=received_at)
    )

    response = await _post_sms(
        app_client,
        message_id="msg-response",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["event_id"] == "msg-response"
    assert body["duplicate"] is False
    assert body["outcome"] == "confirmed"
    assert body["order_number"] == order.order_number
    assert "reason" in body


async def test_poller_path_uses_same_confirmation_vocabulary(
    session: AsyncSession,
    test_settings: Settings,
    seed_order: object,
) -> None:
    """The poller adapter returns the same ``SmsConfirmation`` as the webhook."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-POLLER", sms_sent_at=received_at)
    )
    flow = BuyFlow(session, settings=test_settings)

    confirmation = await flow.handle_incoming_sms(
        IncomingSms(
            message_id="msg-poller",
            sender=_BDV_SENDER,
            body=_reply("bank_reply_success_bdv.txt"),
            received_at=received_at,
        )
    )

    assert isinstance(confirmation, SmsConfirmation)
    assert confirmation.outcome == "confirmed"
    assert confirmation.order_number == order.order_number

    fresh = await _reload(session, order.order_number)
    assert fresh.bank_confirmed_at is not None
    events = await session.execute(
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.external_event_id == "msg-poller")
    )
    assert events.scalar_one() == 1


async def test_short_code_override_sender_confirms(
    session: AsyncSession,
    test_settings: Settings,
    seed_order: object,
) -> None:
    """A sender introduced by settings resolves to the right bank profile."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-OVERRIDE", sms_sent_at=received_at)
    )
    settings = test_settings.model_copy(
        update={"bank_short_codes_bdv": ["2663"]}
    )
    flow = BuyFlow(session, settings=settings)

    confirmation = await flow.handle_incoming_sms(
        IncomingSms(
            message_id="msg-override",
            sender="2663",
            body=_reply("bank_reply_success_bdv.txt"),
            received_at=received_at,
        )
    )

    assert confirmation.outcome == "confirmed"
    assert confirmation.order_number == order.order_number


async def test_no_sms_received_audit_on_duplicate_replay(
    app_client: httpx.AsyncClient,
    seed_order: object,
    session: AsyncSession,
) -> None:
    """A processed replay does not append ``WEBHOOK_REJECTED`` or ``SMS_RECEIVED``."""
    received_at = datetime.now(timezone.utc)
    order = await seed_order(  # type: ignore[operator]
        **_buy_order_kwargs("ORD-REPLAY-AUDIT", sms_sent_at=received_at)
    )
    result = await _post_sms(
        app_client,
        message_id="msg-audit",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )
    assert result.status_code == 202
    order_id = order.id

    replay = await _post_sms(
        app_client,
        message_id="msg-audit",
        body=_reply("bank_reply_success_bdv.txt"),
        received_at=received_at,
    )

    assert replay.status_code == 202
    assert replay.json()["duplicate"] is True
    events = await session.execute(
        select(AuditLog.event).where(AuditLog.order_id == order_id)
    )
    events_found = list(events.scalars().all())
    assert events_found.count(AuditEvent.PAYMENT_VERIFIED) == 1
    assert events_found.count(AuditEvent.SMS_RECEIVED) == 1

"""Buy flow orchestration.

A buy order means we pay VES to the counterparty and receive crypto.  The
payout is triggered by an SMS sent through the Android gateway, so retries are
safe as long as the same ``Idempotency-Key`` is reused.

Key decision: when the SMS provider fails, the order stays in
``SENDING_SMS`` and the typed :class:`RetryableExternalServiceError` is
re-raised after committing an ``EXTERNAL_ERROR`` audit entry.  Moving the
order to ``FAILED`` would make an idempotent retry impossible, so the caller
is expected to retry with the same key.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    DuplicateOrderError,
    InvalidStateTransitionError,
    LiquidVEException,
    NotFoundError,
)
from app.core.logging import get_logger, request_id_var
from app.domain.enums import AuditActor, AuditEvent, BankCode, OrderStatus
from app.domain.schemas import RiskAssessment
from app.infrastructure.clients.binance_client import BinanceC2CClient
from app.infrastructure.clients.cachicamo_client import CachicamoClient
from app.infrastructure.clients.sms_gateway import IncomingSms, SmsGatewayClient
from app.infrastructure.models import AuditLog, DailyVolumeLedger, P2POrder

logger = get_logger("buy_flow")

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


class BuyFlow:
    """Orchestrates buy orders end to end."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        binance: BinanceC2CClient | None = None,
        cachicamo: CachicamoClient | None = None,
        sms: SmsGatewayClient | None = None,
    ) -> None:
        """Initialize the flow.

        Args:
            session: Database session owned by the caller.
            settings: Optional settings override.
            binance: Optional Binance client override.
            cachicamo: Optional Cachicamo client override.
            sms: Optional SMS gateway client override.
        """
        self._session = session
        self._settings = settings or get_settings()
        self._binance = binance
        self._cachicamo = cachicamo
        self._sms = sms

    @asynccontextmanager
    async def _clients(
        self,
    ) -> AsyncIterator[tuple[BinanceC2CClient, CachicamoClient, SmsGatewayClient]]:
        """Yield the clients, owning only the ones created here.

        Yields:
            A tuple of Binance, Cachicamo and SMS clients.
        """
        async with AsyncExitStack() as stack:
            binance = self._binance
            if binance is None:
                binance = await stack.enter_async_context(BinanceC2CClient(self._settings))
            cachicamo = self._cachicamo
            if cachicamo is None:
                cachicamo = await stack.enter_async_context(CachicamoClient(self._settings))
            sms = self._sms
            if sms is None:
                sms = await stack.enter_async_context(SmsGatewayClient(self._settings))
            yield binance, cachicamo, sms

    async def _get_order(self, order_number: str) -> P2POrder:
        """Load an order by number.

        Args:
            order_number: Provider order number.

        Returns:
            The persisted order.

        Raises:
            NotFoundError: If the order does not exist.
        """
        result = await self._session.execute(
            select(P2POrder).where(P2POrder.order_number == order_number)
        )
        order = result.scalar_one_or_none()
        if order is None:
            raise NotFoundError(f"Order {order_number} was not found")
        return order

    async def _ensure_order_idempotent(self, order_number: str) -> None:
        """Reject a duplicate order before touching the provider.

        Args:
            order_number: Provider order number.

        Raises:
            DuplicateOrderError: If an order with this number already exists.
        """
        result = await self._session.execute(
            select(P2POrder.id).where(P2POrder.order_number == order_number)
        )
        if result.scalar_one_or_none() is not None:
            raise DuplicateOrderError(
                f"Order {order_number} was already registered",
                details={"order_number": order_number},
            )

    async def _transition(
        self,
        order: P2POrder,
        target: OrderStatus,
        actor: AuditActor,
        event: AuditEvent,
        message: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Validate and persist an order status transition with its audit row.

        Args:
            order: Order being transitioned.
            target: Destination status.
            actor: Actor responsible for the transition.
            event: Audit event type.
            message: Human readable audit message.
            payload: Optional structured audit payload.

        Raises:
            InvalidStateTransitionError: If the transition is not allowed.
        """
        current = order.status
        if not current.can_transition_to(target):
            raise InvalidStateTransitionError(
                f"Cannot transition order {order.order_number} from {current.value} "
                f"to {target.value}",
                details={"from": current.value, "to": target.value},
            )
        order.status = target
        await self._session.flush()
        self._session.add(
            AuditLog(
                order_id=order.id,
                event=event,
                actor=actor,
                from_status=current,
                to_status=target,
                message=message,
                payload=dict(payload) if payload else None,
                request_id=request_id_var.get(),
            )
        )
        await self._session.flush()

    async def _audit(
        self,
        order: P2POrder,
        event: AuditEvent,
        actor: AuditActor,
        message: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Append an audit entry without changing the status.

        Args:
            order: Related order.
            event: Audit event type.
            actor: Actor responsible for the event.
            message: Human readable message.
            payload: Optional structured payload.
        """
        self._session.add(
            AuditLog(
                order_id=order.id,
                event=event,
                actor=actor,
                from_status=order.status,
                to_status=order.status,
                message=message,
                payload=dict(payload) if payload else None,
                request_id=request_id_var.get(),
            )
        )
        await self._session.flush()

    def _dialect_name(self) -> str:
        """Return the active SQL dialect name.

        Returns:
            The dialect name, defaulting to ``sqlite`` when unbound.
        """
        try:
            return self._session.get_bind().dialect.name
        except Exception:  # noqa: BLE001 - defensive, reports sqlite semantics
            return "sqlite"

    async def _load_or_create_ledger(self, session: AsyncSession) -> DailyVolumeLedger:
        """Load today's VES ledger, creating it atomically when missing.

        Args:
            session: Session used for the read/write.

        Returns:
            The ledger row for today.
        """
        today = datetime.now(timezone.utc).date()
        if self._dialect_name() == "postgresql":
            result = await session.execute(
                select(DailyVolumeLedger)
                .where(
                    DailyVolumeLedger.ledger_date == today,
                    DailyVolumeLedger.currency == "VES",
                )
                .with_for_update()
            )
            ledger = result.scalar_one_or_none()
            if ledger is None:
                ledger = DailyVolumeLedger(
                    ledger_date=today,
                    currency="VES",
                    total_volume=_ZERO,
                    transaction_count=0,
                )
                session.add(ledger)
                await session.flush()
            return ledger
        statement = (
            sqlite_insert(DailyVolumeLedger)
            .values(
                ledger_date=today,
                currency="VES",
                total_volume=_ZERO,
                transaction_count=0,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    DailyVolumeLedger.ledger_date,
                    DailyVolumeLedger.currency,
                ]
            )
        )
        await session.execute(statement)
        result = await session.execute(
            select(DailyVolumeLedger).where(
                DailyVolumeLedger.ledger_date == today,
                DailyVolumeLedger.currency == "VES",
            )
        )
        return result.scalar_one()

    async def _assess_risk(self, amount_ves: Decimal, session: AsyncSession) -> RiskAssessment:
        """Evaluate per-transaction and daily limits.

        Args:
            amount_ves: Transaction amount in VES.
            session: Session used for the ledger read/write.

        Returns:
            The risk assessment.
        """
        ledger = await self._load_or_create_ledger(session)
        projected = (ledger.total_volume + amount_ves).quantize(_CENT)
        limits = {
            "max_tx_amount_ves": str(self._settings.max_tx_amount_ves),
            "daily_limit_ves": str(self._settings.daily_limit_ves),
            "max_usdt_per_order": str(self._settings.max_usdt_per_order),
        }
        reason: str | None = None
        if amount_ves > self._settings.max_tx_amount_ves:
            reason = "Transaction amount exceeds the per-transaction limit"
        elif projected > self._settings.daily_limit_ves:
            reason = "Projected daily volume exceeds the daily limit"
        if reason is not None:
            return RiskAssessment(
                allowed=False,
                requires_manual_review=self._settings.manual_review_on_limit,
                reason=reason,
                projected_daily_volume_ves=projected,
                limits=limits,
            )
        ledger.total_volume = projected
        ledger.transaction_count = ledger.transaction_count + 1
        await session.flush()
        return RiskAssessment(
            allowed=True,
            requires_manual_review=False,
            reason=None,
            projected_daily_volume_ves=projected,
            limits=limits,
        )

    async def _advance_to_sending(self, order: P2POrder) -> None:
        """Move a fresh buy order to ``SENDING_SMS``.

        The shared lifecycle does not allow a direct jump from ``PENDING`` to
        ``SENDING_SMS``, so the order walks through the payment-detection and
        verification states with explicit audit messages.  This keeps a single
        state machine for both directions of the trade.

        Args:
            order: Order in ``PENDING``.
        """
        if order.status == OrderStatus.PENDING:
            await self._transition(
                order,
                OrderStatus.PAYMENT_DETECTED,
                AuditActor.WORKER,
                AuditEvent.STATUS_CHANGED,
                "Buy order accepted; payout intent detected",
            )
        if order.status == OrderStatus.PAYMENT_DETECTED:
            await self._transition(
                order,
                OrderStatus.VERIFYING_CACHICAMO,
                AuditActor.WORKER,
                AuditEvent.STATUS_CHANGED,
                "Buy order passed risk checks; preparing payout",
            )
        if order.status == OrderStatus.VERIFYING_CACHICAMO:
            await self._transition(
                order,
                OrderStatus.SENDING_SMS,
                AuditActor.WORKER,
                AuditEvent.STATUS_CHANGED,
                "Sending mobile payment instruction",
            )

    async def execute(
        self,
        order_number: str,
        *,
        destination_phone: str | None = None,
    ) -> P2POrder:
        """Register a buy order, evaluate risk and send the payout SMS.

        Args:
            order_number: Binance C2C order number.
            destination_phone: Optional counterparty phone override.  When
                absent the persisted counterparty phone is used.

        Returns:
            The persisted order.

        Raises:
            DuplicateOrderError: If the order was already registered.
            RetryableExternalServiceError: If the SMS provider fails; the
                order stays in ``SENDING_SMS`` for an idempotent retry.
        """
        async with self._clients() as (binance, _cachicamo, sms):
            await self._ensure_order_idempotent(order_number)
            detail = await binance.get_order_detail(order_number)
            order = P2POrder(
                order_number=detail.order_number,
                external_order_id=detail.order_number,
                order_type=detail.trade_type,
                status=OrderStatus.PENDING,
                asset=detail.asset,
                fiat_currency=detail.fiat,
                price=detail.price,
                amount_asset=detail.amount,
                amount_fiat=detail.total_price,
                counterparty_name=detail.counterparty_nickname,
                raw_payload=detail.model_dump(mode="json"),
            )
            self._session.add(order)
            try:
                await self._session.flush()
            except IntegrityError as exc:
                await self._session.rollback()
                raise DuplicateOrderError(
                    f"Order {order_number} was already registered",
                    details={"order_number": order_number},
                ) from exc
            await self._audit(
                order,
                AuditEvent.ORDER_CREATED,
                AuditActor.WORKER,
                "Buy order persisted from Binance",
            )
            assessment = await self._assess_risk(order.amount_fiat, self._session)
            order.risk_checked = True
            if not assessment.allowed:
                order.manual_review_reason = assessment.reason
                await self._transition(
                    order,
                    OrderStatus.MANUAL_REVIEW,
                    AuditActor.WORKER,
                    AuditEvent.RISK_BLOCKED,
                    assessment.reason or "Risk limit exceeded",
                    payload={"limits": assessment.limits},
                )
                await self._session.commit()
                return order
            if order.amount_asset > self._settings.max_usdt_per_order:
                order.manual_review_reason = "Order exceeds the maximum USDT per order"
                await self._transition(
                    order,
                    OrderStatus.MANUAL_REVIEW,
                    AuditActor.WORKER,
                    AuditEvent.RISK_BLOCKED,
                    "Order exceeds the maximum USDT per order",
                )
                await self._session.commit()
                return order
            phone = destination_phone or order.counterparty_phone
            if not phone:
                order.manual_review_reason = "Counterparty phone is unavailable"
                await self._transition(
                    order,
                    OrderStatus.MANUAL_REVIEW,
                    AuditActor.WORKER,
                    AuditEvent.STATUS_CHANGED,
                    "Counterparty phone is unavailable",
                )
                await self._session.commit()
                return order
            await self._advance_to_sending(order)
            # Commit the risk reservation and the SENDING_SMS state before
            # calling the provider: the SMS is the money-moving side effect.
            await self._session.commit()
            message = sms.build_payment_sms(
                amount_ves=order.amount_fiat,
                destination_phone=phone,
                bank_code=order.bank_code or BankCode.OTHER,
                reference=order.order_number,
                concept=f"Binance order {order.order_number}",
            )
            try:
                result = await sms.send_sms(
                    phone_number=phone,
                    message=message,
                    idempotency_key=f"{order.order_number}:payout",
                )
            except LiquidVEException as exc:
                await self._audit(
                    order,
                    AuditEvent.EXTERNAL_ERROR,
                    AuditActor.WORKER,
                    "SMS provider failed; order remains in SENDING_SMS",
                    payload={"error": exc.code},
                )
                await self._session.commit()
                raise
            payload = {"provider_message_id": result.message_id, "status": result.status}
            await self._transition(
                order,
                OrderStatus.COMPLETED,
                AuditActor.WORKER,
                AuditEvent.SMS_SENT,
                "Payout instruction sent",
                payload=payload,
            )
            await self._session.commit()
            logger.info("Buy order {} payout sent", order_number)
            return order

    async def handle_incoming_sms(self, sms: IncomingSms) -> P2POrder | None:
        """Match an inbound SMS against open orders.

        Confirmation SMS are free-form, so matching is best effort: the order
        number or the formatted amount must appear in the body.  A matched
        ``MANUAL_REVIEW`` order is moved back to ``PENDING`` so an operator can
        continue the flow.

        Args:
            sms: Inbound SMS read from the gateway.

        Returns:
            The matched order, or ``None`` when nothing matched.
        """
        result = await self._session.execute(
            select(P2POrder)
            .where(P2POrder.status.in_([OrderStatus.MANUAL_REVIEW, OrderStatus.SENDING_SMS]))
            .order_by(P2POrder.created_at.desc())
            .limit(100)
        )
        candidates = list(result.scalars().all())
        for order in candidates:
            formatted_amount = f"{order.amount_fiat:,.2f}"
            if order.order_number in sms.body or formatted_amount in sms.body:
                await self._audit(
                    order,
                    AuditEvent.SMS_RECEIVED,
                    AuditActor.WEBHOOK,
                    "Inbound SMS matched an open order",
                    payload={"sender_masked": _mask_sender(sms.sender)},
                )
                if order.status == OrderStatus.MANUAL_REVIEW:
                    await self._transition(
                        order,
                        OrderStatus.PENDING,
                        AuditActor.OPERATOR,
                        AuditEvent.STATUS_CHANGED,
                        "Inbound SMS confirmation received; returning to pending",
                    )
                await self._session.commit()
                return order
        return None



def _mask_sender(sender: str) -> str:
    """Mask an SMS sender for audit payloads.

    Args:
        sender: Raw sender identifier.

    Returns:
        A masked sender.
    """
    if len(sender) <= 4:
        return "***"
    return f"{'*' * (len(sender) - 4)}{sender[-4:]}"


__all__ = ["BuyFlow"]

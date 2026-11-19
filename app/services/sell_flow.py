"""Sell flow orchestration.

A sell order means the counterparty pays VES and we release crypto.  The flow
is deliberately split so every externally visible side effect has a clear
transaction boundary:

1. ``execute`` persists the order and performs the risk evaluation in a
   single transaction.  Payment arrival is asynchronous and therefore not
   awaited inside this method.
2. ``handle_incoming_payment`` records the unique bank transaction and moves
   the order to ``VERIFYING_CACHICAMO``, committing *before* any
   non-idempotent Binance call.  A replayed webhook can therefore never
   release crypto twice: the unique ``bank_reference`` insert fails first.
3. Verification is idempotent.  Release is not, so the order moves to
   ``MANUAL_REVIEW`` (with an audit entry) when release fails instead of being
   retried blindly.
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
    DuplicateBankReferenceError,
    DuplicateOrderError,
    InvalidStateTransitionError,
    LiquidVEException,
    NotFoundError,
)
from app.core.logging import get_logger, request_id_var
from app.domain.enums import (
    AuditActor,
    AuditEvent,
    BankCode,
    BankTransactionDirection,
    OrderStatus,
    PaymentVerificationStatus,
)
from app.domain.schemas import RiskAssessment
from app.infrastructure.clients.binance_client import BinanceC2CClient
from app.infrastructure.clients.cachicamo_client import CachicamoClient
from app.infrastructure.clients.sms_gateway import SmsGatewayClient
from app.infrastructure.models import AuditLog, BankTransaction, DailyVolumeLedger, P2POrder

logger = get_logger("sell_flow")

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


class SellFlow:
    """Orchestrates sell orders end to end.

    The session is supplied by the caller and owns the transaction.  Clients
    and settings are injectable so tests can substitute fakes.
    """

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

        The audit entry is written in the same transaction.

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

        On PostgreSQL the row is locked with ``SELECT ... FOR UPDATE``.  On
        SQLite an ``INSERT ... ON CONFLICT DO NOTHING`` guard makes creation
        race-free before the read.

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

        The ledger is incremented only when the transaction is allowed, which
        keeps the counter an accurate reservation.

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

    async def execute(self, order_number: str) -> P2POrder:
        """Register a sell order and evaluate risk.

        Payment arrival is handled by :meth:`handle_incoming_payment`.  The
        order row and the risk ledger update commit together, so a rejected
        order never consumes volume budget.

        Args:
            order_number: Binance C2C order number.

        Returns:
            The persisted order, in ``PENDING`` or ``MANUAL_REVIEW``.
        """
        async with self._clients() as (binance, _cachicamo, _sms):
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
                "Sell order persisted from Binance",
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

    async def handle_incoming_payment(
        self,
        *,
        order_number: str,
        bank_reference: str,
        amount_ves: Decimal,
        payer_phone: str | None = None,
        bank_code: BankCode | None = None,
    ) -> P2POrder:
        """Process an incoming mobile payment for a sell order.

        The unique bank transaction insert and the transition to
        ``VERIFYING_CACHICAMO`` are committed before the non-idempotent
        Binance release.  A duplicate webhook therefore raises
        :class:`DuplicateBankReferenceError` instead of releasing twice.

        Args:
            order_number: Binance C2C order number.
            bank_reference: Unique bank reference of the payment.
            amount_ves: Received amount in VES.
            payer_phone: Optional payer phone number.
            bank_code: Optional originating bank.

        Returns:
            The updated order.

        Raises:
            DuplicateBankReferenceError: If the bank reference was processed.
            NotFoundError: If the order does not exist.
            InvalidStateTransitionError: If the order cannot accept a payment.
        """
        async with self._clients() as (binance, cachicamo, _sms):
            order = await self._get_order(order_number)
            transaction = BankTransaction(
                order_id=order.id,
                bank_reference=bank_reference,
                direction=BankTransactionDirection.INCOMING,
                bank_code=bank_code or BankCode.OTHER,
                amount=amount_ves,
                currency="VES",
                payer_phone=payer_phone,
                verified=False,
                verification_status=PaymentVerificationStatus.PENDING,
                raw_payload={"source": "webhook"},
            )
            self._session.add(transaction)
            try:
                await self._session.flush()
            except IntegrityError as exc:
                await self._session.rollback()
                raise DuplicateBankReferenceError(
                    details={"order_number": order_number},
                ) from exc
            if order.status == OrderStatus.PENDING:
                await self._transition(
                    order,
                    OrderStatus.PAYMENT_DETECTED,
                    AuditActor.WEBHOOK,
                    AuditEvent.STATUS_CHANGED,
                    "Incoming payment registered",
                )
            if order.status == OrderStatus.PAYMENT_DETECTED:
                await self._transition(
                    order,
                    OrderStatus.VERIFYING_CACHICAMO,
                    AuditActor.WORKER,
                    AuditEvent.STATUS_CHANGED,
                    "Verifying payment with Cachicamo",
                )
            if order.status != OrderStatus.VERIFYING_CACHICAMO:
                raise InvalidStateTransitionError(
                    f"Order {order_number} cannot accept a payment in {order.status.value}",
                    details={"status": order.status.value},
                )
            await self._session.commit()

            result = await cachicamo.verify_incoming_payment(
                bank_reference=bank_reference,
                amount_ves=amount_ves,
                payer_phone=payer_phone,
                bank_code=bank_code,
            )
            transaction.verification_status = result.status
            transaction.verified = result.status == PaymentVerificationStatus.VERIFIED
            if result.status != PaymentVerificationStatus.VERIFIED:
                order.manual_review_reason = f"Payment verification returned {result.status.value}"
                await self._transition(
                    order,
                    OrderStatus.MANUAL_REVIEW,
                    AuditActor.WORKER,
                    AuditEvent.PAYMENT_VERIFIED,
                    "Payment could not be verified automatically",
                    payload={"verification_status": result.status.value},
                )
                await self._session.commit()
                return order
            await self._audit(
                order,
                AuditEvent.PAYMENT_VERIFIED,
                AuditActor.WORKER,
                "Payment verified by Cachicamo",
            )
            try:
                await binance.release_order(order.order_number)
            except LiquidVEException as exc:
                order.failure_reason = "Binance release failed"
                await self._transition(
                    order,
                    OrderStatus.MANUAL_REVIEW,
                    AuditActor.WORKER,
                    AuditEvent.EXTERNAL_ERROR,
                    "Verified payment could not be released on Binance",
                    payload={"error": exc.code},
                )
                await self._session.commit()
                raise
            await self._transition(
                order,
                OrderStatus.COMPLETED,
                AuditActor.WORKER,
                AuditEvent.STATUS_CHANGED,
                "Order completed and crypto released",
            )
            await self._session.commit()
            logger.info("Sell order {} completed", order_number)
            return order


__all__ = ["SellFlow"]

"""Buy flow orchestration.

A buy order means we pay VES to the counterparty and receive crypto.  The
payout is triggered by an SMS sent through the Android gateway, so retries are
safe only while the same ``Idempotency-Key`` is reused.

The boundary map follows the design's commit scheme:

* **B1** persists ``PENDING`` plus ``ORDER_CREATED`` so ``order_number`` is the
  durable idempotency anchor.
* **B2** persists ``SENDING_PAYMENT``, the ``VES_PAY`` reservation and the attempt
  identity *before* the bank command leaves the process.
* **B3** records an ``EXTERNAL_ERROR`` audit for an unknown fate and leaves the
  order in ``SENDING_PAYMENT`` (the money may have moved; never retried).
* **B4** records ``SMS_SENT`` telemetry while the order stays ``SENDING_PAYMENT``.
* **B5** releases the reservation and moves to ``MANUAL_REVIEW`` for a
  permanent dispatch failure, in one transaction.
* **B6** records the single outgoing bank movement, the reference and
  ``bank_confirmed_at`` when a bank reply confirms the payment.
* **B7** calls ``notify_paid`` and reaches the terminal status only after the
  seller was notified; a failed or uncertain close parks the order in
  ``MANUAL_REVIEW`` with ``binance_close_failed``.  The close is invoked only
  from the B6 success branch, so the four independent gates (status, reference,
  confirmation timestamp and ``message_id`` dedupe) each block a second call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    BankSmsFormatError,
    DuplicateOrderError,
    InvalidStateTransitionError,
    LiquidVEException,
    NotFoundError,
)
from app.core.logging import get_logger, request_id_var
from app.core.security import hash_payload, mask_phone, mask_secret
from app.domain.bank_replies import BankReplyOutcome, BankReplyParse, parse_bank_reply
from app.domain.banks import (
    BANK_SMS_PROFILES,
    BankSmsProfile,
    apply_short_code_overrides,
    cedula_digits,
    national_digits,
    profile_for_sender,
    render_bank_command,
    resolve_bank,
)
from app.domain.enums import (
    AuditActor,
    AuditEvent,
    BankCode,
    BankTransactionDirection,
    OrderStatus,
    OrderType,
)
from app.domain.ports import BinanceBrowser, PaymentInstruction
from app.domain.schemas import SmsConfirmation
from app.infrastructure.clients.binance_client import BinanceC2CClient
from app.infrastructure.clients.cachicamo_client import CachicamoClient
from app.infrastructure.clients.sms_gateway import IncomingSms, SmsGatewayClient
from app.infrastructure.models import AuditLog, BankTransaction, P2POrder, WebhookEvent
from app.services.risk_policy import LEDGER_VES, LEDGER_VES_PAY, RiskPolicy
from app.services.sms_dispatch import (
    DispatchFate,
    DispatchResult,
    dispatch_bank_command,
)

logger = get_logger("buy_flow")

_PAYMENT_DATA_UNAVAILABLE = "payment_data_unavailable"
_AWAITING_OPERATOR = "awaiting_operator_approval"
_SMS_DISPATCH_FAILED = "sms_dispatch_failed"
_DUPLICATE_BANK_REFERENCE = "duplicate_bank_reference"
_BANK_REJECTED_PAYMENT = "bank_rejected_payment"
_BANK_REPLY_UNPARSEABLE = "bank_reply_unparseable"
_BANK_REPLY_AMBIGUOUS = "bank_reply_ambiguous"
_BINANCE_CLOSE_FAILED = "binance_close_failed"
_SMS_CONFIRMATION_TIMEOUT = "sms_confirmation_timeout"
_SMS_PROVIDER = "sms_gateway"
_PENDING_PAYMENT_STATUS = "PENDING_PAYMENT"
_PRE_DISPATCH_STATUSES = frozenset(
    {
        OrderStatus.PENDING,
        OrderStatus.PAYMENT_DETECTED,
        OrderStatus.VERIFYING_CACHICAMO,
    }
)


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
        browser: BinanceBrowser | None = None,
    ) -> None:
        """Initialize the flow.

        Args:
            session: Database session owned by the caller.
            settings: Optional settings override.
            binance: Optional Binance client override.
            cachicamo: Optional Cachicamo client override.
            sms: Optional SMS gateway client override.
            browser: Optional browser port override.  When absent, extraction
                degrades to ``MANUAL_REVIEW`` instead of crashing.
        """
        self._session = session
        self._settings = settings or get_settings()
        self._binance = binance
        self._cachicamo = cachicamo
        self._sms = sms
        self._browser = browser

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

    async def _advance_to_sending(
        self,
        order: P2POrder,
        *,
        actor: AuditActor = AuditActor.WORKER,
    ) -> None:
        """Move a fresh buy order to ``SENDING_PAYMENT``.

        The shared lifecycle does not allow a direct jump from ``PENDING`` to
        ``SENDING_PAYMENT``, so the order walks through the payment-detection and
        verification states with explicit audit messages.  This keeps a single
        state machine for both directions of the trade.

        Args:
            order: Order in ``PENDING``.
            actor: Actor responsible for the transitions.
        """
        if order.status == OrderStatus.PENDING:
            await self._transition(
                order,
                OrderStatus.PAYMENT_DETECTED,
                actor,
                AuditEvent.STATUS_CHANGED,
                "Buy order accepted; payout intent detected",
            )
        if order.status == OrderStatus.PAYMENT_DETECTED:
            await self._transition(
                order,
                OrderStatus.VERIFYING_CACHICAMO,
                actor,
                AuditEvent.STATUS_CHANGED,
                "Buy order passed risk checks; preparing payout",
            )
        if order.status == OrderStatus.VERIFYING_CACHICAMO:
            await self._transition(
                order,
                OrderStatus.SENDING_PAYMENT,
                actor,
                AuditEvent.STATUS_CHANGED,
                "Sending mobile payment instruction",
            )

    async def _park(
        self,
        order: P2POrder,
        reason: str,
        *,
        event: AuditEvent,
        message: str,
        payload: Mapping[str, Any] | None = None,
        release_channels: Mapping[str, Decimal] | None = None,
        actor: AuditActor = AuditActor.WORKER,
    ) -> P2POrder:
        """Move an order to ``MANUAL_REVIEW`` with a machine-readable reason.

        The BUY path now reserves only at B2, after every park decision, so
        the gate-off and extraction parks pass no release at all.  When
        ``release_channels`` is supplied each ``channel -> amount`` pair is
        released inside the same transaction, so the ledger and the state
        transition commit together.

        Args:
            order: Order being parked.
            reason: Machine-readable review reason.
            event: Audit event type for the transition.
            message: Human readable audit message.
            payload: Optional extra audit payload.
            release_channels: Optional channel amounts to release.
            actor: Actor responsible for the transition.

        Returns:
            The persisted order.
        """
        if release_channels is not None and order.status.can_transition_to(
            OrderStatus.MANUAL_REVIEW
        ):
            policy = RiskPolicy(self._session, self._settings)
            for currency, amount in release_channels.items():
                await policy.release(currency=currency, amount_ves=amount)
        order.manual_review_reason = reason
        body: dict[str, Any] = {"reason": reason}
        if payload:
            body.update(payload)
        await self._transition(
            order,
            OrderStatus.MANUAL_REVIEW,
            actor,
            event,
            message,
            payload=body,
        )
        await self._session.commit()
        return order

    async def execute(
        self,
        order_number: str,
        *,
        destination_phone: str | None = None,
        operator_approved: bool = False,
    ) -> P2POrder:
        """Register a buy order, evaluate risk and dispatch the payout SMS.

        Args:
            order_number: Binance C2C order number.
            destination_phone: Reserved for the operator endpoint (PR 10); it is
                never used as the gateway destination or as payment data.
            operator_approved: Human override supplied only by the authenticated
                operator endpoint, never by the worker.

        Returns:
            The persisted order.

        Raises:
            DuplicateOrderError: If the order was already registered.
            InvalidStateTransitionError: If the provider order is not a BUY.
        """
        _ = destination_phone
        async with self._clients() as (binance, _cachicamo, sms):
            await self._ensure_order_idempotent(order_number)
            detail = await binance.get_order_detail(order_number)
            if detail.trade_type is not OrderType.BUY:
                raise InvalidStateTransitionError(
                    f"Order {order_number} is not a BUY order",
                    code="direction_mismatch",
                    details={"order_type": detail.trade_type.value},
                )
            if detail.status != _PENDING_PAYMENT_STATUS:
                raise InvalidStateTransitionError(
                    f"Order {order_number} is no longer awaiting payment",
                    code="external_status_not_pending_payment",
                    details={"status": detail.status},
                )
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
            # B1: the order number is the durable idempotency anchor.
            await self._session.commit()
            policy = RiskPolicy(self._session, self._settings)
            shared = await policy.check_shared(
                amount_ves=order.amount_fiat,
                amount_asset=order.amount_asset,
            )
            order.risk_checked = True
            if not shared.allowed:
                return await self._park(
                    order,
                    shared.reason or "risk_limit_exceeded",
                    event=AuditEvent.RISK_BLOCKED,
                    message=shared.reason or "Risk limit exceeded",
                    payload={"limits": shared.limits},
                )
            return await self._dispatch(
                order, sms=sms, operator_approved=operator_approved
            )

    async def _dispatch(
        self,
        order: P2POrder,
        *,
        sms: SmsGatewayClient,
        operator_approved: bool,
        actor: AuditActor = AuditActor.WORKER,
    ) -> P2POrder:
        """Run extraction, breakers, durable dispatch and outcome recording.

        Args:
            order: A persisted order that has not been dispatched yet.
            sms: Gateway client or compatible double.
            operator_approved: Human override for the operator gate.
            actor: Actor responsible for the emitted audits.

        Returns:
            The persisted order after dispatch.

        Raises:
            InvalidStateTransitionError: If the order is not in a legal
                pre-dispatch status (a stale second request is a no-op when the
                order is already ``SENDING_PAYMENT``).
        """
        if order.status is OrderStatus.SENDING_PAYMENT:
            logger.info(
                "Dispatch skipped for {}: already SENDING_PAYMENT",
                order.order_number,
            )
            return order
        if order.status not in _PRE_DISPATCH_STATUSES:
            raise InvalidStateTransitionError(
                f"Order {order.order_number} cannot be dispatched from "
                f"{order.status.value}",
                code="invalid_dispatch_status",
                details={"status": order.status.value},
            )

        instruction = await self._extract(order, actor=actor)
        if instruction is None:
            return order
        phone = national_digits(instruction.phone)
        cedula = cedula_digits(instruction.id_number)

        policy = RiskPolicy(self._session, self._settings)
        shared = await policy.check_shared(
            amount_ves=instruction.amount_ves,
            amount_asset=order.amount_asset,
        )
        if not shared.allowed:
            return await self._park(
                order,
                shared.reason or "risk_limit_exceeded",
                event=AuditEvent.RISK_BLOCKED,
                message=shared.reason or "Risk limit exceeded",
                payload={"limits": shared.limits},
                actor=actor,
            )
        assessment = await policy.assess_buy(
            amount_ves=instruction.amount_ves,
            amount_asset=order.amount_asset,
            price=order.price,
            bank_value=instruction.bank_numeric_code,
        )
        order.risk_checked = True
        if not assessment.allowed:
            return await self._park(
                order,
                assessment.reason or "risk_limit_exceeded",
                event=AuditEvent.RISK_BLOCKED,
                message=assessment.reason or "Risk limit exceeded",
                payload={"limits": assessment.limits},
                actor=actor,
            )
        if not self._settings.buy_auto_pay_enabled and not operator_approved:
            return await self._park(
                order,
                _AWAITING_OPERATOR,
                event=AuditEvent.RISK_BLOCKED,
                message="Awaiting operator approval",
                actor=actor,
            )

        profile = resolve_bank(instruction.bank_numeric_code)
        if profile is None:  # pragma: no cover - assess_buy already resolved it
            return await self._park(
                order,
                "unsupported_destination_bank",
                event=AuditEvent.RISK_BLOCKED,
                message="Unsupported destination bank",
                actor=actor,
            )
        profile = apply_short_code_overrides(
            profile, self._short_code_overrides(profile.bank)
        )
        try:
            command = render_bank_command(
                profile,
                phone=phone,
                cedula=cedula,
                amount=instruction.amount_ves,
            )
        except BankSmsFormatError as exc:
            return await self._park(
                order,
                _PAYMENT_DATA_UNAVAILABLE,
                event=AuditEvent.EXTERNAL_ERROR,
                message="Payment data could not be rendered",
                payload={"error": exc.code},
                actor=actor,
            )

        # B2: reserve both channels and commit the attempt identity before the call.
        await policy.reserve_buy(amount_ves=instruction.amount_ves)
        order.bank_code = profile.bank
        order.bank_short_code = profile.short_codes[0]
        order.counterparty_phone = phone
        order.counterparty_id_number = cedula
        order.idempotency_key = f"{order.order_number}:payout"
        order.manual_review_reason = None
        await self._advance_to_sending(order, actor=actor)
        await self._session.commit()

        result = await dispatch_bank_command(
            sms,
            short_codes=profile.short_codes,
            message=command,
            order_number=order.order_number,
        )
        if result.fate is DispatchFate.DELIVERED:
            return await self._record_sent(order, result, phone, actor=actor)
        if result.fate is DispatchFate.PERMANENT:
            return await self._record_permanent(
                order, result, instruction.amount_ves, actor=actor
            )
        return await self._record_unknown(order, result, actor=actor)

    async def _extract(
        self,
        order: P2POrder,
        *,
        actor: AuditActor = AuditActor.WORKER,
    ) -> PaymentInstruction | None:
        """Extract the payment instruction through the browser port.

        Incomplete data, a missing browser and a port failure all park the
        order in ``MANUAL_REVIEW`` with ``payment_data_unavailable`` and never
        dispatch.

        Args:
            order: Order being dispatched.
            actor: Actor responsible for the emitted audits.

        Returns:
            The complete instruction, or ``None`` when the order was parked.
        """
        if self._browser is None:
            await self._park(
                order,
                _PAYMENT_DATA_UNAVAILABLE,
                event=AuditEvent.STATUS_CHANGED,
                message="No browser is available to extract payment data",
                actor=actor,
            )
            return None
        try:
            instruction = await self._browser.extract_payment_instruction(
                order.order_number
            )
        except LiquidVEException as exc:
            await self._park(
                order,
                _PAYMENT_DATA_UNAVAILABLE,
                event=AuditEvent.EXTERNAL_ERROR,
                message="Payment data extraction failed",
                payload={"error": exc.code},
                actor=actor,
            )
            return None
        if not (
            instruction.bank_numeric_code
            and instruction.phone
            and instruction.id_number
            and instruction.amount_ves > 0
        ):
            await self._park(
                order,
                _PAYMENT_DATA_UNAVAILABLE,
                event=AuditEvent.STATUS_CHANGED,
                message="Payment data is incomplete",
                actor=actor,
            )
            return None
        try:
            national_digits(instruction.phone)
            cedula_digits(instruction.id_number)
        except BankSmsFormatError:
            await self._park(
                order,
                _PAYMENT_DATA_UNAVAILABLE,
                event=AuditEvent.STATUS_CHANGED,
                message="Payment data is incomplete",
                actor=actor,
            )
            return None
        return instruction

    async def _record_sent(
        self,
        order: P2POrder,
        result: DispatchResult,
        phone: str,
        *,
        actor: AuditActor = AuditActor.WORKER,
    ) -> P2POrder:
        """Commit B4 telemetry for a delivered command.

        Args:
            order: Dispatched order.
            result: Delivered dispatch result.
            phone: Counterparty phone, masked in the audit payload.
            actor: Actor responsible for the audit.

        Returns:
            The persisted order.
        """
        order.bank_short_code = result.short_code
        order.sms_message_id = result.message_id
        order.sms_sent_at = result.sent_at or datetime.now(timezone.utc)
        await self._audit(
            order,
            AuditEvent.SMS_SENT,
            actor,
            "Payout instruction sent",
            payload={
                "bank_short_code": result.short_code,
                "phone_masked": mask_phone(phone),
                "provider_message_id": result.message_id,
            },
        )
        await self._session.commit()
        logger.info("Buy order {} payout instruction accepted", order.order_number)
        return order

    async def _record_permanent(
        self,
        order: P2POrder,
        result: DispatchResult,
        amount_ves: Decimal,
        *,
        actor: AuditActor = AuditActor.WORKER,
    ) -> P2POrder:
        """Commit B5: release both reservations and park in ``MANUAL_REVIEW``.

        A dispatched BUY charged both the shared ``VES`` and the ``VES_PAY``
        channel, so both are released.  The releases and the transition commit
        atomically and the transition is guarded by the legal
        ``SENDING_PAYMENT -> MANUAL_REVIEW`` edge, so it cannot run twice.

        Args:
            order: Dispatched order.
            result: Permanent dispatch result.
            amount_ves: Reserved amount to release from both channels.
            actor: Actor responsible for the transition.

        Returns:
            The persisted order.
        """
        if not order.status.can_transition_to(OrderStatus.MANUAL_REVIEW):
            return order
        order.bank_short_code = result.short_code
        policy = RiskPolicy(self._session, self._settings)
        released_ves = await policy.release(
            currency=LEDGER_VES, amount_ves=amount_ves
        )
        released_ves_pay = await policy.release(
            currency=LEDGER_VES_PAY, amount_ves=amount_ves
        )
        order.manual_review_reason = _SMS_DISPATCH_FAILED
        await self._transition(
            order,
            OrderStatus.MANUAL_REVIEW,
            actor,
            AuditEvent.EXTERNAL_ERROR,
            "SMS dispatch failed permanently; reservations released",
            payload={
                "reason": _SMS_DISPATCH_FAILED,
                "error": result.error_code,
                "bank_short_code": result.short_code,
                "released_ves": str(released_ves),
                "released_ves_pay": str(released_ves_pay),
                "amount_ves": str(amount_ves),
            },
        )
        await self._session.commit()
        return order

    async def _record_unknown(
        self,
        order: P2POrder,
        result: DispatchResult,
        *,
        actor: AuditActor = AuditActor.WORKER,
    ) -> P2POrder:
        """Commit B3: record an unknown fate without retrying.

        Args:
            order: Dispatched order.
            result: Unknown dispatch result.
            actor: Actor responsible for the audit.

        Returns:
            The persisted order, still in ``SENDING_PAYMENT``.
        """
        await self._audit(
            order,
            AuditEvent.EXTERNAL_ERROR,
            actor,
            "SMS dispatch outcome unknown; order remains in SENDING_PAYMENT",
            payload={
                "reason": "sms_dispatch_unknown",
                "error": result.error_code,
                "bank_short_code": result.short_code,
            },
        )
        await self._session.commit()
        return order

    async def sweep_timeouts(self) -> int:
        """Flag stale ``SENDING_PAYMENT`` windows without acting on the money.

        A past-window order is audited once with
        ``reason=sms_confirmation_timeout``.  The status, the reservation and
        the already-committed attempt identity are left untouched: the command
        is never resent and the ``VES_PAY`` budget is never released (R-BUY-010).

        Returns:
            The number of orders flagged by this sweep.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(
            seconds=self._settings.sms_confirmation_timeout_seconds
        )
        last_dispatch = func.coalesce(P2POrder.sms_sent_at, P2POrder.updated_at)
        result = await self._session.execute(
            select(P2POrder).where(
                P2POrder.status == OrderStatus.SENDING_PAYMENT,
                last_dispatch < cutoff,
            )
        )
        flagged = 0
        for order in result.scalars().all():
            prior = await self._session.execute(
                select(AuditLog).where(
                    AuditLog.order_id == order.id,
                    AuditLog.event == AuditEvent.EXTERNAL_ERROR,
                )
            )
            already_flagged = any(
                (entry.payload or {}).get("reason") == _SMS_CONFIRMATION_TIMEOUT
                for entry in prior.scalars()
            )
            if already_flagged:
                continue
            await self._audit(
                order,
                AuditEvent.EXTERNAL_ERROR,
                AuditActor.WORKER,
                "No bank reply inside the confirmation window; review required",
                payload={
                    "reason": _SMS_CONFIRMATION_TIMEOUT,
                    "status": order.status.value,
                },
            )
            await self._session.commit()
            flagged += 1
        return flagged

    def _profile_for_sender(self, sender: str) -> BankSmsProfile | None:
        """Resolve an inbound sender to a profile, honouring setting overrides.

        Registry short codes resolve first; when the operator configures extra
        short codes through settings, those are matched too.  An unknown sender
        yields ``None`` so it can never confirm anything.

        Args:
            sender: SMS sender short code.

        Returns:
            The matching profile with overrides applied, or ``None``.
        """
        cleaned = sender.strip() if isinstance(sender, str) else ""
        if not cleaned:
            return None
        profile = profile_for_sender(cleaned)
        if profile is not None:
            return apply_short_code_overrides(
                profile, self._short_code_overrides(profile.bank)
            )
        for candidate in BANK_SMS_PROFILES.values():
            overridden = apply_short_code_overrides(
                candidate, self._short_code_overrides(candidate.bank)
            )
            if cleaned in overridden.short_codes:
                return overridden
        return None

    def _short_code_overrides(self, bank: BankCode) -> list[str]:
        """Return the configured short-code overrides for a bank.

        Args:
            bank: Destination bank.

        Returns:
            The ordered override list, empty for banks without a setting.
        """
        if bank is BankCode.BANCO_DE_VENEZUELA:
            return list(self._settings.bank_short_codes_bdv)
        if bank is BankCode.BANESCO:
            return list(self._settings.bank_short_codes_banesco)
        return []

    async def resume(
        self,
        order_number: str,
        *,
        operator_approved: bool = False,
    ) -> P2POrder:
        """Resume processing without ever repeating a money effect.

        An order in ``SENDING_PAYMENT`` is a no-op: nothing is sent, nothing is
        reserved and the persisted instruction is not re-extracted.  Terminal
        orders and an unapproved ``MANUAL_REVIEW`` order are refused with a
        typed duplicate error.

        Args:
            order_number: Provider order number.
            operator_approved: Human override supplied only by the operator
                endpoint; the worker never sets it.

        Returns:
            The persisted order for a ``SENDING_PAYMENT`` resume.

        Raises:
            DuplicateOrderError: If the order is terminal or parked without
                operator approval.
        """
        order = await self._get_order(order_number)
        if order.status is OrderStatus.SENDING_PAYMENT:
            await self._audit(
                order,
                AuditEvent.STATUS_CHANGED,
                AuditActor.WORKER,
                "Resume while SENDING_PAYMENT: no dispatch",
                payload={"resumed": True, "bank_short_code": order.bank_short_code},
            )
            await self._session.commit()
            return order
        if order.status in (
            OrderStatus.PENDING,
            OrderStatus.PAYMENT_DETECTED,
            OrderStatus.VERIFYING_CACHICAMO,
        ):
            async with self._clients() as (_binance, _cachicamo, sms):
                return await self._dispatch(
                    order, sms=sms, operator_approved=operator_approved
                )
        await self._audit(
            order,
            AuditEvent.IDEMPOTENCY_HIT,
            AuditActor.SYSTEM,
            "Duplicate processing refused",
            payload={"status": order.status.value},
        )
        await self._session.commit()
        raise DuplicateOrderError(
            f"Order {order_number} was already processed",
            details={"order_number": order_number, "status": order.status.value},
        )

    async def handle_incoming_sms(self, sms: IncomingSms) -> SmsConfirmation:
        """Confirm a poller-read SMS through the shared confirmation path.

        The webhook and the Android-gateway poller both funnel through
        :meth:`confirm_bank_payment`, so a message that already arrived as a
        webhook is deduplicated here and both callers share one outcome
        vocabulary.

        Args:
            sms: Inbound SMS read from the gateway.

        Returns:
            The confirmation outcome for this message.
        """
        return await self.confirm_bank_payment(
            message_id=sms.message_id,
            sender=sms.sender,
            body=sms.body,
            received_at=sms.received_at,
        )

    async def confirm_bank_payment(
        self,
        *,
        message_id: str,
        sender: str,
        body: str,
        received_at: datetime,
    ) -> SmsConfirmation:
        """Correlate one inbound bank reply and confirm at most one order.

        The replay anchor (B0′) is committed before anything money-adjacent
        happens.  A processed replay writes nothing; an unprocessed row is
        re-processed at-least-once and marked processed at the end.

        Args:
            message_id: Provider message identifier (dedupe key).
            sender: Bank short code that sent the reply.
            body: Raw SMS body.
            received_at: Provider timestamp used to bound the window.

        Returns:
            The confirmation outcome; never raises for an ordinary no-match,
            ambiguity, failure or duplicate reference.
        """
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        fresh = await self._claim_sms_event(message_id, sender, body, received_at)
        if not fresh:
            return SmsConfirmation(
                outcome="no_match",
                reason="already_processed",
                duplicate=True,
            )
        try:
            confirmation = await self._process_bank_reply(
                message_id=message_id,
                sender=sender,
                body=body,
                received_at=received_at,
            )
        except Exception as exc:  # noqa: BLE001 - record and let the provider retry
            await self._finish_sms_event(
                message_id, error=f"{type(exc).__name__}: {exc}"
            )
            raise
        await self._finish_sms_event(message_id, error=None)
        return confirmation

    async def _claim_sms_event(
        self,
        message_id: str,
        sender: str,
        body: str,
        received_at: datetime,
    ) -> bool:
        """Commit the B0′ replay anchor before any money-adjacent side effect.

        Args:
            message_id: Provider message identifier.
            sender: Bank short code.
            body: Raw SMS body.
            received_at: Provider timestamp.

        Returns:
            ``True`` when this delivery must be processed (new row or an
            unprocessed replay); ``False`` for an already processed replay.
        """
        masked_sender = _mask_sender(sender)
        self._session.add(
            WebhookEvent(
                provider=_SMS_PROVIDER,
                external_event_id=message_id,
                payload_hash=hash_payload(body.encode("utf-8")),
                signature_valid=True,
                processed=False,
                payload={
                    "message_id": message_id,
                    "sender_masked": masked_sender,
                    "body": body,
                    "received_at": received_at.isoformat(),
                },
            )
        )
        self._session.add(
            AuditLog(
                order_id=None,
                event=AuditEvent.WEBHOOK_RECEIVED,
                actor=AuditActor.WEBHOOK,
                message="SMS webhook received",
                payload={
                    "provider": _SMS_PROVIDER,
                    "message_id": message_id,
                    "sender_masked": masked_sender,
                },
                request_id=request_id_var.get(),
            )
        )
        try:
            await self._session.flush()
            await self._session.commit()
            return True
        except IntegrityError:
            await self._session.rollback()
            result = await self._session.execute(
                select(WebhookEvent.processed).where(
                    WebhookEvent.provider == _SMS_PROVIDER,
                    WebhookEvent.external_event_id == message_id,
                )
            )
            processed = result.scalar_one_or_none()
            return processed is not True

    async def _finish_sms_event(self, message_id: str, *, error: str | None) -> None:
        """Mark the replay anchor processed, recording any processing error.

        Args:
            message_id: Provider message identifier.
            error: Optional error summary; ``None`` marks a clean processing.
        """
        result = await self._session.execute(
            select(WebhookEvent).where(
                WebhookEvent.provider == _SMS_PROVIDER,
                WebhookEvent.external_event_id == message_id,
            )
        )
        event = result.scalar_one_or_none()
        if event is None:
            return
        event.processed = error is None
        event.processing_error = error
        await self._session.commit()

    async def _process_bank_reply(
        self,
        *,
        message_id: str,
        sender: str,
        body: str,
        received_at: datetime,
    ) -> SmsConfirmation:
        """Resolve the profile, parse the reply and apply the exactly-one rule.

        Args:
            message_id: Provider message identifier.
            sender: Bank short code.
            body: Raw SMS body.
            received_at: Provider timestamp.

        Returns:
            The confirmation outcome for this reply.
        """
        profile = self._profile_for_sender(sender)
        if profile is None:
            await self._reject_webhook("unknown_sender", message_id)
            return SmsConfirmation(outcome="no_match", reason="unknown_sender")
        parsed = parse_bank_reply(body, profile)
        candidates = await self._match_candidates(
            profile.bank, parsed.amount, received_at
        )
        if not candidates:
            reason = "amount_mismatch" if parsed.amount is not None else "no_candidate"
            await self._reject_webhook(reason, message_id)
            return SmsConfirmation(outcome="no_match", reason=reason)
        if len(candidates) > 1:
            logger.error(
                "Bank reply {} matched {} candidates; operator alert required",
                message_id,
                len(candidates),
            )
            await self._reject_webhook(
                "ambiguous",
                message_id,
                payload={"candidate_count": len(candidates)},
            )
            return SmsConfirmation(
                outcome="ambiguous",
                reason="multiple_candidates",
            )
        order = candidates[0]
        if parsed.outcome is BankReplyOutcome.SUCCESS:
            return await self._confirm_success(
                order,
                parsed,
                profile,
                message_id=message_id,
                sender=sender,
            )
        reason = {
            BankReplyOutcome.FAILURE: _BANK_REJECTED_PAYMENT,
            BankReplyOutcome.UNPARSEABLE: _BANK_REPLY_UNPARSEABLE,
            BankReplyOutcome.AMBIGUOUS: _BANK_REPLY_AMBIGUOUS,
        }[parsed.outcome]
        await self._park(
            order,
            reason,
            event=AuditEvent.SMS_RECEIVED,
            message=f"Bank reply requires manual review: {parsed.outcome.value}",
            payload={
                "message_id": message_id,
                "sender_masked": _mask_sender(sender),
                "reply_outcome": parsed.outcome.value,
            },
            actor=AuditActor.WEBHOOK,
        )
        return SmsConfirmation(
            outcome="matched",
            order_number=order.order_number,
            reason=reason,
        )

    async def _match_candidates(
        self,
        bank: BankCode,
        amount: Decimal | None,
        received_at: datetime,
    ) -> list[P2POrder]:
        """Return zero, one or two candidate orders for a bank reply.

        The query is deliberately narrow: only BUY orders still waiting for a
        bank reply, of the same bank, without a reference or confirmation, whose
        dispatch falls inside the confirmation window.  ``limit(2)`` is enough
        to prove ambiguity without scanning the whole table.

        Args:
            bank: Bank of the sender profile.
            amount: Amount parsed from the reply, or ``None`` when the reply
                does not expose one (the candidate baseline then applies).
            received_at: Provider timestamp used as the window upper bound.

        Returns:
            Up to two matching orders, oldest first.
        """
        window_start = received_at - timedelta(
            seconds=self._settings.sms_confirmation_timeout_seconds
        )
        last_dispatch = func.coalesce(P2POrder.sms_sent_at, P2POrder.updated_at)
        conditions = [
            P2POrder.order_type == OrderType.BUY,
            P2POrder.status == OrderStatus.SENDING_PAYMENT,
            P2POrder.bank_code == bank,
            P2POrder.bank_reference.is_(None),
            P2POrder.bank_confirmed_at.is_(None),
            last_dispatch >= window_start,
            last_dispatch <= received_at,
        ]
        if amount is not None:
            conditions.append(P2POrder.amount_fiat == amount)
        result = await self._session.execute(
            select(P2POrder)
            .where(*conditions)
            .order_by(P2POrder.created_at.asc())
            .limit(2)
        )
        return list(result.scalars().all())

    async def _reject_webhook(
        self,
        reason: str,
        message_id: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Append a ``WEBHOOK_REJECTED`` audit entry for an unmatched reply.

        Args:
            reason: Machine-readable rejection reason.
            message_id: Provider message identifier.
            payload: Optional extra audit payload.
        """
        body: dict[str, Any] = {
            "provider": _SMS_PROVIDER,
            "message_id": message_id,
            "reason": reason,
        }
        if payload:
            body.update(payload)
        self._session.add(
            AuditLog(
                order_id=None,
                event=AuditEvent.WEBHOOK_REJECTED,
                actor=AuditActor.WEBHOOK,
                message=f"SMS webhook rejected: {reason}",
                payload=body,
                request_id=request_id_var.get(),
            )
        )
        await self._session.flush()

    async def _confirm_success(
        self,
        order: P2POrder,
        parsed: BankReplyParse,
        profile: BankSmsProfile,
        *,
        message_id: str,
        sender: str,
    ) -> SmsConfirmation:
        """Commit B6: one OUTGOING movement plus the order confirmation.

        ``verification_status`` stays ``NULL`` on purpose: the bank's own reply
        is the verification for this channel and the enum is Cachicamo
        vocabulary.  A UNIQUE ``bank_reference`` collision rolls back only this
        transaction (B0′ is already committed) and parks the order for review.

        Args:
            order: The single candidate order.
            parsed: A ``SUCCESS`` parse carrying exactly one reference.
            profile: Resolved bank profile.
            message_id: Provider message identifier.
            sender: Bank short code.

        Returns:
            ``confirmed`` on success, ``matched`` when the reference collided.
        """
        reference = parsed.reference or ""
        order_number = order.order_number
        masked_sender = _mask_sender(sender)
        try:
            self._session.add(
                BankTransaction(
                    order_id=order.id,
                    bank_reference=reference,
                    direction=BankTransactionDirection.OUTGOING,
                    bank_code=profile.bank,
                    amount=order.amount_fiat,
                    currency="VES",
                    verified=True,
                    verification_status=None,
                    raw_payload={
                        "source": _SMS_PROVIDER,
                        "message_id": message_id,
                        "sender_masked": masked_sender,
                    },
                )
            )
            order.bank_reference = reference
            order.bank_confirmed_at = datetime.now(timezone.utc)
            order.manual_review_reason = None
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            return await self._park_duplicate_reference(
                order_number, reference, message_id
            )
        await self._audit(
            order,
            AuditEvent.SMS_RECEIVED,
            AuditActor.WEBHOOK,
            "Bank reply confirmed the outgoing payment",
            payload={
                "message_id": message_id,
                "sender_masked": masked_sender,
                "reference_masked": mask_secret(reference, visible=4),
            },
        )
        await self._audit(
            order,
            AuditEvent.PAYMENT_VERIFIED,
            AuditActor.WEBHOOK,
            "Outgoing bank movement recorded from the bank reply",
            payload={
                "reference_masked": mask_secret(reference, visible=4),
                "amount_ves": str(order.amount_fiat),
            },
        )
        await self._session.commit()
        logger.info("Buy order {} bank payment confirmed", order_number)
        if self._browser is None:
            logger.warning(
                "Buy order {} confirmed but no browser is wired; close pending",
                order_number,
            )
            return SmsConfirmation(outcome="confirmed", order_number=order_number)
        if await self._close_order(order, reference):
            return SmsConfirmation(outcome="confirmed", order_number=order_number)
        return SmsConfirmation(
            outcome="matched",
            order_number=order_number,
            reason=_BINANCE_CLOSE_FAILED,
        )

    async def _close_order(self, order: P2POrder, reference: str) -> bool:
        """Attempt the single Binance close and persist its outcome.

        B6 (the financial anchor) is already committed when this runs, so a
        crash leaves the order in the queryable "confirmed, close pending"
        state.  Exactly one notification is attempted: the confirmation path's
        four independent gates guarantee this method is unreachable for an
        already closed order, and there is no retry or fallback for an unknown
        close outcome.

        Args:
            order: Order confirmed by the bank reply.
            reference: Bank reference posted to the seller.

        Returns:
            ``True`` when the order reached the terminal status; ``False`` when
            the close failed and the order was parked in ``MANUAL_REVIEW``.
        """
        browser = self._browser
        if browser is None:  # pragma: no cover - the caller guarantees a browser
            return False
        try:
            await browser.notify_paid(order.order_number, reference)
        except Exception as exc:  # noqa: BLE001 - any close failure must fail closed
            order.manual_review_reason = _BINANCE_CLOSE_FAILED
            await self._transition(
                order,
                OrderStatus.MANUAL_REVIEW,
                AuditActor.WORKER,
                AuditEvent.EXTERNAL_ERROR,
                "Bank payment confirmed but the Binance close failed",
                payload={
                    "reason": _BINANCE_CLOSE_FAILED,
                    "reference_masked": mask_secret(reference, visible=4),
                    "error": getattr(exc, "code", type(exc).__name__),
                },
            )
            await self._session.commit()
            logger.error(
                "Buy order {} confirmed but the Binance close failed",
                order.order_number,
            )
            return False
        payload = {
            "reference_masked": mask_secret(reference, visible=4),
            "amount_ves": str(order.amount_fiat),
        }
        await self._transition(
            order,
            OrderStatus.AWAITING_RELEASE,
            AuditActor.WORKER,
            AuditEvent.STATUS_CHANGED,
            "Bank payment confirmed; awaiting the counterparty release",
            payload=payload,
        )
        await self._transition(
            order,
            OrderStatus.COMPLETED,
            AuditActor.WORKER,
            AuditEvent.STATUS_CHANGED,
            "Counterparty release observed after the seller was notified",
            payload=payload,
        )
        await self._session.commit()
        logger.info("Buy order {} completed after Binance close", order.order_number)
        return True

    async def _park_duplicate_reference(
        self,
        order_number: str,
        reference: str,
        message_id: str,
    ) -> SmsConfirmation:
        """Move an order to review after a UNIQUE reference collision.

        Args:
            order_number: Order whose confirmation collided.
            reference: Colliding bank reference.
            message_id: Provider message identifier.

        Returns:
            ``matched`` with the ``duplicate_bank_reference`` reason.
        """
        order = await self._get_order(order_number)
        order.manual_review_reason = _DUPLICATE_BANK_REFERENCE
        await self._transition(
            order,
            OrderStatus.MANUAL_REVIEW,
            AuditActor.WEBHOOK,
            AuditEvent.STATUS_CHANGED,
            "Duplicate bank reference; manual review required",
            payload={
                "reason": _DUPLICATE_BANK_REFERENCE,
                "message_id": message_id,
                "reference_masked": mask_secret(reference, visible=4),
            },
        )
        await self._session.commit()
        logger.warning(
            "Buy order {} confirmed with a duplicate bank reference", order_number
        )
        return SmsConfirmation(
            outcome="matched",
            order_number=order_number,
            reason=_DUPLICATE_BANK_REFERENCE,
        )


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

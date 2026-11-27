"""Run a single human-authorized Banesco payment and retain bank evidence."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    ExternalServiceError,
    PaymentDataUnavailable,
    ReceiptAmountMismatch,
)
from app.core.logging import get_logger
from app.domain.banesco import (
    BANESCO_DESTINATION_BANK_OPTIONS,
    BanescoPaymentTarget,
    project_banesco_document,
    project_phone,
)
from app.domain.enums import (
    AuditActor,
    AuditEvent,
    BankCode,
    BankTransactionDirection,
    OrderStatus,
    OrderType,
    PaymentVerificationStatus,
)
from app.domain.ports import PaymentInstruction
from app.infrastructure.clients.banesco_browser import BanescoBrowser, BanescoReceipt
from app.infrastructure.database import session_scope
from app.infrastructure.models import BankTransaction, P2POrder
from app.infrastructure.receipts import store_receipt
from app.services.buy_flow import record_status
from app.services.risk_policy import RiskPolicy

logger = get_logger("banesco_payment")
_SLOT = 1
_UNKNOWN_OUTCOME = "payment_outcome_unknown"


class AuthorizationNotifier(Protocol):
    async def request_authorization(self, order_number: str, url: str) -> None: ...


@dataclass(slots=True)
class PendingAuthorization:
    token_digest: bytes
    order_number: str
    target: BanescoPaymentTarget
    link_deadline: float
    opened: asyncio.Event
    answer: asyncio.Future[str]
    entry_deadline: float | None = None


class BanescoPayments:
    def __init__(
        self,
        settings: Settings,
        notifier: AuthorizationNotifier,
        *,
        browser=BanescoBrowser,
    ) -> None:
        self._settings = settings
        self._notifier = notifier
        self._browser = browser
        self._task: asyncio.Task[None] | None = None
        self._starting = asyncio.Lock()
        self._pending: PendingAuthorization | None = None

    async def reconcile_startup(self) -> None:
        """Park previous attempts, including older purchases in flight."""
        async with session_scope() as session:
            interrupted = await session.scalars(
                select(P2POrder).where(
                    P2POrder.order_type == OrderType.BUY,
                    P2POrder.status == OrderStatus.SENDING_PAYMENT,
                )
            )
            for order in interrupted:
                self._park(session, order, "payment_interrupted")

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def start(
        self, session: AsyncSession, order: P2POrder, instruction: PaymentInstruction
    ) -> None:
        async with self._starting:
            if self._task is not None and not self._task.done():
                raise ConflictError("Another Banesco payment is in progress")
            if (
                order.order_type is not OrderType.BUY
                or order.status is not OrderStatus.PENDING
                or order.idempotency_key is not None
            ):
                raise ConflictError("The order cannot start a Banesco payment")
            target = self._target(order, instruction)
            policy = RiskPolicy(session, self._settings)
            risk = await policy.check_shared(
                amount_ves=order.amount_fiat, amount_asset=order.amount_asset
            )
            if risk.allowed:
                risk = await policy.assess_buy(
                    amount_ves=order.amount_fiat,
                    amount_asset=order.amount_asset,
                    price=order.price,
                    bank_value=target.destination_bank_code,
                )
            if not risk.allowed:
                self._park(
                    session,
                    order,
                    risk.reason or "risk_limit_exceeded",
                    event=AuditEvent.RISK_BLOCKED,
                )
                await session.commit()
                return
            try:
                await policy.reserve_buy(amount_ves=order.amount_fiat)
                order.risk_checked = True
                order.payment_slot = _SLOT
                order.idempotency_key = f"{order.order_number}:payment"
                order.manual_review_reason = None
                order.bank_code = BankCode.from_aliases(target.destination_bank_option)
                for status in (
                    OrderStatus.PAYMENT_DETECTED,
                    OrderStatus.VERIFYING_CACHICAMO,
                    OrderStatus.SENDING_PAYMENT,
                ):
                    record_status(
                        session,
                        order,
                        status,
                        AuditActor.OPERATOR,
                        AuditEvent.PAYMENT_INITIATED,
                        "Manual Banesco payment approved",
                    )
                await session.commit()
            except (IntegrityError, StaleDataError) as exc:
                await session.rollback()
                raise ConflictError(
                    "The order or payment slot is already in use"
                ) from exc
            self._task = asyncio.create_task(
                self._run(order.id, order.order_number, target), name="banesco-payment"
            )

    def _target(
        self, order: P2POrder, instruction: PaymentInstruction
    ) -> BanescoPaymentTarget:
        if (
            instruction.order_number != order.order_number
            or instruction.amount_ves != order.amount_fiat
            or instruction.amount_ves.quantize(Decimal("0.01"))
            != instruction.amount_ves
        ):
            raise PaymentDataUnavailable(
                "Bank instructions do not match the exact order amount"
            )
        bank = BANESCO_DESTINATION_BANK_OPTIONS.get(instruction.bank_numeric_code)
        if bank is None:
            raise PaymentDataUnavailable("Destination bank is not supported by Banesco")
        phone_prefix, phone_rest = project_phone(instruction.phone)
        id_type, id_digits = project_banesco_document(instruction.id_number)
        return BanescoPaymentTarget(
            phone_prefix=phone_prefix,
            phone_rest=phone_rest,
            id_type=id_type,
            id_digits=id_digits,
            destination_bank_code=bank.numeric_code,
            destination_bank_option=bank.label,
            amount_ves=instruction.amount_ves,
            concept=self._settings.banesco_payment_concept,
        )

    def pending(self, token: str) -> PendingAuthorization | None:
        pending = self._pending
        if pending is None or pending.answer.done():
            return None
        deadline = (
            pending.link_deadline
            if pending.entry_deadline is None
            else pending.entry_deadline
        )
        if deadline <= time.monotonic() or not hmac.compare_digest(
            pending.token_digest, hashlib.sha256(token.encode()).digest()
        ):
            return None
        return pending

    def open_authorization(self, token: str) -> PendingAuthorization | None:
        pending = self.pending(token)
        if pending is not None and pending.entry_deadline is None:
            pending.entry_deadline = (
                time.monotonic() + self._settings.telegram_clave_timeout_seconds
            )
            pending.opened.set()
        return pending

    def authorize(self, token: str, code: str) -> bool:
        pending = self.pending(token)
        if pending is None or pending.entry_deadline is None:
            return False
        pending.answer.set_result(code)
        return True

    async def _run(
        self, order_id: int, order_number: str, target: BanescoPaymentTarget
    ) -> None:
        timeout_reason = _UNKNOWN_OUTCOME
        try:
            async with self._browser(self._settings) as bank:
                await bank.fill_and_submit_payment(target)
                token = secrets.token_urlsafe(32)
                pending = PendingAuthorization(
                    token_digest=hashlib.sha256(token.encode()).digest(),
                    order_number=order_number,
                    target=target,
                    link_deadline=time.monotonic()
                    + self._settings.operator_link_timeout_seconds,
                    opened=asyncio.Event(),
                    answer=asyncio.get_running_loop().create_future(),
                )
                self._pending = pending
                origin = self._settings.operator_public_url.rstrip("/")
                prefix = self._settings.api_v1_prefix
                url = f"{origin}{prefix}/orders/buy/authorize#{token}"
                timeout_reason = "telegram_notice_failed"
                await asyncio.wait_for(
                    self._notifier.request_authorization(order_number, url),
                    timeout=min(
                        self._settings.http_timeout_seconds,
                        self._settings.operator_link_timeout_seconds,
                    ),
                )
                timeout_reason = "operator_link_timeout"
                if not pending.opened.is_set():
                    await asyncio.wait_for(
                        pending.opened.wait(),
                        timeout=max(0, pending.link_deadline - time.monotonic()),
                    )
                timeout_reason = "operator_entry_timeout"
                assert pending.entry_deadline is not None
                code = await asyncio.wait_for(
                    pending.answer,
                    timeout=max(0, pending.entry_deadline - time.monotonic()),
                )
                self._pending = None
                del pending
                timeout_reason = _UNKNOWN_OUTCOME
                try:
                    browser_deadline = (
                        time.monotonic()
                        + self._settings.banesco_browser_timeout_ms / 1000
                    )
                    await bank.authorize_payment(code, browser_deadline)
                finally:
                    del code
                receipt = await bank.read_receipt()
                await self._record_receipt(order_id, target.amount_ves, receipt)
        except asyncio.CancelledError:
            await self._record_review(order_id, "payment_interrupted")
            raise
        except asyncio.TimeoutError:
            await self._record_review(order_id, timeout_reason)
        except ReceiptAmountMismatch:
            await self._record_review(order_id, "receipt_amount_mismatch")
        except ExternalServiceError as exc:
            reason = (
                "telegram_notice_failed"
                if exc.service == "telegram"
                else _UNKNOWN_OUTCOME
            )
            await self._record_review(order_id, reason)
        except Exception:
            await self._record_review(order_id, _UNKNOWN_OUTCOME)
        finally:
            if self._pending is not None:
                self._pending.answer.cancel()
                self._pending = None

    @staticmethod
    def _park(
        session: AsyncSession,
        order: P2POrder,
        reason: str,
        *,
        event: AuditEvent = AuditEvent.EXTERNAL_ERROR,
    ) -> None:
        order.payment_slot = None
        order.manual_review_reason = reason
        record_status(
            session,
            order,
            OrderStatus.MANUAL_REVIEW,
            AuditActor.SYSTEM,
            event,
            reason,
        )

    async def _record_review(self, order_id: int, reason: str) -> None:
        try:
            async with session_scope() as session:
                order = await session.get(P2POrder, order_id)
                if order is not None and order.status is OrderStatus.SENDING_PAYMENT:
                    self._park(session, order, reason)
        except Exception as exc:
            logger.error(
                "Could not record Banesco outcome for order ID {} ({})",
                order_id,
                type(exc).__name__,
            )

    async def _record_receipt(
        self, order_id: int, expected_amount: Decimal, receipt: BanescoReceipt
    ) -> None:
        digest = store_receipt(self._settings.receipt_storage_dir, expected_amount, receipt)
        async with session_scope() as session:
            order = await session.get(P2POrder, order_id)
            if order is None or order.status is not OrderStatus.SENDING_PAYMENT:
                raise ConflictError("The payment attempt is no longer active")
            order.bank_confirmed_at = datetime.now(timezone.utc)
            order.bank_reference = receipt.reference
            order.receipt_sha256 = digest
            order.payment_slot = None
            session.add(
                BankTransaction(
                    order_id=order.id,
                    bank_reference=receipt.reference,
                    direction=BankTransactionDirection.OUTGOING,
                    bank_code=BankCode.BANESCO,
                    amount=receipt.amount,
                    verified=True,
                    verification_status=PaymentVerificationStatus.VERIFIED,
                    raw_payload={"source": "banesco_online", "receipt_sha256": digest},
                )
            )
            record_status(
                session,
                order,
                OrderStatus.PAYMENT_CONFIRMED,
                AuditActor.SYSTEM,
                AuditEvent.PAYMENT_CAPTURED,
                "Banesco receipt verified",
            )

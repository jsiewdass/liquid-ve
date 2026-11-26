"""Shared risk policy and daily-volume ledger accounting.

Both trade directions used to carry a private copy of the ledger read/write
and the limit assessment.  This module owns the single implementation:

* :data:`LEDGER_VES` is the shared VES spend channel used by both directions.
* :data:`LEDGER_VES_PAY` is the outgoing Pago Movil channel (BUY only).
* :meth:`RiskPolicy.assess_shared` keeps the legacy SELL semantics of
  check-and-increment in one call, so extraction cannot change behaviour.
* :meth:`RiskPolicy.check_shared` is the read-only shared-cap projection used
  by the BUY path, which must reserve nothing until B2.
* :meth:`RiskPolicy.assess_buy` runs the ordered BUY circuit breakers and is
  read-only; :meth:`RiskPolicy.reserve_buy` performs the double-cap
  reservation at B2, charging both ``VES`` and ``VES_PAY``.
* :meth:`RiskPolicy.reserve` / :meth:`RiskPolicy.release` move volume on a
  single channel and are the primitives the BUY breakers build on.

Row locking is dialect-specific and preserved verbatim: PostgreSQL uses
``SELECT ... FOR UPDATE`` (creating the row inside the same transaction when
missing) while SQLite relies on ``INSERT ... ON CONFLICT DO NOTHING`` followed
by a ``SELECT``, serialised by SQLite's single-writer lock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import RiskLimitExceededError
from app.domain.banks import resolve_bank
from app.domain.schemas import RiskAssessment
from app.infrastructure.models import DailyVolumeLedger

LEDGER_VES = "VES"
LEDGER_VES_PAY = "VES_PAY"
CHANNELS = frozenset({LEDGER_VES, LEDGER_VES_PAY})

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")
_PERCENT = Decimal("100")


def _ensure_decimal(value: Decimal, field_name: str) -> Decimal:
    """Reject float money inputs explicitly.

    Args:
        value: Candidate monetary value.
        field_name: Field name used in the error message.

    Returns:
        The value unchanged.

    Raises:
        ValueError: If a float was supplied.
    """
    if isinstance(value, float):
        raise ValueError(f"{field_name} must be a Decimal, not a float")
    return value


class RiskPolicy:
    """Evaluate and account daily-volume risk for one session.

    The session is supplied by the caller and owns the transaction, so a
    reservation is only durable when the caller commits the surrounding unit
    of work (the BUY boundary map relies on this).
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """Initialize the policy.

        Args:
            session: Database session owned by the caller.
            settings: Active application settings.
        """
        self._session = session
        self._settings = settings

    def _dialect_name(self) -> str:
        """Return the active SQL dialect name.

        Returns:
            The dialect name, defaulting to ``sqlite`` when unbound.
        """
        try:
            return self._session.get_bind().dialect.name
        except Exception:  # noqa: BLE001 - defensive, reports sqlite semantics
            return "sqlite"

    async def load_or_create_ledger(self, *, currency: str) -> DailyVolumeLedger:
        """Load today's ledger for one channel, creating it atomically.

        On PostgreSQL the row is locked with ``SELECT ... FOR UPDATE``.  On
        SQLite an ``INSERT ... ON CONFLICT DO NOTHING`` guard makes creation
        race-free before the read.

        Args:
            currency: Ledger channel (see :data:`CHANNELS`).

        Returns:
            The ledger row for today and the requested channel.
        """
        today = datetime.now(timezone.utc).date()
        if self._dialect_name() == "postgresql":
            result = await self._session.execute(
                select(DailyVolumeLedger)
                .where(
                    DailyVolumeLedger.ledger_date == today,
                    DailyVolumeLedger.currency == currency,
                )
                .with_for_update()
            )
            ledger = result.scalar_one_or_none()
            if ledger is None:
                ledger = DailyVolumeLedger(
                    ledger_date=today,
                    currency=currency,
                    total_volume=_ZERO,
                    transaction_count=0,
                )
                self._session.add(ledger)
                await self._session.flush()
            return ledger
        statement = (
            sqlite_insert(DailyVolumeLedger)
            .values(
                ledger_date=today,
                currency=currency,
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
        await self._session.execute(statement)
        result = await self._session.execute(
            select(DailyVolumeLedger).where(
                DailyVolumeLedger.ledger_date == today,
                DailyVolumeLedger.currency == currency,
            )
        )
        return result.scalar_one()

    async def channel_total(self, *, currency: str) -> Decimal:
        """Read a channel's total without creating the ledger row.

        The BUY path evaluates its breakers before it is allowed to reserve
        anything, so a check must never materialise a ledger row (R-BUY-005:
        a parked order reserves no budget).

        Args:
            currency: Ledger channel to read.

        Returns:
            The channel total for today, or ``0.00`` when no row exists.

        Raises:
            ValueError: If ``currency`` is not a known channel.
        """
        self._require_channel(currency)
        today = datetime.now(timezone.utc).date()
        result = await self._session.execute(
            select(DailyVolumeLedger.total_volume).where(
                DailyVolumeLedger.ledger_date == today,
                DailyVolumeLedger.currency == currency,
            )
        )
        total = result.scalar_one_or_none()
        return total if total is not None else _ZERO

    async def check_channel_cap(
        self,
        *,
        currency: str,
        amount_ves: Decimal,
        limit: Decimal,
    ) -> RiskAssessment:
        """Project a channel total against ``limit`` without reserving it.

        Args:
            currency: Ledger channel to project.
            amount_ves: Amount that would be added in VES.
            limit: Channel cap for the day.

        Returns:
            The read-only projection assessment.
        """
        total = await self.channel_total(currency=currency)
        projected = (total + amount_ves).quantize(_CENT)
        allowed = projected <= limit
        return RiskAssessment(
            allowed=allowed,
            requires_manual_review=False if allowed else self._settings.manual_review_on_limit,
            reason=None,
            projected_daily_volume_ves=projected,
            limits={},
        )

    async def check_shared(
        self, *, amount_ves: Decimal, amount_asset: Decimal
    ) -> RiskAssessment:
        """Project the shared VES limits without reserving volume.

        This is the check-only counterpart of :meth:`assess_shared`: the BUY
        path uses it so a blocked order parks with no reservation, and a
        dispatched order is charged once at B2 through :meth:`reserve_buy`.

        Args:
            amount_ves: Transaction amount in VES.
            amount_asset: Transaction amount in the traded asset.  Accepted
                for a stable call signature; the shared limits are VES-only.

        Returns:
            The read-only risk assessment.
        """
        total = await self.channel_total(currency=LEDGER_VES)
        projected = (total + amount_ves).quantize(_CENT)
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
        return RiskAssessment(
            allowed=reason is None,
            requires_manual_review=False if reason is None else self._settings.manual_review_on_limit,
            reason=reason,
            projected_daily_volume_ves=projected,
            limits=limits,
        )

    async def assess_shared(
        self, *, amount_ves: Decimal, amount_asset: Decimal
    ) -> RiskAssessment:
        """Evaluate the shared VES limits, reserving volume when allowed.

        This is the exact check-then-increment used by the legacy sell flow:
        the per-transaction limit is checked first, then the projected daily
        total, and the ledger is only incremented on the allow path.

        Args:
            amount_ves: Transaction amount in VES.
            amount_asset: Transaction amount in the traded asset.  Accepted
                for a stable call signature; the shared limits are VES-only.

        Returns:
            The risk assessment.
        """
        ledger = await self.load_or_create_ledger(currency=LEDGER_VES)
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
        await self._session.flush()
        return RiskAssessment(
            allowed=True,
            requires_manual_review=False,
            reason=None,
            projected_daily_volume_ves=projected,
            limits=limits,
        )

    async def assess_buy(
        self,
        *,
        amount_ves: Decimal,
        amount_asset: Decimal,
        price: Decimal,
        bank_value: str | None,
    ) -> RiskAssessment:
        """Evaluate the ordered BUY circuit breakers.

        The breakers run in the specification order and stop on the first
        failure: per-order cap, daily SMS cap, implied-rate tolerance, then
        destination-bank support.  The evaluation is read-only: no channel is
        reserved here, so the caller can still park (gate off, incomplete
        extraction) without leaving budget charged.  A dispatched order
        reserves both channels through :meth:`reserve_buy` at B2.

        Args:
            amount_ves: Payout amount in VES.
            amount_asset: Order amount in the traded asset.
            price: Quoted unit price (VES per asset unit).
            bank_value: Extracted bank code, name or alias.

        Returns:
            The risk assessment.  ``reason`` is a machine-readable code:
            ``amount_over_cap``, ``daily_pay_cap_exceeded``,
            ``rate_out_of_tolerance`` or ``unsupported_destination_bank``.

        Raises:
            ValueError: If a money argument is a ``float``.
        """
        _ensure_decimal(amount_ves, "amount_ves")
        _ensure_decimal(amount_asset, "amount_asset")
        _ensure_decimal(price, "price")
        limits = {
            "max_tx_amount_ves": str(self._settings.max_tx_amount_ves),
            "daily_limit_ves": str(self._settings.daily_limit_ves),
            "max_usdt_per_order": str(self._settings.max_usdt_per_order),
            "max_buy_amount_ves": str(self._settings.max_buy_amount_ves),
            "daily_pay_limit_ves": str(self._settings.daily_pay_limit_ves),
            "rate_tolerance_pct": str(self._settings.rate_tolerance_pct),
        }

        def _blocked(reason: str, projected: Decimal = _ZERO) -> RiskAssessment:
            return RiskAssessment(
                allowed=False,
                requires_manual_review=self._settings.manual_review_on_limit,
                reason=reason,
                projected_daily_volume_ves=projected,
                limits=limits,
            )

        if (
            amount_ves > self._settings.max_buy_amount_ves
            or amount_asset > self._settings.max_usdt_per_order
        ):
            return _blocked("amount_over_cap")

        projection = await self.check_channel_cap(
            currency=LEDGER_VES_PAY,
            amount_ves=amount_ves,
            limit=self._settings.daily_pay_limit_ves,
        )
        if not projection.allowed:
            return _blocked(
                "daily_pay_cap_exceeded", projection.projected_daily_volume_ves
            )

        if not self._rate_within_tolerance(amount_ves, amount_asset, price):
            return _blocked(
                "rate_out_of_tolerance", projection.projected_daily_volume_ves
            )

        if bank_value is None or resolve_bank(bank_value) is None:
            return _blocked(
                "unsupported_destination_bank", projection.projected_daily_volume_ves
            )

        return RiskAssessment(
            allowed=True,
            requires_manual_review=False,
            reason=None,
            projected_daily_volume_ves=projection.projected_daily_volume_ves,
            limits=limits,
        )

    async def reserve_buy(self, *, amount_ves: Decimal) -> Decimal:
        """Reserve both daily channels for one dispatched BUY order.

        A dispatched BUY consumes the shared ``VES`` cap and the outgoing
        ``VES_PAY`` cap (the approved double-cap semantics).  The reservation
        is atomic in effect: when the second channel refuses, the first is
        released before the typed error propagates, so neither channel stays
        charged.

        Args:
            amount_ves: Amount to reserve in VES on both channels.

        Returns:
            The new ``VES_PAY`` channel total.

        Raises:
            ValueError: If ``amount_ves`` is a ``float``.
            RiskLimitExceededError: If either channel projection exceeds its
                cap.
        """
        _ensure_decimal(amount_ves, "amount_ves")
        await self.reserve(currency=LEDGER_VES, amount_ves=amount_ves)
        try:
            return await self.reserve(
                currency=LEDGER_VES_PAY, amount_ves=amount_ves
            )
        except RiskLimitExceededError:
            await self.release(currency=LEDGER_VES, amount_ves=amount_ves)
            raise

    def _rate_within_tolerance(
        self,
        amount_ves: Decimal,
        amount_asset: Decimal,
        price: Decimal,
    ) -> bool:
        """Return whether the implied rate is inside the configured tolerance.

        Args:
            amount_ves: Payout amount in VES.
            amount_asset: Order amount in the traded asset.
            price: Quoted unit price.

        Returns:
            ``True`` when the relative deviation is at most
            ``rate_tolerance_pct``.  Degenerate inputs are never tolerated.
        """
        if amount_asset <= 0 or price <= 0:
            return False
        implied = amount_ves / amount_asset
        deviation = abs(implied - price) / price
        return deviation <= (self._settings.rate_tolerance_pct / _PERCENT)

    def _require_channel(self, currency: str) -> None:
        """Reject a channel that has no ledger semantics.

        Args:
            currency: Candidate ledger channel.

        Raises:
            ValueError: If ``currency`` is not in :data:`CHANNELS`.
        """
        if currency not in CHANNELS:
            raise ValueError(f"Unknown ledger channel: {currency!r}")

    def _channel_limit(self, currency: str) -> Decimal:
        """Return the daily cap for a channel.

        Args:
            currency: Ledger channel.

        Returns:
            The daily cap in VES.  The outgoing payment channel uses
            ``daily_pay_limit_ves``; every other channel uses the shared
            ``daily_limit_ves``.
        """
        if currency == LEDGER_VES_PAY:
            return self._settings.daily_pay_limit_ves
        return self._settings.daily_limit_ves

    async def reserve(self, *, currency: str, amount_ves: Decimal) -> Decimal:
        """Reserve volume on a single channel.

        The projection is computed and compared against the channel cap before
        anything is written, so a refused reservation leaves the row untouched.

        Args:
            currency: Ledger channel to reserve against.
            amount_ves: Amount to reserve in VES.

        Returns:
            The new channel total.

        Raises:
            ValueError: If ``currency`` is not a known channel.
            RiskLimitExceededError: If the projection exceeds the channel cap.
        """
        self._require_channel(currency)
        ledger = await self.load_or_create_ledger(currency=currency)
        projected = (ledger.total_volume + amount_ves).quantize(_CENT)
        limit = self._channel_limit(currency)
        if projected > limit:
            raise RiskLimitExceededError(
                f"Projected {currency} volume exceeds the daily limit",
                details={
                    "currency": currency,
                    "projected": str(projected),
                    "limit": str(limit),
                },
            )
        ledger.total_volume = projected
        ledger.transaction_count = ledger.transaction_count + 1
        await self._session.flush()
        return projected

    async def release(self, *, currency: str, amount_ves: Decimal) -> Decimal:
        """Release previously reserved volume on a single channel.

        The total is clamped at zero and the transaction count never drops
        below zero, so an over-release cannot corrupt the counter.

        Args:
            currency: Ledger channel to release from.
            amount_ves: Amount to release in VES.

        Returns:
            The new channel total.

        Raises:
            ValueError: If ``currency`` is not a known channel.
        """
        self._require_channel(currency)
        ledger = await self.load_or_create_ledger(currency=currency)
        remaining = max(_ZERO, (ledger.total_volume - amount_ves).quantize(_CENT))
        ledger.total_volume = remaining
        ledger.transaction_count = max(0, ledger.transaction_count - 1)
        await self._session.flush()
        return remaining


__all__ = ["LEDGER_VES", "LEDGER_VES_PAY", "CHANNELS", "RiskPolicy"]

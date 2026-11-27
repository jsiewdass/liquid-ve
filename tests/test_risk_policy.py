"""RiskPolicy extraction tests and the SELL characterisation net.

The characterisation test is written against the *current* sell-flow
behaviour before the extraction so the refactor cannot drift the prose
reasons or the ledger arithmetic.  It must stay green across PR 2.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import RiskLimitExceededError
from app.domain.enums import OrderStatus, OrderType
from app.domain.schemas import RiskAssessment
from app.infrastructure.clients.binance_client import BinanceC2COrder
from app.infrastructure.models import DailyVolumeLedger
from app.services.risk_policy import CHANNELS, LEDGER_VES, LEDGER_VES_PAY, RiskPolicy
from app.services.sell_flow import SellFlow

_LEDGER_VES = "VES"


class _NoopClient:
    """Placeholder for a flow client that the tested path never calls."""


class _FakeBinance:
    """Minimal Binance double returning canned order details."""

    def __init__(self, orders: dict[str, BinanceC2COrder]) -> None:
        """Store the canned orders.

        Args:
            orders: Order number to detail mapping.
        """
        self._orders = orders

    async def get_order_detail(self, order_number: str) -> BinanceC2COrder:
        """Return the canned detail for ``order_number``.

        Args:
            order_number: Provider order number.

        Returns:
            The canned order detail.
        """
        return self._orders[order_number]


def _detail(
    order_number: str,
    *,
    total_price: str,
    amount: str = "10.00000000",
    price: str = "150.00000000",
    trade_type: OrderType = OrderType.SELL,
    status: str = "PENDING",
) -> BinanceC2COrder:
    """Build a normalized Binance C2C order detail.

    Args:
        order_number: Provider order number.
        total_price: Fiat total in VES.
        amount: Asset amount.
        price: Unit price.
        trade_type: Trade direction.
        status: External provider status.

    Returns:
        The normalized order detail.
    """
    return BinanceC2COrder(
        order_number=order_number,
        trade_type=trade_type,
        asset="USDT",
        fiat="VES",
        price=Decimal(price),
        amount=Decimal(amount),
        total_price=Decimal(total_price),
        status=status,
        counterparty_nickname="counterparty",
        created_at=datetime.now(timezone.utc),
    )


async def _ledger(session: AsyncSession, currency: str) -> DailyVolumeLedger:
    """Load today's ledger row for a channel.

    Args:
        session: Active session.
        currency: Ledger channel.

    Returns:
        The persisted ledger row.
    """
    result = await session.execute(
        select(DailyVolumeLedger).where(DailyVolumeLedger.currency == currency)
    )
    return result.scalar_one()


def _sell_flow(
    session: AsyncSession,
    settings: Settings,
    orders: dict[str, BinanceC2COrder],
) -> SellFlow:
    """Build a SellFlow wired to fakes.

    Args:
        session: Active session.
        settings: Test settings.
        orders: Canned Binance orders.

    Returns:
        The flow under test.
    """
    return SellFlow(
        session,
        settings=settings,
        binance=_FakeBinance(orders),
        cachicamo=_NoopClient(),
    )


async def test_sell_flow_risk_reason_and_ledger_unchanged(
    session: AsyncSession, test_settings: Settings
) -> None:
    """SELL prose reasons and VES ledger arithmetic stay byte-identical."""
    flow = _sell_flow(
        session,
        test_settings,
        {
            "SELL-ALLOW": _detail("SELL-ALLOW", total_price="1500.00"),
            "SELL-BLOCK": _detail("SELL-BLOCK", total_price="6000.00"),
            "SELL-DAILY": _detail("SELL-DAILY", total_price="4500.00"),
        },
    )

    allowed = await flow.execute("SELL-ALLOW")
    assert allowed.status is OrderStatus.PENDING
    assert allowed.risk_checked is True
    assert allowed.manual_review_reason is None

    ledger = await _ledger(session, _LEDGER_VES)
    assert ledger.total_volume == Decimal("1500.00")
    assert ledger.transaction_count == 1

    blocked = await flow.execute("SELL-BLOCK")
    assert blocked.status is OrderStatus.MANUAL_REVIEW
    assert blocked.manual_review_reason == (
        "Transaction amount exceeds the per-transaction limit"
    )

    ledger = await _ledger(session, _LEDGER_VES)
    assert ledger.total_volume == Decimal("1500.00")
    assert ledger.transaction_count == 1

    ledger.total_volume = Decimal("16000.00")
    await session.commit()

    daily = await flow.execute("SELL-DAILY")
    assert daily.status is OrderStatus.MANUAL_REVIEW
    assert daily.manual_review_reason == (
        "Projected daily volume exceeds the daily limit"
    )

    ledger = await _ledger(session, _LEDGER_VES)
    assert ledger.total_volume == Decimal("16000.00")
    assert ledger.transaction_count == 1


async def test_assess_shared_allows_and_increments_ves_ledger(
    session: AsyncSession, test_settings: Settings
) -> None:
    """A within-limit assessment allows the trade and reserves the VES row."""
    policy = RiskPolicy(session, test_settings)

    assessment = await policy.assess_shared(
        amount_ves=Decimal("1500.00"),
        amount_asset=Decimal("10.00000000"),
    )

    assert assessment.allowed is True
    assert assessment.requires_manual_review is False
    assert assessment.reason is None
    assert assessment.projected_daily_volume_ves == Decimal("1500.00")
    assert set(assessment.limits) == {
        "max_tx_amount_ves",
        "daily_limit_ves",
        "max_usdt_per_order",
    }

    ledger = await _ledger(session, LEDGER_VES)
    assert ledger.total_volume == Decimal("1500.00")
    assert ledger.transaction_count == 1


async def test_assess_shared_block_reason_matches_legacy_sell_flow(
    session: AsyncSession, test_settings: Settings
) -> None:
    """The legacy per-transaction prose reason and no-reserve path are kept."""
    policy = RiskPolicy(session, test_settings)

    assessment = await policy.assess_shared(
        amount_ves=Decimal("6000.00"),
        amount_asset=Decimal("40.00000000"),
    )

    assert assessment.allowed is False
    assert assessment.reason == "Transaction amount exceeds the per-transaction limit"

    ledger = await _ledger(session, LEDGER_VES)
    assert ledger.total_volume == Decimal("0.00")
    assert ledger.transaction_count == 0


async def test_ves_and_ves_sms_rows_are_independent(
    session: AsyncSession, test_settings: Settings
) -> None:
    """The shared VES and outgoing SMS channels never share a counter row."""
    policy = RiskPolicy(session, test_settings)

    await policy.reserve(currency=LEDGER_VES, amount_ves=Decimal("100.00"))
    await policy.reserve(currency=LEDGER_VES_PAY, amount_ves=Decimal("200.00"))

    ves = await _ledger(session, LEDGER_VES)
    sms = await _ledger(session, LEDGER_VES_PAY)
    assert ves.total_volume == Decimal("100.00")
    assert ves.transaction_count == 1
    assert sms.total_volume == Decimal("200.00")
    assert sms.transaction_count == 1
    assert ves.id != sms.id


async def test_ves_pay_channel_preserves_double_cap_and_release_semantics(
    session: AsyncSession, test_settings: Settings
) -> None:
    """``VES_PAY`` replaces ``VES_SMS`` while keeping the all-or-nothing cap."""
    assert LEDGER_VES_PAY == "VES_PAY"
    assert CHANNELS == frozenset({LEDGER_VES, LEDGER_VES_PAY})
    assert "VES_SMS" not in CHANNELS

    policy = RiskPolicy(session, test_settings)
    assert policy._channel_limit(LEDGER_VES_PAY) == test_settings.daily_pay_limit_ves

    await policy.reserve(currency=LEDGER_VES_PAY, amount_ves=Decimal("19000.00"))

    with pytest.raises(RiskLimitExceededError):
        await policy.reserve_buy(amount_ves=Decimal("1500.00"))

    shared = await _ledger(session, LEDGER_VES)
    assert shared.total_volume == Decimal("0.00")
    assert shared.transaction_count == 0

    pay = await _ledger(session, LEDGER_VES_PAY)
    assert pay.total_volume == Decimal("19000.00")
    assert pay.transaction_count == 1


async def test_reserve_refuses_when_projected_over_limit(
    session: AsyncSession, test_settings: Settings
) -> None:
    """Reserve rejects a projection above the channel cap and writes nothing."""
    policy = RiskPolicy(session, test_settings)
    await policy.reserve(currency=LEDGER_VES, amount_ves=Decimal("19000.00"))

    with pytest.raises(RiskLimitExceededError):
        await policy.reserve(currency=LEDGER_VES, amount_ves=Decimal("1500.00"))

    ledger = await _ledger(session, LEDGER_VES)
    assert ledger.total_volume == Decimal("19000.00")
    assert ledger.transaction_count == 1


async def test_daily_cap_resets_at_utc_rollover(
    session: AsyncSession, test_settings: Settings
) -> None:
    """R-BUY-004: the daily cap is keyed by UTC date and resets at midnight."""
    policy = RiskPolicy(session, test_settings)

    with freeze_time("2026-03-10 12:00:00", tz_offset=0):
        await policy.reserve(currency=LEDGER_VES_PAY, amount_ves=Decimal("19000.00"))
        with pytest.raises(RiskLimitExceededError):
            await policy.reserve(
                currency=LEDGER_VES_PAY, amount_ves=Decimal("1500.00")
            )
        await session.commit()

    with freeze_time("2026-03-11 00:00:01", tz_offset=0):
        total = await policy.reserve(
            currency=LEDGER_VES_PAY, amount_ves=Decimal("1500.00")
        )
        assert total == Decimal("1500.00")

    result = await session.execute(
        select(DailyVolumeLedger)
        .where(DailyVolumeLedger.currency == LEDGER_VES_PAY)
        .order_by(DailyVolumeLedger.ledger_date)
    )
    rows = result.scalars().all()
    assert [(row.ledger_date.isoformat(), row.total_volume) for row in rows] == [
        ("2026-03-10", Decimal("19000.00")),
        ("2026-03-11", Decimal("1500.00")),
    ]


async def test_release_clamps_at_zero_and_decrements_count(
    session: AsyncSession, test_settings: Settings
) -> None:
    """Release floors the total at zero and never makes the count negative."""
    policy = RiskPolicy(session, test_settings)
    await policy.reserve(currency=LEDGER_VES, amount_ves=Decimal("100.00"))

    remaining = await policy.release(
        currency=LEDGER_VES, amount_ves=Decimal("500.00")
    )

    assert remaining == Decimal("0.00")
    ledger = await _ledger(session, LEDGER_VES)
    assert ledger.total_volume == Decimal("0.00")
    assert ledger.transaction_count == 0


async def test_release_rejects_unknown_channel(
    session: AsyncSession, test_settings: Settings
) -> None:
    """An unknown channel is a programming error, not a phantom ledger row."""
    policy = RiskPolicy(session, test_settings)

    with pytest.raises(ValueError):
        await policy.release(currency="USD", amount_ves=Decimal("10.00"))


async def test_sell_flow_delegates_to_risk_policy(
    session: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SellFlow.execute must delegate the shared assessment to RiskPolicy."""
    calls: list[tuple[Decimal, Decimal]] = []

    async def _fake_assess(
        self: RiskPolicy, *, amount_ves: Decimal, amount_asset: Decimal
    ) -> RiskAssessment:
        calls.append((amount_ves, amount_asset))
        return RiskAssessment(
            allowed=False,
            requires_manual_review=True,
            reason="delegated",
            projected_daily_volume_ves=amount_ves,
            limits={},
        )

    monkeypatch.setattr(RiskPolicy, "assess_shared", _fake_assess)
    flow = _sell_flow(
        session,
        test_settings,
        {"SELL-DELEGATE": _detail("SELL-DELEGATE", total_price="1500.00")},
    )

    order = await flow.execute("SELL-DELEGATE")

    assert calls == [(Decimal("1500.00"), Decimal("10.00000000"))]
    assert order.status is OrderStatus.MANUAL_REVIEW
    assert order.manual_review_reason == "delegated"


_BREAKER_CASES = [
    pytest.param(
        "6000.00", "40.00000000", "150.00000000", "0102", None, "amount_over_cap",
        id="ves-over-cap",
    ),
    pytest.param(
        "1200.00", "1200.00000000", "1.00000000", "0102", None, "amount_over_cap",
        id="asset-over-cap",
    ),
    pytest.param(
        "2000.00", "10.00000000", "200.00000000", "0102", "19000.00",
        "daily_pay_cap_exceeded",
        id="daily-pay-cap",
    ),
    pytest.param(
        "1500.00", "10.00000000", "160.00000000", "0102", None,
        "rate_out_of_tolerance",
        id="rate-out-of-tolerance",
    ),
    pytest.param(
        "1500.00", "10.00000000", "150.00000000", "9999", None,
        "unsupported_destination_bank",
        id="unsupported-bank-code",
    ),
    pytest.param(
        "1500.00", "10.00000000", "150.00000000", None, None,
        "unsupported_destination_bank",
        id="missing-bank",
    ),
    pytest.param(
        "6000.00", "2000.00000000", "1.00000000", "0105", None,
        "amount_over_cap",
        id="first-failure-wins",
    ),
]


@pytest.mark.parametrize(
    "amount_ves,amount_asset,price,bank_value,pre_reserve,expected",
    _BREAKER_CASES,
)
async def test_buy_breakers_block_in_order(
    session: AsyncSession,
    test_settings: Settings,
    amount_ves: str,
    amount_asset: str,
    price: str,
    bank_value: str | None,
    pre_reserve: str | None,
    expected: str,
) -> None:
    """The ordered breakers return the first machine-readable reason."""
    policy = RiskPolicy(session, test_settings)
    if pre_reserve is not None:
        await policy.reserve(
            currency=LEDGER_VES_PAY, amount_ves=Decimal(pre_reserve)
        )

    assessment = await policy.assess_buy(
        amount_ves=Decimal(amount_ves),
        amount_asset=Decimal(amount_asset),
        price=Decimal(price),
        bank_value=bank_value,
    )

    assert assessment.allowed is False
    assert assessment.reason == expected
    assert {
        "max_buy_amount_ves",
        "daily_pay_limit_ves",
        "rate_tolerance_pct",
    } <= set(assessment.limits)

    result = await session.execute(
        select(DailyVolumeLedger).where(DailyVolumeLedger.currency == LEDGER_VES_PAY)
    )
    ledger = result.scalar_one_or_none()
    total = ledger.total_volume if ledger is not None else Decimal("0.00")
    assert total == Decimal(pre_reserve) if pre_reserve else total == Decimal("0.00")


async def test_buy_allow_path_checks_read_only_then_reserves_both_channels(
    session: AsyncSession, test_settings: Settings
) -> None:
    """``assess_buy`` is check-only; ``reserve_buy`` charges both daily channels."""
    policy = RiskPolicy(session, test_settings)

    assessment = await policy.assess_buy(
        amount_ves=Decimal("1500.00"),
        amount_asset=Decimal("10.00000000"),
        price=Decimal("150.00000000"),
        bank_value="0102",
    )

    assert assessment.allowed is True
    assert assessment.requires_manual_review is False
    assert assessment.reason is None
    assert assessment.projected_daily_volume_ves == Decimal("1500.00")

    # Check-only: neither channel exists yet.
    rows = await session.execute(select(DailyVolumeLedger))
    assert rows.scalars().all() == []

    reserved = await policy.reserve_buy(amount_ves=Decimal("1500.00"))
    assert reserved == Decimal("1500.00")

    sms = await _ledger(session, LEDGER_VES_PAY)
    assert sms.total_volume == Decimal("1500.00")
    assert sms.transaction_count == 1

    shared = await _ledger(session, LEDGER_VES)
    assert shared.total_volume == Decimal("1500.00")
    assert shared.transaction_count == 1


async def test_reserve_buy_releases_the_first_channel_when_the_second_is_capped(
    session: AsyncSession, test_settings: Settings
) -> None:
    """``reserve_buy`` is atomic in effect: both channels or neither."""
    policy = RiskPolicy(session, test_settings)
    await policy.reserve(
        currency=LEDGER_VES_PAY, amount_ves=Decimal("19000.00")
    )

    with pytest.raises(RiskLimitExceededError):
        await policy.reserve_buy(amount_ves=Decimal("1500.00"))

    shared = await _ledger(session, LEDGER_VES)
    assert shared.total_volume == Decimal("0.00")
    assert shared.transaction_count == 0

    sms = await _ledger(session, LEDGER_VES_PAY)
    assert sms.total_volume == Decimal("19000.00")
    assert sms.transaction_count == 1


async def test_buy_limits_snapshot_has_buy_keys(
    session: AsyncSession, test_settings: Settings
) -> None:
    """The limits snapshot exposes the buy scalars as strings."""
    policy = RiskPolicy(session, test_settings)

    assessment = await policy.assess_buy(
        amount_ves=Decimal("1500.00"),
        amount_asset=Decimal("10.00000000"),
        price=Decimal("150.00000000"),
        bank_value="0102",
    )

    assert assessment.limits["max_buy_amount_ves"] == "5000.00"
    assert assessment.limits["daily_pay_limit_ves"] == "20000.00"
    assert assessment.limits["rate_tolerance_pct"] == "1.00"

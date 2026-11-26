"""Four-valued bank-reply parser tests.

The parser is pure: every case is a committed synthetic fixture plus one bank
profile.  Outcomes are exhaustive, so an unknown wording can never be mistaken
for a confirmation.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.bank_replies import (
    BankReplyFailureReason,
    BankReplyOutcome,
    parse_bank_reply,
)
from app.domain.banks import BANK_SMS_PROFILES, BankSmsProfile

_FIXTURES = Path(__file__).parent / "fixtures" / "sms"

_PROFILES: tuple[tuple[str, BankSmsProfile], ...] = (
    ("bdv", BANK_SMS_PROFILES["0102"]),
    ("banesco", BANK_SMS_PROFILES["0134"]),
)


def _reply(name: str) -> str:
    """Read one synthetic reply fixture.

    Args:
        name: Fixture file name inside ``tests/fixtures/sms``.

    Returns:
        The fixture body without a trailing newline.
    """
    return (_FIXTURES / name).read_text(encoding="utf-8").strip()


@pytest.mark.parametrize(("bank_key", "profile"), _PROFILES)
def test_success_yields_one_reference(bank_key: str, profile: BankSmsProfile) -> None:
    """A single success pattern yields exactly one reference."""
    parsed = parse_bank_reply(_reply(f"bank_reply_success_{bank_key}.txt"), profile)

    assert parsed.outcome is BankReplyOutcome.SUCCESS
    assert parsed.reference is not None
    assert parsed.reference.isdigit()
    assert parsed.failure_reason is None
    assert parsed.amount is None


@pytest.mark.parametrize(("bank_key", "profile"), _PROFILES)
def test_failure_yields_typed_reason(bank_key: str, profile: BankSmsProfile) -> None:
    """A failure pattern yields a typed reason and never a reference."""
    parsed = parse_bank_reply(_reply(f"bank_reply_failure_{bank_key}.txt"), profile)

    assert parsed.outcome is BankReplyOutcome.FAILURE
    assert parsed.reference is None
    assert parsed.failure_reason == BankReplyFailureReason.BANK_REJECTED_PAYMENT.value


@pytest.mark.parametrize(("bank_key", "profile"), _PROFILES)
def test_success_and_failure_is_ambiguous(
    bank_key: str, profile: BankSmsProfile
) -> None:
    """A body matching both vocabularies is ambiguous, not a success."""
    parsed = parse_bank_reply(_reply(f"bank_reply_ambiguous_{bank_key}.txt"), profile)

    assert parsed.outcome is BankReplyOutcome.AMBIGUOUS
    assert parsed.reference is None


def test_multiple_references_is_ambiguous() -> None:
    """More than one distinct reference cannot confirm either payment."""
    parsed = parse_bank_reply(
        _reply("bank_reply_multiple_references_bdv.txt"),
        BANK_SMS_PROFILES["0102"],
    )

    assert parsed.outcome is BankReplyOutcome.AMBIGUOUS
    assert parsed.reference is None


def test_unknown_wording_is_unparseable() -> None:
    """Unknown wording routes to ``UNPARSEABLE`` instead of guessing."""
    parsed = parse_bank_reply(
        _reply("bank_reply_unknown_bdv.txt"),
        BANK_SMS_PROFILES["0102"],
    )

    assert parsed.outcome is BankReplyOutcome.UNPARSEABLE
    assert parsed.reference is None
    assert parsed.failure_reason is None


@pytest.mark.parametrize(("bank_key", "profile"), _PROFILES)
def test_edited_failure_pattern_applies_without_code_change(
    bank_key: str, profile: BankSmsProfile
) -> None:
    """Removing the failure pattern turns a failure into ``UNPARSEABLE``."""
    body = _reply(f"bank_reply_failure_{bank_key}.txt")
    assert parse_bank_reply(body, profile).outcome is BankReplyOutcome.FAILURE

    without_failure_patterns = replace(profile, failure_patterns=())
    assert (
        parse_bank_reply(body, without_failure_patterns).outcome
        is BankReplyOutcome.UNPARSEABLE
    )


def test_reply_amount_is_parsed_with_parse_ves_amount() -> None:
    """An amount captured by the pattern is parsed into a ``Decimal``."""
    parsed = parse_bank_reply(
        _reply("bank_reply_success_with_amount_bdv.txt"),
        BANK_SMS_PROFILES["0102"],
    )

    assert parsed.outcome is BankReplyOutcome.SUCCESS
    assert parsed.amount == Decimal("1500.00")


def test_repeated_success_pattern_with_same_reference_is_success() -> None:
    """Two success patterns that agree on the reference are not ambiguity."""
    profile = BANK_SMS_PROFILES["0102"]
    duplicated = replace(
        profile,
        success_patterns=(*profile.success_patterns, *profile.success_patterns),
    )

    parsed = parse_bank_reply(_reply("bank_reply_success_bdv.txt"), duplicated)

    assert parsed.outcome is BankReplyOutcome.SUCCESS
    assert parsed.reference is not None


def test_blank_body_is_unparseable() -> None:
    """An empty body never confirms anything."""
    parsed = parse_bank_reply("   ", BANK_SMS_PROFILES["0102"])

    assert parsed.outcome is BankReplyOutcome.UNPARSEABLE
    assert parsed.reference is None

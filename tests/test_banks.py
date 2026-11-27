"""Unit tests for the bank payment field normalization.

Every test here is a pure unit test: no network, no fakes, no database and no
settings.  The module under test performs no I/O, so the tests exercise plain
values only.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.exceptions import BankFieldFormatError, BankAmountParseError
from app.domain.banks import (
    format_ves_amount,
    national_digits,
    parse_ves_amount,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("1500"), "1500,00"),
        (Decimal("1500.5"), "1500,50"),
        (Decimal("25000.00"), "25000,00"),
    ],
)
def test_format_ves_amount_matches_venezuelan_format(value: Decimal, expected: str) -> None:
    """Render a Decimal with a comma decimal separator and two decimals."""
    assert format_ves_amount(value) == expected


def test_format_ves_amount_rejects_float() -> None:
    """Refuse a float amount instead of silently accepting binary floats."""
    with pytest.raises(BankFieldFormatError):
        format_ves_amount(1500.0)  # type: ignore[arg-type]


def test_format_ves_amount_rejects_non_positive() -> None:
    """Refuse zero and negative amounts."""
    with pytest.raises(BankFieldFormatError):
        format_ves_amount(Decimal("0"))
    with pytest.raises(BankFieldFormatError):
        format_ves_amount(Decimal("-10.00"))


def test_e164_phone_becomes_national_digits() -> None:
    """Strip the +58 prefix and prepend a leading zero."""
    assert national_digits("+584141234567") == "04141234567"


def test_national_phone_is_idempotent() -> None:
    """An already-national phone is returned unchanged."""
    assert national_digits("04141234567") == "04141234567"
    assert national_digits("0414-123-4567") == "04141234567"






def test_parse_ves_amount_inverts_formatting() -> None:
    """Invert the Venezuelan amount format back into a Decimal."""
    assert parse_ves_amount("1.500,00") == Decimal("1500.00")
    assert parse_ves_amount("25000,00") == Decimal("25000.00")


@pytest.mark.parametrize("text", ["", "   ", "no amount here", "1,2,3"])
def test_parse_ves_amount_rejects_unmatched_text(text: str) -> None:
    """Refuse text that is not a recognizable amount."""
    with pytest.raises(BankAmountParseError):
        parse_ves_amount(text)

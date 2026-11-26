"""Unit tests for the pure bank SMS domain module.

Every test here is a pure unit test: no network, no fakes, no database and no
settings.  The module under test performs no I/O, so the tests exercise plain
values only.
"""

from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.core.exceptions import BankSmsFormatError, BankSmsParseError
from app.domain.banks import (
    BANK_SMS_PROFILES,
    SHORT_CODE_INDEX,
    apply_short_code_overrides,
    cedula_digits,
    format_ves_amount,
    national_digits,
    parse_ves_amount,
    profile_for_sender,
    render_bank_command,
    resolve_bank,
)
from app.domain.enums import BankCode


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
    with pytest.raises(BankSmsFormatError):
        format_ves_amount(1500.0)  # type: ignore[arg-type]


def test_format_ves_amount_rejects_non_positive() -> None:
    """Refuse zero and negative amounts."""
    with pytest.raises(BankSmsFormatError):
        format_ves_amount(Decimal("0"))
    with pytest.raises(BankSmsFormatError):
        format_ves_amount(Decimal("-10.00"))


def test_e164_phone_becomes_national_digits() -> None:
    """Strip the +58 prefix and prepend a leading zero."""
    assert national_digits("+584141234567") == "04141234567"


def test_national_phone_is_idempotent() -> None:
    """An already-national phone is returned unchanged."""
    assert national_digits("04141234567") == "04141234567"
    assert national_digits("0414-123-4567") == "04141234567"


def test_cedula_digits_and_optional_prefix() -> None:
    """Remove the document letter and apply an optional profile format."""
    assert cedula_digits("V-12345678") == "12345678"
    assert cedula_digits("V-12345678", cedula_format="V-{cedula}") == "V-12345678"
    # R is a valid Venezuelan document prefix and must normalize too.
    assert cedula_digits("R-12345678") == "12345678"
    assert cedula_digits("r12345678") == "12345678"


@pytest.mark.parametrize("bad", ["", "   ", "abc", "12", "0414"])
def test_cedula_rejects_non_normalizable_input(bad: str) -> None:
    """Refuse a cedula that cannot be reduced to 6-9 bare digits."""
    with pytest.raises(BankSmsFormatError):
        cedula_digits(bad)


def test_parse_ves_amount_inverts_formatting() -> None:
    """Invert the Venezuelan amount format back into a Decimal."""
    assert parse_ves_amount("1.500,00") == Decimal("1500.00")
    assert parse_ves_amount("25000,00") == Decimal("25000.00")


@pytest.mark.parametrize("text", ["", "   ", "no amount here", "1,2,3"])
def test_parse_ves_amount_rejects_unmatched_text(text: str) -> None:
    """Refuse text that is not a recognizable amount."""
    with pytest.raises(BankSmsParseError):
        parse_ves_amount(text)



def test_profile_carries_routing_data() -> None:
    """A profile is immutable data with every field the renderer needs."""
    profile = BANK_SMS_PROFILES["0102"]
    assert profile.bank is BankCode.BANCO_DE_VENEZUELA
    assert re.fullmatch(r"\d{4}", profile.numeric_code)
    assert profile.short_codes
    assert len(profile.success_patterns) >= 1
    for placeholder in ("numeric_code", "phone_national", "cedula", "amount_ve"):
        assert "{" + placeholder + "}" in profile.command_template


def test_shipped_registry_primary_short_codes() -> None:
    """The shipped registry resolves each bank to its primary short code."""
    assert BANK_SMS_PROFILES["0102"].short_codes[0] == "2661"
    assert BANK_SMS_PROFILES["0134"].short_codes[0] == "2846"
    assert BANK_SMS_PROFILES["0102"].short_codes == ("2661", "2662")


def test_registry_is_immutable() -> None:
    """Neither the registry maps nor the profiles may be mutated at runtime."""
    with pytest.raises(TypeError):
        BANK_SMS_PROFILES["9999"] = BANK_SMS_PROFILES["0102"]  # type: ignore[index]
    with pytest.raises(TypeError):
        SHORT_CODE_INDEX["9999"] = BANK_SMS_PROFILES["0102"]  # type: ignore[index]
    profile = BANK_SMS_PROFILES["0102"]
    with pytest.raises((FrozenInstanceError, AttributeError)):
        profile.numeric_code = "9999"  # type: ignore[misc]


def test_code_and_alias_both_resolve() -> None:
    """Trimmed codes, member names and aliases all resolve to a profile."""
    bdv = resolve_bank(" 0102 ")
    assert bdv is BANK_SMS_PROFILES["0102"]
    assert bdv is not None
    assert bdv.bank is BankCode.BANCO_DE_VENEZUELA
    banesco = resolve_bank("banesco")
    assert banesco is BANK_SMS_PROFILES["0134"]
    assert resolve_bank("BANCO_DE_VENEZUELA") is bdv
    assert resolve_bank("Banco de Venezuela") is bdv


@pytest.mark.parametrize("value", ["0105", "", "   ", "some random bank"])
def test_unsupported_bank_returns_none(value: str) -> None:
    """Unknown or unsupported input yields no profile, never a nearest bank."""
    assert resolve_bank(value) is None


def test_ambiguous_alias_returns_none() -> None:
    """An input matching two distinct banks is refused rather than guessed."""
    assert resolve_bank("bdv bod") is None


def test_profile_for_sender_uses_short_code_index() -> None:
    """A sender short code maps back to its bank profile."""
    assert profile_for_sender("2661") is BANK_SMS_PROFILES["0102"]
    assert profile_for_sender(" 2846 ") is BANK_SMS_PROFILES["0134"]
    assert profile_for_sender("9999") is None


def test_short_code_overrides_replace_primary_first() -> None:
    """Overrides lead the ordered short codes without mutating the original."""
    bdv = BANK_SMS_PROFILES["0102"]
    updated = apply_short_code_overrides(bdv, ("9999",))
    assert updated.short_codes == ("9999", "2661", "2662")
    assert bdv.short_codes == ("2661", "2662")
    deduped = apply_short_code_overrides(bdv, ("2662", "9999"))
    assert deduped.short_codes == ("2662", "9999", "2661")
    assert apply_short_code_overrides(bdv, ()).short_codes == bdv.short_codes


def test_command_matches_product_example() -> None:
    """Render the exact Banco de Venezuela product example."""
    command = render_bank_command(
        BANK_SMS_PROFILES["0102"],
        phone="04141234567",
        cedula="12345678",
        amount=Decimal("1500"),
    )
    assert command == "Pagar 0102 04141234567 12345678 1500,00"


def test_banesco_command_matches_example() -> None:
    """Render the exact Banesco product example."""
    command = render_bank_command(
        BANK_SMS_PROFILES["0134"],
        phone="04141234567",
        cedula="12345678",
        amount=Decimal("1500"),
    )
    assert command == "Pagar 0134 04141234567 12345678 1500,00"


@pytest.mark.parametrize("cedula", ["", "   ", "abc"])
def test_missing_cedula_raises_and_renders_nothing(cedula: str) -> None:
    """A missing or invalid cedula refuses rendering instead of a partial command."""
    with pytest.raises(BankSmsFormatError):
        render_bank_command(
            BANK_SMS_PROFILES["0102"],
            phone="04141234567",
            cedula=cedula,
            amount=Decimal("1500"),
        )


@pytest.mark.parametrize(
    ("phone", "cedula", "amount", "expected"),
    [
        ("+584141234567", "V-12345678", Decimal("1500"), "Pagar 0102 04141234567 12345678 1500,00"),
        ("0414-123-4567", "12.345.678", Decimal("1500.5"), "Pagar 0102 04141234567 12345678 1500,50"),
    ],
)
def test_rendered_command_has_no_stray_whitespace(
    phone: str, cedula: str, amount: Decimal, expected: str
) -> None:
    """Normalized inputs render a clean, single-spaced command."""
    command = render_bank_command(
        BANK_SMS_PROFILES["0102"], phone=phone, cedula=cedula, amount=amount
    )
    assert command == expected
    assert " ".join(command.split()) == command
    assert "  " not in command
    assert command == command.strip()

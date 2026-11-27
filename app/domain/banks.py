"""Normalization for Venezuelan bank payment fields."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.core.exceptions import BankFieldFormatError, BankAmountParseError
from app.domain.schemas import validate_venezuelan_phone

_MONEY_QUANTUM = Decimal("0.01")
_NATIONAL_PHONE_RE = re.compile(r"^0[0-9]{10}$")


def format_ves_amount(value: Decimal) -> str:
    """Render a Decimal in Venezuelan amount format.

    The output uses a comma decimal separator, exactly two decimals, no
    thousands separator and no currency symbol.

    Args:
        value: Positive monetary amount.  Floats are forbidden.

    Returns:
        The amount rendered as ``"1500,00"``.

    Raises:
        BankFieldFormatError: If ``value`` is not a finite positive ``Decimal``.
    """
    if not isinstance(value, Decimal):
        raise BankFieldFormatError("Amount must be a Decimal, not a float")
    if not value.is_finite():
        raise BankFieldFormatError("Amount must be a finite Decimal")
    if value <= 0:
        raise BankFieldFormatError("Amount must be greater than zero")
    quantized = value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    return f"{quantized:.2f}".replace(".", ",")


def national_digits(phone: str) -> str:
    """Normalize a Venezuelan phone to national digits.

    Accepts E.164 (``+58``) and national (``0``-prefixed) input and always
    returns eleven digits starting with ``0``.  Normalizing an already-national
    value is idempotent.

    Args:
        phone: Raw phone number in any accepted form.

    Returns:
        The national representation such as ``"04141234567"``.

    Raises:
        BankFieldFormatError: If the value is not a valid Venezuelan mobile
            number.
    """
    if not isinstance(phone, str) or not phone.strip():
        raise BankFieldFormatError("Phone number is required")
    try:
        e164 = validate_venezuelan_phone(phone)
    except ValueError as exc:
        raise BankFieldFormatError(str(exc)) from exc
    national = f"0{e164.removeprefix('+58')}"
    if not _NATIONAL_PHONE_RE.fullmatch(national):
        raise BankFieldFormatError("Phone must normalize to eleven national digits")
    return national


def parse_ves_amount(text: str) -> Decimal:
    """Parse a Venezuelan-formatted amount back into a Decimal.

    Strips grouping dots, treats the comma as the decimal separator and
    quantizes to two decimals.  Unmatched text is refused rather than guessed.

    Args:
        text: Amount text such as ``"1.500,00"``.

    Returns:
        The parsed positive amount quantized to two decimals.

    Raises:
        BankAmountParseError: If the text is not a recognizable positive amount.
    """
    if not isinstance(text, str) or not text.strip():
        raise BankAmountParseError("Amount text is required")
    cleaned = text.strip().replace(" ", "").replace(".", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise BankAmountParseError(f"Unrecognized amount: {text!r}") from exc
    if not value.is_finite() or value <= 0:
        raise BankAmountParseError(f"Amount must be a positive number: {text!r}")
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)

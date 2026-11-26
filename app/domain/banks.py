"""Pure, I/O-free bank SMS domain: profiles, normalization and rendering.

This module turns a supported Venezuelan bank plus a counterparty's payment
data into one exact Pago Movil command.  It performs no I/O, reads no
environment variables and touches no external provider; every function is
deterministic and money is always handled as :class:`decimal.Decimal`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import MappingProxyType

from app.core.exceptions import BankSmsFormatError, BankSmsParseError
from app.domain.enums import BankCode
from app.domain.schemas import validate_venezuelan_phone

__all__ = [
    "BankSmsProfile",
    "BANK_SMS_PROFILES",
    "SHORT_CODE_INDEX",
    "resolve_bank",
    "profile_for_sender",
    "apply_short_code_overrides",
    "format_ves_amount",
    "parse_ves_amount",
    "national_digits",
    "cedula_digits",
    "render_bank_command",
]

_MONEY_QUANTUM = Decimal("0.01")
_SEPARATOR_RE = re.compile(r"[\s\-.()]")
_CEDULA_PREFIX_RE = re.compile(r"^[VEJGPR]", re.IGNORECASE)
_NATIONAL_PHONE_RE = re.compile(r"^0\d{10}$")
_NUMERIC_CODE_RE = re.compile(r"^\d{4}$")
_COMMAND_TEMPLATE = "Pagar {numeric_code} {phone_national} {cedula} {amount_ve}"
# Provisional wording for the shipped banks (design open question D2).  The
# amount group is optional so a reply without a visible amount still confirms;
# the matcher then falls back to the candidate's own amount.  Unknown wording
# is intentionally left unmatched so it routes to ``UNPARSEABLE``.
_PROVISIONAL_SUCCESS_PATTERNS: tuple[str, ...] = (
    r"Referencia:\s*(?P<reference>\d{6,12})"
    r"(?:[^\n]*?Monto:\s*(?P<amount>\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}))?",
)
# Conservative failure vocabulary: only explicit rejection wording is treated
# as a failure.  Everything else stays ``UNPARSEABLE`` for a human to review.
_PROVISIONAL_FAILURE_PATTERNS: tuple[str, ...] = (
    r"(?:rechazad[oa]|fallid[oa]|no\s+procesad[oa]|fondos\s+insuficientes)",
)


@dataclass(frozen=True, slots=True)
class BankSmsProfile:
    """Immutable routing data for one supported bank.

    Attributes:
        bank: Domain bank identity.
        display_name: Human readable bank name.
        numeric_code: Four-digit bank code used inside the command.
        short_codes: Ordered SMS destination short codes, primary first.
        command_template: Template naming every required command component.
        success_patterns: Reply patterns that indicate a successful payment.
        failure_patterns: Optional reply patterns that indicate a failed
            payment.
        cedula_format: Optional template applied to the normalized document
            number; ``None`` renders bare digits.
        aliases: Free-form spellings accepted by :func:`resolve_bank`.
    """

    bank: BankCode
    display_name: str
    numeric_code: str
    short_codes: tuple[str, ...]
    command_template: str
    success_patterns: tuple[str, ...]
    failure_patterns: tuple[str, ...] = ()
    cedula_format: str | None = None
    aliases: tuple[str, ...] = ()


_BDV_PROFILE = BankSmsProfile(
    bank=BankCode.BANCO_DE_VENEZUELA,
    display_name="Banco de Venezuela",
    numeric_code="0102",
    short_codes=("2661", "2662"),
    command_template=_COMMAND_TEMPLATE,
    success_patterns=_PROVISIONAL_SUCCESS_PATTERNS,
    failure_patterns=_PROVISIONAL_FAILURE_PATTERNS,
    cedula_format=None,
    aliases=("bdv", "venezuela", "banco de venezuela"),
)

_BANESCO_PROFILE = BankSmsProfile(
    bank=BankCode.BANESCO,
    display_name="Banesco Banco Universal",
    numeric_code="0134",
    short_codes=("2846",),
    command_template=_COMMAND_TEMPLATE,
    success_patterns=_PROVISIONAL_SUCCESS_PATTERNS,
    failure_patterns=_PROVISIONAL_FAILURE_PATTERNS,
    cedula_format=None,
    aliases=("banesco", "banesco banco universal"),
)

BANK_SMS_PROFILES: Mapping[str, BankSmsProfile] = MappingProxyType(
    {
        _BDV_PROFILE.numeric_code: _BDV_PROFILE,
        _BANESCO_PROFILE.numeric_code: _BANESCO_PROFILE,
    }
)

SHORT_CODE_INDEX: Mapping[str, BankSmsProfile] = MappingProxyType(
    {
        short_code: profile
        for profile in BANK_SMS_PROFILES.values()
        for short_code in profile.short_codes
    }
)

_PROFILE_BY_BANK: Mapping[BankCode, BankSmsProfile] = MappingProxyType(
    {profile.bank: profile for profile in BANK_SMS_PROFILES.values()}
)


def _normalize_alias(value: str) -> str:
    """Normalize free-form text for bank alias matching.

    Args:
        value: Raw text such as ``"Banco de Venezuela"``.

    Returns:
        Lowercase ASCII text without accents or separators.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return "".join(char for char in without_accents.lower() if char.isalnum())


def _build_alias_candidates() -> Mapping[str, tuple[BankCode, ...]]:
    """Build the normalized alias index used by :func:`resolve_bank`.

    Returns:
        A read-only mapping from normalized alias to the distinct bank codes
        that claim it.
    """
    collected: dict[str, set[BankCode]] = {}
    for member in BankCode:
        for alias in (member.name, member.display_name):
            key = _normalize_alias(alias)
            if key:
                collected.setdefault(key, set()).add(member)
    for profile in BANK_SMS_PROFILES.values():
        for alias in profile.aliases:
            key = _normalize_alias(alias)
            if key:
                collected.setdefault(key, set()).add(profile.bank)
    return MappingProxyType(
        {key: tuple(codes) for key, codes in collected.items()}
    )


_ALIAS_CANDIDATES: Mapping[str, tuple[BankCode, ...]] = _build_alias_candidates()


def resolve_bank(value: str) -> BankSmsProfile | None:
    """Resolve a numeric code, member name or alias to a bank profile.

    A four-digit input is looked up only in :data:`BANK_SMS_PROFILES`; any
    other input is matched against bank names and aliases, longest alias first.
    Unknown, unsupported, empty or ambiguous input yields ``None``; the result
    is never ``OTHER`` and never a nearest bank.

    Args:
        value: Raw bank code, name or alias.

    Returns:
        The matching profile, or ``None`` when no single supported bank
        matches.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if _NUMERIC_CODE_RE.fullmatch(trimmed):
        return BANK_SMS_PROFILES.get(trimmed)
    normalized = _normalize_alias(trimmed)
    if not normalized:
        return None
    best_length = 0
    matched: set[BankCode] = set()
    for alias, codes in _ALIAS_CANDIDATES.items():
        if normalized == alias or normalized.startswith(alias) or alias in normalized:
            if len(alias) > best_length:
                best_length = len(alias)
                matched = set(codes)
            elif len(alias) == best_length:
                matched.update(codes)
    if len(matched) != 1:
        return None
    return _PROFILE_BY_BANK.get(next(iter(matched)))


def profile_for_sender(sender: str) -> BankSmsProfile | None:
    """Return the profile whose short code matches an inbound sender.

    Args:
        sender: SMS sender short code.

    Returns:
        The matching profile, or ``None`` when the sender is unknown.
    """
    if not isinstance(sender, str):
        return None
    return SHORT_CODE_INDEX.get(sender.strip())


def apply_short_code_overrides(
    profile: BankSmsProfile,
    overrides: Sequence[str],
) -> BankSmsProfile:
    """Return a copy of ``profile`` with overrides ordered ahead of defaults.

    Args:
        profile: Base profile.
        overrides: Additional short codes, primary first.

    Returns:
        A new profile whose short codes are the overrides followed by the
        remaining defaults, de-duplicated while preserving order.
    """
    merged: list[str] = []
    for code in (*overrides, *profile.short_codes):
        cleaned = code.strip()
        if cleaned and cleaned not in merged:
            merged.append(cleaned)
    return replace(profile, short_codes=tuple(merged))


def format_ves_amount(value: Decimal) -> str:
    """Render a Decimal in Venezuelan amount format.

    The output uses a comma decimal separator, exactly two decimals, no
    thousands separator and no currency symbol.

    Args:
        value: Positive monetary amount.  Floats are forbidden.

    Returns:
        The amount rendered as ``"1500,00"``.

    Raises:
        BankSmsFormatError: If ``value`` is not a finite positive ``Decimal``.
    """
    if not isinstance(value, Decimal):
        raise BankSmsFormatError("Amount must be a Decimal, not a float")
    if not value.is_finite():
        raise BankSmsFormatError("Amount must be a finite Decimal")
    if value <= 0:
        raise BankSmsFormatError("Amount must be greater than zero")
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
        BankSmsFormatError: If the value is not a valid Venezuelan mobile
            number.
    """
    if not isinstance(phone, str) or not phone.strip():
        raise BankSmsFormatError("Phone number is required")
    try:
        e164 = validate_venezuelan_phone(phone)
    except ValueError as exc:
        raise BankSmsFormatError(str(exc)) from exc
    national = f"0{e164.removeprefix('+58')}"
    if not _NATIONAL_PHONE_RE.fullmatch(national):
        raise BankSmsFormatError("Phone must normalize to eleven national digits")
    return national


def cedula_digits(value: str, *, cedula_format: str | None = None) -> str:
    """Reduce a Venezuelan identity document to bare digits.

    Strips the ``V``/``E``/``J``/``G``/``P`` prefix, dashes and spaces, requires
    six to nine digits, then applies ``cedula_format`` when supplied.

    Args:
        value: Raw identity document such as ``"V-12345678"``.
        cedula_format: Optional template with a ``{cedula}`` placeholder.

    Returns:
        The bare digits, or the formatted document when a template is given.

    Raises:
        BankSmsFormatError: If the value cannot be reduced to 6-9 digits.
    """
    if not isinstance(value, str) or not value.strip():
        raise BankSmsFormatError("Cedula is required")
    cleaned = _SEPARATOR_RE.sub("", value.strip())
    cleaned = _CEDULA_PREFIX_RE.sub("", cleaned)
    if not cleaned.isdigit() or not 6 <= len(cleaned) <= 9:
        raise BankSmsFormatError(
            "Cedula must contain 6-9 digits with an optional V/E/J/G/P/R prefix"
        )
    if cedula_format is None:
        return cleaned
    return cedula_format.format(cedula=cleaned)


def parse_ves_amount(text: str) -> Decimal:
    """Parse a Venezuelan-formatted amount back into a Decimal.

    Strips grouping dots, treats the comma as the decimal separator and
    quantizes to two decimals.  Unmatched text is refused rather than guessed.

    Args:
        text: Amount text such as ``"1.500,00"``.

    Returns:
        The parsed positive amount quantized to two decimals.

    Raises:
        BankSmsParseError: If the text is not a recognizable positive amount.
    """
    if not isinstance(text, str) or not text.strip():
        raise BankSmsParseError("Amount text is required")
    cleaned = text.strip().replace(" ", "").replace(".", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise BankSmsParseError(f"Unrecognized amount: {text!r}") from exc
    if not value.is_finite() or value <= 0:
        raise BankSmsParseError(f"Amount must be a positive number: {text!r}")
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def render_bank_command(
    profile: BankSmsProfile,
    *,
    phone: str,
    cedula: str,
    amount: Decimal,
) -> str:
    """Render the exact Pago Movil command for a bank profile.

    Every component is validated before rendering; a missing or invalid
    component raises instead of producing a partial command.

    Args:
        profile: Bank profile providing the code and template.
        phone: Counterparty phone in any accepted form.
        cedula: Counterparty identity document in any accepted form.
        amount: Positive ``Decimal`` amount to pay.

    Returns:
        The command such as ``"Pagar 0102 04141234567 12345678 1500,00"``.

    Raises:
        BankSmsFormatError: If any component is invalid or the rendered command
            would contain stray whitespace.
    """
    if not isinstance(profile, BankSmsProfile):
        raise BankSmsFormatError("A bank profile is required")
    numeric_code = profile.numeric_code.strip()
    if not _NUMERIC_CODE_RE.fullmatch(numeric_code):
        raise BankSmsFormatError("Bank numeric code must be four digits")
    phone_national = national_digits(phone)
    rendered_cedula = cedula_digits(cedula, cedula_format=profile.cedula_format)
    amount_ve = format_ves_amount(amount)
    command = profile.command_template.format(
        numeric_code=numeric_code,
        phone_national=phone_national,
        cedula=rendered_cedula,
        amount_ve=amount_ve,
    )
    if " ".join(command.split()) != command:
        raise BankSmsFormatError("Rendered command contains stray whitespace")
    return command

"""Pure Banesco Online payment domain.

This module holds the deterministic, I/O-free parts of the Banesco Pago Móvil
rail: security-question normalization, the two-control phone/document
projections, the versioned destination-bank map, the payment-target DTO and its
amount rendering.  It never imports Playwright, never reads settings and never
performs I/O.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

from app.core.exceptions import (
    BanescoLayoutUnexpected,
    ClaveDinamicaRejected,
    DestinationBankAmbiguous,
    IdTypeMissing,
    IdTypeUnsupported,
    PAYMENT_RAIL_ERROR_CODES,
    PaymentDataUnavailable,
    UnsupportedDestinationBank,
    UnsupportedPhonePrefix,
)
from app.domain.banks import BankSmsFormatError, format_ves_amount, national_digits

__all__ = [
    "QuestionKey",
    "QUESTION_EXAMPLE_LABELS",
    "QUESTION_ALIASES",
    "QUESTION_ANSWER_SETTINGS",
    "normalize_question",
    "resolve_question_keys",
    "BANESCO_PHONE_PREFIXES",
    "project_phone",
    "project_document",
    "project_banesco_document",
    "CLAVE_DINAMICA_MIN_DIGITS",
    "CLAVE_DINAMICA_MAX_DIGITS",
    "validate_clave_dinamica",
    "BanescoBankOption",
    "BANESCO_DESTINATION_BANK_OPTIONS_VERSION",
    "BANESCO_DESTINATION_BANK_OPTIONS",
    "match_destination_bank_option",
    "BanescoPaymentTarget",
    "format_ves_amount",
    "PAYMENT_RAIL_ERROR_CODES",
]


class QuestionKey(str, Enum):
    """Identity of one supported Banesco security question.

    The values are generic question identities, never the operator's answer.
    """

    HOBBIE = "HOBBIE"
    CHILDHOOD_HERO = "CHILDHOOD_HERO"
    FAVOURITE_DESSERT = "FAVOURITE_DESSERT"
    COUNTRY_TO_VISIT = "COUNTRY_TO_VISIT"


QUESTION_EXAMPLE_LABELS: Mapping[QuestionKey, str] = MappingProxyType(
    {
        QuestionKey.HOBBIE: "hobbie",
        QuestionKey.CHILDHOOD_HERO: "¿Quién fue tu héroe de la infancia?",
        QuestionKey.FAVOURITE_DESSERT: "¿Cuál es tu postre favorito?",
        QuestionKey.COUNTRY_TO_VISIT: "¿Qué país siempre has querido conocer?",
    }
)
"""Sanitized generic wording for each key; never the operator's real text."""

QUESTION_ANSWER_SETTINGS: Mapping[QuestionKey, str] = MappingProxyType(
    {
        QuestionKey.HOBBIE: "BANESCO_SECURITY_ANSWER_HOBBIE",
        QuestionKey.CHILDHOOD_HERO: "BANESCO_SECURITY_ANSWER_CHILDHOOD_HERO",
        QuestionKey.FAVOURITE_DESSERT: "BANESCO_SECURITY_ANSWER_FAVOURITE_DESSERT",
        QuestionKey.COUNTRY_TO_VISIT: "BANESCO_SECURITY_ANSWER_COUNTRY_TO_VISIT",
    }
)
"""Settings variable name for each key; failures may name these only."""

_QUESTION_ALIAS_SOURCES: tuple[tuple[str, QuestionKey], ...] = (
    # Live Spanish wording captured from the real security-question screen.
    ("hobbie", QuestionKey.HOBBIE),
    ("¿Quién fue el héroe de su infancia?", QuestionKey.CHILDHOOD_HERO),
    ("¿Cuál es mi postre favorito?", QuestionKey.FAVOURITE_DESSERT),
    ("¿Quién fue tu héroe de la infancia?", QuestionKey.CHILDHOOD_HERO),
    ("¿Quién fue tu héroe cuando eras niño?", QuestionKey.CHILDHOOD_HERO),
    ("¿Cuál es tu postre favorito?", QuestionKey.FAVOURITE_DESSERT),
    ("¿Cuál es tu dulce favorito?", QuestionKey.FAVOURITE_DESSERT),
    ("¿Qué país siempre has querido conocer?", QuestionKey.COUNTRY_TO_VISIT),
    ("¿Cuál país siempre has querido conocer?", QuestionKey.COUNTRY_TO_VISIT),
    # English forms are retained as harmless additional aliases.
    ("hobby", QuestionKey.HOBBIE),
    ("What is your favourite hobby?", QuestionKey.HOBBIE),
    ("What is your favorite hobby?", QuestionKey.HOBBIE),
    ("Who was your childhood hero?", QuestionKey.CHILDHOOD_HERO),
    ("What is your favourite dessert?", QuestionKey.FAVOURITE_DESSERT),
    ("What is your favorite dessert?", QuestionKey.FAVOURITE_DESSERT),
    ("Which country would you like to visit?", QuestionKey.COUNTRY_TO_VISIT),
    ("What country would you like to visit?", QuestionKey.COUNTRY_TO_VISIT),
)

_TRAILING_PUNCT_RE = re.compile(r"[\s?:.]+$")
_REQUIRED_RECOGNIZED_QUESTIONS = 2


def _strip_combining_marks(value: str) -> str:
    """Remove Unicode combining marks from decomposed text.

    Args:
        value: NFKD-decomposed text.

    Returns:
        Text without combining marks.
    """
    return "".join(char for char in value if not unicodedata.combining(char))


def normalize_question(text: str) -> str:
    """Normalize a security-question label for exact alias matching.

    Applies NFKD decomposition, strips combining marks, casefolds, collapses
    whitespace and strips trailing ``?``, ``:`` and ``.`` characters.

    Args:
        text: Visible question label read from the bank page.

    Returns:
        The normalized label.

    Raises:
        BanescoLayoutUnexpected: If ``text`` is not a string.
    """
    if not isinstance(text, str):
        raise BanescoLayoutUnexpected("Security question label is not text")
    decomposed = unicodedata.normalize("NFKD", text)
    casefolded = _strip_combining_marks(decomposed).casefold()
    collapsed = " ".join(casefolded.split())
    return _TRAILING_PUNCT_RE.sub("", collapsed).strip()


def _build_question_aliases() -> Mapping[str, QuestionKey]:
    """Build the immutable normalized-label to question-key index.

    Returns:
        A read-only exact-match alias table.
    """
    return MappingProxyType(
        {
            normalize_question(label): key
            for label, key in _QUESTION_ALIAS_SOURCES
        }
    )


QUESTION_ALIASES: Mapping[str, QuestionKey] = _build_question_aliases()
"""Exact normalized-label alias table; matching is never fuzzy."""


def resolve_question_keys(
    texts: Sequence[str],
    *,
    answers: Mapping[QuestionKey, str | None],
) -> tuple[QuestionKey, QuestionKey]:
    """Resolve exactly two distinct recognized question keys.

    Every label must resolve to a pinned key, the recognized count must be
    exactly two, the keys must be distinct, and each key must have a configured
    non-empty answer.  Anything else fails closed.  Order and position are
    never assumed.

    Args:
        texts: Visible question labels in display order.
        answers: Configured answer by key.  Values are inspected for presence
            only and never echoed.

    Returns:
        The two recognized keys in display order.

    Raises:
        BanescoLayoutUnexpected: On any wrong shape, unknown label, repeat or
            missing configured answer.
    """
    if not texts:
        raise BanescoLayoutUnexpected("No security questions were displayed")
    if len(texts) > _REQUIRED_RECOGNIZED_QUESTIONS:
        raise BanescoLayoutUnexpected(
            "More than two security questions were displayed"
        )
    resolved: list[QuestionKey] = []
    for raw in texts:
        normalized = normalize_question(raw)
        key = QUESTION_ALIASES.get(normalized)
        if key is None:
            raise BanescoLayoutUnexpected(
                "Unknown security question label", details={"stage": "questions"}
            )
        resolved.append(key)
    if len(resolved) != _REQUIRED_RECOGNIZED_QUESTIONS:
        raise BanescoLayoutUnexpected(
            "Exactly two security questions are required"
        )
    if len(set(resolved)) != _REQUIRED_RECOGNIZED_QUESTIONS:
        raise BanescoLayoutUnexpected("Repeated security question")
    for key in resolved:
        answer = answers.get(key)
        if not isinstance(answer, str) or not answer.strip():
            raise BanescoLayoutUnexpected(
                "Missing configured security answer for "
                f"{QUESTION_ANSWER_SETTINGS[key]}",
                details={"setting": QUESTION_ANSWER_SETTINGS[key]},
            )
    return (resolved[0], resolved[1])


BANESCO_PHONE_PREFIXES: frozenset[str] = frozenset(
    {"0412", "0414", "0416", "0422", "0424", "0426"}
)
"""National mobile prefixes the Banesco payment form offers (captured live)."""

_PHONE_REST_RE = re.compile(r"^\d{7}$")
_DOCUMENT_DIGITS_RE = re.compile(r"^\d{6,9}$")
_DOCUMENT_SEPARATORS_RE = re.compile(r"[\s.\-]")
_DOCUMENT_LETTERS: frozenset[str] = frozenset("VEJGPR")
_BANESCO_ID_TYPES: frozenset[str] = frozenset("VEJGR")


def project_phone(phone: str) -> tuple[str, str]:
    """Project a beneficiary phone onto the two Banesco form controls.

    The first four normalized national digits become the offered prefix and the
    remaining digits become the second control.  The prefix must be one of the
    reviewed Banesco prefixes; the remainder must be exactly seven digits.  A
    full phone is never projected into one control and no prefix is guessed.

    Args:
        phone: Beneficiary phone in any accepted Venezuelan form.

    Returns:
        A ``(prefix, rest)`` tuple such as ``("0414", "1234567")``.

    Raises:
        UnsupportedPhonePrefix: If the national prefix is not offered.
        PaymentDataUnavailable: If the phone cannot be normalized.
    """
    try:
        national = national_digits(phone)
    except BankSmsFormatError as exc:
        raise PaymentDataUnavailable(
            "Beneficiary phone is not a valid Venezuelan mobile number"
        ) from exc
    prefix, rest = national[:4], national[4:]
    if prefix not in BANESCO_PHONE_PREFIXES:
        raise UnsupportedPhonePrefix(details={"stage": "phone"})
    if not _PHONE_REST_RE.fullmatch(rest):
        raise PaymentDataUnavailable(
            "Beneficiary phone remainder must be exactly seven digits"
        )
    return prefix, rest


def project_document(document: str) -> tuple[str, str]:
    """Project an identity document onto a normalized letter and bare digits.

    The domain layer normalizes ``V/E/J/G/R`` and also ``P``.  A missing letter
    is never defaulted to ``V``.

    Args:
        document: Document such as ``"V-12345678"`` or ``"r12345678"``.

    Returns:
        A ``(letter, digits)`` tuple such as ``("V", "12345678")``.

    Raises:
        IdTypeMissing: If no leading letter is present.
        IdTypeUnsupported: If the leading letter is outside ``V/E/J/G/R/P``.
        PaymentDataUnavailable: If the digits are not 6-9 bare digits.
    """
    if not isinstance(document, str) or not document.strip():
        raise PaymentDataUnavailable("Identity document is required")
    compact = _DOCUMENT_SEPARATORS_RE.sub("", document.strip())
    if not compact:
        raise PaymentDataUnavailable("Identity document is required")
    letter = compact[0]
    if letter.isdigit():
        raise IdTypeMissing(
            "Identity document has no type letter", details={"stage": "document"}
        )
    if not letter.isalpha():
        raise PaymentDataUnavailable("Identity document is malformed")
    normalized_letter = letter.upper()
    if normalized_letter not in _DOCUMENT_LETTERS:
        raise IdTypeUnsupported(details={"stage": "document"})
    digits = compact[1:]
    if not _DOCUMENT_DIGITS_RE.fullmatch(digits):
        raise PaymentDataUnavailable(
            "Identity document digits must be 6-9 bare digits"
        )
    return normalized_letter, digits


def project_banesco_document(document: str) -> tuple[str, str]:
    """Project a document that the Banesco mapper will accept.

    Accepts only ``V/E/J/G/R``.  ``P`` normalizes at the domain layer but the
    rail must reject it rather than substitute another letter.

    Args:
        document: Document in any accepted domain form.

    Returns:
        A ``(letter, digits)`` tuple with a Banesco-accepted letter.

    Raises:
        IdTypeUnsupported: If the normalized letter is ``P`` or otherwise
            outside the Banesco set.
        IdTypeMissing: If no leading letter is present.
        PaymentDataUnavailable: If the document is malformed.
    """
    letter, digits = project_document(document)
    if letter not in _BANESCO_ID_TYPES:
        raise IdTypeUnsupported(details={"stage": "document"})
    return letter, digits


CLAVE_DINAMICA_MIN_DIGITS = 6
"""Minimum accepted Clave Dinámica length, confirmed from the live OTP field."""

CLAVE_DINAMICA_MAX_DIGITS = 8
"""Maximum accepted Clave Dinámica length, confirmed from the live OTP field."""

_CLAVE_DIGITS_RE = re.compile(r"^[0-9]{6,8}$")


def validate_clave_dinamica(value: str) -> str:
    """Validate a Clave Dinámica of six to eight bare digits.

    The live ``#otp`` control accepts six to eight digits (``maxlength="8"``),
    so a six-digit-only rule would reject valid keys.  The key is never echoed
    in a failure and is returned stripped of surrounding whitespace.

    Args:
        value: Operator-provided key, possibly padded with whitespace.

    Returns:
        The stripped key.

    Raises:
        ClaveDinamicaRejected: If the value is not text or not 6-8 ASCII
            digits.
    """
    if not isinstance(value, str):
        raise ClaveDinamicaRejected(details={"stage": "clave"})
    stripped = value.strip()
    if not _CLAVE_DIGITS_RE.fullmatch(stripped):
        raise ClaveDinamicaRejected(details={"stage": "clave"})
    return stripped


@dataclass(frozen=True, slots=True)
class BanescoBankOption:
    """One reviewed ``Banco Destino`` option on the Banesco payment form.

    Attributes:
        numeric_code: Four-digit destination bank code.
        label: Exact reviewed ``<option>`` label.
        value: Exact reviewed ``<option value>`` when the page sets one.
    """

    numeric_code: str
    label: str
    value: str | None


BANESCO_DESTINATION_BANK_OPTIONS_VERSION = 2
"""Version of the reviewed destination-bank option map.

Version 2 replaces the two provisional labels with the 25 live ``#banco``
options captured from the real Pago Móvil form.
"""

_BANESCO_BANK_OPTION_SOURCES: tuple[tuple[str, str], ...] = (
    ("0134", "BANESCO BANCO UNIVERSAL S.A.C.A."),
    ("0156", "100% BANCO, BANCO UNIVERSAL C.A."),
    ("0172", "BANCAMIGA BANCO UNIVERSAL, C.A."),
    ("0114", "BANCARIBE C.A. BANCO UNIVERSAL"),
    ("0171", "BANCO ACTIVO"),
    ("0128", "BANCO CARONI C.A. BANCO UNIVERSAL"),
    ("0177", "BANCO DE LA FANB"),
    ("0146", "BANCO DE LA GENTE EMPRENDEDORA C.A."),
    ("0102", "BANCO DE VENEZUELA"),
    ("0163", "BANCO DEL TESORO"),
    ("0007", "BANCO DIGITAL DE LOS TRABAJADORES"),
    ("0178", "BANCO DIGITAL N58"),
    ("0115", "BANCO EXTERIOR C.A. BANCO UNIVERSAL"),
    ("0105", "BANCO MERCANTIL"),
    ("0191", "BANCO NACIONAL DE CREDITO"),
    ("0138", "BANCO PLAZA, BANCO UNIVERSAL"),
    ("0108", "BANCO PROVINCIAL"),
    ("0137", "BANCO SOFITASA"),
    ("0168", "BANCRECER, S.A. BANCO MICROFINANCIERO"),
    ("0174", "BANPLUS BANCO UNIVERSAL, C.A."),
    ("0151", "BFC BANCO FONDO COMUN"),
    ("0157", "DEL SUR BANCO UNIVERSAL C.A."),
    ("0601", "INSTITUTO MUNICIPAL DE CREDITO POPULAR"),
    ("0169", "R4, BANCO MICROFINANCIERO, C.A"),
    ("0104", "VENEZOLANO DE CREDITO"),
)

BANESCO_DESTINATION_BANK_OPTIONS: Mapping[str, BanescoBankOption] = MappingProxyType(
    {
        code: BanescoBankOption(numeric_code=code, label=label, value=code)
        for code, label in _BANESCO_BANK_OPTION_SOURCES
    }
)
"""Frozen, versioned destination-bank options supported by the rail."""

_OPTION_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_option_text(text: str) -> str:
    """Normalize a bank option label for exact matching.

    Args:
        text: Live or reviewed option label.

    Returns:
        Accent-free, whitespace-collapsed, casefolded text.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = _strip_combining_marks(decomposed)
    return _OPTION_WHITESPACE_RE.sub(" ", without_accents.casefold()).strip()


def match_destination_bank_option(
    numeric_code: str,
    live_options: Sequence[BanescoBankOption],
) -> BanescoBankOption:
    """Select the single reviewed live option for a destination code.

    Matching is exact after whitespace/accent normalization.  It never
    fuzzy-matches, never selects by position and never substitutes a nearest
    bank.

    Args:
        numeric_code: Four-digit destination bank code from the order.
        live_options: Options read from the live ``Banco Destino`` control.

    Returns:
        The single matching live option.

    Raises:
        UnsupportedDestinationBank: When the code is not reviewed, the live
            label changed, or no live option matches.
        DestinationBankAmbiguous: When more than one live option matches.
    """
    reviewed = BANESCO_DESTINATION_BANK_OPTIONS.get(numeric_code)
    if reviewed is None:
        raise UnsupportedDestinationBank(details={"reason": "code_not_reviewed"})
    reviewed_label = _normalize_option_text(reviewed.label)
    matches = [
        option
        for option in live_options
        if isinstance(option, BanescoBankOption)
        and _normalize_option_text(option.label) == reviewed_label
        and (reviewed.value is None or option.value == reviewed.value)
    ]
    if not matches:
        raise UnsupportedDestinationBank(details={"reason": "label_changed"})
    if len(matches) > 1:
        raise DestinationBankAmbiguous()
    return matches[0]


@dataclass(frozen=True, slots=True)
class BanescoPaymentTarget:
    """Fully validated two-control payment target.

    Attributes:
        phone_prefix: Four offered national digits such as ``"0414"``.
        phone_rest: Exactly seven national digits.
        id_type: One of ``V``, ``E``, ``J``, ``G`` or ``R``.
        id_digits: Six to nine bare document digits.
        destination_bank_code: Reviewed four-digit destination code.
        destination_bank_option: Exact reviewed live option label.
        amount_ves: Positive, finite ``Decimal`` amount in VES.
        concept: Non-empty PII-free concept text.
    """

    phone_prefix: str
    phone_rest: str
    id_type: str
    id_digits: str
    destination_bank_code: str
    destination_bank_option: str
    amount_ves: Decimal
    concept: str

    def __post_init__(self) -> None:
        """Reject any malformed target before a browser session can start.

        Raises:
            PaymentDataUnavailable: If any field violates its invariant.
        """
        if self.phone_prefix not in BANESCO_PHONE_PREFIXES:
            raise PaymentDataUnavailable("Unsupported phone prefix")
        if not isinstance(self.phone_rest, str) or not _PHONE_REST_RE.fullmatch(
            self.phone_rest
        ):
            raise PaymentDataUnavailable("Phone remainder must be seven digits")
        if self.id_type not in _BANESCO_ID_TYPES:
            raise PaymentDataUnavailable("Document type is not Banesco-supported")
        if not isinstance(self.id_digits, str) or not _DOCUMENT_DIGITS_RE.fullmatch(
            self.id_digits
        ):
            raise PaymentDataUnavailable("Document digits must be 6-9 digits")
        if self.destination_bank_code not in BANESCO_DESTINATION_BANK_OPTIONS:
            raise PaymentDataUnavailable("Destination bank is not reviewed")
        if (
            not isinstance(self.destination_bank_option, str)
            or not self.destination_bank_option.strip()
        ):
            raise PaymentDataUnavailable("Destination bank option label is required")
        if (
            not isinstance(self.amount_ves, Decimal)
            or not self.amount_ves.is_finite()
            or self.amount_ves <= 0
        ):
            raise PaymentDataUnavailable(
                "Amount must be a positive finite Decimal"
            )
        if not isinstance(self.concept, str) or not self.concept.strip():
            raise PaymentDataUnavailable("Concept text is required")

"""Domain enumerations and the order state machine."""

from __future__ import annotations

import unicodedata
from enum import Enum


class OrderType(str, Enum):
    """Direction of a P2P trade from the operator's perspective.

    ``BUY`` means we receive VES and release crypto to the counterparty.
    ``SELL`` means we pay VES and purchase crypto from the counterparty.
    """

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """Lifecycle of a P2P order."""

    PENDING = "PENDING"
    PAYMENT_DETECTED = "PAYMENT_DETECTED"
    VERIFYING_CACHICAMO = "VERIFYING_CACHICAMO"
    SENDING_SMS = "SENDING_SMS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    MANUAL_REVIEW = "MANUAL_REVIEW"

    def can_transition_to(self, target: "OrderStatus") -> bool:
        """Return whether a transition to ``target`` is legal.

        Args:
            target: Destination status.

        Returns:
            ``True`` when the transition is declared in
            :data:`ALLOWED_TRANSITIONS`.
        """
        return target in ALLOWED_TRANSITIONS[self]

    def is_terminal(self) -> bool:
        """Return ``True`` when the status admits no further transitions."""
        return not ALLOWED_TRANSITIONS[self]

    def is_successful(self) -> bool:
        """Return ``True`` only for the successfully completed status."""
        return self is OrderStatus.COMPLETED


ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset(
        {OrderStatus.PAYMENT_DETECTED, OrderStatus.FAILED, OrderStatus.MANUAL_REVIEW}
    ),
    OrderStatus.PAYMENT_DETECTED: frozenset(
        {OrderStatus.VERIFYING_CACHICAMO, OrderStatus.MANUAL_REVIEW, OrderStatus.FAILED}
    ),
    OrderStatus.VERIFYING_CACHICAMO: frozenset(
        {
            OrderStatus.SENDING_SMS,
            OrderStatus.COMPLETED,
            OrderStatus.MANUAL_REVIEW,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.SENDING_SMS: frozenset(
        {OrderStatus.COMPLETED, OrderStatus.MANUAL_REVIEW, OrderStatus.FAILED}
    ),
    OrderStatus.COMPLETED: frozenset(),
    OrderStatus.FAILED: frozenset(),
    OrderStatus.MANUAL_REVIEW: frozenset({OrderStatus.PENDING, OrderStatus.FAILED}),
}


class BankCode(str, Enum):
    """Venezuelan banks relevant to mobile payment reconciliation."""

    BANESCO = "BANESCO"
    MERCANTIL = "MERCANTIL"
    PROVINCIAL = "PROVINCIAL"
    BANCO_DE_VENEZUELA = "BANCO_DE_VENEZUELA"
    BNC = "BNC"
    BOD = "BOD"
    BANPLUS = "BANPLUS"
    BANCAMIGA = "BANCAMIGA"
    BANCO_EXTERIOR = "BANCO_EXTERIOR"
    BANCO_OCCIDENTAL = "BANCO_OCCIDENTAL"
    BICENTENARIO = "BICENTENARIO"
    BANCO_DEL_TESORO = "BANCO_DEL_TESORO"
    BANCO_PLAZA = "BANCO_PLAZA"
    BANFANB = "BANFANB"
    SOFITASA = "SOFITASA"
    FONDO_COMUN = "FONDO_COMUN"
    CACHICAMO = "CACHICAMO"
    OTHER = "OTHER"

    @property
    def display_name(self) -> str:
        """Return a human readable bank name."""
        return _BANK_DISPLAY_NAMES[self]

    @classmethod
    def from_aliases(cls, value: str) -> "BankCode":
        """Resolve a free-form bank spelling to a member.

        Matching is case- and accent-insensitive and tolerates generic
        prefixes/suffixes such as ``"banco"``.

        Args:
            value: Raw bank name as received from a provider or operator.

        Returns:
            The matching :class:`BankCode`, or :attr:`BankCode.OTHER` when
            unknown.
        """
        normalized = _normalize_alias(value)
        if not normalized:
            return cls.OTHER
        direct = _BANK_ALIASES.get(normalized)
        if direct is not None:
            return direct
        for alias, code in _BANK_ALIASES.items():
            if normalized.startswith(alias) or alias in normalized:
                return code
        return cls.OTHER


_BANK_DISPLAY_NAMES: dict[BankCode, str] = {
    BankCode.BANESCO: "Banesco Banco Universal",
    BankCode.MERCANTIL: "Banco Mercantil",
    BankCode.PROVINCIAL: "BBVA Provincial",
    BankCode.BANCO_DE_VENEZUELA: "Banco de Venezuela",
    BankCode.BNC: "Banco Nacional de Credito",
    BankCode.BOD: "Banco Occidental de Descuento",
    BankCode.BANPLUS: "Banplus Banco Universal",
    BankCode.BANCAMIGA: "Bancamiga Banco Universal",
    BankCode.BANCO_EXTERIOR: "Banco Exterior",
    BankCode.BANCO_OCCIDENTAL: "Banco Occidental",
    BankCode.BICENTENARIO: "Banco Bicentenario",
    BankCode.BANCO_DEL_TESORO: "Banco del Tesoro",
    BankCode.BANCO_PLAZA: "Banco Plaza",
    BankCode.BANFANB: "Banfanb",
    BankCode.SOFITASA: "Sofitasa",
    BankCode.FONDO_COMUN: "Fondo Comun",
    BankCode.CACHICAMO: "Cachicamo",
    BankCode.OTHER: "Other",
}


def _normalize_alias(value: str) -> str:
    """Normalize free-form text for bank alias matching.

    Args:
        value: Raw text.

    Returns:
        Lowercase ASCII text without separators.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return "".join(char for char in without_accents.lower() if char.isalnum())


_BANK_ALIAS_ENTRIES: tuple[tuple[str, BankCode], ...] = (
    # Enum names, normalized from the member names.
    *(tuple((member.name, member) for member in BankCode)),
    # Common provider spellings.
    ("Banesco Banco Universal", BankCode.BANESCO),
    ("Banco Mercantil", BankCode.MERCANTIL),
    ("Mercantil Banco", BankCode.MERCANTIL),
    ("BBVA", BankCode.PROVINCIAL),
    ("BBVA Provincial", BankCode.PROVINCIAL),
    ("Provincial", BankCode.PROVINCIAL),
    ("Banco de Venezuela", BankCode.BANCO_DE_VENEZUELA),
    ("Banco de Venezuela BVC", BankCode.BANCO_DE_VENEZUELA),
    ("Bdv", BankCode.BANCO_DE_VENEZUELA),
    ("Banco Nacional de Credito", BankCode.BNC),
    ("Banco Nacional de CrÃ©dito", BankCode.BNC),
    ("Banco Occidental de Descuento", BankCode.BOD),
    ("Banco Exterior", BankCode.BANCO_EXTERIOR),
    ("Banco Bicentenario", BankCode.BICENTENARIO),
    ("Banco del Tesoro", BankCode.BANCO_DEL_TESORO),
    ("Banco Plaza", BankCode.BANCO_PLAZA),
    ("Fondo Comun", BankCode.FONDO_COMUN),
    ("Fondo ComÃºn", BankCode.FONDO_COMUN),
    ("Bancamiga", BankCode.BANCAMIGA),
    ("Bancaribe", BankCode.OTHER),
)

_BANK_ALIASES: dict[str, BankCode] = {
    _normalize_alias(alias): member for alias, member in _BANK_ALIAS_ENTRIES
}


class BankTransactionDirection(str, Enum):
    """Cash movement direction relative to our accounts."""

    INCOMING = "INCOMING"
    OUTGOING = "OUTGOING"


class PaymentVerificationStatus(str, Enum):
    """Result of verifying a mobile payment with Cachicamo."""

    VERIFIED = "VERIFIED"
    NOT_FOUND = "NOT_FOUND"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    PENDING = "PENDING"
    REJECTED = "REJECTED"


class SmsDirection(str, Enum):
    """Direction of an SMS handled by the Android gateway."""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class AuditActor(str, Enum):
    """Who or what produced an audit entry."""

    SYSTEM = "SYSTEM"
    WORKER = "WORKER"
    WEBHOOK = "WEBHOOK"
    OPERATOR = "OPERATOR"
    API_CLIENT = "API_CLIENT"


class AuditEvent(str, Enum):
    """Auditable event types."""

    ORDER_CREATED = "ORDER_CREATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    PAYMENT_VERIFIED = "PAYMENT_VERIFIED"
    SMS_SENT = "SMS_SENT"
    SMS_RECEIVED = "SMS_RECEIVED"
    RISK_BLOCKED = "RISK_BLOCKED"
    IDEMPOTENCY_HIT = "IDEMPOTENCY_HIT"
    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED"
    WEBHOOK_REJECTED = "WEBHOOK_REJECTED"
    EXTERNAL_ERROR = "EXTERNAL_ERROR"


__all__ = [
    "OrderType",
    "OrderStatus",
    "ALLOWED_TRANSITIONS",
    "BankCode",
    "BankTransactionDirection",
    "PaymentVerificationStatus",
    "SmsDirection",
    "AuditActor",
    "AuditEvent",
]

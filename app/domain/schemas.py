"""Pydantic v2 schemas shared by the API, services and clients."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import BankCode, OrderStatus, OrderType

_PHONE_CLEAN_RE = re.compile(r"[\s\-().]")
_ID_PATTERN = re.compile(r"^([VEJGPR])-?(\d{6,9})$", re.IGNORECASE)


def validate_venezuelan_phone(value: str) -> str:
    """Normalize a Venezuelan mobile number to E.164 format.

    Accepts ``+58412...``, ``0058412...``, ``0412...`` and ``412...`` and
    returns ``+58`` plus ten digits.

    Args:
        value: Raw phone number.

    Returns:
        The normalized E.164 phone number.

    Raises:
        ValueError: If the value is not a valid Venezuelan mobile number.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Phone number is required")
    cleaned = _PHONE_CLEAN_RE.sub("", value.strip())
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    if cleaned.startswith("00"):
        cleaned = cleaned[2:]
    if cleaned.startswith("58"):
        national = cleaned[2:]
    elif cleaned.startswith("0"):
        national = cleaned[1:]
    else:
        national = cleaned
    if len(national) != 10 or not national.isdigit() or not national.startswith("4"):
        raise ValueError("Phone must be a Venezuelan mobile number such as +584121234567")
    return f"+58{national}"


def validate_venezuelan_id(value: str) -> str:
    """Validate and normalize a Venezuelan identity document.

    Args:
        value: Document number such as ``V-12345678``.

    Returns:
        Uppercase normalized document number with a dash.

    Raises:
        ValueError: If the document does not match the accepted pattern.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Identity document is required")
    normalized = value.strip().upper()
    if "-" not in normalized:
        normalized = f"{normalized[0]}-{normalized[1:]}" if normalized else normalized
    match = _ID_PATTERN.match(normalized)
    if not match:
        raise ValueError("Identity document must look like V-12345678, E-12345678 or J-12345678")
    return f"{match.group(1)}-{match.group(2)}"


class OrderStatusRead(BaseModel):
    """Read model exposing the order status."""

    model_config = ConfigDict(from_attributes=True)

    status: OrderStatus


class OrderRead(BaseModel):
    """Read model for a persisted P2P order."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_number: str
    order_type: OrderType
    status: OrderStatus
    asset: str
    fiat_currency: str
    price: Decimal
    amount_asset: Decimal
    amount_fiat: Decimal
    bank_code: BankCode | None = None
    bank_reference: str | None = None
    created_at: datetime
    updated_at: datetime


class OrderCreateRequest(BaseModel):
    """Payload used to register a new order."""

    order_number: str = Field(min_length=1, max_length=64)
    order_type: OrderType
    price: Decimal = Field(gt=0)
    amount_asset: Decimal = Field(gt=0)
    amount_fiat: Decimal = Field(gt=0)
    counterparty_phone: str | None = None
    bank_code: BankCode | None = None
    bank_reference: str | None = Field(default=None, max_length=64)

    @field_validator("counterparty_phone")
    @classmethod
    def _validate_phone(cls, value: str | None) -> str | None:
        """Normalize the optional counterparty phone number."""
        return validate_venezuelan_phone(value) if value else value


class CachicamoWebhookPayload(BaseModel):
    """Inbound Cachicamo payment notification."""

    event_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=64)
    bank_reference: str = Field(min_length=1, max_length=64)
    amount_ves: Decimal = Field(gt=0)
    payer_phone: str | None = None
    bank_code: BankCode | None = None
    occurred_at: datetime

    @field_validator("amount_ves")
    @classmethod
    def _validate_amount(cls, value: Decimal) -> Decimal:
        """Require a positive amount with at most two decimal places."""
        if value <= 0:
            raise ValueError("amount_ves must be greater than zero")
        if value.as_tuple().exponent < -2:
            raise ValueError("amount_ves must have at most two decimal places")
        return value

    @field_validator("payer_phone")
    @classmethod
    def _validate_payer_phone(cls, value: str | None) -> str | None:
        """Normalize the optional payer phone number."""
        return validate_venezuelan_phone(value) if value else value


class SmsInboundWebhookPayload(BaseModel):
    """Inbound SMS notification from the Android gateway."""

    message_id: str = Field(min_length=1, max_length=128)
    sender: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1)
    received_at: datetime


class RiskAssessment(BaseModel):
    """Outcome of the pre-trade risk evaluation."""

    allowed: bool
    requires_manual_review: bool
    reason: str | None = None
    projected_daily_volume_ves: Decimal
    limits: dict[str, str]


class HealthComponent(BaseModel):
    """Health status of a single dependency."""

    name: str
    healthy: bool
    latency_ms: float
    detail: str | None = None


class HealthResponse(BaseModel):
    """Aggregated readiness report."""

    status: Literal["ok", "degraded"]
    version: str
    environment: str
    components: list[HealthComponent]


class WebhookAcceptedResponse(BaseModel):
    """Acknowledgement returned for every accepted webhook."""

    accepted: bool
    event_id: str | None = None
    duplicate: bool = False
    message: str


class SmsConfirmation(BaseModel):
    """Outcome of correlating one inbound bank reply.

    ``outcome`` is the same vocabulary the webhook exposes: ``confirmed`` for a
    parsed success with exactly one candidate, ``matched`` when the reply
    reached an order but needs a human (failure, unparseable, ambiguous parse,
    duplicate reference), ``ambiguous`` when several orders matched, and
    ``no_match`` otherwise.  ``duplicate`` marks a replay of an already
    processed ``message_id``.
    """

    outcome: Literal["matched", "confirmed", "ambiguous", "no_match"]
    order_number: str | None = None
    reason: str | None = None
    duplicate: bool = False


class SmsWebhookResponse(WebhookAcceptedResponse):
    """Acceptance envelope for the enriched SMS webhook contract."""

    outcome: Literal["matched", "confirmed", "ambiguous", "no_match"] = "no_match"
    order_number: str | None = None
    reason: str | None = None


class BuyPayRequest(BaseModel):
    """Optional operator payload for approving a parked BUY payout.

    ``destination_phone`` is normalized for logging/audit but never used as a
    gateway destination, never as payment data and never echoed back.
    """

    destination_phone: str | None = None
    reason: str | None = Field(default=None, max_length=280)

    @field_validator("destination_phone")
    @classmethod
    def _validate_phone(cls, value: str | None) -> str | None:
        """Normalize the optional operator-supplied phone number."""
        return validate_venezuelan_phone(value) if value else value


class BuyPayResponse(BaseModel):
    """Operator-facing view of a BUY payout attempt.

    Deliberately carries no amount, phone or cedula: only lifecycle data that
    is safe to show to an operator.
    """

    order_number: str
    status: OrderStatus
    outcome: Literal[
        "dispatched",
        "resumed",
        "awaiting_approval",
        "noop",
        "manual_review",
    ]
    manual_review_reason: str | None = None
    bank_short_code: str | None = None
    sms_message_id: str | None = None


class ApiErrorDetail(BaseModel):
    """Error body fields matching :meth:`LiquidVEException.to_dict`."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ApiErrorResponse(BaseModel):
    """Canonical API error envelope."""

    error: ApiErrorDetail

    @classmethod
    def from_exception(cls, exc: Any) -> "ApiErrorResponse":
        """Build the envelope from a :class:`LiquidVEException`-like object.

        Args:
            exc: Exception exposing ``code``, ``message`` and ``details``.

        Returns:
            The populated error envelope.
        """
        return cls(
            error=ApiErrorDetail(
                code=str(getattr(exc, "code", "liquidve_error")),
                message=str(getattr(exc, "message", str(exc))),
                details=dict(getattr(exc, "details", {}) or {}),
            )
        )


__all__ = [
    "validate_venezuelan_phone",
    "validate_venezuelan_id",
    "OrderStatusRead",
    "OrderRead",
    "OrderCreateRequest",
    "CachicamoWebhookPayload",
    "SmsInboundWebhookPayload",
    "RiskAssessment",
    "HealthComponent",
    "HealthResponse",
    "WebhookAcceptedResponse",
    "SmsConfirmation",
    "SmsWebhookResponse",
    "BuyPayRequest",
    "BuyPayResponse",
    "ApiErrorDetail",
    "ApiErrorResponse",
]

"""Cachicamo Cash API adapter.

Incoming payment verification is a safe, retried GET.  Outgoing payment
registration moves money, carries an ``Idempotency-Key`` header and is never
retried blindly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.exceptions import ExternalServiceResponseError
from app.domain.enums import BankCode, PaymentVerificationStatus
from app.infrastructure.clients.base import BaseAsyncHTTPClient

# TODO(integration): confirm paths against the Cachicamo Cash API contract.
_VERIFY_PATH = "/v1/payments/verify"
_OUTGOING_PATH = "/v1/payments/outgoing"
_BALANCE_PATH = "/v1/balance"
_HEALTH_PATH = "/health"


def _parse_optional_datetime(value: Any) -> datetime | None:
    """Parse an optional ISO-8601 timestamp.

    Args:
        value: Raw value.

    Returns:
        A timezone-aware datetime or ``None``.

    Raises:
        ValueError: If the value is present but malformed.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_decimal(value: Any, field_name: str) -> Decimal:
    """Parse a decimal field raising a typed error on failure.

    Args:
        value: Raw value.
        field_name: Field name used in the error message.

    Returns:
        The parsed decimal.

    Raises:
        ExternalServiceResponseError: If the value cannot be parsed.
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExternalServiceResponseError(
            f"Cachicamo response has an invalid {field_name}",
            service="cachicamo",
        ) from exc


class CachicamoVerificationResult(BaseModel):
    """Result of verifying an incoming mobile payment."""

    status: PaymentVerificationStatus
    bank_reference: str
    amount_ves: Decimal | None = None
    bank_code: BankCode | None = None
    verified_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CachicamoPaymentResult(BaseModel):
    """Result of registering an outgoing mobile payment."""

    external_id: str
    status: str
    amount_ves: Decimal
    created_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class CachicamoClient(BaseAsyncHTTPClient):
    """Client for the Cachicamo Cash API."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        """Initialize the client.

        Args:
            settings: Active application settings.
            client: Optional pre-built httpx client for tests.
        """
        super().__init__(settings, base_url=settings.cachicamo_base_url, client=client)

    @property
    def service_name(self) -> str:
        """Stable provider name."""
        return "cachicamo"

    def _auth_headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        """Build authorization and idempotency headers.

        Args:
            idempotency_key: Optional key required on money-moving calls.

        Returns:
            Header mapping.  Values are never logged.
        """
        headers = {
            "Authorization": f"Bearer {self._settings.cachicamo_api_key.get_secret_value()}",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def verify_incoming_payment(
        self,
        *,
        bank_reference: str,
        amount_ves: Decimal,
        payer_phone: str | None = None,
        bank_code: BankCode | None = None,
    ) -> CachicamoVerificationResult:
        """Verify that an incoming mobile payment was received.

        Args:
            bank_reference: Bank reference of the payment.
            amount_ves: Expected amount in VES.
            payer_phone: Optional payer phone in E.164 format.
            bank_code: Optional originating bank.

        Returns:
            The verification result normalized to domain types.

        Raises:
            ExternalServiceResponseError: If the payload is malformed.
        """
        params: dict[str, Any] = {
            "bank_reference": bank_reference,
            "amount_ves": str(amount_ves),
        }
        if payer_phone:
            params["payer_phone"] = payer_phone
        if bank_code:
            params["bank_code"] = bank_code.value
        payload = await self.get(
            _VERIFY_PATH,
            params=params,
            headers=self._auth_headers(),
            idempotent=True,
        )
        if not isinstance(payload, dict):
            raise ExternalServiceResponseError(
                "Cachicamo verification payload is not an object",
                service=self.service_name,
            )
        try:
            status = PaymentVerificationStatus(str(payload["status"]).upper())
        except (KeyError, ValueError) as exc:
            raise ExternalServiceResponseError(
                "Cachicamo verification status is invalid",
                service=self.service_name,
            ) from exc
        raw_bank_code = payload.get("bank_code")
        resolved_bank_code: BankCode | None = None
        if raw_bank_code:
            resolved_bank_code = BankCode.from_aliases(str(raw_bank_code))
        amount_raw = payload.get("amount_ves", payload.get("amount"))
        try:
            verified_at = _parse_optional_datetime(payload.get("verified_at"))
        except ValueError as exc:
            raise ExternalServiceResponseError(
                "Cachicamo verification timestamp is malformed",
                service=self.service_name,
            ) from exc
        return CachicamoVerificationResult(
            status=status,
            bank_reference=str(payload.get("bank_reference", bank_reference)),
            amount_ves=_parse_decimal(amount_raw, "amount_ves") if amount_raw is not None else None,
            bank_code=resolved_bank_code,
            verified_at=verified_at,
            raw=payload,
        )

    async def register_outgoing_payment(
        self,
        *,
        destination_phone: str,
        amount_ves: Decimal,
        bank_code: BankCode,
        concept: str,
        idempotency_key: str,
    ) -> CachicamoPaymentResult:
        """Register an outgoing mobile payment.

        This call moves money.  It sends ``Idempotency-Key`` and is executed
        with retries disabled so a transient failure never double-pays.

        Args:
            destination_phone: Destination phone in E.164 format.
            amount_ves: Amount to pay.
            bank_code: Destination bank.
            concept: Payment concept shown to the recipient.
            idempotency_key: Stable key unique to this payout.

        Returns:
            The registered payment.

        Raises:
            ExternalServiceResponseError: If the payload is malformed.
        """
        body = {
            "destination_phone": destination_phone,
            "amount_ves": str(amount_ves),
            "bank_code": bank_code.value,
            "concept": concept,
        }
        payload = await self.post(
            _OUTGOING_PATH,
            json=body,
            headers=self._auth_headers(idempotency_key=idempotency_key),
            idempotent=False,
        )
        if not isinstance(payload, dict):
            raise ExternalServiceResponseError(
                "Cachicamo payment payload is not an object",
                service=self.service_name,
            )
        try:
            created_at = _parse_optional_datetime(payload.get("created_at")) or datetime.now(
                timezone.utc
            )
            return CachicamoPaymentResult(
                external_id=str(payload["external_id"]),
                status=str(payload.get("status", "PENDING")),
                amount_ves=_parse_decimal(payload.get("amount_ves", amount_ves), "amount_ves"),
                created_at=created_at,
                raw=payload,
            )
        except (KeyError, ValueError) as exc:
            raise ExternalServiceResponseError(
                "Cachicamo payment payload is missing external_id",
                service=self.service_name,
            ) from exc

    async def get_balance(self) -> Decimal:
        """Return the available VES balance.

        Returns:
            The available balance as a decimal.

        Raises:
            ExternalServiceResponseError: If the payload is malformed.
        """
        payload = await self.get(
            _BALANCE_PATH,
            headers=self._auth_headers(),
            idempotent=True,
        )
        if not isinstance(payload, dict) or "balance" not in payload:
            raise ExternalServiceResponseError(
                "Cachicamo balance payload is malformed",
                service=self.service_name,
            )
        return _parse_decimal(payload["balance"], "balance")

    async def health_check(self) -> bool:
        """Return whether the Cachicamo API is reachable."""
        return await self._safe_health_check(_HEALTH_PATH)


__all__ = [
    "CachicamoClient",
    "CachicamoVerificationResult",
    "CachicamoPaymentResult",
]

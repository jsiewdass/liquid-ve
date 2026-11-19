"""Android SMS gateway adapter reachable over Tailscale.

The gateway authenticates with a bearer token and accepts an
``Idempotency-Key`` header on message-creating calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.exceptions import ExternalServiceResponseError
from app.domain.enums import BankCode
from app.infrastructure.clients.base import BaseAsyncHTTPClient

_MESSAGES_PATH = "/messages"
_HEALTH_PATH = "/health"


def _parse_iso_datetime(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp into an aware datetime.

    Args:
        value: Raw timestamp.

    Returns:
        A timezone-aware datetime.

    Raises:
        ValueError: If the value is missing or malformed.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Timestamp is required")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SmsSendResult(BaseModel):
    """Result of sending an SMS."""

    message_id: str
    status: str
    sent_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class IncomingSms(BaseModel):
    """Inbound SMS read from the gateway inbox."""

    message_id: str
    sender: str
    body: str
    received_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class SmsGatewayClient(BaseAsyncHTTPClient):
    """Client for the Tailscale-hosted Android SMS gateway."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        """Initialize the client.

        Args:
            settings: Active application settings.
            client: Optional pre-built httpx client for tests.
        """
        super().__init__(settings, base_url=settings.sms_gateway_base_url, client=client)

    @property
    def service_name(self) -> str:
        """Stable provider name."""
        return "sms_gateway"

    def _auth_headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        """Build bearer and idempotency headers.

        Args:
            idempotency_key: Optional key for message-creating calls.

        Returns:
            Header mapping.  Values are never logged.
        """
        headers = {
            "Authorization": f"Bearer {self._settings.sms_gateway_token.get_secret_value()}",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def send_sms(
        self,
        *,
        phone_number: str,
        message: str,
        idempotency_key: str,
    ) -> SmsSendResult:
        """Send an SMS through the gateway.

        Sending is not retried automatically because it creates a message on
        the device; callers retry with the same idempotency key instead.

        Args:
            phone_number: Destination phone number.
            message: Message body.
            idempotency_key: Stable key unique to this message.

        Returns:
            The send result.

        Raises:
            ExternalServiceResponseError: If the payload is malformed.
        """
        payload = await self.post(
            _MESSAGES_PATH,
            json={"phone_number": phone_number, "message": message},
            headers=self._auth_headers(idempotency_key=idempotency_key),
            idempotent=False,
        )
        if not isinstance(payload, dict):
            raise ExternalServiceResponseError(
                "SMS gateway send payload is not an object",
                service=self.service_name,
            )
        try:
            return SmsSendResult(
                message_id=str(payload["message_id"]),
                status=str(payload.get("status", "SENT")),
                sent_at=_parse_iso_datetime(payload.get("sent_at")),
                raw=payload,
            )
        except (KeyError, ValueError) as exc:
            raise ExternalServiceResponseError(
                "SMS gateway send payload is malformed",
                service=self.service_name,
            ) from exc

    async def fetch_inbox(
        self,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[IncomingSms]:
        """Read inbound messages from the gateway inbox.

        Args:
            since: Optional lower bound for message timestamps.
            limit: Maximum number of messages to return.

        Returns:
            Parsed inbound messages ordered as returned by the gateway.

        Raises:
            ExternalServiceResponseError: If the payload is malformed.
        """
        params: dict[str, Any] = {"limit": limit}
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            params["since"] = since.isoformat()
        payload = await self.get(
            _MESSAGES_PATH,
            params=params,
            headers=self._auth_headers(),
            idempotent=True,
        )
        raw_messages = payload.get("messages") if isinstance(payload, dict) else payload
        if not isinstance(raw_messages, list):
            raise ExternalServiceResponseError(
                "SMS gateway inbox payload is not a list",
                service=self.service_name,
            )
        messages: list[IncomingSms] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                raise ExternalServiceResponseError(
                    "SMS gateway inbox item is not an object",
                    service=self.service_name,
                )
            try:
                messages.append(
                    IncomingSms(
                        message_id=str(item["message_id"]),
                        sender=str(item["sender"]),
                        body=str(item["body"]),
                        received_at=_parse_iso_datetime(item.get("received_at")),
                        raw=item,
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ExternalServiceResponseError(
                    "SMS gateway inbox item is malformed",
                    service=self.service_name,
                ) from exc
        return messages

    async def health_check(self) -> bool:
        """Return whether the SMS gateway is reachable."""
        return await self._safe_health_check(_HEALTH_PATH)

    @staticmethod
    def build_payment_sms(
        *,
        amount_ves: Decimal,
        destination_phone: str,
        bank_code: BankCode,
        reference: str,
        concept: str,
    ) -> str:
        """Build the deterministic outgoing mobile-payment instruction.

        The text is end-user content and is intentionally written in Spanish.

        Args:
            amount_ves: Amount to pay in VES.
            destination_phone: Destination phone number.
            bank_code: Destination bank.
            reference: Bank reference to quote.
            concept: Payment concept.

        Returns:
            The SMS body with amounts formatted using comma thousands and dot
            decimals.
        """
        formatted_amount = f"{amount_ves:,.2f}"
        return (
            f"liquidVE | Pago movil {bank_code.display_name}: "
            f"envia Bs {formatted_amount} al {destination_phone}. "
            f"Referencia: {reference}. Concepto: {concept}."
        )


__all__ = ["SmsGatewayClient", "SmsSendResult", "IncomingSms"]

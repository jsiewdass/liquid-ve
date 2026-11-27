"""HMAC signing, masking helpers and FastAPI security dependencies.

Secrets are never written to logs.  Docstrings in this module intentionally
describe behaviour without reproducing credential values.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone
from typing import Annotated, Mapping

from fastapi import Header

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ReplayAttackError,
    SignatureVerificationError,
    UnauthorizedError,
)

_SHA256_PREFIX = "sha256="
_PHONE_CLEAN_RE = re.compile(r"[^\d+]")


class CachicamoWebhookVerifier:
    """Verify Cachicamo webhook signatures and extract replay identifiers."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialize the verifier.

        Args:
            settings: Optional settings override, mainly for tests.
        """
        self._settings = settings or get_settings()

    def _headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Build a case-insensitive header lookup.

        Args:
            headers: Raw request headers.

        Returns:
            A mapping keyed by lowercase header name.
        """
        return {str(key).lower(): str(value) for key, value in headers.items()}

    def verify(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        """Validate the signature and timestamp of an inbound webhook.

        Args:
            raw_body: Exact request body bytes.
            headers: Request headers as received on the wire.

        Raises:
            SignatureVerificationError: If the signature headers are missing
                or the digest does not match.
            ReplayAttackError: If the timestamp is outside the tolerance
                window.
        """
        lookup = self._headers(headers)
        signature = lookup.get(self._settings.cachicamo_signature_header.lower())
        timestamp = lookup.get(self._settings.cachicamo_timestamp_header.lower())
        if not signature or not timestamp:
            raise SignatureVerificationError("Missing webhook signature headers")
        verify_hmac_signature(
            self._settings.cachicamo_webhook_secret.get_secret_value(),
            raw_body,
            signature,
            timestamp=timestamp,
            tolerance_seconds=self._settings.cachicamo_webhook_tolerance_seconds,
        )

    def extract_event_id(self, headers: Mapping[str, str]) -> str | None:
        """Return the provider event identifier when present.

        Args:
            headers: Request headers as received on the wire.

        Returns:
            The event id header value or ``None``.
        """
        lookup = self._headers(headers)
        return lookup.get(self._settings.cachicamo_event_id_header.lower())


def compute_hmac_signature(secret: str, payload: bytes, *, timestamp: str | None = None) -> str:
    """Compute an HMAC-SHA256 signature.

    Args:
        secret: Shared secret used as the HMAC key.
        payload: Raw payload bytes.
        timestamp: Optional timestamp prepended as ``timestamp.`` before
            signing, which binds the payload to a point in time.

    Returns:
        Lowercase hexadecimal digest.
    """
    signed = payload if timestamp is None else f"{timestamp}.".encode("utf-8") + payload
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def verify_hmac_signature(
    secret: str,
    payload: bytes,
    signature: str,
    *,
    timestamp: str | None = None,
    tolerance_seconds: int | None = None,
) -> None:
    """Verify an HMAC-SHA256 signature using a constant-time comparison.

    Args:
        secret: Shared secret used as the HMAC key.
        payload: Raw payload bytes.
        signature: Provided signature, optionally prefixed with ``sha256=``.
        timestamp: Unix timestamp in seconds sent alongside the signature.
        tolerance_seconds: Maximum accepted clock skew.  Defaults to the
            configured Cachicamo tolerance.

    Raises:
        SignatureVerificationError: If the signature is missing or invalid.
        ReplayAttackError: If a timestamp is provided and outside tolerance.
    """
    if not signature:
        raise SignatureVerificationError("Missing signature")
    normalized = signature
    if normalized.startswith(_SHA256_PREFIX):
        normalized = normalized[len(_SHA256_PREFIX) :]
    if timestamp is not None:
        window = (
            tolerance_seconds
            if tolerance_seconds is not None
            else get_settings().cachicamo_webhook_tolerance_seconds
        )
        try:
            sent_at = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise SignatureVerificationError("Invalid signature timestamp") from exc
        now = int(datetime.now(timezone.utc).timestamp())
        if abs(now - sent_at) > window:
            raise ReplayAttackError("Signature timestamp outside the accepted window")
    expected = compute_hmac_signature(secret, payload, timestamp=timestamp)
    if not hmac.compare_digest(expected, normalized):
        raise SignatureVerificationError("Signature mismatch")


def mask_secret(value: str, visible: int = 4) -> str:
    """Mask a secret, keeping only the last ``visible`` characters.

    Args:
        value: Secret value.
        visible: Number of trailing characters to keep.

    Returns:
        A masked representation.  Secrets short enough that revealing a
        suffix would leak them are fully masked.
    """
    if not value:
        return "***"
    if len(value) <= visible:
        return "***"
    return f"{'*' * (len(value) - visible)}{value[-visible:]}"


def mask_phone(phone: str) -> str:
    """Mask a phone number while keeping a short suffix.

    Args:
        phone: Raw phone number.

    Returns:
        A masked phone number suitable for logs.
    """
    cleaned = _PHONE_CLEAN_RE.sub("", phone)
    if not cleaned:
        return "***"
    return mask_secret(cleaned, visible=4)


def mask_id_number(cedula: str) -> str:
    """Mask a Venezuelan identity document number.

    Args:
        cedula: Document number such as ``V-12345678``.

    Returns:
        A masked document number preserving the letter prefix.
    """
    cleaned = cedula.strip()
    if not cleaned:
        return "***"
    match = re.match(r"^([VEJGPvejgp])[- ]?(.*)$", cleaned)
    if match:
        return f"{match.group(1).upper()}-****"
    return "***"


def hash_payload(payload: bytes) -> str:
    """Return the SHA-256 hex digest of a payload.

    Args:
        payload: Raw payload bytes.

    Returns:
        Lowercase hexadecimal digest used for deduplication.
    """
    return hashlib.sha256(payload).hexdigest()


async def require_internal_api_key(
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    *, app_settings: Settings | None = None,
) -> None:
    """Validate the internal API key header.

    Args:
        x_api_key: Header value injected by FastAPI.

    Raises:
        UnauthorizedError: If the header is missing or does not match.
    """
    if not x_api_key:
        raise UnauthorizedError("Missing API key")
    expected = (app_settings or get_settings()).internal_api_key.get_secret_value()
    if not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise UnauthorizedError("Invalid API key")


__all__ = [
    "CachicamoWebhookVerifier",
    "compute_hmac_signature",
    "verify_hmac_signature",
    "mask_secret",
    "mask_phone",
    "mask_id_number",
    "hash_payload",
    "require_internal_api_key",
]

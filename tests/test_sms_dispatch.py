"""Unit tests for gateway outcome classification.

The classifier is pure: it maps an observed gateway exception or a returned
result to one of four fates without doing any I/O.  See design section 2.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.exceptions import (
    ConflictError,
    ExternalServiceRateLimitedError,
    ExternalServiceResponseError,
    RetryableExternalServiceError,
    UnauthorizedError,
)
from app.infrastructure.clients.sms_gateway import SmsSendResult
from app.services.sms_dispatch import (
    DispatchFate,
    classify_gateway_exception,
    classify_gateway_result,
)


def _result() -> SmsSendResult:
    """Build a successful gateway result.

    Returns:
        A deterministic ``SENT`` result.
    """
    return SmsSendResult(
        message_id="msg-1",
        status="SENT",
        sent_at=datetime.now(timezone.utc),
    )


def test_classifies_delivered() -> None:
    """A returned send result is classified as delivered."""
    assert classify_gateway_result(_result()) is DispatchFate.DELIVERED


def test_classifies_not_delivered_4xx() -> None:
    """A rejected 4xx request is classified as not delivered."""
    exc = ExternalServiceResponseError(
        "rejected", service="sms_gateway", details={"status": 422}
    )

    assert classify_gateway_exception(exc) is DispatchFate.NOT_DELIVERED


def test_classifies_unauthorized_as_permanent() -> None:
    """Invalid credentials are a permanent failure with no usable fallback."""
    exc = UnauthorizedError("nope", details={"status": 401})

    assert classify_gateway_exception(exc) is DispatchFate.PERMANENT


def test_classifies_conflict_as_unknown() -> None:
    """A 409 means a message with this key may already exist."""

    exc = ConflictError("duplicate key", details={"status": 409})

    assert classify_gateway_exception(exc) is DispatchFate.UNKNOWN


def test_classifies_retryable_and_malformed_as_unknown() -> None:
    """Retryable, rate-limited, transport, timeout and malformed bodies are unknown."""
    fates = {
        classify_gateway_exception(
            RetryableExternalServiceError("boom", service="sms_gateway")
        ),
        classify_gateway_exception(
            ExternalServiceRateLimitedError(service="sms_gateway")
        ),
        classify_gateway_exception(
            ExternalServiceResponseError("malformed", service="sms_gateway")
        ),
        classify_gateway_exception(TimeoutError("slow")),
    }

    assert fates == {DispatchFate.UNKNOWN}

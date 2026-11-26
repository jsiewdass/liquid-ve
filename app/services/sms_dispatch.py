"""Gateway outcome classification and the single-fallback bank-command dispatch.

The SMS gateway creates a real bank debit, so the dispatch layer never guesses
and never retries an unknown outcome.  Classification is a pure, I/O-free
function of the observed result (design section 2):

* a returned :class:`SmsSendResult` is ``DELIVERED``;
* a rejected 4xx request is ``NOT_DELIVERED`` and earns exactly one fallback;
* invalid credentials are ``PERMANENT`` (another short code cannot fix them);
* a 409 conflict, a retryable/rate-limited/transport/timeout failure or a
  malformed 2xx body is ``UNKNOWN`` and must not be retried.

:func:`dispatch_bank_command` performs at most two physical requests — the
primary short code and, only after a classified ``NOT_DELIVERED``, the next one
with the same body and the same ``Idempotency-Key``.  The destination is always
the bank short code, never the counterparty phone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.core.exceptions import (
    BankSmsFormatError,
    ConflictError,
    ExternalServiceResponseError,
    UnauthorizedError,
)
from app.infrastructure.clients.sms_gateway import SmsGatewayClient, SmsSendResult

__all__ = [
    "DispatchFate",
    "DispatchResult",
    "classify_gateway_exception",
    "classify_gateway_result",
    "dispatch_bank_command",
]

_NOT_DELIVERED_MIN_STATUS = 400
_NOT_DELIVERED_MAX_STATUS = 499


class DispatchFate(str, Enum):
    """Outcome of one SMS gateway attempt."""

    DELIVERED = "DELIVERED"
    NOT_DELIVERED = "NOT_DELIVERED"
    PERMANENT = "PERMANENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Result of a bank-command dispatch.

    Attributes:
        fate: Classified outcome of the final attempt.
        short_code: Short code the final attempt used.
        message_id: Gateway message id when the result was delivered.
        sent_at: Gateway send timestamp when the result was delivered.
        error_code: Typed error code for a non-delivered outcome.
    """

    fate: DispatchFate
    short_code: str
    message_id: str | None = None
    sent_at: datetime | None = None
    error_code: str | None = None


def classify_gateway_result(
    result: SmsSendResult,  # noqa: ARG001 - signature documents the input
) -> DispatchFate:
    """Classify a returned gateway result.

    Any returned :class:`SmsSendResult` means the gateway accepted the message
    for delivery.

    Args:
        result: The returned send result.

    Returns:
        :attr:`DispatchFate.DELIVERED`.
    """
    return DispatchFate.DELIVERED


def classify_gateway_exception(exc: BaseException) -> DispatchFate:
    """Classify an exception raised by the gateway adapter.

    Args:
        exc: The exception observed while sending.

    Returns:
        The classified fate.  ``ConflictError`` is ``UNKNOWN``,
        ``UnauthorizedError`` is ``PERMANENT``, a
        ``ExternalServiceResponseError`` carrying a 4xx status is
        ``NOT_DELIVERED`` and everything else is ``UNKNOWN``.
    """
    if isinstance(exc, ConflictError):
        return DispatchFate.UNKNOWN
    if isinstance(exc, UnauthorizedError):
        return DispatchFate.PERMANENT
    if isinstance(exc, ExternalServiceResponseError):
        status = exc.details.get("status")
        if (
            isinstance(status, int)
            and _NOT_DELIVERED_MIN_STATUS <= status <= _NOT_DELIVERED_MAX_STATUS
        ):
            return DispatchFate.NOT_DELIVERED
        return DispatchFate.UNKNOWN
    return DispatchFate.UNKNOWN


def _error_code(exc: BaseException) -> str:
    """Return a stable error code for an exception.

    Args:
        exc: The raised exception.

    Returns:
        The typed code when available, otherwise the class name.
    """
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else type(exc).__name__


async def _attempt(
    sms: SmsGatewayClient,
    *,
    short_code: str,
    message: str,
    idempotency_key: str,
) -> DispatchResult:
    """Make exactly one gateway request to one short code.

    Args:
        sms: Gateway client or compatible double.
        short_code: Destination short code.
        message: Rendered command body.
        idempotency_key: Stable per-order key.

    Returns:
        The classified attempt result.
    """
    try:
        send_result = await sms.send_sms(
            phone_number=short_code,
            message=message,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001 - every failure is classified
        return DispatchResult(
            fate=classify_gateway_exception(exc),
            short_code=short_code,
            error_code=_error_code(exc),
        )
    return DispatchResult(
        fate=classify_gateway_result(send_result),
        short_code=short_code,
        message_id=send_result.message_id,
        sent_at=send_result.sent_at,
    )


async def dispatch_bank_command(
    sms: SmsGatewayClient,
    *,
    short_codes: Sequence[str],
    message: str,
    order_number: str,
) -> DispatchResult:
    """Send the bank command with at most one classified fallback.

    The primary short code is always tried first.  Only a classified
    ``NOT_DELIVERED`` result falls back, and only to the second short code with
    the same body and the same ``Idempotency-Key``.  A second not-delivered
    result, an exhausted short-code list, invalid credentials or an unknown
    outcome is terminal.

    Args:
        sms: Gateway client or compatible double.
        short_codes: Ordered destination short codes, primary first.
        message: Rendered command body.
        order_number: Provider order number; the key is ``{order}:payout``.

    Returns:
        The classified dispatch result.

    Raises:
        BankSmsFormatError: If ``short_codes`` is empty.
    """
    if not short_codes:
        raise BankSmsFormatError("At least one short code is required")
    idempotency_key = f"{order_number}:payout"
    primary = short_codes[0]
    result = await _attempt(
        sms,
        short_code=primary,
        message=message,
        idempotency_key=idempotency_key,
    )
    if result.fate is not DispatchFate.NOT_DELIVERED:
        return result
    if len(short_codes) < 2:
        return DispatchResult(
            fate=DispatchFate.PERMANENT,
            short_code=primary,
            error_code=result.error_code,
        )
    fallback = await _attempt(
        sms,
        short_code=short_codes[1],
        message=message,
        idempotency_key=idempotency_key,
    )
    if fallback.fate is DispatchFate.NOT_DELIVERED:
        return DispatchResult(
            fate=DispatchFate.PERMANENT,
            short_code=fallback.short_code,
            error_code=fallback.error_code,
        )
    return fallback

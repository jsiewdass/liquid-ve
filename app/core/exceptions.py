"""Typed application exceptions.

Every error raised by liquidVE business code derives from
:class:`LiquidVEException` so the HTTP layer can render a single, stable error
envelope.  Subclasses only adjust defaults; the constructor signature is
shared and always keyword-only for the non-message arguments.
"""

from __future__ import annotations

import re
from typing import Any

_HTML_FRAGMENT_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
_PAYMENT_ERROR_FORBIDDEN_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "token",
    "secret",
    "securityanswer",
    "answer",
    "clave",
    "user",
)


def _sanitize_payment_details(
    details: dict[str, Any] | None,
) -> dict[str, Any]:
    """Drop credential, token, HTML and raw-byte values from error details.

    Payment rail failures are allowed to carry small operational context such
    as ``{"stage": "login"}``.  They must never carry a credential, an answer,
    a token, a Clave Dinámica, raw bytes or an HTML fragment.

    Args:
        details: Candidate detail mapping.

    Returns:
        A sanitized copy safe to render in an error envelope.
    """
    if not details:
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in details.items():
        normalized = str(key).lower().replace("-", "").replace("_", "")
        if any(part in normalized for part in _PAYMENT_ERROR_FORBIDDEN_KEY_PARTS):
            continue
        if isinstance(value, (bytes, bytearray)):
            continue
        if isinstance(value, str) and _HTML_FRAGMENT_RE.search(value):
            continue
        sanitized[key] = value
    return sanitized


class LiquidVEException(Exception):
    """Base class for every recoverable liquidVE error.

    Attributes:
        message: Human readable description (never contains secrets).
        code: Stable machine readable identifier.
        details: Optional structured context safe for API consumers.
        http_status: HTTP status the API layer should return.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "liquidve_error",
        details: dict[str, Any] | None = None,
        http_status: int = 500,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        """Render the canonical error envelope.

        Returns:
            A mapping shaped as ``{"error": {"code", "message", "details"}}``.
        """
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, http_status={self.http_status})"


class ConfigurationError(LiquidVEException):
    """Raised when the runtime configuration is invalid or incomplete."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "configuration_error",
        details: dict[str, Any] | None = None,
        http_status: int = 500,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class NotFoundError(LiquidVEException):
    """Raised when a requested resource does not exist."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "not_found",
        details: dict[str, Any] | None = None,
        http_status: int = 404,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class ConflictError(LiquidVEException):
    """Raised when the request conflicts with the current resource state."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "conflict",
        details: dict[str, Any] | None = None,
        http_status: int = 409,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class DuplicateOrderError(ConflictError):
    """Raised when an order number was already persisted."""

    def __init__(
        self,
        message: str = "Order already exists",
        *,
        code: str = "duplicate_order",
        details: dict[str, Any] | None = None,
        http_status: int = 409,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class DuplicateBankReferenceError(ConflictError):
    """Raised when a bank reference was already processed."""

    def __init__(
        self,
        message: str = "Bank reference already processed",
        *,
        code: str = "duplicate_bank_reference",
        details: dict[str, Any] | None = None,
        http_status: int = 409,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class InvalidStateTransitionError(ConflictError):
    """Raised when an order lifecycle transition is not allowed."""

    def __init__(
        self,
        message: str = "Invalid order state transition",
        *,
        code: str = "invalid_state_transition",
        details: dict[str, Any] | None = None,
        http_status: int = 409,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class RiskLimitExceededError(LiquidVEException):
    """Raised when a transaction violates a hard risk limit."""

    def __init__(
        self,
        message: str = "Risk limit exceeded",
        *,
        code: str = "risk_limit_exceeded",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class BankSmsFormatError(LiquidVEException):
    """Raised when bank SMS input cannot be normalized or rendered."""

    def __init__(
        self,
        message: str = "Bank SMS value is invalid",
        *,
        code: str = "bank_sms_format_error",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class BankSmsParseError(LiquidVEException):
    """Raised when a bank reply or scraped amount cannot be parsed."""

    def __init__(
        self,
        message: str = "Bank SMS value could not be parsed",
        *,
        code: str = "bank_sms_parse_error",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class PaymentRailError(LiquidVEException):
    """Base class for every fail-closed payment rail failure.

    Subclasses pin a stable ``code``.  Details are sanitized on construction so
    a credential, security answer, token, Clave Dinámica, raw byte payload or
    HTML fragment can never leave the rail.
    """

    def __init__(
        self,
        message: str = "Payment rail error",
        *,
        code: str = "payment_rail_error",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
        service: str = "payment_rail",
    ) -> None:
        self.service = service
        super().__init__(
            message,
            code=code,
            details=_sanitize_payment_details(details),
            http_status=http_status,
        )


class BanescoLayoutUnexpected(PaymentRailError):
    """Raised when the Banesco Online layout differs from the reviewed shape."""

    def __init__(
        self,
        message: str = "Unexpected Banesco Online layout",
        *,
        code: str = "banesco_layout_unexpected",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class UnsupportedPhonePrefix(PaymentRailError):
    """Raised when a phone prefix is not offered by the payment form."""

    def __init__(
        self,
        message: str = "Unsupported beneficiary phone prefix",
        *,
        code: str = "unsupported_phone_prefix",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class IdTypeMissing(PaymentRailError):
    """Raised when a document letter is absent; never defaulted."""

    def __init__(
        self,
        message: str = "Document type is missing",
        *,
        code: str = "id_type_missing",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class IdTypeUnsupported(PaymentRailError):
    """Raised when a document letter is outside the accepted Banesco set."""

    def __init__(
        self,
        message: str = "Document type is not supported by Banesco",
        *,
        code: str = "id_type_unsupported",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class PaymentDataUnavailable(PaymentRailError):
    """Raised when required payment data is missing or malformed."""

    def __init__(
        self,
        message: str = "Payment data is unavailable",
        *,
        code: str = "payment_data_unavailable",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class UnsupportedDestinationBank(PaymentRailError):
    """Raised when a destination bank is absent from the reviewed option map."""

    def __init__(
        self,
        message: str = "Destination bank is not supported",
        *,
        code: str = "unsupported_destination_bank",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class DestinationBankAmbiguous(PaymentRailError):
    """Raised when a destination bank matches more than one reviewed option."""

    def __init__(
        self,
        message: str = "Destination bank option is ambiguous",
        *,
        code: str = "destination_bank_ambiguous",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class BanescoLoginFailed(PaymentRailError):
    """Raised when the Banesco login or online clave is rejected."""

    def __init__(
        self,
        message: str = "Banesco Online login failed",
        *,
        code: str = "banesco_login_failed",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class BanescoCaptchaDetected(PaymentRailError):
    """Raised when the bank presents a captcha or anti-bot challenge."""

    def __init__(
        self,
        message: str = "Banesco Online captcha detected",
        *,
        code: str = "banesco_captcha_detected",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class BanescoAccountLocked(PaymentRailError):
    """Raised when the bank reports a locked account."""

    def __init__(
        self,
        message: str = "Banesco Online account is locked",
        *,
        code: str = "banesco_account_locked",
        details: dict[str, Any] | None = None,
        http_status: int = 423,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class BanescoMaintenance(PaymentRailError):
    """Raised when the bank reports scheduled or unscheduled maintenance."""

    def __init__(
        self,
        message: str = "Banesco Online is under maintenance",
        *,
        code: str = "banesco_maintenance",
        details: dict[str, Any] | None = None,
        http_status: int = 503,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class BanescoSessionExpired(PaymentRailError):
    """Raised when the bank session expires or the view state is invalidated."""

    def __init__(
        self,
        message: str = "Banesco Online session expired",
        *,
        code: str = "banesco_session_expired",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class BanescoAdModalBlocked(PaymentRailError):
    """Raised when an advertisement modal blocks navigation and cannot close."""

    def __init__(
        self,
        message: str = "Banesco advertisement modal blocked navigation",
        *,
        code: str = "banesco_ad_modal_blocked",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class BanescoPaymentRejected(PaymentRailError):
    """Raised when the bank explicitly rejects the payment."""

    def __init__(
        self,
        message: str = "Banesco payment was rejected",
        *,
        code: str = "banesco_payment_rejected",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class BanescoPaymentOutcomeUnknown(PaymentRailError):
    """Raised when the payment may have been submitted but is unverified."""

    def __init__(
        self,
        message: str = "Banesco payment outcome is unknown",
        *,
        code: str = "banesco_payment_outcome_unknown",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class ClaveDinamicaTimeout(PaymentRailError):
    """Raised when no accepted Clave Dinámica arrives before the deadline."""

    def __init__(
        self,
        message: str = "Clave Dinámica timed out",
        *,
        code: str = "clave_dinamica_timeout",
        details: dict[str, Any] | None = None,
        http_status: int = 504,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class ClaveDinamicaRejected(PaymentRailError):
    """Raised when the operator key is malformed, uncorrelated or refused."""

    def __init__(
        self,
        message: str = "Clave Dinámica was rejected",
        *,
        code: str = "clave_dinamica_rejected",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class ReceiptAmountMismatch(PaymentRailError):
    """Raised when a captured receipt amount differs from the expected amount."""

    def __init__(
        self,
        message: str = "Receipt amount does not match the expected amount",
        *,
        code: str = "receipt_amount_mismatch",
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class ReceiptConflict(PaymentRailError):
    """Raised when a different receipt digest already exists for the order."""

    def __init__(
        self,
        message: str = "A different receipt already exists for this order",
        *,
        code: str = "receipt_conflict",
        details: dict[str, Any] | None = None,
        http_status: int = 409,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class ReceiptUnavailable(PaymentRailError):
    """Raised when no receipt can be captured for a submitted payment."""

    def __init__(
        self,
        message: str = "Receipt is unavailable",
        *,
        code: str = "receipt_unavailable",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="banesco_online",
        )


class EvidenceUploadFailed(PaymentRailError):
    """Raised when receipt evidence cannot be uploaded to the exchange."""

    def __init__(
        self,
        message: str = "Evidence upload failed",
        *,
        code: str = "evidence_upload_failed",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="binance_browser",
        )


class BinanceCloseFailed(PaymentRailError):
    """Raised when the exchange order cannot be closed after payment."""

    def __init__(
        self,
        message: str = "Binance close failed",
        *,
        code: str = "binance_close_failed",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="binance_browser",
        )


class AppealAlreadyFiled(PaymentRailError):
    """Raised when an appeal was already filed for the order."""

    def __init__(
        self,
        message: str = "An appeal was already filed",
        *,
        code: str = "appeal_already_filed",
        details: dict[str, Any] | None = None,
        http_status: int = 409,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="release_watcher",
        )


class ReleaseTimeout(PaymentRailError):
    """Raised when the exchange does not release before the deadline."""

    def __init__(
        self,
        message: str = "Release timed out",
        *,
        code: str = "release_timeout",
        details: dict[str, Any] | None = None,
        http_status: int = 504,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            http_status=http_status,
            service="release_watcher",
        )


PAYMENT_RAIL_ERROR_CODES: dict[str, type[PaymentRailError]] = {
    "banesco_login_failed": BanescoLoginFailed,
    "banesco_layout_unexpected": BanescoLayoutUnexpected,
    "banesco_captcha_detected": BanescoCaptchaDetected,
    "banesco_account_locked": BanescoAccountLocked,
    "banesco_maintenance": BanescoMaintenance,
    "banesco_session_expired": BanescoSessionExpired,
    "banesco_ad_modal_blocked": BanescoAdModalBlocked,
    "banesco_payment_rejected": BanescoPaymentRejected,
    "banesco_payment_outcome_unknown": BanescoPaymentOutcomeUnknown,
    "unsupported_phone_prefix": UnsupportedPhonePrefix,
    "id_type_missing": IdTypeMissing,
    "id_type_unsupported": IdTypeUnsupported,
    "payment_data_unavailable": PaymentDataUnavailable,
    "unsupported_destination_bank": UnsupportedDestinationBank,
    "destination_bank_ambiguous": DestinationBankAmbiguous,
    "clave_dinamica_timeout": ClaveDinamicaTimeout,
    "clave_dinamica_rejected": ClaveDinamicaRejected,
    "receipt_amount_mismatch": ReceiptAmountMismatch,
    "receipt_conflict": ReceiptConflict,
    "receipt_unavailable": ReceiptUnavailable,
    "evidence_upload_failed": EvidenceUploadFailed,
    "binance_close_failed": BinanceCloseFailed,
    "appeal_already_filed": AppealAlreadyFiled,
    "release_timeout": ReleaseTimeout,
}
"""Every fail-closed payment-rail error code mapped to its exception class."""


class SecurityError(LiquidVEException):
    """Base class for authentication and integrity failures."""

    def __init__(
        self,
        message: str = "Security check failed",
        *,
        code: str = "security_error",
        details: dict[str, Any] | None = None,
        http_status: int = 401,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class SignatureVerificationError(SecurityError):
    """Raised when an HMAC signature does not match."""

    def __init__(
        self,
        message: str = "Invalid signature",
        *,
        code: str = "invalid_signature",
        details: dict[str, Any] | None = None,
        http_status: int = 401,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class ReplayAttackError(SecurityError):
    """Raised when a signed request is outside the accepted time window."""

    def __init__(
        self,
        message: str = "Replayed request rejected",
        *,
        code: str = "replayed_request",
        details: dict[str, Any] | None = None,
        http_status: int = 409,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class UnauthorizedError(SecurityError):
    """Raised when credentials are missing or invalid."""

    def __init__(
        self,
        message: str = "Unauthorized",
        *,
        code: str = "unauthorized",
        details: dict[str, Any] | None = None,
        http_status: int = 401,
    ) -> None:
        super().__init__(message, code=code, details=details, http_status=http_status)


class ExternalServiceError(LiquidVEException):
    """Raised when a third-party dependency fails."""

    def __init__(
        self,
        message: str,
        *,
        service: str = "external",
        code: str | None = None,
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        self.service = service
        super().__init__(
            message,
            code=code or f"{service}_error",
            details=details,
            http_status=http_status,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(service={self.service!r}, "
            f"code={self.code!r}, http_status={self.http_status})"
        )


class RetryableExternalServiceError(ExternalServiceError):
    """Raised after exhausting retries for a transient upstream failure."""

    def __init__(
        self,
        message: str,
        *,
        service: str = "external",
        code: str | None = None,
        details: dict[str, Any] | None = None,
        http_status: int = 503,
    ) -> None:
        super().__init__(
            message,
            service=service,
            code=code,
            details=details,
            http_status=http_status,
        )


class ExternalServiceResponseError(ExternalServiceError):
    """Raised when an upstream response is malformed or semantically wrong."""

    def __init__(
        self,
        message: str,
        *,
        service: str = "external",
        code: str = "invalid_external_response",
        details: dict[str, Any] | None = None,
        http_status: int = 502,
    ) -> None:
        super().__init__(
            message,
            service=service,
            code=code,
            details=details,
            http_status=http_status,
        )


class ExternalServiceRateLimitedError(ExternalServiceError):
    """Raised when the upstream keeps answering 429 after retries."""

    def __init__(
        self,
        message: str = "Upstream rate limit exhausted",
        *,
        service: str = "external",
        code: str | None = None,
        details: dict[str, Any] | None = None,
        http_status: int = 429,
    ) -> None:
        super().__init__(
            message,
            service=service,
            code=code,
            details=details,
            http_status=http_status,
        )


class CircuitOpenError(ExternalServiceError):
    """Raised when the circuit breaker for a dependency is open."""

    def __init__(
        self,
        message: str = "Circuit breaker is open",
        *,
        service: str = "external",
        code: str | None = None,
        details: dict[str, Any] | None = None,
        http_status: int = 503,
    ) -> None:
        super().__init__(
            message,
            service=service,
            code=code,
            details=details,
            http_status=http_status,
        )


__all__ = [
    "LiquidVEException",
    "ConfigurationError",
    "NotFoundError",
    "ConflictError",
    "DuplicateOrderError",
    "DuplicateBankReferenceError",
    "InvalidStateTransitionError",
    "RiskLimitExceededError",
    "BankSmsFormatError",
    "BankSmsParseError",
    "PaymentRailError",
    "BanescoLayoutUnexpected",
    "UnsupportedPhonePrefix",
    "IdTypeMissing",
    "IdTypeUnsupported",
    "PaymentDataUnavailable",
    "UnsupportedDestinationBank",
    "DestinationBankAmbiguous",
    "BanescoLoginFailed",
    "BanescoCaptchaDetected",
    "BanescoAccountLocked",
    "BanescoMaintenance",
    "BanescoSessionExpired",
    "BanescoAdModalBlocked",
    "BanescoPaymentRejected",
    "BanescoPaymentOutcomeUnknown",
    "ClaveDinamicaTimeout",
    "ClaveDinamicaRejected",
    "ReceiptAmountMismatch",
    "ReceiptConflict",
    "ReceiptUnavailable",
    "EvidenceUploadFailed",
    "BinanceCloseFailed",
    "AppealAlreadyFiled",
    "ReleaseTimeout",
    "PAYMENT_RAIL_ERROR_CODES",
    "SecurityError",
    "SignatureVerificationError",
    "ReplayAttackError",
    "UnauthorizedError",
    "ExternalServiceError",
    "RetryableExternalServiceError",
    "ExternalServiceResponseError",
    "ExternalServiceRateLimitedError",
    "CircuitOpenError",
]

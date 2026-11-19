"""Typed application exceptions.

Every error raised by liquidVE business code derives from
:class:`LiquidVEException` so the HTTP layer can render a single, stable error
envelope.  Subclasses only adjust defaults; the constructor signature is
shared and always keyword-only for the non-message arguments.
"""

from __future__ import annotations

from typing import Any


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

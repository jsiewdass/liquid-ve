"""Async HTTP client primitives shared by every external adapter."""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

from app.core.exceptions import (
    ConflictError,
    ExternalServiceRateLimitedError,
    ExternalServiceResponseError,
    LiquidVEException,
    RetryableExternalServiceError,
    UnauthorizedError,
)

if TYPE_CHECKING:
    from app.core.config import Settings


class BaseAsyncHTTPClient(ABC):
    """Base class for resilient asynchronous HTTP adapters.

    Retries use exponential backoff with full jitter and only apply to
    transport errors, HTTP 408, HTTP 429 and HTTP 5xx responses when the call
    is idempotent.  Non-idempotent money-moving calls are never retried
    blindly.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize the adapter.

        Args:
            settings: Active application settings.
            base_url: Provider base URL.
            client: Optional pre-built client, mainly for tests.
        """
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(
                settings.http_timeout_seconds,
                connect=settings.http_connect_timeout_seconds,
            ),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            headers={"User-Agent": "liquidVE/1.0"},
            follow_redirects=False,
        )

    @property
    @abstractmethod
    def service_name(self) -> str:
        """Stable provider name used in error codes and logs."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return provider reachability without raising.

        Implementations must swallow exceptions and return ``False``.
        """

    async def __aenter__(self) -> "BaseAsyncHTTPClient":
        """Enter the async context manager."""
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Close the underlying HTTP client on exit."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this adapter owns it."""
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _mask_secret(value: str | None) -> str:
        """Mask a credential for debug logging.

        Args:
            value: Secret value.

        Returns:
            A masked representation that never reveals more than four
            trailing characters.
        """
        if not value:
            return "***"
        if len(value) <= 4:
            return "***"
        return f"{'*' * (len(value) - 4)}{value[-4:]}"

    def _backoff_seconds(self, attempt: int) -> float:
        """Compute a full-jitter exponential backoff delay.

        Args:
            attempt: One-based attempt number.

        Returns:
            Delay in seconds, capped by ``http_backoff_max_seconds``.
        """
        exponential = self._settings.http_backoff_base_seconds * (2 ** (attempt - 1))
        capped = min(exponential, self._settings.http_backoff_max_seconds)
        return random.uniform(0.0, capped)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        """Parse a ``Retry-After`` header.

        Args:
            response: Upstream response.

        Returns:
            Delay in seconds, or ``None`` when absent/unparseable.
        """
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                target = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            delta = (target - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, delta)

    def _safe_path(self, path: str) -> str:
        """Strip query strings so secrets never reach logs.

        Args:
            path: Request path, possibly containing a query string.

        Returns:
            The path without its query component.
        """
        return path.split("?", 1)[0]

    @staticmethod
    def _decode_response(response: httpx.Response, service: str) -> Any:
        """Decode a successful response body.

        Args:
            response: Upstream response.
            service: Provider name used in errors.

        Returns:
            Parsed JSON, raw text or ``None`` for empty bodies.

        Raises:
            ExternalServiceResponseError: If the body claims to be JSON but
                cannot be decoded.
        """
        if response.status_code == 204 or not response.content:
            return None
        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            return response.text
        try:
            return response.json()
        except ValueError as exc:
            raise ExternalServiceResponseError(
                "Upstream returned invalid JSON",
                service=service,
            ) from exc

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | str | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = True,
        expected_statuses: tuple[int, ...] = (200, 201, 202),
    ) -> Any:
        """Execute an HTTP request with retries and error translation.

        Args:
            method: HTTP method.
            path: Request path (query strings are never logged).
            json: Optional JSON body.
            params: Query parameters, either a mapping or a pre-encoded string.
            headers: Additional headers.  Never logged.
            idempotent: Whether retries are allowed.
            expected_statuses: Explicitly accepted success statuses.

        Returns:
            Decoded response payload.

        Raises:
            UnauthorizedError: For HTTP 401 and 403.
            ConflictError: For HTTP 409.
            ExternalServiceRateLimitedError: For HTTP 429 after retries.
            ExternalServiceResponseError: For other 4xx responses and invalid
                payloads.
            RetryableExternalServiceError: For 5xx or transport failures after
                retries.
        """
        service = self.service_name
        max_attempts = max(1, self._settings.http_max_retries)
        safe_path = self._safe_path(path)
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    headers=dict(headers) if headers else None,
                )
            except httpx.TransportError as exc:
                if idempotent and attempt < max_attempts:
                    delay = self._backoff_seconds(attempt)
                    logger.warning(
                        "{} transport error on {} {} (attempt {}/{}), retrying in {:.2f}s",
                        service,
                        method,
                        safe_path,
                        attempt,
                        max_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RetryableExternalServiceError(
                    f"{service} transport failure",
                    service=service,
                    details={"method": method, "path": safe_path},
                ) from exc

            status = response.status_code
            if status in expected_statuses or 200 <= status < 300:
                return self._decode_response(response, service)

            if status in (401, 403):
                raise UnauthorizedError(
                    f"{service} rejected the credentials",
                    details={"status": status, "path": safe_path},
                )
            if status == 409:
                raise ConflictError(
                    f"{service} reported a conflict",
                    details={"status": status, "path": safe_path},
                )
            if status == 429:
                if idempotent and attempt < max_attempts:
                    delay = self._retry_after_seconds(response) or self._backoff_seconds(attempt)
                    logger.warning(
                        "{} rate limited on {} {} (attempt {}/{}), retrying in {:.2f}s",
                        service,
                        method,
                        safe_path,
                        attempt,
                        max_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ExternalServiceRateLimitedError(
                    f"{service} rate limit exhausted",
                    service=service,
                    details={"status": status, "path": safe_path},
                )
            if status == 408 or 500 <= status <= 599:
                if idempotent and attempt < max_attempts:
                    delay = self._retry_after_seconds(response) or self._backoff_seconds(attempt)
                    logger.warning(
                        "{} server error {} on {} {} (attempt {}/{}), retrying in {:.2f}s",
                        service,
                        status,
                        method,
                        safe_path,
                        attempt,
                        max_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise RetryableExternalServiceError(
                    f"{service} is unavailable (status {status})",
                    service=service,
                    details={"status": status, "path": safe_path},
                )
            raise ExternalServiceResponseError(
                f"{service} returned an unexpected status {status}",
                service=service,
                details={"status": status, "path": safe_path},
            )
        raise RetryableExternalServiceError(  # pragma: no cover - loop guarantees a return
            f"{service} exhausted retries",
            service=service,
        )

    async def get(self, path: str, **kwargs: Any) -> Any:
        """Send a GET request."""
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        """Send a POST request."""
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> Any:
        """Send a PUT request."""
        return await self.request("PUT", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        """Send a DELETE request."""
        return await self.request("DELETE", path, **kwargs)

    async def _safe_health_check(self, path: str) -> bool:
        """Run a GET health probe swallowing every error.

        Args:
            path: Health endpoint path.

        Returns:
            ``True`` when the probe answered with a 2xx status.
        """
        try:
            await self.request("GET", path, idempotent=True, expected_statuses=(200,))
            return True
        except LiquidVEException as exc:
            logger.debug("{} health check failed: {}", self.service_name, type(exc).__name__)
            return False
        except Exception as exc:  # noqa: BLE001 - health checks never raise
            logger.debug("{} health check failed: {}", self.service_name, type(exc).__name__)
            return False


__all__ = ["BaseAsyncHTTPClient"]

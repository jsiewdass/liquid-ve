"""Binance C2C adapter.

The official ``binance-connector`` SDK does not cover C2C endpoints, so
signed calls are implemented directly over httpx.  The SDK is used only for
spot account-level checks and is imported lazily so the module remains
importable when the dependency is absent.

Warning:
    A standard (non-merchant) Binance account cannot release USDT through the
    public C2C API and cannot read the counterparty's private bank data there.
    ``release_order`` is therefore only valid for merchant/API-enabled
    accounts; standard-account automation requires the Playwright adapter
    (``binance_browser.py``, not part of this scaffold yet).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel

from app.core.config import Settings
from app.core.exceptions import ExternalServiceError, ExternalServiceResponseError
from app.domain.enums import OrderType
from app.infrastructure.clients.base import BaseAsyncHTTPClient

# TODO(integration): confirm these paths against the official C2C API docs
# before enabling real money movement in production.
_ORDER_HISTORY_PATH = "/sapi/v1/c2c/orderMatch/listUserOrderHistory"
_ORDER_DETAIL_PATH = "/sapi/v1/c2c/orderMatch/getUserOrderDetail"
_MARK_PAID_PATH = "/sapi/v1/c2c/orderMatch/markOrderAsPaid"
_RELEASE_PATH = "/sapi/v1/c2c/orderMatch/releaseCoin"


def _sign_query(params: Mapping[str, Any]) -> str:
    """Build the canonical, sorted query string used for signing.

    Pure function with no I/O so it can be unit tested in isolation.

    Args:
        params: Query parameters.

    Returns:
        URL-encoded parameters sorted by key.
    """
    normalized = {key: str(value) for key, value in params.items() if value is not None}
    return urlencode(sorted(normalized.items()))


def _parse_timestamp(value: Any) -> datetime:
    """Parse a Binance timestamp expressed as epoch milliseconds or ISO text.

    Args:
        value: Raw timestamp value.

    Returns:
        A timezone-aware datetime in UTC.

    Raises:
        ValueError: If the value cannot be interpreted.
    """
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return datetime.fromtimestamp(float(stripped) / 1000.0, tz=timezone.utc)
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    raise ValueError("Unsupported timestamp value")


class BinanceC2COrder(BaseModel):
    """Normalized representation of a Binance C2C order."""

    order_number: str
    trade_type: OrderType
    asset: str
    fiat: str
    price: Decimal
    amount: Decimal
    total_price: Decimal
    status: str
    counterparty_nickname: str | None = None
    created_at: datetime

    @classmethod
    def from_api_payload(cls, payload: Mapping[str, Any]) -> "BinanceC2COrder":
        """Map a raw Binance C2C payload into the normalized DTO.

        Args:
            payload: Raw provider payload.

        Returns:
            The normalized order.

        Raises:
            ExternalServiceResponseError: If required fields are missing or
                invalid.
        """
        try:
            trade_type = OrderType(str(payload["tradeType"]).upper())
            return cls(
                order_number=str(payload["orderNumber"]),
                trade_type=trade_type,
                asset=str(payload.get("asset", "USDT")),
                fiat=str(payload.get("fiat", "VES")),
                price=Decimal(str(payload["price"])),
                amount=Decimal(str(payload["amount"])),
                total_price=Decimal(str(payload["totalPrice"])),
                status=str(payload.get("orderStatus", "")),
                counterparty_nickname=payload.get("counterPartNickName"),
                created_at=_parse_timestamp(payload.get("createTime")),
            )
        except (KeyError, ValueError, ArithmeticError) as exc:
            raise ExternalServiceResponseError(
                "Binance C2C payload could not be parsed",
                service="binance_c2c",
                details={"error": type(exc).__name__},
            ) from exc


class BinanceC2CClient(BaseAsyncHTTPClient):
    """Signed HTTP client for Binance C2C order operations."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        """Initialize the client.

        Args:
            settings: Active application settings.
            client: Optional pre-built httpx client for tests.
        """
        super().__init__(settings, base_url=settings.binance_c2c_base_url, client=client)

    @property
    def service_name(self) -> str:
        """Stable provider name."""
        return "binance_c2c"

    def _auth_headers(self) -> dict[str, str]:
        """Build authentication headers without logging the key."""
        return {"X-MBX-APIKEY": self._settings.binance_api_key.get_secret_value()}

    def _signed_query(self, params: Mapping[str, Any]) -> str:
        """Attach timestamp, recvWindow and HMAC signature to the query.

        Args:
            params: Business parameters.

        Returns:
            The full signed query string.
        """
        payload: dict[str, Any] = dict(params)
        payload["timestamp"] = int(time.time() * 1000)
        payload.setdefault("recvWindow", self._settings.binance_recv_window_ms)
        query = _sign_query(payload)
        secret = self._settings.binance_api_secret.get_secret_value().encode("utf-8")
        signature = hmac.new(secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{query}&signature={signature}"

    @staticmethod
    def _extract_data(payload: Any) -> Any:
        """Extract the ``data`` field from a Binance envelope when present.

        Args:
            payload: Decoded provider response.

        Returns:
            The unwrapped payload.
        """
        if isinstance(payload, Mapping) and "data" in payload:
            return payload["data"]
        return payload

    async def get_order_history(
        self,
        *,
        page: int = 1,
        rows: int = 20,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        trade_type: OrderType | str | None = None,
        order_status: str | None = None,
    ) -> list[BinanceC2COrder]:
        """Return recent C2C orders, optionally filtered by trade and status.

        The filters travel on the *signed* query string as ``tradeType`` and
        ``orderStatus``; the read stays idempotent so a transient failure is
        retried by the base client.

        Args:
            page: One-based page number.
            rows: Page size.
            start_timestamp: Optional epoch milliseconds lower bound.
            end_timestamp: Optional epoch milliseconds upper bound.
            trade_type: Optional ``BUY``/``SELL`` provider filter.
            order_status: Optional external status such as ``PENDING_PAYMENT``.

        Returns:
            Normalized C2C orders.
        """
        trade_value = (
            trade_type.value if isinstance(trade_type, OrderType) else trade_type
        )
        params = {
            "page": page,
            "rows": rows,
            "startTimestamp": start_timestamp,
            "endTimestamp": end_timestamp,
            "tradeType": trade_value,
            "orderStatus": order_status,
        }
        payload = await self.post(
            _ORDER_HISTORY_PATH,
            params=self._signed_query(params),
            headers=self._auth_headers(),
            idempotent=True,
        )
        data = self._extract_data(payload)
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise ExternalServiceResponseError(
                "Binance order history payload is not a list",
                service=self.service_name,
            )
        return [BinanceC2COrder.from_api_payload(item) for item in data]

    async def get_order_detail(self, order_number: str) -> BinanceC2COrder:
        """Fetch a single C2C order by number.

        Args:
            order_number: Binance C2C order number.

        Returns:
            The normalized order.
        """
        payload = await self.get(
            _ORDER_DETAIL_PATH,
            params=self._signed_query({"adOrderNo": order_number}),
            headers=self._auth_headers(),
            idempotent=True,
        )
        data = self._extract_data(payload)
        if not isinstance(data, Mapping):
            raise ExternalServiceResponseError(
                "Binance order detail payload is not an object",
                service=self.service_name,
            )
        return BinanceC2COrder.from_api_payload(data)

    async def mark_order_as_paid(self, order_number: str) -> None:
        """Mark an order as paid on Binance.

        This call is not idempotent and is therefore never retried.

        Args:
            order_number: Binance C2C order number.
        """
        await self.post(
            _MARK_PAID_PATH,
            params=self._signed_query({"adOrderNo": order_number}),
            headers=self._auth_headers(),
            idempotent=False,
        )

    async def release_order(self, order_number: str) -> None:
        """Release crypto for an order on Binance.

        This call moves money and is not idempotent, so it is never retried
        automatically.

        Note:
            Available only to merchant/API-enabled accounts.  Standard
            accounts must use the Playwright adapter instead; the resulting
            provider error is surfaced as a typed ``LiquidVEException`` so the
            order is parked for review rather than silently completed.

        Args:
            order_number: Binance C2C order number.
        """
        await self.post(
            _RELEASE_PATH,
            params=self._signed_query({"adOrderNo": order_number}),
            headers=self._auth_headers(),
            idempotent=False,
        )

    async def get_spot_account_status(self) -> dict[str, Any]:
        """Read spot account details using the official SDK.

        The official connector is synchronous, so the call runs in a worker
        thread.

        Returns:
            Raw SDK account payload as a dictionary.

        Raises:
            ExternalServiceError: If the SDK is unavailable or the call fails.
        """

        def _call() -> dict[str, Any]:
            from binance.spot import Spot  # imported lazily by design

            spot = Spot(
                api_key=self._settings.binance_api_key.get_secret_value(),
                api_secret=self._settings.binance_api_secret.get_secret_value(),
            )
            return spot.account(recvWindow=self._settings.binance_recv_window_ms)

        try:
            return await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001 - converted to a typed error
            raise ExternalServiceError(
                "Binance spot account check failed",
                service=self.service_name,
                details={"error": type(exc).__name__},
            ) from exc

    async def health_check(self) -> bool:
        """Return whether the C2C host is reachable."""
        # TODO(integration): switch to a documented lightweight endpoint once
        # the provider exposes one.
        return await self._safe_health_check("/")


__all__ = ["BinanceC2COrder", "BinanceC2CClient", "_sign_query"]

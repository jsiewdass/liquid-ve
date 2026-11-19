"""Application settings.

Settings are loaded once from environment variables (and an optional ``.env``
file) and cached with :func:`functools.lru_cache`.  Import the module-level
``settings`` object for read-only access outside of dependency injection.

All credentials use :class:`pydantic.SecretStr` so they are not rendered in
logs, tracebacks or ``repr`` output.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from ipaddress import ip_address, ip_network
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

_VES_QUANT = Decimal("0.01")
_USDT_QUANT = Decimal("0.00000001")
_TAILSCALE_CGNAT = ip_network("100.64.0.0/10")
_SQLITE_DRIVER_RE = re.compile(r"\+(aiosqlite|asyncpg)\b")


class Settings(BaseSettings):
    """Typed, validated configuration for the liquidVE engine."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- application -----------------------------------------------------
    app_name: str = "liquidVE"
    app_env: Literal["local", "staging", "production"] = "local"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    secret_key: SecretStr
    internal_api_key: SecretStr

    # --- database --------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/liquidve.db"
    database_echo: bool = False
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_timeout_seconds: int = 30

    # --- Binance C2C -----------------------------------------------------
    binance_api_key: SecretStr
    binance_api_secret: SecretStr
    binance_c2c_base_url: str = "https://p2p.binance.com"
    binance_recv_window_ms: int = 5000
    binance_asset: str = "USDT"
    binance_fiat: str = "VES"

    # --- Cachicamo -------------------------------------------------------
    cachicamo_base_url: str
    cachicamo_api_key: SecretStr
    cachicamo_webhook_secret: SecretStr
    cachicamo_webhook_tolerance_seconds: int = 300
    cachicamo_signature_header: str = "X-Cachicamo-Signature"
    cachicamo_timestamp_header: str = "X-Cachicamo-Timestamp"
    cachicamo_event_id_header: str = "X-Cachicamo-Event-Id"

    # --- Android SMS gateway (Tailscale) ---------------------------------
    sms_gateway_base_url: str
    sms_gateway_token: SecretStr
    sms_gateway_timeout_seconds: float = 10.0
    sms_gateway_poll_interval_seconds: int = 15
    sms_poller_enabled: bool = False
    sms_gateway_shared_secret: SecretStr | None = None

    # --- outbound HTTP policy -------------------------------------------
    http_timeout_seconds: float = 15.0
    http_connect_timeout_seconds: float = 5.0
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 0.5
    http_backoff_max_seconds: float = 8.0

    # --- risk limits -----------------------------------------------------
    max_tx_amount_ves: Decimal
    min_tx_amount_ves: Decimal = Decimal("1.00")
    daily_limit_ves: Decimal
    max_usdt_per_order: Decimal
    manual_review_on_limit: bool = True

    # --- logging ---------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    # --- HTTP server -----------------------------------------------------
    cors_origins: Annotated[list[str], NoDecode] = []
    allowed_hosts: Annotated[list[str], NoDecode] = ["*"]
    trusted_proxies: Annotated[list[str], NoDecode] = []

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator("cors_origins", "allowed_hosts", "trusted_proxies", mode="before")
    @classmethod
    def _parse_list_env(cls, value: Any) -> Any:
        """Parse comma-separated list variables coming from the environment.

        Args:
            value: Raw value provided by pydantic-settings.

        Returns:
            A list of trimmed strings, or the value unchanged when already a
            sequence.
        """
        if value is None:
            return []
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                try:
                    decoded = json.loads(stripped)
                except ValueError:
                    decoded = None
                if isinstance(decoded, list):
                    return decoded
                return [item.strip() for item in stripped.split(",") if item.strip()]
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator(
        "max_tx_amount_ves",
        "min_tx_amount_ves",
        "daily_limit_ves",
        "max_usdt_per_order",
    )
    @classmethod
    def _validate_money_amounts(cls, value: Decimal, info: ValidationInfo) -> Decimal:
        """Ensure monetary limits are positive and sensibly quantized.

        Args:
            value: Parsed decimal value.
            info: Validation metadata used to identify the field.

        Returns:
            The quantized decimal value.

        Raises:
            ValueError: If the value is not finite or not strictly positive.
        """
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:  # pragma: no cover - defensive
            raise ValueError(f"{info.field_name} must be a decimal number") from exc
        if not amount.is_finite():
            raise ValueError(f"{info.field_name} must be finite")
        if amount <= 0:
            raise ValueError(f"{info.field_name} must be greater than zero")
        quantum = _USDT_QUANT if info.field_name == "max_usdt_per_order" else _VES_QUANT
        return amount.quantize(quantum, rounding=ROUND_HALF_UP)

    @model_validator(mode="after")
    def _validate_consistency(self) -> "Settings":
        """Validate cross-field and environment-dependent invariants.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: If limits are inconsistent, URLs are insecure or
                production secrets are too short.
        """
        if self.daily_limit_ves < self.max_tx_amount_ves:
            raise ValueError("daily_limit_ves must be greater than or equal to max_tx_amount_ves")
        if self.min_tx_amount_ves > self.max_tx_amount_ves:
            raise ValueError("min_tx_amount_ves must be less than or equal to max_tx_amount_ves")
        for field_name in ("binance_c2c_base_url", "cachicamo_base_url", "sms_gateway_base_url"):
            _ensure_secure_base_url(field_name, getattr(self, field_name))
        if self.is_production:
            if self.debug:
                raise ValueError("debug must be disabled in production")
            if len(self.secret_key.get_secret_value()) < 32:
                raise ValueError("secret_key must be at least 32 characters in production")
            if len(self.cachicamo_webhook_secret.get_secret_value()) < 32:
                raise ValueError(
                    "cachicamo_webhook_secret must be at least 32 characters in production"
                )
        return self

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        """Return ``True`` when running with the production environment."""
        return self.app_env == "production"

    @property
    def sync_database_url(self) -> str:
        """Return the database URL without the async driver suffix.

        Useful for tools that only speak synchronous SQLAlchemy (for example
        some Alembic helpers or test utilities).
        """
        return _SQLITE_DRIVER_RE.sub("", self.database_url)

    def base_settings_summary(self) -> dict[str, Any]:
        """Return a log-safe summary of the active configuration.

        Passwords embedded in the database URL are hidden.  No credential
        value is ever included.

        Returns:
            A dictionary of non-secret configuration values.
        """
        try:
            safe_url = make_url(self.database_url).render_as_string(hide_password=True)
        except Exception:  # noqa: BLE001 - summary must never fail
            safe_url = "<redacted>"
        return {
            "app_name": self.app_name,
            "app_env": self.app_env,
            "debug": self.debug,
            "api_v1_prefix": self.api_v1_prefix,
            "database_url": safe_url,
            "log_level": self.log_level,
            "log_json": self.log_json,
            "sms_poller_enabled": self.sms_poller_enabled,
            "cors_origins": list(self.cors_origins),
            "allowed_hosts": list(self.allowed_hosts),
            "max_tx_amount_ves": str(self.max_tx_amount_ves),
            "min_tx_amount_ves": str(self.min_tx_amount_ves),
            "daily_limit_ves": str(self.daily_limit_ves),
            "max_usdt_per_order": str(self.max_usdt_per_order),
        }


def _is_private_or_local_host(host: str) -> bool:
    """Return ``True`` for loopback, RFC1918 and Tailscale CGNAT hosts.

    Args:
        host: Hostname or IP address extracted from a URL.

    Returns:
        Whether plain HTTP is acceptable for this host.
    """
    normalized = host.strip().lower().strip("[]")
    if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith(".local"):
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        return False
    if address.is_loopback or address.is_private:
        return True
    return address in _TAILSCALE_CGNAT


def _ensure_secure_base_url(field_name: str, url: str) -> None:
    """Validate that an external base URL uses HTTPS unless it is local.

    Args:
        field_name: Settings field name, used in the error message.
        url: URL to validate.

    Raises:
        ValueError: If the URL is malformed or uses plain HTTP for a public
            host.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field_name} must be an absolute http(s) URL")
    if parsed.scheme == "https":
        return
    if not _is_private_or_local_host(parsed.hostname):
        raise ValueError(f"{field_name} must use https unless the host is local or on Tailscale")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()


settings: Annotated[Settings, "Cached application settings"] = get_settings()


__all__ = ["Settings", "get_settings", "settings"]

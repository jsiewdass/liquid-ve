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

    # --- buy flow (SMS payout) -------------------------------------------
    buy_auto_pay_enabled: bool = False
    max_buy_amount_ves: Decimal
    daily_pay_limit_ves: Decimal
    # Deprecated one-release aliases; both resolve onto the canonical field.
    daily_sms_limit_ves: Decimal | None = None
    rate_tolerance_pct: Decimal = Decimal("1.0")
    payment_confirmation_timeout_seconds: int = 300
    sms_confirmation_timeout_seconds: int | None = None
    bank_short_codes_bdv: Annotated[list[str], NoDecode] = []
    bank_short_codes_banesco: Annotated[list[str], NoDecode] = []

    # --- Binance P2P browser (buy flow) ----------------------------------
    binance_browser_enabled: bool = False
    binance_browser_headless: bool = True
    binance_browser_timeout_ms: int = 20000
    binance_session_state_path: str = "./data/binance_state.json"
    binance_p2p_order_url_template: str = (
        "https://p2p.binance.com/en/order/{order_number}"
    )

    # --- BUY detection worker --------------------------------------------
    buy_worker_enabled: bool = False
    buy_worker_poll_interval_seconds: int = 30

    # --- BUY payment rail (Banesco Online) -------------------------------
    banesco_rail_enabled: bool = False
    banesco_online_login_url: str = (
        "https://www.banesconline.com/Mantis/WebSite/Login.aspx"
    )
    banesco_online_user: SecretStr | None = None
    banesco_online_password: SecretStr | None = None
    banesco_security_answer_hobbie: SecretStr | None = None
    banesco_security_answer_childhood_hero: SecretStr | None = None
    banesco_security_answer_favourite_dessert: SecretStr | None = None
    banesco_security_answer_country_to_visit: SecretStr | None = None
    banesco_browser_headless: bool = True
    banesco_browser_timeout_ms: int = 30000
    banesco_max_concurrent_payments: int = 1
    banesco_clave_retry_limit: int = 0
    banesco_payment_concept: str = "Pago"
    banesco_default_id_type: str = ""
    buy_payment_rail: Literal["banesco"] = "banesco"

    # --- Telegram operator handoff ---------------------------------------
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_operator_chat_id: int | None = None
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_clave_timeout_seconds: int = 300
    telegram_id_type_timeout_seconds: int = 300
    telegram_poll_timeout_seconds: int = 30
    telegram_max_clarifications: int = 2

    # --- receipt evidence ------------------------------------------------
    receipt_storage_dir: str = "./data/receipts"
    receipt_retention_days: int = 30

    # --- release watcher and appeal --------------------------------------
    release_watcher_enabled: bool = False
    release_poll_interval_seconds: int = 30
    release_timeout_seconds: int = 900
    release_appeal_mode: Literal["auto", "alert", "off"] = "auto"

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
    @field_validator(
        "cors_origins",
        "allowed_hosts",
        "trusted_proxies",
        "bank_short_codes_bdv",
        "bank_short_codes_banesco",
        mode="before",
    )
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
        "max_buy_amount_ves",
        "daily_pay_limit_ves",
        "daily_sms_limit_ves",
    )
    @classmethod
    def _validate_money_amounts(
        cls, value: Decimal | None, info: ValidationInfo
    ) -> Decimal | None:
        """Ensure monetary limits are positive and sensibly quantized.

        Args:
            value: Parsed decimal value, or ``None`` for an unused alias.
            info: Validation metadata used to identify the field.

        Returns:
            The quantized decimal value, or ``None`` for an unused alias.

        Raises:
            ValueError: If the value is not finite or not strictly positive.
        """
        if value is None:
            return None
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

    @field_validator("rate_tolerance_pct")
    @classmethod
    def _validate_rate_tolerance(cls, value: Decimal) -> Decimal:
        """Ensure the implied-rate tolerance is a percentage in ``(0, 100]``.

        Args:
            value: Parsed tolerance in percent.

        Returns:
            The tolerance quantized to two decimals.

        Raises:
            ValueError: If the value is not finite or outside ``(0, 100]``.
        """
        try:
            tolerance = Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:  # pragma: no cover - defensive
            raise ValueError("rate_tolerance_pct must be a decimal number") from exc
        if not tolerance.is_finite() or tolerance <= 0 or tolerance > Decimal("100"):
            raise ValueError("rate_tolerance_pct must be greater than 0 and at most 100")
        return tolerance.quantize(_VES_QUANT, rounding=ROUND_HALF_UP)

    @model_validator(mode="before")
    @classmethod
    def _apply_deprecated_aliases(cls, data: Any) -> Any:
        """Resolve one-release deprecated setting names onto canonical fields.

        Args:
            data: Raw settings data assembled from the environment and init.

        Returns:
            The data with deprecated names folded into their canonical field.
        """
        if not isinstance(data, dict):
            return data
        if (
            data.get("daily_pay_limit_ves") is None
            and data.get("daily_sms_limit_ves") is not None
        ):
            data["daily_pay_limit_ves"] = data["daily_sms_limit_ves"]
        if (
            data.get("payment_confirmation_timeout_seconds") is None
            and data.get("sms_confirmation_timeout_seconds") is not None
        ):
            data["payment_confirmation_timeout_seconds"] = data[
                "sms_confirmation_timeout_seconds"
            ]
        return data

    @model_validator(mode="after")
    def _validate_consistency(self) -> "Settings":
        """Validate cross-field and environment-dependent invariants.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: If limits are inconsistent, URLs are insecure,
                enabled features miss a required setting, or production
                secrets are too short.
        """
        # The deprecated aliases always mirror the resolved canonical value.
        self.daily_sms_limit_ves = self.daily_pay_limit_ves
        self.sms_confirmation_timeout_seconds = self.payment_confirmation_timeout_seconds

        if self.daily_limit_ves < self.max_tx_amount_ves:
            raise ValueError("daily_limit_ves must be greater than or equal to max_tx_amount_ves")
        if self.min_tx_amount_ves > self.max_tx_amount_ves:
            raise ValueError("min_tx_amount_ves must be less than or equal to max_tx_amount_ves")
        if self.daily_pay_limit_ves < self.max_buy_amount_ves:
            raise ValueError(
                "daily_pay_limit_ves must be greater than or equal to max_buy_amount_ves"
            )
        for field_name in ("binance_c2c_base_url", "cachicamo_base_url", "sms_gateway_base_url"):
            _ensure_secure_base_url(field_name, getattr(self, field_name))
        if self.banesco_default_id_type not in {"", "V", "E", "J", "G", "R"}:
            raise ValueError("banesco_default_id_type must be empty or one of V, E, J, G, R")
        if self.receipt_retention_days < 0:
            raise ValueError("receipt_retention_days must be greater than or equal to zero")
        if self.banesco_rail_enabled:
            for field_name in (
                "banesco_online_user",
                "banesco_online_password",
                "banesco_security_answer_hobbie",
                "banesco_security_answer_childhood_hero",
                "banesco_security_answer_favourite_dessert",
                "banesco_security_answer_country_to_visit",
            ):
                _require_secret(field_name, getattr(self, field_name))
            _ensure_https_url("banesco_online_login_url", self.banesco_online_login_url)
            if self.banesco_browser_timeout_ms <= 0:
                raise ValueError("banesco_browser_timeout_ms must be greater than zero")
            if self.banesco_max_concurrent_payments < 1:
                raise ValueError("banesco_max_concurrent_payments must be at least one")
            if not self.telegram_enabled:
                raise ValueError("telegram_enabled must be true when banesco_rail_enabled is true")
            if not self.binance_browser_enabled:
                raise ValueError(
                    "binance_browser_enabled must be true when banesco_rail_enabled is true"
                )
        if self.telegram_enabled or self.banesco_rail_enabled:
            _require_secret("telegram_bot_token", self.telegram_bot_token)
            if self.telegram_operator_chat_id is None or self.telegram_operator_chat_id <= 0:
                raise ValueError("telegram_operator_chat_id must be a positive integer")
            if self.telegram_clave_timeout_seconds <= 0:
                raise ValueError("telegram_clave_timeout_seconds must be greater than zero")
            if self.telegram_poll_timeout_seconds <= 0:
                raise ValueError("telegram_poll_timeout_seconds must be greater than zero")
        if self.release_watcher_enabled:
            if self.release_poll_interval_seconds <= 0:
                raise ValueError("release_poll_interval_seconds must be greater than zero")
            if self.release_timeout_seconds <= 0:
                raise ValueError("release_timeout_seconds must be greater than zero")
            if self.release_appeal_mode == "auto" and not self.binance_browser_enabled:
                raise ValueError(
                    "binance_browser_enabled must be true when release_appeal_mode is auto"
                )
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
            "buy_auto_pay_enabled": self.buy_auto_pay_enabled,
            "max_buy_amount_ves": str(self.max_buy_amount_ves),
            "daily_pay_limit_ves": str(self.daily_pay_limit_ves),
            "daily_sms_limit_ves": str(self.daily_sms_limit_ves),
            "rate_tolerance_pct": str(self.rate_tolerance_pct),
            "binance_browser_enabled": self.binance_browser_enabled,
            "binance_browser_headless": self.binance_browser_headless,
            "binance_browser_timeout_ms": self.binance_browser_timeout_ms,
            "buy_worker_enabled": self.buy_worker_enabled,
            "buy_worker_poll_interval_seconds": self.buy_worker_poll_interval_seconds,
            "banesco_rail_enabled": self.banesco_rail_enabled,
            "banesco_browser_headless": self.banesco_browser_headless,
            "banesco_browser_timeout_ms": self.banesco_browser_timeout_ms,
            "banesco_max_concurrent_payments": self.banesco_max_concurrent_payments,
            "telegram_enabled": self.telegram_enabled,
            "receipt_retention_days": self.receipt_retention_days,
            "release_watcher_enabled": self.release_watcher_enabled,
            "release_poll_interval_seconds": self.release_poll_interval_seconds,
            "release_timeout_seconds": self.release_timeout_seconds,
            "release_appeal_mode": self.release_appeal_mode,
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


def _require_secret(field_name: str, value: SecretStr | None) -> None:
    """Require a non-empty secret value when a rail feature is enabled.

    Args:
        field_name: Settings field name, used in the error message only.
        value: Secret value to check.

    Raises:
        ValueError: If the value is missing or blank.
    """
    if value is None or not value.get_secret_value().strip():
        raise ValueError(f"{field_name} is required when the payment rail is enabled")


def _ensure_https_url(field_name: str, url: str) -> None:
    """Require an absolute HTTPS URL.

    Args:
        field_name: Settings field name, used in the error message.
        url: URL to validate.

    Raises:
        ValueError: If the URL is not an absolute HTTPS URL.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field_name} must be an absolute https URL")


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

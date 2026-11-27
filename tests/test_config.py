"""Buy-flow settings rollout.

The two money caps are *required*: a silently defaulted spend ceiling is worse
than a failed boot.  Everything else has a safe default so an existing ``.env``
keeps working.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings

_RAIL_SECRET_ENV = (
    "BANESCO_ONLINE_USER",
    "BANESCO_ONLINE_PASSWORD",
    "BANESCO_SECURITY_ANSWER_HOBBIE",
    "BANESCO_SECURITY_ANSWER_CHILDHOOD_HERO",
    "BANESCO_SECURITY_ANSWER_FAVOURITE_DESSERT",
    "BANESCO_SECURITY_ANSWER_COUNTRY_TO_VISIT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OPERATOR_CHAT_ID",
)


def _clear_rail_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every rail secret and switch from the process environment.

    Args:
        monkeypatch: Pytest environment patcher.
    """
    for name in (
        *_RAIL_SECRET_ENV,
        "BANESCO_RAIL_ENABLED",
        "TELEGRAM_ENABLED",
        "RELEASE_WATCHER_ENABLED",
        "BINANCE_BROWSER_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def test_buy_settings_defaults(test_settings: Settings) -> None:
    """The operator gate and the non-required scalars default safely."""
    assert test_settings.rate_tolerance_pct == Decimal("1.00")


def test_max_buy_cap_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing the per-order cap from the environment fails validation."""
    monkeypatch.delenv("MAX_BUY_AMOUNT_VES", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_daily_pay_cap_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daily cap is required."""
    monkeypatch.delenv("DAILY_PAY_LIMIT_VES", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_daily_pay_limit_must_cover_max_buy() -> None:
    """The daily pay cap may not be smaller than the per-order cap."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            max_buy_amount_ves=Decimal("5000.00"),
            daily_pay_limit_ves=Decimal("1000.00"),
        )

    equal = Settings(
        _env_file=None,
        max_buy_amount_ves=Decimal("5000.00"),
        daily_pay_limit_ves=Decimal("5000.00"),
    )
    assert equal.daily_pay_limit_ves == equal.max_buy_amount_ves


def test_buy_caps_reject_float_and_zero() -> None:
    """Zero, negative and non-positive floats are refused on both caps."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_buy_amount_ves=0.0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, daily_pay_limit_ves=Decimal("0"))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_buy_amount_ves=Decimal("-1.00"))


def test_rate_tolerance_validator_bounds_and_quantizes() -> None:
    """Tolerance is strictly between 0 and 100 percent and quantized to cents."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rate_tolerance_pct=Decimal("0"))
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rate_tolerance_pct=Decimal("100.01"))

    exact = Settings(_env_file=None, rate_tolerance_pct=Decimal("1.0"))
    assert exact.rate_tolerance_pct == Decimal("1.00")

    down = Settings(_env_file=None, rate_tolerance_pct=Decimal("1.004"))
    assert down.rate_tolerance_pct == Decimal("1.00")

    up = Settings(_env_file=None, rate_tolerance_pct=Decimal("1.006"))
    assert up.rate_tolerance_pct == Decimal("1.01")


def test_settings_builds_from_conftest_env_alone() -> None:
    """The required caps are satisfied by the shared conftest environment."""
    get_settings.cache_clear()
    try:
        built = Settings()
        assert built.max_buy_amount_ves == Decimal("5000.00")
        assert built.daily_pay_limit_ves == Decimal("20000.00")
        assert built.rate_tolerance_pct == Decimal("1.00")
    finally:
        get_settings.cache_clear()


def test_summary_exposes_buy_scalars(test_settings: Settings) -> None:
    """The log-safe summary includes the four non-secret buy scalars."""
    summary = test_settings.base_settings_summary()

    assert summary["daily_pay_limit_ves"] == "20000.00"
    assert summary["max_buy_amount_ves"] == "5000.00"
    assert summary["rate_tolerance_pct"] == "1.00"


def test_browser_settings_defaults(test_settings: Settings) -> None:
    """Browser automation ships off with explicit, non-secret defaults."""
    assert test_settings.binance_browser_enabled is False
    assert test_settings.binance_browser_headless is True
    assert test_settings.binance_browser_timeout_ms == 20000
    assert test_settings.binance_session_state_path == "./data/binance_state.json"
    assert (
        test_settings.binance_p2p_order_url_template
        == "https://p2p.binance.com/en/order/{order_number}"
    )


def test_browser_switches_default_off() -> None:
    """Settings built from the shared environment alone keeps the browser off."""
    built = Settings(_env_file=None)

    assert built.binance_browser_enabled is False
    assert built.binance_browser_headless is True
    assert built.binance_browser_timeout_ms == 20000


def test_browser_settings_parse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every browser setting is overridable from the environment."""
    monkeypatch.setenv("BINANCE_BROWSER_ENABLED", "true")
    monkeypatch.setenv("BINANCE_BROWSER_HEADLESS", "false")
    monkeypatch.setenv("BINANCE_BROWSER_TIMEOUT_MS", "45000")
    monkeypatch.setenv("BINANCE_SESSION_STATE_PATH", "./tmp/custom_state.json")
    monkeypatch.setenv(
        "BINANCE_P2P_ORDER_URL_TEMPLATE",
        "https://p2p.binance.com/en/order/detail/{order_number}",
    )

    built = Settings(_env_file=None)

    assert built.binance_browser_enabled is True
    assert built.binance_browser_headless is False
    assert built.binance_browser_timeout_ms == 45000
    assert built.binance_session_state_path == "./tmp/custom_state.json"
    assert (
        built.binance_p2p_order_url_template
        == "https://p2p.binance.com/en/order/detail/{order_number}"
    )


def test_summary_exposes_browser_scalars(test_settings: Settings) -> None:
    """The log-safe summary states whether browser automation is enabled."""
    summary = test_settings.base_settings_summary()

    assert summary["binance_browser_enabled"] is False
    assert summary["binance_browser_headless"] is True
    assert summary["binance_browser_timeout_ms"] == 20000


def test_worker_settings_default_off(test_settings: Settings) -> None:
    """The buy worker ships disabled with a safe polling interval."""
    assert test_settings.buy_worker_enabled is False
    assert test_settings.buy_worker_poll_interval_seconds == 30


def test_feature_switches_default_off(test_settings: Settings) -> None:
    """The whole BUY path is unreachable behind three default-off switches."""
    assert test_settings.buy_worker_enabled is False
    assert test_settings.binance_browser_enabled is False


def test_worker_settings_parse_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker switch and interval are overridable from the environment."""
    monkeypatch.setenv("BUY_WORKER_ENABLED", "true")
    monkeypatch.setenv("BUY_WORKER_POLL_INTERVAL_SECONDS", "45")

    built = Settings(_env_file=None)

    assert built.buy_worker_enabled is True
    assert built.buy_worker_poll_interval_seconds == 45


def test_new_rail_settings_are_defaulted_and_summary_is_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every new rail setting defaults safely and the summary never leaks secrets."""
    _clear_rail_env(monkeypatch)

    built = Settings(_env_file=None)

    assert built.banesco_rail_enabled is False
    assert built.banesco_online_login_url.startswith("https://")
    assert built.banesco_online_user is None
    assert built.banesco_online_password is None
    assert built.banesco_security_answer_hobbie is None
    assert built.banesco_security_answer_childhood_hero is None
    assert built.banesco_security_answer_favourite_dessert is None
    assert built.banesco_security_answer_country_to_visit is None
    assert built.banesco_browser_headless is True
    assert built.banesco_browser_timeout_ms == 30000
    assert built.banesco_max_concurrent_payments == 1
    assert built.banesco_clave_retry_limit == 0
    assert built.banesco_payment_concept == "Pago"
    assert built.banesco_default_id_type == ""
    assert built.telegram_enabled is False
    assert built.telegram_bot_token is None
    assert built.telegram_operator_chat_id is None
    assert built.telegram_api_base_url == "https://api.telegram.org"
    assert built.telegram_clave_timeout_seconds == 30
    assert built.receipt_storage_dir == "./data/receipts"
    assert built.receipt_retention_days == 30
    assert built.release_watcher_enabled is False
    assert built.release_poll_interval_seconds == 30
    assert built.release_timeout_seconds == 900
    assert built.release_appeal_mode == "auto"
    assert built.buy_payment_rail == "banesco"

    canaries = {
        "banesco_online_user": "canary-banesco-user",
        "banesco_online_password": "canary-banesco-password",
        "banesco_security_answer_hobbie": "canary-answer-hobbie",
        "banesco_security_answer_childhood_hero": "canary-answer-hero",
        "banesco_security_answer_favourite_dessert": "canary-answer-dessert",
        "banesco_security_answer_country_to_visit": "canary-answer-country",
        "telegram_bot_token": "canary-telegram-token",
    }
    configured = Settings(
        _env_file=None,
        telegram_operator_chat_id=987654321,
        **canaries,
    )
    summary = configured.base_settings_summary()
    rendered = json.dumps(summary, default=str)

    for key in (
        "banesco_rail_enabled",
        "telegram_enabled",
        "release_watcher_enabled",
        "release_poll_interval_seconds",
        "release_timeout_seconds",
        "release_appeal_mode",
        "receipt_retention_days",
        "banesco_browser_headless",
        "banesco_browser_timeout_ms",
        "banesco_max_concurrent_payments",
    ):
        assert key in summary
    for secret_name, secret_value in canaries.items():
        assert secret_name not in summary
        assert secret_value not in rendered
    assert "987654321" not in rendered


def test_rail_enabled_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabling the rail without a password fails closed at import time."""
    _clear_rail_env(monkeypatch)

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            banesco_rail_enabled=True,
            banesco_online_user="test-banesco-user",
            banesco_security_answer_hobbie="test-answer",
            banesco_security_answer_childhood_hero="test-answer",
            banesco_security_answer_favourite_dessert="test-answer",
            banesco_security_answer_country_to_visit="test-answer",
            telegram_enabled=True,
            telegram_bot_token="test-telegram-bot-token",
            telegram_operator_chat_id=123456789,
            binance_browser_enabled=True,
        )


def test_rail_enabled_requires_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rail cannot run without the Telegram operator handoff enabled."""
    _clear_rail_env(monkeypatch)

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            banesco_rail_enabled=True,
            banesco_online_user="test-banesco-user",
            banesco_online_password="test-banesco-password",
            banesco_security_answer_hobbie="test-answer",
            banesco_security_answer_childhood_hero="test-answer",
            banesco_security_answer_favourite_dessert="test-answer",
            banesco_security_answer_country_to_visit="test-answer",
            telegram_enabled=False,
            telegram_bot_token="test-telegram-bot-token",
            telegram_operator_chat_id=123456789,
            binance_browser_enabled=True,
        )


def test_auto_appeal_requires_the_binance_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Automatic appeals are browser actions; without a browser they are refused."""
    _clear_rail_env(monkeypatch)

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            release_watcher_enabled=True,
            release_appeal_mode="auto",
            binance_browser_enabled=False,
        )


def test_default_id_type_rejects_unknown_letter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an empty value or a reviewed letter is accepted for the default ID type."""
    _clear_rail_env(monkeypatch)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, banesco_default_id_type="P")

    for letter in ("", "V", "E", "J", "G", "R"):
        built = Settings(_env_file=None, banesco_default_id_type=letter)
        assert built.banesco_default_id_type == letter


@pytest.mark.parametrize("seconds", [0, 31, 300])
def test_bank_code_window_cannot_exceed_thirty_seconds(seconds: int) -> None:
    with pytest.raises(ValidationError, match="telegram_clave_timeout_seconds"):
        Settings(_env_file=None, telegram_clave_timeout_seconds=seconds)

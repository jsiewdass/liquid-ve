"""Tests for the Playwright Binance browser adapter (part 2).

Two hard rules drive this file:

* ``playwright.async_api`` is imported lazily.  Importing the module, building
  the adapter and even failing on a missing session state must never import
  Playwright, which is asserted in-process and in a subprocess.
* the default run never launches a browser.  The one live-DOM smoke is marked
  ``integration`` **and** gated on ``LIQUIDVE_BROWSER_TESTS``, because
  ``playwright`` is installed in this venv and ``importorskip`` would pass.

Extraction runs through a fake Playwright context injected on the adapter, so
navigation, timeout wiring and parser delegation are covered without Chromium.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.core.exceptions import ExternalServiceError, ExternalServiceResponseError
from app.domain.ports import PaymentInstruction
from app.infrastructure.clients.binance_browser import (
    PlaywrightBinanceBrowser,
    validate_session_state,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "binance"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIVE_COOKIE = {"name": "session", "value": "token", "expires": -1}


def _read_fixture(name: str) -> str:
    """Return one committed Binance order snapshot as UTF-8 text."""
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _write_state(path: Path, *, cookies: list[dict[str, Any]] | None = None) -> Path:
    """Write a Playwright storage-state file and return its path."""
    payload = {"cookies": cookies if cookies is not None else [_LIVE_COOKIE], "origins": []}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _settings_with(settings: Settings, *, state_path: str, **overrides: Any) -> Settings:
    """Derive settings pointing at ``state_path`` with optional field overrides."""
    return settings.model_copy(update={"binance_session_state_path": state_path, **overrides})


class _FakePage:
    """Playwright page double recording navigation calls."""

    def __init__(self, html: str, *, error: Exception | None = None) -> None:
        self.html = html
        self.error = error
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append((url, kwargs))
        if self.error is not None:
            raise self.error

    async def content(self) -> str:
        return self.html

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    """Playwright browser-context double."""

    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.default_timeout_ms: int | None = None
        self.closed = False

    async def new_page(self) -> _FakePage:
        return self.page

    def set_default_timeout(self, timeout_ms: int) -> None:
        self.default_timeout_ms = timeout_ms

    async def close(self) -> None:
        self.closed = True


class _FakeBrowserHandle:
    """Launched-browser double recording ``new_context`` options."""

    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.new_context_kwargs: dict[str, Any] | None = None
        self.closed = False

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        self.new_context_kwargs = kwargs
        return self.context

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    """Chromium launcher double recording ``launch`` options."""

    def __init__(self, context: _FakeContext, *, error: Exception | None = None) -> None:
        self.context = context
        self.error = error
        self.launch_kwargs: dict[str, Any] | None = None
        self.handle: _FakeBrowserHandle | None = None

    async def launch(self, **kwargs: Any) -> _FakeBrowserHandle:
        self.launch_kwargs = kwargs
        if self.error is not None:
            raise self.error
        self.handle = _FakeBrowserHandle(self.context)
        return self.handle


class _FakePlaywrightDriver:
    """Playwright driver double exposing ``chromium`` and ``stop``."""

    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch, driver: _FakePlaywrightDriver
) -> None:
    """Install a fake ``playwright.async_api`` module for one test."""

    class _FakeAsyncPlaywright:
        async def start(self) -> _FakePlaywrightDriver:
            return driver

    fake_api = types.ModuleType("playwright.async_api")
    fake_api.async_playwright = _FakeAsyncPlaywright  # type: ignore[attr-defined]
    fake_package = types.ModuleType("playwright")
    fake_package.async_api = fake_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", fake_package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)


def _build_extraction_browser(
    settings: Settings, page: _FakePage
) -> PlaywrightBinanceBrowser:
    """Build an adapter with a fake context injected instead of Chromium."""
    browser = PlaywrightBinanceBrowser(settings)
    browser._context = _FakeContext(page)
    return browser


# ---------------------------------------------------------------------------
# Module import and the pure session-state helper
# ---------------------------------------------------------------------------


def test_adapter_module_import_does_not_require_playwright() -> None:
    """Importing the adapter module must not import Playwright."""
    probe = (
        "import sys, app.infrastructure.clients.binance_browser as m; "
        "assert 'playwright' not in sys.modules; "
        "assert hasattr(m, 'PlaywrightBinanceBrowser'); "
        "assert hasattr(m, 'validate_session_state'); "
        "assert hasattr(m, 'parse_payment_instructions'); "
        "print('adapter browser-free OK')"
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "adapter browser-free OK" in completed.stdout


def test_validate_session_state_accepts_a_valid_state_file(tmp_path: Path) -> None:
    """A state file with one live session cookie passes validation."""
    validate_session_state(str(_write_state(tmp_path / "state.json")))


def test_validate_session_state_keeps_live_cookie_alongside_expired_one(
    tmp_path: Path,
) -> None:
    """One live cookie is enough even when another cookie already expired."""
    expired_at = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    live_at = (datetime.now(timezone.utc) + timedelta(days=7)).timestamp()
    state = _write_state(
        tmp_path / "state.json",
        cookies=[
            {"name": "old", "value": "token", "expires": expired_at},
            {"name": "fresh", "value": "token", "expires": live_at},
        ],
    )

    validate_session_state(str(state))


def test_validate_session_state_rejects_expired_cookies(tmp_path: Path) -> None:
    """A state whose cookies all expired cannot authenticate a session."""
    expired_at = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
    state = _write_state(
        tmp_path / "state.json",
        cookies=[{"name": "session", "value": "token", "expires": expired_at}],
    )

    with pytest.raises(ExternalServiceError) as excinfo:
        validate_session_state(str(state))

    assert excinfo.value.code == "binance_session_expired"


@pytest.mark.parametrize("content", [None, "   ", "{not json", '{"origins": []}'])
def test_validate_session_state_rejects_unusable_files(
    tmp_path: Path, content: str | None
) -> None:
    """Missing, empty, malformed or cookie-less states are typed failures."""
    state = tmp_path / "state.json"
    if content is not None:
        state.write_text(content, encoding="utf-8")

    with pytest.raises(ExternalServiceError) as excinfo:
        validate_session_state(str(state))

    assert excinfo.value.service == "binance_browser"
    assert excinfo.value.code == "binance_session_expired"


# ---------------------------------------------------------------------------
# Lazy import, typed session errors and lifecycle
# ---------------------------------------------------------------------------


async def test_missing_state_error_is_typed(
    test_settings: Settings, tmp_path: Path
) -> None:
    """Extraction fails with a typed error before importing Playwright."""
    browser = PlaywrightBinanceBrowser(
        _settings_with(test_settings, state_path=str(tmp_path / "absent.json"))
    )

    with pytest.raises(ExternalServiceError) as excinfo:
        await browser.extract_payment_instruction("ORD-1")

    assert excinfo.value.service == "binance_browser"
    assert excinfo.value.code == "binance_session_expired"
    assert "playwright" not in sys.modules


@pytest.mark.parametrize("headless", [True, False])
async def test_aenter_wires_storage_state_headless_and_timeout(
    test_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    headless: bool,
) -> None:
    """Aentering launches with the state file, headless flag and timeout."""
    state = _write_state(tmp_path / "state.json")
    context = _FakeContext(_FakePage(""))
    chromium = _FakeChromium(context)
    driver = _FakePlaywrightDriver(chromium)
    _install_fake_playwright(monkeypatch, driver)
    settings = _settings_with(
        test_settings,
        state_path=str(state),
        binance_browser_headless=headless,
        binance_browser_timeout_ms=20000,
    )

    async with PlaywrightBinanceBrowser(settings) as browser:
        assert browser._context is context

    assert chromium.launch_kwargs == {"headless": headless}
    assert chromium.handle is not None
    assert chromium.handle.new_context_kwargs == {"storage_state": str(state)}
    assert context.default_timeout_ms == 20000
    assert context.closed is True
    assert chromium.handle.closed is True
    assert driver.stopped is True


async def test_aenter_failure_is_typed(
    test_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Chromium launch failure surfaces as a typed browser error."""
    state = _write_state(tmp_path / "state.json")
    chromium = _FakeChromium(
        _FakeContext(_FakePage("")), error=RuntimeError("chromium exploded")
    )
    driver = _FakePlaywrightDriver(chromium)
    _install_fake_playwright(monkeypatch, driver)

    with pytest.raises(ExternalServiceError) as excinfo:
        async with PlaywrightBinanceBrowser(
            _settings_with(test_settings, state_path=str(state))
        ):
            pass

    assert excinfo.value.code == "binance_browser_start_failed"
    assert driver.stopped is True


async def test_first_call_starts_the_browser_without_a_context_manager(
    test_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare extraction call lazily starts the browser and can close it."""
    state = _write_state(tmp_path / "state.json")
    page = _FakePage(_read_fixture("order_view_bdv.html"))
    chromium = _FakeChromium(_FakeContext(page))
    driver = _FakePlaywrightDriver(chromium)
    _install_fake_playwright(monkeypatch, driver)
    browser = PlaywrightBinanceBrowser(
        _settings_with(test_settings, state_path=str(state))
    )

    try:
        instruction = await browser.extract_payment_instruction("ORD-BDV-0001")
    finally:
        await browser.aclose()

    assert instruction.amount_ves == Decimal("1500.00")
    assert page.goto_calls[0][0] == "https://p2p.binance.com/en/order/ORD-BDV-0001"
    assert chromium.launch_kwargs == {"headless": True}
    assert driver.stopped is True
    assert browser._context is None


async def test_aclose_is_safe_before_start(test_settings: Settings) -> None:
    """Closing an adapter that never started is a no-op."""
    browser = PlaywrightBinanceBrowser(test_settings)

    await browser.aclose()
    await browser.aclose()

    assert browser._context is None


# ---------------------------------------------------------------------------
# Extraction through a fake context (no Chromium)
# ---------------------------------------------------------------------------


async def test_extraction_navigates_template_parses_and_closes_page(
    test_settings: Settings, tmp_path: Path
) -> None:
    """Extraction navigates the configured URL and delegates to the parser."""
    state = _write_state(tmp_path / "state.json")
    page = _FakePage(_read_fixture("order_view_bdv.html"))
    browser = _build_extraction_browser(
        _settings_with(test_settings, state_path=str(state)), page
    )

    instruction = await browser.extract_payment_instruction("ORD-BDV-0001")

    assert isinstance(instruction, PaymentInstruction)
    assert instruction.order_number == "ORD-BDV-0001"
    assert instruction.bank_numeric_code == "0102"
    assert instruction.phone == "04141234567"
    assert instruction.id_number == "V-12345678"
    assert instruction.amount_ves == Decimal("1500.00")
    assert page.goto_calls == [
        ("https://p2p.binance.com/en/order/ORD-BDV-0001", {"timeout": 20000})
    ]
    assert page.closed is True
    assert "playwright" not in sys.modules


async def test_extraction_uses_custom_url_template_and_timeout(
    test_settings: Settings, tmp_path: Path
) -> None:
    """The order URL and timeout come from settings, not hardcoded values."""
    state = _write_state(tmp_path / "state.json")
    page = _FakePage(_read_fixture("order_view_banesco.html"))
    browser = _build_extraction_browser(
        _settings_with(
            test_settings,
            state_path=str(state),
            binance_p2p_order_url_template=(
                "https://p2p.binance.com/en/order/detail/{order_number}"
            ),
            binance_browser_timeout_ms=45000,
        ),
        page,
    )

    instruction = await browser.extract_payment_instruction("ORD-BAN-0002")

    assert instruction.bank_numeric_code == "0134"
    assert page.goto_calls == [
        (
            "https://p2p.binance.com/en/order/detail/ORD-BAN-0002",
            {"timeout": 45000},
        )
    ]


async def test_unparseable_page_raises_payment_data_unavailable(
    test_settings: Settings, tmp_path: Path
) -> None:
    """A loaded page without payment data is a typed response error."""
    state = _write_state(tmp_path / "state.json")
    page = _FakePage("<div>no order labels here</div>")
    browser = _build_extraction_browser(
        _settings_with(test_settings, state_path=str(state)), page
    )

    with pytest.raises(ExternalServiceResponseError) as excinfo:
        await browser.extract_payment_instruction("ORD-1")

    assert excinfo.value.service == "binance_browser"
    assert excinfo.value.code == "payment_data_unavailable"
    assert page.closed is True


async def test_navigation_failure_is_typed_and_closes_page(
    test_settings: Settings, tmp_path: Path
) -> None:
    """A navigation timeout surfaces as a typed error and still closes the page."""
    state = _write_state(tmp_path / "state.json")
    page = _FakePage("", error=RuntimeError("net::ERR_TIMED_OUT"))
    browser = _build_extraction_browser(
        _settings_with(test_settings, state_path=str(state)), page
    )

    with pytest.raises(ExternalServiceError) as excinfo:
        await browser.extract_payment_instruction("ORD-1")

    assert excinfo.value.service == "binance_browser"
    assert excinfo.value.code == "binance_navigation_failed"
    assert page.closed is True


# ---------------------------------------------------------------------------
# Close (notify_paid) through a fake context (no Chromium)
# ---------------------------------------------------------------------------


class _FakeNotifyLocator:
    """Locator double recording click/fill/press/wait_for calls.

    The same object stands in for the close button, the chat input and the
    confirmation text, which is enough to assert ordering and script the
    unconfirmed-DOM failure without a browser.
    """

    def __init__(self, *, confirm_error: Exception | None = None) -> None:
        self.confirm_error = confirm_error
        self.clicks: list[dict[str, Any]] = []
        self.filled: list[str] = []
        self.pressed: list[str] = []
        self.waits: list[dict[str, Any]] = []

    @property
    def first(self) -> "_FakeNotifyLocator":
        """Return this locator, mirroring Playwright's ``.first``."""
        return self

    async def click(self, **kwargs: Any) -> None:
        """Record a click."""
        self.clicks.append(kwargs)

    async def fill(self, value: str, **kwargs: Any) -> None:
        """Record a typed value."""
        self.filled.append(value)

    async def press(self, key: str, **kwargs: Any) -> None:
        """Record a key press."""
        self.pressed.append(key)

    async def wait_for(self, **kwargs: Any) -> None:
        """Record the wait, or raise the scripted confirmation failure."""
        self.waits.append(kwargs)
        if self.confirm_error is not None:
            raise self.confirm_error


class _FakeNotifyPage:
    """Playwright page double for the close interaction."""

    def __init__(self, *, confirm_error: Exception | None = None) -> None:
        self.locator_obj = _FakeNotifyLocator(confirm_error=confirm_error)
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []
        self.role_calls: list[tuple[str, str | None]] = []
        self.text_calls: list[tuple[str, bool]] = []
        self.locator_selectors: list[str] = []
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> None:
        """Record navigation."""
        self.goto_calls.append((url, kwargs))

    def get_by_role(self, role: str, *, name: str | None = None) -> _FakeNotifyLocator:
        """Record and return the locator for a role query."""
        self.role_calls.append((role, name))
        return self.locator_obj

    def locator(self, selector: str) -> _FakeNotifyLocator:
        """Record and return the locator for a CSS selector."""
        self.locator_selectors.append(selector)
        return self.locator_obj

    def get_by_text(self, text: str, *, exact: bool = False) -> _FakeNotifyLocator:
        """Record and return the locator for a text query."""
        self.text_calls.append((text, exact))
        return self.locator_obj

    async def close(self) -> None:
        """Mark the page closed."""
        self.closed = True


def _build_notify_browser(
    settings: Settings, page: _FakeNotifyPage
) -> PlaywrightBinanceBrowser:
    """Build an adapter with a fake context injected for the close path."""
    browser = PlaywrightBinanceBrowser(settings)
    browser._context = _FakeContext(page)  # type: ignore[arg-type]
    return browser


async def test_notify_paid_clicks_posts_reference_and_confirms(
    test_settings: Settings, tmp_path: Path
) -> None:
    """The close clicks notify, posts the reference and waits for the DOM."""
    state = _write_state(tmp_path / "state.json")
    page = _FakeNotifyPage()
    browser = _build_notify_browser(
        _settings_with(test_settings, state_path=str(state)), page
    )

    await browser.notify_paid("ORD-NOTIFY", "REF-90218473")

    assert page.goto_calls == [
        ("https://p2p.binance.com/en/order/ORD-NOTIFY", {"timeout": 20000})
    ]
    assert page.role_calls == [("button", "Transferido, notificar al vendedor")]
    assert page.locator_selectors == ["textarea"]
    assert page.locator_obj.filled == ["REF-90218473"]
    assert page.locator_obj.pressed == ["Enter"]
    assert page.text_calls == [("REF-90218473", False)]
    assert page.locator_obj.waits[-1]["state"] == "visible"
    assert page.closed is True
    assert "playwright" not in sys.modules


async def test_notify_paid_rejects_an_empty_reference(
    test_settings: Settings, tmp_path: Path
) -> None:
    """A close without a reference is a typed failure before any navigation."""
    state = _write_state(tmp_path / "state.json")
    page = _FakeNotifyPage()
    browser = _build_notify_browser(
        _settings_with(test_settings, state_path=str(state)), page
    )

    with pytest.raises(ExternalServiceResponseError) as excinfo:
        await browser.notify_paid("ORD-NOTIFY", "   ")

    assert excinfo.value.code == "binance_close_unconfirmed"
    assert page.goto_calls == []
    assert page.closed is False


# ---------------------------------------------------------------------------
# Opt-in live smoke (never collected into the default run)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("LIQUIDVE_BROWSER_TESTS"),
    reason=(
        "manual smoke only: set LIQUIDVE_BROWSER_TESTS=1 and a real "
        "BINANCE_SESSION_STATE_PATH to run against a fixture order"
    ),
)
async def test_notify_paid_raises_on_uncertain_dom(
    test_settings: Settings, tmp_path: Path
) -> None:
    """A close whose resulting DOM never confirms must raise, never succeed.

    Marked ``integration`` and gated on ``LIQUIDVE_BROWSER_TESTS`` because a
    real close posts a chat message.  When enabled it models the uncertain DOM
    deterministically so it never touches a live order.
    """
    state = _write_state(tmp_path / "state.json")
    page = _FakeNotifyPage(confirm_error=TimeoutError("reference never appeared"))
    browser = _build_notify_browser(
        _settings_with(test_settings, state_path=str(state)), page
    )

    with pytest.raises(ExternalServiceError) as excinfo:
        await browser.notify_paid("ORD-NOTIFY", "REF-1")

    assert excinfo.value.service == "binance_browser"
    assert excinfo.value.code == "binance_close_unconfirmed"
    assert page.closed is True
    assert "playwright" not in sys.modules


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("LIQUIDVE_BROWSER_TESTS"),
    reason=(
        "manual smoke only: set LIQUIDVE_BROWSER_TESTS=1 and a real "
        "BINANCE_SESSION_STATE_PATH to run against a fixture order"
    ),
)
async def test_live_dom_extraction_smoke(test_settings: Settings) -> None:
    """Scrape a real Binance order page with a real browser session.

    Never run against a live money order: only a fixture/sandbox order number.
    """
    order_number = os.getenv("LIQUIDVE_BROWSER_ORDER_NUMBER")
    if not order_number:
        pytest.skip("set LIQUIDVE_BROWSER_ORDER_NUMBER to a fixture order number")

    browser = PlaywrightBinanceBrowser(test_settings)
    try:
        instruction = await browser.extract_payment_instruction(order_number)
    finally:
        await browser.aclose()

    assert instruction.order_number == order_number
    assert instruction.amount_ves > 0

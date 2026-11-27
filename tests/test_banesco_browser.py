"""Tests for the Banesco Online browser driver (S3 / PR 4).

Two hard rules drive this file:

* importing ``banesco_browser`` or ``banesco_pages`` never imports Playwright,
  asserted in-process and in a subprocess;
* the default run never launches a browser.  The driver's real Playwright import
  is replaced by a spied loader and a scripted ``PageLike`` fake, so login,
  security questions, clave, modal and Pago Móvil navigation are all exercised
  without Chromium, a network socket or a real credential.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.infrastructure.clients.banesco_browser as banesco_browser_module
from app.core.config import Settings
from app.core.exceptions import (
    ConfigurationError,
    BanescoAdModalBlocked,
    BanescoCaptchaDetected,
    BanescoLoginFailed,
    BanescoPaymentOutcomeUnknown,
)
from app.domain.banesco import BanescoPaymentTarget
from app.infrastructure.clients.banesco_browser import BanescoBrowser
from app.infrastructure.clients.banesco_pages import (
    MENU_SELECTORS,
    SCREEN_FIXTURES,
    Screen,
    detect_screen,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "banesco"
_REPO_ROOT = Path(__file__).resolve().parents[1]

_MENU_MARKUP = (
    '<html><body><div id="menu">'
    '<a id="ctl00_btnBPM" href="Aplicacion.aspx?opc=24">Pago Movil</a>'
    "</div></body></html>"
)
_SUBMENU_MARKUP = (
    '<html><body><div id="menu">'
    '<a id="ctl00_btnBPM" href="Aplicacion.aspx?opc=24">Pago Movil</a>'
    '<a href="#">Enviar pago</a>'
    "</div></body></html>"
)


def _read_fixture(name: str) -> str:
    """Read one sanitized Banesco fixture as UTF-8 markup.

    Args:
        name: Fixture file name under ``tests/fixtures/banesco``.

    Returns:
        The fixture markup.
    """
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _selector_present(markup: str, selector: str) -> bool:
    """Return whether the fake can resolve a page-object selector.

    Args:
        markup: Markup currently served by the fake page.
        selector: Selector requested by a page object.

    Returns:
        ``True`` when any comma-separated alternative is present.
    """
    for part in (piece.strip() for piece in selector.split(",")):
        if not part:
            continue
        if part.startswith("#"):
            if re.search(rf"""\bid\s*=\s*["']{re.escape(part[1:])}["']""", markup, re.I):
                return True
        elif part.startswith("text="):
            if part[len("text=") :].strip().lower() in markup.lower():
                return True
        else:  # pragma: no cover - guards the fake against new selectors
            raise AssertionError(f"fake page cannot resolve selector: {selector}")
    return False


class _ScriptedPage:
    """``PageLike`` double that advances screens on known clicks.

    Attributes:
        markup: Markup currently served.
        url: Last URL passed to :meth:`goto`.
        actions: Ordered tuples describing every recorded action.
        transitions: ``(screen, selector) -> markup`` transitions.
    """

    def __init__(self, markup: str = "") -> None:
        """Initialize the fake with optional markup.

        Args:
            markup: Markup returned by :meth:`content`.
        """
        self.markup = markup
        self.url = ""
        self.actions: list[tuple[Any, ...]] = []
        self.transitions: dict[tuple[Screen, str], str] = {}

    async def goto(self, url: str) -> None:
        """Record a navigation.

        Args:
            url: Destination URL.
        """
        self.url = url
        self.actions.append(("goto", url))

    async def content(self) -> str:
        """Return the served markup.

        Returns:
            The current markup.
        """
        return self.markup

    async def fill(self, selector: str, value: str) -> None:
        """Record a fill.

        Args:
            selector: Field selector.
            value: Value typed into the field.
        """
        self.actions.append(("fill", selector, value))

    async def click(self, selector: str) -> None:
        """Record a click and advance to the scripted next screen.

        Args:
            selector: Element selector.
        """
        screen = detect_screen(self.markup)
        self.actions.append(("click", selector))
        next_markup = self.transitions.get((screen, selector))
        if next_markup is not None:
            self.markup = next_markup

    async def select_option(self, selector: str, value: str) -> None:
        """Record a dropdown selection.

        Args:
            selector: Select selector.
            value: Option value.
        """
        self.actions.append(("select_option", selector, value))

    async def wait_for_load_state(self, state: str = "domcontentloaded") -> None:
        """Record a load-state wait.

        Args:
            state: Load state requested.
        """
        self.actions.append(("wait_for_load_state", state))

    async def wait_for_selector(
        self, selector: str, *, timeout: int | None = None
    ) -> object:
        """Record a selector wait and fail when absent.

        Args:
            selector: Selector requested.
            timeout: Optional timeout in milliseconds.

        Returns:
            An opaque sentinel.

        Raises:
            LookupError: When the selector is not in the served markup.
        """
        if not _selector_present(self.markup, selector):
            raise LookupError(f"selector not found: {selector}")
        return object()


class _FakeContext:
    """Browser-context double recording page creation and close."""

    def __init__(self, page: _ScriptedPage) -> None:
        """Store the page to serve.

        Args:
            page: Scripted page returned by :meth:`new_page`.
        """
        self.page = page
        self.default_timeout_ms: int | None = None
        self.closed = False

    async def new_page(self) -> _ScriptedPage:
        """Return the scripted page.

        Returns:
            The served page.
        """
        return self.page

    def set_default_timeout(self, timeout_ms: int) -> None:
        """Record the default timeout.

        Args:
            timeout_ms: Timeout in milliseconds.
        """
        self.default_timeout_ms = timeout_ms

    async def close(self) -> None:
        """Mark the context closed."""
        self.closed = True


class _FakeBrowserHandle:
    """Launched-browser double recording context creation and close."""

    def __init__(self, context: _FakeContext) -> None:
        """Store the context to serve."""
        self.context = context
        self.new_context_kwargs: dict[str, Any] | None = None
        self.closed = False

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        """Record context options and return the context."""
        self.new_context_kwargs = kwargs
        return self.context

    async def close(self) -> None:
        """Mark the browser closed."""
        self.closed = True


class _FakeChromium:
    """Chromium launcher double recording launch options."""

    def __init__(self, context: _FakeContext, *, error: Exception | None = None) -> None:
        """Store the context to launch and an optional launch failure."""
        self.context = context
        self.error = error
        self.launch_kwargs: dict[str, Any] | None = None
        self.handle: _FakeBrowserHandle | None = None

    async def launch(self, **kwargs: Any) -> _FakeBrowserHandle:
        """Record launch options and return the browser handle."""
        self.launch_kwargs = kwargs
        if self.error is not None:
            raise self.error
        self.handle = _FakeBrowserHandle(self.context)
        return self.handle


class _FakePlaywrightDriver:
    """Playwright driver double exposing ``chromium`` and ``stop``."""

    def __init__(self, chromium: _FakeChromium) -> None:
        """Store the chromium launcher."""
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        """Mark the driver stopped."""
        self.stopped = True


def _install_loader(
    monkeypatch: pytest.MonkeyPatch, driver: _FakePlaywrightDriver
) -> list[str]:
    """Replace the driver's lazy Playwright import with a spied fake.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        driver: Fake driver returned by ``async_playwright().start()``.

    Returns:
        A mutable list recording every call to the loader.
    """
    calls: list[str] = []

    class _FakeAsyncPlaywright:
        async def start(self) -> _FakePlaywrightDriver:
            return driver

    def _fake_loader() -> type[_FakeAsyncPlaywright]:
        calls.append("loaded")
        return _FakeAsyncPlaywright

    monkeypatch.setattr(
        banesco_browser_module, "_import_async_playwright", _fake_loader
    )
    return calls


def _build_driver() -> tuple[_ScriptedPage, _FakeContext, _FakeChromium, _FakePlaywrightDriver]:
    """Build a scripted login flow ending on the Pago Móvil form.

    Returns:
        The scripted page and its fake browser/context/driver.
    """
    page = _ScriptedPage(_read_fixture(SCREEN_FIXTURES[Screen.LOGIN_USER]))
    page.transitions[(Screen.LOGIN_USER, "#bAceptar")] = _read_fixture(
        SCREEN_FIXTURES[Screen.SECURITY_QUESTIONS]
    )
    page.transitions[(Screen.SECURITY_QUESTIONS, "#bAceptar")] = _read_fixture(
        SCREEN_FIXTURES[Screen.LOGIN_PASSWORD]
    )
    page.transitions[(Screen.LOGIN_PASSWORD, "#bAceptar")] = _read_fixture(
        SCREEN_FIXTURES[Screen.DASHBOARD_MODAL]
    )
    page.transitions[(Screen.DASHBOARD_MODAL, "#closeButton")] = _MENU_MARKUP
    page.transitions[(Screen.UNKNOWN, MENU_SELECTORS["pago_movil"])] = _SUBMENU_MARKUP
    page.transitions[(Screen.UNKNOWN, MENU_SELECTORS["enviar_pago"])] = _read_fixture(
        SCREEN_FIXTURES[Screen.PAGOMOVIL_FORM]
    )
    context = _FakeContext(page)
    chromium = _FakeChromium(context)
    driver = _FakePlaywrightDriver(chromium)
    return page, context, chromium, driver


async def test_banesco_browser_lazy_imports_playwright_only_in_aenter(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver validates settings first and imports Playwright only on entry."""
    missing_settings = test_settings.model_copy(
        update={"banesco_online_password": None}
    )
    missing_browser = BanescoBrowser(missing_settings)
    with pytest.raises(ConfigurationError) as excinfo:
        await missing_browser.__aenter__()

    assert "BANESCO_ONLINE_PASSWORD" in excinfo.value.details["missing"]
    assert "playwright" not in sys.modules

    page, context, chromium, driver = _build_driver()
    calls = _install_loader(monkeypatch, driver)
    browser = BanescoBrowser(test_settings)

    assert calls == []
    assert "playwright" not in sys.modules

    async with browser as entered:
        assert entered is browser
        assert calls == ["loaded"]
        assert browser._context is context
        assert "playwright" not in sys.modules

    assert browser._context is None
    assert context.closed is True
    assert context.default_timeout_ms == test_settings.banesco_browser_timeout_ms
    assert chromium.launch_kwargs == {
        "headless": test_settings.banesco_browser_headless
    }
    assert chromium.handle is not None
    assert chromium.handle.closed is True
    assert driver.stopped is True
    assert page.markup == _read_fixture(SCREEN_FIXTURES[Screen.LOGIN_USER])


async def test_login_and_open_payment_form_stops_at_payment_form(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single public coroutine logs in and stops on the payment form."""
    page, _context, _chromium, driver = _build_driver()
    _install_loader(monkeypatch, driver)

    async with BanescoBrowser(test_settings) as browser:
        await browser.login_and_open_payment_form()

    user = test_settings.banesco_online_user
    password = test_settings.banesco_online_password
    assert user is not None and password is not None
    assert page.url == test_settings.banesco_online_login_url
    assert ("fill", "#txtUsuario", user.get_secret_value()) in page.actions
    assert (
        "fill",
        "#txtPrimeraR",
        test_settings.banesco_security_answer_hobbie.get_secret_value(),
    ) in page.actions
    assert (
        "fill",
        "#txtSegundaR",
        test_settings.banesco_security_answer_country_to_visit.get_secret_value(),
    ) in page.actions
    assert ("fill", "#txtClave", password.get_secret_value()) in page.actions
    assert ("click", "#closeButton") in page.actions
    assert ("click", MENU_SELECTORS["pago_movil"]) in page.actions
    assert ("click", MENU_SELECTORS["enviar_pago"]) in page.actions
    assert detect_screen(page.markup) is Screen.PAGOMOVIL_FORM
    assert not [
        action
        for action in page.actions
        if action[0] == "click" and action[1] == "#enviar"
    ]
    payment_controls = {
        "#pref",
        "#tel",
        "#NacCli",
        "#ced",
        "#banco",
        "#monto",
        "#concepto",
        "#nom",
    }
    assert not [
        action
        for action in page.actions
        if action[0] == "fill" and action[1] in payment_controls
    ]


async def test_login_and_open_payment_form_fails_typed_on_error_page(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A captcha after the user submit aborts the composition, typed."""
    page = _ScriptedPage(_read_fixture(SCREEN_FIXTURES[Screen.LOGIN_USER]))
    page.transitions[(Screen.LOGIN_USER, "#bAceptar")] = (
        "<html><body><form>"
        '<span id="lblMensaje">Complete el captcha</span>'
        '<input id="bAceptar" type="submit">'
        "</form></body></html>"
    )
    context = _FakeContext(page)
    chromium = _FakeChromium(context)
    driver = _FakePlaywrightDriver(chromium)
    _install_loader(monkeypatch, driver)

    async with BanescoBrowser(test_settings) as browser:
        with pytest.raises(BanescoCaptchaDetected):
            await browser.login_and_open_payment_form()

    assert ("click", "#bAceptar") in page.actions


async def test_login_and_open_payment_form_fails_when_menu_is_blocked(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undismissable modal aborts before any payment navigation."""
    page = _ScriptedPage(_read_fixture(SCREEN_FIXTURES[Screen.LOGIN_USER]))
    page.transitions[(Screen.LOGIN_USER, "#bAceptar")] = _read_fixture(
        SCREEN_FIXTURES[Screen.SECURITY_QUESTIONS]
    )
    page.transitions[(Screen.SECURITY_QUESTIONS, "#bAceptar")] = _read_fixture(
        SCREEN_FIXTURES[Screen.LOGIN_PASSWORD]
    )
    page.transitions[(Screen.LOGIN_PASSWORD, "#bAceptar")] = (
        '<html><body><div id="modalAnuncios">'
        '<button id="actionButton">Comenzar</button>'
        "</div></body></html>"
    )
    context = _FakeContext(page)
    chromium = _FakeChromium(context)
    driver = _FakePlaywrightDriver(chromium)
    _install_loader(monkeypatch, driver)

    async with BanescoBrowser(test_settings) as browser:
        with pytest.raises(BanescoAdModalBlocked):
            await browser.login_and_open_payment_form()

    assert ("click", MENU_SELECTORS["pago_movil"]) not in page.actions


async def test_rejected_clave_is_not_retried(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected online clave aborts with one submit and no retry."""
    page = _ScriptedPage(_read_fixture(SCREEN_FIXTURES[Screen.LOGIN_USER]))
    page.transitions[(Screen.LOGIN_USER, "#bAceptar")] = _read_fixture(
        SCREEN_FIXTURES[Screen.SECURITY_QUESTIONS]
    )
    page.transitions[(Screen.SECURITY_QUESTIONS, "#bAceptar")] = _read_fixture(
        SCREEN_FIXTURES[Screen.LOGIN_PASSWORD]
    )
    page.transitions[(Screen.LOGIN_PASSWORD, "#bAceptar")] = _read_fixture(
        SCREEN_FIXTURES[Screen.LOGIN_ERROR]
    )
    context = _FakeContext(page)
    chromium = _FakeChromium(context)
    driver = _FakePlaywrightDriver(chromium)
    _install_loader(monkeypatch, driver)

    async with BanescoBrowser(test_settings) as browser:
        with pytest.raises(BanescoLoginFailed):
            await browser.login_and_open_payment_form()

    clave_clicks = [
        action
        for action in page.actions
        if action[0] == "click" and action[1] == "#bAceptar"
    ]
    assert len(clave_clicks) == 3


def _build_payment_driver() -> tuple[
    _ScriptedPage, _FakeContext, _FakeChromium, _FakePlaywrightDriver
]:
    """Extend the login flow through the form, confirmation and clave screens.

    Returns:
        The scripted page and its fake browser/context/driver.
    """
    page, context, chromium, driver = _build_driver()
    page.transitions[(Screen.PAGOMOVIL_FORM, "#enviar")] = _read_fixture(
        SCREEN_FIXTURES[Screen.CONFIRMATION]
    )
    page.transitions[(Screen.CONFIRMATION, "#butonPago")] = _read_fixture(
        SCREEN_FIXTURES[Screen.CLAVE_DINAMICA]
    )
    return page, context, chromium, driver


async def test_fill_and_submit_payment_composes_and_never_touches_otp(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver fills the form, double-submits, detects the clave form only."""
    page, _context, _chromium, driver = _build_payment_driver()
    _install_loader(monkeypatch, driver)
    target = BanescoPaymentTarget(
        phone_prefix="0414",
        phone_rest="1234567",
        id_type="V",
        id_digits="12345678",
        destination_bank_code="0134",
        destination_bank_option="BANESCO BANCO UNIVERSAL S.A.C.A.",
        amount_ves=Decimal("1500.00"),
        concept="Pago",
    )

    async with BanescoBrowser(test_settings) as browser:
        await browser.fill_and_submit_payment(target)

    assert ("click", MENU_SELECTORS["pago_movil"]) in page.actions
    assert ("click", MENU_SELECTORS["enviar_pago"]) in page.actions
    assert ("select_option", "#pref", "0414") in page.actions
    assert ("fill", "#tel", "1234567") in page.actions
    assert ("select_option", "#NacCli", "V") in page.actions
    assert ("fill", "#ced", "12345678") in page.actions
    assert ("select_option", "#banco", "0134") in page.actions
    assert ("fill", "#monto", "1500,00") in page.actions
    assert ("fill", "#concepto", "Pago") in page.actions
    assert not [
        action
        for action in page.actions
        if action[0] == "fill" and action[1] == "#nom"
    ]

    submit_order = [
        action[1]
        for action in page.actions
        if action[0] == "click" and action[1] in ("#enviar", "#butonPago")
    ]
    assert submit_order == ["#enviar", "#butonPago"]
    assert detect_screen(page.markup) is Screen.CLAVE_DINAMICA

    # Detection only: the clave control and its submit are never actioned.
    forbidden = [
        action
        for action in page.actions
        if action[0] in ("fill", "click", "select_option")
        and action[1] in ("#otp", "#acceptar")
    ]
    assert forbidden == []


async def test_authorization_click_is_once_and_never_after_deadline(test_settings: Settings) -> None:
    page = _ScriptedPage(_read_fixture(SCREEN_FIXTURES[Screen.CLAVE_DINAMICA]))
    browser = BanescoBrowser(test_settings)
    browser._page = page

    with pytest.raises(asyncio.TimeoutError):
        await browser.authorize_payment("012345", time.monotonic() - 1)
    assert page.actions == []

    deadline = time.monotonic() + 1
    await browser.authorize_payment("012345", deadline)
    with pytest.raises(BanescoPaymentOutcomeUnknown):
        await browser.authorize_payment("012345", deadline)
    assert [action for action in page.actions if action[0] in {"fill", "click"}] == [
        ("fill", "#otp", "012345"),
        ("click", "#acceptar"),
    ]


def test_banesco_adapter_import_is_browser_free_in_subprocess() -> None:
    """Both modules import in-process and in a subprocess without Playwright."""
    module = importlib.import_module("app.infrastructure.clients.banesco_browser")
    importlib.import_module("app.infrastructure.clients.banesco_pages")
    assert "playwright" not in sys.modules
    assert hasattr(module, "BanescoBrowser")
    assert not hasattr(module.BanescoBrowser, "pay")

    probe = (
        "import sys, app.infrastructure.clients.banesco_pages as p, "
        "app.infrastructure.clients.banesco_browser as b; "
        "assert 'playwright' not in sys.modules, 'playwright imported'; "
        "assert hasattr(b, 'BanescoBrowser'); "
        "assert not hasattr(b.BanescoBrowser, 'pay'); "
        "assert hasattr(p, 'LoginPage'); "
        "print('BANESCO ADAPTER IMPORT OK')"
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "BANESCO ADAPTER IMPORT OK" in completed.stdout

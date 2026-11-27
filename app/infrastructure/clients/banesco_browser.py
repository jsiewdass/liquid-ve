"""Banesco Online browser driver (S3).

The driver owns one browser context per payment attempt and composes the
page objects from :mod:`app.infrastructure.clients.banesco_pages` in order:

    login -> security questions -> clave -> ad modal -> menu -> Pago Móvil

``playwright.async_api`` is imported lazily inside :meth:`BanescoBrowser.__aenter__`
only. Credentials are validated before the browser is launched.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.core.exceptions import (
    BanescoLayoutUnexpected,
    BanescoLoginFailed,
    BanescoPaymentOutcomeUnknown,
    ConfigurationError,
)
from app.domain.banesco import (
    QUESTION_ANSWER_SETTINGS,
    BanescoPaymentTarget,
    QuestionKey,
)
from app.infrastructure.clients.banesco_pages import (
    LOGIN_FRAME_NAME,
    MENU_SELECTORS,
    PAGOMOVIL_FRAME_NAME,
    RECEIPT_HEADING,
    SCREEN_ANCHOR_SELECTORS,
    AdModalPage,
    ClaveDinamicaPage,
    ConfirmationPage,
    LoginPage,
    PagoMovilFormPage,
    PageLike,
    PasswordPage,
    Screen,
    SecurityQuestionsPage,
    detect_login_error,
    detect_screen,
    parse_receipt,
)

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = ["BanescoBrowser"]

@dataclass(frozen=True, slots=True)
class BanescoReceipt:
    reference: str
    amount: Decimal
    image: bytes


_ANSWER_SETTING_NAMES: tuple[str, ...] = tuple(
    QUESTION_ANSWER_SETTINGS[key] for key in QuestionKey
)
_REQUIRED_SECRET_SETTINGS: tuple[str, ...] = (
    "BANESCO_ONLINE_USER",
    "BANESCO_ONLINE_PASSWORD",
    *_ANSWER_SETTING_NAMES,
)

_LOGIN_FRAME_URL_MARKERS: tuple[str, ...] = (
    "/CAU/",
    "LoginDNA",
    "AU_ValDNA",
    "ContrasenaDNA",
)
"""URL fragments that identify the login-flow iframe across navigations."""


def _import_async_playwright() -> Any:
    """Import Playwright's async entry point lazily.

    Returns:
        The ``async_playwright`` callable from ``playwright.async_api``.
    """
    from playwright.async_api import async_playwright

    return async_playwright


def _secret_value(value: Any) -> str | None:
    """Return a non-empty secret value without ever exposing it elsewhere.

    Args:
        value: A ``SecretStr``, plain string or ``None``.

    Returns:
        The plain string value, or ``None`` when it is absent or blank.
    """
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    raw = getter() if callable(getter) else value
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw


def _is_https_url(value: Any) -> bool:
    """Return whether ``value`` is an absolute ``https`` URL.

    Args:
        value: Candidate URL.

    Returns:
        ``True`` for an absolute ``https`` URL.
    """
    return isinstance(value, str) and value.strip().lower().startswith("https://")


def _missing_required_settings(settings: Settings) -> tuple[str, ...]:
    """List the required setting names that are absent or malformed.

    Only names are returned; no value is ever read into the result.

    Args:
        settings: Application settings.

    Returns:
        Missing or malformed setting variable names.
    """
    missing: list[str] = []
    if not _is_https_url(settings.banesco_online_login_url):
        missing.append("BANESCO_ONLINE_LOGIN_URL")
    for name in _REQUIRED_SECRET_SETTINGS:
        if _secret_value(getattr(settings, name.lower(), None)) is None:
            missing.append(name)
    if settings.banesco_browser_timeout_ms <= 0:
        missing.append("BANESCO_BROWSER_TIMEOUT_MS")
    return tuple(missing)


async def _close_quietly(resource: Any | None, method: str = "close") -> None:
    """Await one cleanup method, swallowing failures.

    Cleanup never masks the error that triggered it.

    Args:
        resource: Object exposing an async cleanup method.
        method: Name of the async cleanup method to await.
    """
    if resource is None:
        return
    closer = getattr(resource, method, None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # noqa: BLE001 - cleanup must not raise
        pass


class _FrameView:
    """Adapt a Playwright frame to the narrow :class:`PageLike` protocol.

    The Banesco login controls live inside the ``ctl00_cp_frmaplicacion``
    iframe, so the page objects must receive that frame rather than the
    top-level page.  A frame cannot navigate, so ``goto`` fails closed.
    """

    def __init__(self, frame: Any) -> None:
        """Store the underlying Playwright frame.

        Args:
            frame: Playwright ``Frame`` object.
        """
        self._frame = frame

    @property
    def url(self) -> str:
        """Return the frame URL."""
        return str(getattr(self._frame, "url", ""))

    async def goto(self, url: str) -> None:
        """Reject navigation; only the top-level page may navigate.

        Args:
            url: Ignored.

        Raises:
            BanescoLayoutUnexpected: Always, because a frame cannot navigate.
        """
        _ = url
        raise BanescoLayoutUnexpected(
            "The login frame cannot navigate", details={"stage": "login_frame"}
        )

    async def content(self) -> str:
        """Return the frame markup."""
        return await self._frame.content()

    async def fill(self, selector: str, value: str) -> None:
        """Fill one control inside the frame."""
        await self._frame.fill(selector, value)

    async def click(self, selector: str) -> None:
        """Click one element inside the frame."""
        await self._frame.click(selector)

    async def select_option(self, selector: str, value: str) -> None:
        """Select one option inside the frame."""
        await self._frame.select_option(selector, value)

    async def wait_for_load_state(self, state: str = "domcontentloaded") -> None:
        """Wait for a load state inside the frame."""
        await self._frame.wait_for_load_state(state)

    async def wait_for_selector(
        self, selector: str, *, timeout: int | None = None
    ) -> object:
        """Wait for one selector inside the frame."""
        return await self._frame.wait_for_selector(selector, timeout=timeout)


class BanescoBrowser:
    """Lazy Playwright driver for the Banesco Online login prefix.

    Attributes:
        _settings: Settings supplying the login URL, credentials, answers,
            headless flag and navigation timeout.
    """

    def __init__(self, settings: Settings) -> None:
        """Store settings without importing or launching Playwright.

        Args:
            settings: Application settings for the Banesco rail.
        """
        self._settings = settings
        self._authorization_started = False
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: PageLike | None = None

    async def __aenter__(self) -> "BanescoBrowser":
        """Validate settings, then lazily import and start Chromium.

        Returns:
            This driver, ready for interaction.  A second entry after a
            successful start is a no-op.

        Raises:
            ConfigurationError: When a required setting is absent or malformed;
                details name variables only.
            BanescoLoginFailed: When the browser session cannot be started.
        """
        if self._context is not None:
            return self
        missing = _missing_required_settings(self._settings)
        if missing:
            raise ConfigurationError(
                "Banesco Online settings are incomplete",
                code="banesco_configuration_incomplete",
                details={"missing": list(missing)},
            )

        async_playwright = _import_async_playwright()

        started: Any | None = None
        launched: Any | None = None
        context: Any | None = None
        page: Any | None = None
        try:
            started = await async_playwright().start()
            launched = await started.chromium.launch(
                headless=self._settings.banesco_browser_headless
            )
            context = await launched.new_context()
            context.set_default_timeout(self._settings.banesco_browser_timeout_ms)
            page = await context.new_page()
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await _close_quietly(page)
            await _close_quietly(context)
            await _close_quietly(launched)
            await _close_quietly(started, "stop")
            raise BanescoLoginFailed(
                "Banesco Online browser session could not start",
                details={"stage": "start"},
            ) from exc

        self._playwright = started
        self._browser = launched
        self._context = context
        self._page = page
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Release every resource held by the driver.

        Args:
            exc_type: Exception class raised inside the context, if any.
            exc: Exception instance raised inside the context, if any.
            traceback: Traceback of the exception, if any.
        """
        await self.aclose()

    async def aclose(self) -> None:
        """Close the page, context, browser and driver, ignoring failures."""
        page, context, browser, playwright = (
            self._page,
            self._context,
            self._browser,
            self._playwright,
        )
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        await _close_quietly(page)
        await _close_quietly(context)
        await _close_quietly(browser)
        await _close_quietly(playwright, "stop")

    def _answers(self) -> dict[QuestionKey, str | None]:
        """Build the answer mapping from settings without echoing values.

        Returns:
            Configured answer by question key; missing values stay ``None`` so
            the pure validator can fail closed.
        """
        return {
            key: _secret_value(
                getattr(
                    self._settings,
                    QUESTION_ANSWER_SETTINGS[key].lower(),
                    None,
                )
            )
            for key in QuestionKey
        }

    @property
    def page(self) -> PageLike | None:
        """Return the current top-level page, when a session is open.

        Returns:
            The page-like object, or ``None`` before the session starts.
        """
        return self._page

    async def _login_view(self) -> PageLike:
        """Return the page-like object that holds the login controls.

        The real portal renders the login inside the ``ctl00_cp_frmaplicacion``
        iframe.  Fakes and single-document pages expose no ``frame`` resolver,
        in which case the page itself is used.

        Returns:
            A :class:`PageLike` bound to the login frame or to the page.

        Raises:
            BanescoLayoutUnexpected: When the login frame never appears.
        """
        page = self._page
        if page is None:  # pragma: no cover - callers guarantee a page
            raise BanescoLoginFailed(
                "Banesco Online browser session is unavailable",
                details={"stage": "start"},
            )
        resolver = getattr(page, "frame", None)
        if not callable(resolver):
            return page

        timeout_ms = self._settings.banesco_browser_timeout_ms
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            frame = resolver(name=LOGIN_FRAME_NAME)
            if frame is not None:
                return _FrameView(frame)
            frames = getattr(page, "frames", None)
            if isinstance(frames, list):
                for candidate in frames:
                    url = str(getattr(candidate, "url", ""))
                    if any(marker in url for marker in _LOGIN_FRAME_URL_MARKERS):
                        return _FrameView(candidate)
            await asyncio.sleep(0.1)

        markup = await page.content()
        if "txtUsuario" in markup or "txtClave" in markup:
            return page
        raise BanescoLayoutUnexpected(
            "The login frame did not appear", details={"stage": "login_frame"}
        )

    async def _await_login_screen(self, screen: Screen) -> PageLike:
        """Re-resolve the login frame and wait for the expected screen.

        The iframe replaces its document on every submission, so the frame must
        be resolved again after each navigation instead of reusing a previous
        reference.

        Args:
            screen: Screen expected after the previous submission.

        Returns:
            A page-like view bound to the freshly resolved frame.

        Raises:
            BanescoLayoutUnexpected: When the expected anchor never appears.
        """
        view = await self._login_view()
        anchor = SCREEN_ANCHOR_SELECTORS.get(screen)
        if anchor is None:  # pragma: no cover - every login screen has an anchor
            return view
        timeout = self._settings.banesco_browser_timeout_ms
        try:
            await view.wait_for_selector(anchor, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            markup = ""
            try:
                markup = await view.content()
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            typed = detect_login_error(markup, stage=screen.value)
            if typed is not None:
                raise typed from exc
            raise BanescoLayoutUnexpected(
                f"Expected {screen.value} after navigation",
                details={
                    "stage": screen.value,
                    "expected": screen.value,
                    "actual": detect_screen(markup).value,
                },
            ) from exc
        return view

    async def _payment_view(self) -> PageLike:
        """Return the page-like object that holds the Pago Móvil controls.

        The Pago Móvil form, confirmation and Clave Dinámica live inside the
        ``embedded`` iframe.  Fakes and single-document pages expose no
        ``frame`` resolver, in which case the page itself is used.

        Returns:
            A :class:`PageLike` bound to the Pago Móvil frame or to the page.

        Raises:
            BanescoLoginFailed: When no page is available.
            BanescoLayoutUnexpected: When the Pago Móvil frame never appears.
        """
        page = self._page
        if page is None:  # pragma: no cover - login() guarantees a page
            raise BanescoLoginFailed(
                "Banesco Online browser session is unavailable",
                details={"stage": "start"},
            )
        resolver = getattr(page, "frame", None)
        if not callable(resolver):
            return page
        timeout_ms = self._settings.banesco_browser_timeout_ms
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            frame = resolver(name=PAGOMOVIL_FRAME_NAME)
            if frame is not None:
                return _FrameView(frame)
            await asyncio.sleep(0.1)
        raise BanescoLayoutUnexpected(
            "The Pago Movil frame did not appear", details={"stage": "form"}
        )

    async def login(self) -> None:
        """Log in and dismiss the advertisement modal.

        Returns once the authenticated dashboard is visible.  It stops before
        any product navigation and never touches a payment control.

        Raises:
            ConfigurationError: When a required setting is missing.
            PaymentRailError: Any typed fail-closed login or modal failure.
        """
        if self._context is None or self._page is None:
            await self.__aenter__()
        page = self._page
        if page is None:  # pragma: no cover - __aenter__ guarantees or raises
            raise BanescoLoginFailed(
                "Banesco Online browser session is unavailable",
                details={"stage": "start"},
            )

        settings = self._settings
        timeout = settings.banesco_browser_timeout_ms
        user = _secret_value(settings.banesco_online_user)
        clave = _secret_value(settings.banesco_online_password)
        if user is None or clave is None:  # pragma: no cover - validated earlier
            raise ConfigurationError(
                "Banesco Online settings are incomplete",
                code="banesco_configuration_incomplete",
                details={"missing": list(_REQUIRED_SECRET_SETTINGS)},
            )

        login_url = settings.banesco_online_login_url
        await LoginPage(page, login_url=login_url, timeout_ms=timeout).open()

        user_page = await self._login_view()
        await LoginPage(
            user_page, login_url=login_url, timeout_ms=timeout
        ).submit_user(user)

        questions_page = await self._await_login_screen(Screen.SECURITY_QUESTIONS)
        questions = SecurityQuestionsPage(questions_page, timeout_ms=timeout)
        await questions.answer(self._answers())
        await questions.submit()

        clave_page = await self._await_login_screen(Screen.LOGIN_PASSWORD)
        await PasswordPage(
            clave_page, timeout_ms=timeout, outcome_page=page
        ).submit_clave(clave)

        modal = AdModalPage(page, timeout_ms=timeout)
        await modal.dismiss_if_present()
        await modal.assert_menu_reachable()

    async def login_and_open_payment_form(self) -> None:
        """Log in and stop once the Pago Móvil payment form is visible.

        The composition is: goto login, user, security questions, clave, ad
        modal, menu, ``Pago Móvil``, ``Enviar pago``.  It never fills a payment
        control, never clicks a payment button and never retries a step.

        Raises:
            ConfigurationError: When a required setting is missing.
            PaymentRailError: Any typed fail-closed login, modal or navigation
                failure.
        """
        await self.login()
        page = self._page
        if page is None:  # pragma: no cover - login() guarantees a page
            raise BanescoLoginFailed(
                "Banesco Online browser session is unavailable",
                details={"stage": "start"},
            )
        await self._open_pago_movil()

    async def _open_pago_movil(self) -> PageLike:
        """Open the Pago Móvil product and return its frame view.

        The dashboard exposes a direct link (``#ctl00_btnBPM``) to the product.
        The product renders inside the ``embedded`` iframe; when that app shows
        an ``Enviar pago`` entry it is clicked, otherwise the form is assumed to
        be already displayed.

        Returns:
            A page-like view bound to the Pago Móvil frame.

        Raises:
            BanescoLoginFailed: When no page is available.
        """
        page = self._page
        if page is None:  # pragma: no cover - callers ensure a page
            raise BanescoLoginFailed(
                "Banesco Online browser session is unavailable",
                details={"stage": "start"},
            )
        await page.click(MENU_SELECTORS["pago_movil"])
        await page.wait_for_load_state("domcontentloaded")

        view = await self._payment_view()
        try:
            await view.wait_for_selector("text=Enviar pago", timeout=2000)
        except Exception:  # noqa: BLE001 - the form may be shown directly
            return view
        await view.click("text=Enviar pago")
        await view.wait_for_load_state("domcontentloaded")
        return await self._payment_view()

    async def fill_and_submit_payment(self, target: BanescoPaymentTarget) -> None:
        """Prepare the payment and leave the bank session at the authorization form."""
        await self.login()
        page = self._page
        if page is None:  # pragma: no cover - login() guarantees a page
            raise BanescoLoginFailed(
                "Banesco Online browser session is unavailable",
                details={"stage": "start"},
            )
        timeout = self._settings.banesco_browser_timeout_ms

        form = PagoMovilFormPage(
            await self._open_pago_movil(), timeout_ms=timeout
        )
        await form.fill(target)
        await form.submit_first()

        confirmation = ConfirmationPage(
            await self._payment_view(), timeout_ms=timeout
        )
        await confirmation.assert_confirmation()
        await confirmation.verify_target(target)
        await confirmation.submit_second()

        await ClaveDinamicaPage(
            await self._payment_view(), timeout_ms=timeout
        ).assert_present()

    async def authorize_payment(self, code: str, deadline: float) -> None:
        """Submit once within the browser operation timeout."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        if self._authorization_started:
            raise BanescoPaymentOutcomeUnknown()
        self._authorization_started = True

        async def submit() -> None:
            view = await self._payment_view()
            await ClaveDinamicaPage(view, timeout_ms=max(1, int(remaining * 1000))).authorize(code, deadline)

        await asyncio.wait_for(submit(), timeout=remaining)

    async def read_receipt(self) -> BanescoReceipt:
        """Read and capture a positive receipt from the current session."""
        deadline = time.monotonic() + self._settings.banesco_browser_timeout_ms / 1000
        while time.monotonic() < deadline:
            view = await self._payment_view()
            try:
                await view.wait_for_selector(f"text={RECEIPT_HEADING}", timeout=500)
                reference, amount = parse_receipt(await view.content())
            except Exception:
                await asyncio.sleep(0.1)
                continue
            if self._page is None:
                break
            image = await self._page.screenshot()
            after_reference, after_amount = parse_receipt(await view.content())
            if (reference, amount) != (after_reference, after_amount):
                break
            return BanescoReceipt(reference=reference, amount=amount, image=image)
        raise BanescoPaymentOutcomeUnknown()

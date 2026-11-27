"""Binance P2P browser adapter: pure HTML parser plus Playwright adapter.

The module is split into two strictly separated parts:

* **Part 1 — the pure parser.** Only the standard library (``re``, ``html``,
  ``unicodedata``) and the pure bank domain helpers are imported.  The parser
  returns ``None`` for any missing or unparseable field; it never returns a
  partial or guessed instruction.
* **Part 2 — the Playwright adapter.** ``playwright.async_api`` is imported
  lazily inside :meth:`PlaywrightBinanceBrowser.__aenter__` only, so importing
  this module never requires the wheel or a Chromium binary and the default
  test run stays browser-free.  The session-state file is validated *before*
  that lazy import, so a missing or expired session fails with a typed
  ``binance_session_expired`` error even in a browser-free process.
"""

from __future__ import annotations

import html as html_module
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.exceptions import (
    BankAmountParseError,
    ExternalServiceError,
    ExternalServiceResponseError,
)
from app.domain.banks import parse_ves_amount
from app.domain.ports import PaymentInstruction

if TYPE_CHECKING:
    from app.core.config import Settings

__all__ = [
    "parse_payment_instructions",
    "validate_session_state",
    "PlaywrightBinanceBrowser",
]

_BROWSER_SERVICE = "binance_browser"

# Provisional UI handles (design D5): the exact DOM is unverified, so every
# failure to reach or confirm the resulting state raises instead of succeeding
# silently.  The reference bubble is the confirmation anchor: the close is not
# reported until the text becomes visible in the order chat.
_CLOSE_BUTTON_NAME = "Transferido, notificar al vendedor"
_CHAT_INPUT_SELECTOR = "textarea"
_CHAT_SEND_KEY = "Enter"

_VES_AMOUNT_RE = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$|^\d+,\d{2}$")
_LABEL_VALUE_RE = re.compile(
    r"<dt\b[^>]*>(?P<label>.*?)</dt>\s*<dd\b[^>]*>(?P<value>.*?)</dd>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_BANK_CODE_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")

_FIELD_ALIASES: Mapping[str, tuple[str, ...]] = {
    "bank": ("banco", "bank", "banco de destino", "destination bank"),
    "phone": (
        "telefono",
        "celular",
        "phone",
        "numero de telefono",
        "phone number",
    ),
    "id_number": (
        "cedula",
        "documento",
        "ci",
        "numero de documento",
        "identity number",
        "id number",
    ),
    "amount": ("monto a pagar", "monto", "amount", "total"),
}


def _clean_text(raw: str) -> str:
    """Strip markup and HTML entities from one label or value cell.

    Args:
        raw: Inner HTML of a ``<dt>`` or ``<dd>`` element.

    Returns:
        Entity-decoded, tag-free text with collapsed whitespace.
    """
    without_tags = _TAG_RE.sub(" ", raw)
    unescaped = html_module.unescape(without_tags)
    return _WHITESPACE_RE.sub(" ", unescaped).strip()


def _normalize_label(label: str) -> str:
    """Normalize a label for alias matching.

    Args:
        label: Cleaned label text such as ``"Cédula:"``.

    Returns:
        Accent-free, case-folded text without trailing punctuation.
    """
    decomposed = unicodedata.normalize("NFKD", label)
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    collapsed = _WHITESPACE_RE.sub(" ", without_accents.casefold()).strip()
    return collapsed.strip(" :*")


def _extract_label_fields(html: str) -> Mapping[str, str]:
    """Collect label/value pairs from an order-view snapshot.

    Only ``<dt>``/``<dd>`` pairs are considered, which matches the definition
    list used by the provider page.  A later pair wins on a duplicate label.

    Args:
        html: Raw order-view HTML.

    Returns:
        A mapping from normalized label to cleaned value.
    """
    fields: dict[str, str] = {}
    for match in _LABEL_VALUE_RE.finditer(html):
        label = _normalize_label(_clean_text(match.group("label")))
        value = _clean_text(match.group("value"))
        if label and value:
            fields[label] = value
    return fields


def _field_value(fields: Mapping[str, str], field_name: str) -> str | None:
    """Return the first alias with a value for one canonical field.

    Args:
        fields: Normalized label/value mapping.
        field_name: Canonical field key from :data:`_FIELD_ALIASES`.

    Returns:
        The matching value, or ``None`` when no alias is present.
    """
    for alias in _FIELD_ALIASES[field_name]:
        value = fields.get(alias)
        if value:
            return value
    return None


def _parse_amount_ves(text: str | None) -> Decimal | None:
    """Parse an exact Venezuelan amount, refusing mangled input.

    Args:
        text: Cleaned amount text such as ``"1.500,00"``.

    Returns:
        The parsed positive ``Decimal``, or ``None`` when the text does not
        match the Venezuelan amount format.
    """
    if not text:
        return None
    normalized = _WHITESPACE_RE.sub("", text)
    if _VES_AMOUNT_RE.fullmatch(normalized) is None:
        return None
    try:
        return parse_ves_amount(normalized)
    except BankAmountParseError:
        return None


def parse_payment_instructions(
    html: str, *, order_number: str
) -> PaymentInstruction | None:
    """Extract a complete payment instruction from an order-view snapshot.

    Args:
        html: Raw Binance P2P order-view HTML.
        order_number: Authoritative provider order number supplied by the
            caller.

    Returns:
        A frozen :class:`~app.domain.ports.PaymentInstruction` when every
        required field (bank code, phone, cedula and exact amount) is present
        and parseable, otherwise ``None``.
    """
    if not isinstance(html, str) or not html.strip():
        return None
    if not isinstance(order_number, str) or not order_number.strip():
        return None

    fields = _extract_label_fields(html)
    bank_value = _field_value(fields, "bank")
    phone = _field_value(fields, "phone")
    id_number = _field_value(fields, "id_number")
    amount_ves = _parse_amount_ves(_field_value(fields, "amount"))
    if not bank_value or not phone or not id_number or amount_ves is None:
        return None

    bank_code_match = _BANK_CODE_RE.search(bank_value)
    if bank_code_match is None:
        return None

    return PaymentInstruction(
        order_number=order_number.strip(),
        bank_numeric_code=bank_code_match.group(1),
        phone=phone,
        id_number=id_number,
        amount_ves=amount_ves,
        raw_fields=dict(fields),
    )


def _session_expired(message: str) -> ExternalServiceError:
    """Build the typed error used for every unusable session state.

    Args:
        message: Human readable description without secrets or PII.

    Returns:
        An :class:`~app.core.exceptions.ExternalServiceError` carrying
        ``service="binance_browser"`` and ``code="binance_session_expired"``.
    """
    return ExternalServiceError(
        message, service=_BROWSER_SERVICE, code="binance_session_expired"
    )


def _all_cookies_expired(cookies: list[Any], reference: datetime) -> bool:
    """Decide whether every stored cookie is past its expiry instant.

    Playwright exports session cookies with ``expires == -1``; those never
    expire on their own and therefore count as live.

    Args:
        cookies: Cookie entries from a Playwright storage-state document.
        reference: Instant to compare cookie expiries against.

    Returns:
        ``True`` when no cookie is usable, ``False`` as soon as one is.
    """
    deadline = reference.timestamp()
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        expires = cookie.get("expires", -1)
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            continue
        if expires < 0 or expires > deadline:
            return False
    return True


def validate_session_state(path: str, *, now: datetime | None = None) -> None:
    """Validate that a Playwright storage-state file is present and usable.

    This helper is pure, does no I/O beyond reading the file and runs *before*
    any Playwright import, which keeps ``binance_session_expired`` observable
    in a browser-free test run.

    Args:
        path: Filesystem path to a Playwright storage-state JSON file.
        now: Reference instant for the cookie-expiry check; defaults to the
            current UTC time.

    Raises:
        ExternalServiceError: With ``code="binance_session_expired"`` when the
            path is empty, the file is missing, empty, unreadable, malformed,
            carries no cookies, or every cookie has already expired.
    """
    if not path:
        raise _session_expired("Binance session state path is not configured")
    state_path = Path(path)
    if not state_path.is_file():
        raise _session_expired("Binance session state file is missing")
    try:
        raw = state_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise _session_expired("Binance session state file is unreadable") from exc
    if not raw:
        raise _session_expired("Binance session state file is empty")
    try:
        state = json.loads(raw)
    except ValueError as exc:
        raise _session_expired("Binance session state file is not valid JSON") from exc

    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(cookies, list) or not cookies:
        raise _session_expired("Binance session state carries no cookies")
    reference = now or datetime.now(timezone.utc)
    if _all_cookies_expired(cookies, reference):
        raise _session_expired("Binance session cookies have expired")


async def _close_quietly(resource: Any | None, method: str = "close") -> None:
    """Await one cleanup method, swallowing failures.

    Cleanup must never mask the error that triggered it.

    Args:
        resource: Playwright object exposing an async ``close`` or ``stop``.
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


class PlaywrightBinanceBrowser:
    """Playwright-backed ``BinanceBrowser`` adapter for Binance P2P orders.

    Playwright is imported lazily in :meth:`__aenter__`, so constructing or
    importing this class never requires the wheel or Chromium.  The adapter is
    usable either as an async context manager or directly: the first call that
    needs a page starts the browser on demand.

    Attributes:
        _settings: Settings supplying the session state, headless flag,
            navigation timeout and order URL template.
    """

    def __init__(self, settings: Settings) -> None:
        """Store the settings without importing or launching anything.

        Args:
            settings: Application settings for the browser adapter.
        """
        self._settings = settings
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None

    async def __aenter__(self) -> "PlaywrightBinanceBrowser":
        """Validate the session state, then start Chromium with it.

        Returns:
            This adapter, ready for interaction.  A second entry after a
            successful start is a no-op.

        Raises:
            ExternalServiceError: ``binance_session_expired`` when the storage
                state is unusable, or ``binance_browser_start_failed`` when
                Playwright cannot start a browser with it.
        """
        if self._context is not None:
            return self
        state_path = self._settings.binance_session_state_path
        validate_session_state(state_path)

        from playwright.async_api import async_playwright  # local import on purpose

        started: Any | None = None
        launched: Any | None = None
        context: Any | None = None
        try:
            started = await async_playwright().start()
            launched = await started.chromium.launch(
                headless=self._settings.binance_browser_headless
            )
            context = await launched.new_context(storage_state=state_path)
            context.set_default_timeout(self._settings.binance_browser_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await _close_quietly(context)
            await _close_quietly(launched)
            await _close_quietly(started, "stop")
            raise ExternalServiceError(
                "Binance browser could not be started",
                service=_BROWSER_SERVICE,
                code="binance_browser_start_failed",
            ) from exc

        self._playwright = started
        self._browser = launched
        self._context = context
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Release every Playwright resource held by the adapter.

        Args:
            exc_type: Exception class raised inside the context, if any.
            exc: Exception instance raised inside the context, if any.
            traceback: Traceback of the exception, if any.
        """
        await self.aclose()

    async def aclose(self) -> None:
        """Close the context, browser and driver, ignoring cleanup errors."""
        context, browser, playwright = (
            self._context,
            self._browser,
            self._playwright,
        )
        self._context = None
        self._browser = None
        self._playwright = None
        await _close_quietly(context)
        await _close_quietly(browser)
        await _close_quietly(playwright, "stop")

    async def _ensure_context(self) -> Any:
        """Return the live browser context, starting the browser on first use.

        Returns:
            The Playwright browser context.

        Raises:
            ExternalServiceError: The same typed errors as :meth:`__aenter__`.
        """
        if self._context is None:
            await self.__aenter__()
        context = self._context
        if context is None:  # pragma: no cover - __aenter__ guarantees or raises
            raise ExternalServiceError(
                "Binance browser context is unavailable",
                service=_BROWSER_SERVICE,
                code="binance_browser_not_started",
            )
        return context

    async def extract_payment_instruction(
        self, order_number: str
    ) -> PaymentInstruction:
        """Navigate to the order view and parse the payment instruction.

        Args:
            order_number: Provider order number.

        Returns:
            The parsed, complete payment instruction built from the rendered
            page HTML by :func:`parse_payment_instructions`.

        Raises:
            ExternalServiceError: ``binance_session_expired`` when the session
                state is unusable, ``binance_browser_start_failed`` when the
                browser cannot start, or ``binance_navigation_failed`` when the
                order page cannot be opened or read.
            ExternalServiceResponseError: ``payment_data_unavailable`` when the
                page loads but the parser cannot find every required field.
        """
        context = await self._ensure_context()
        url = self._settings.binance_p2p_order_url_template.format(
            order_number=order_number
        )
        page: Any | None = None
        try:
            page = await context.new_page()
            await page.goto(url, timeout=self._settings.binance_browser_timeout_ms)
            html = await page.content()
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise ExternalServiceError(
                "Binance order page could not be loaded",
                service=_BROWSER_SERVICE,
                code="binance_navigation_failed",
            ) from exc
        finally:
            await _close_quietly(page)

        instruction = parse_payment_instructions(html, order_number=order_number)
        if instruction is None:
            raise ExternalServiceResponseError(
                "Binance order page did not expose payment data",
                service=_BROWSER_SERVICE,
                code="payment_data_unavailable",
            )
        return instruction

    async def notify_paid(self, order_number: str, reference: str) -> None:
        """Mark a P2P order as paid by notifying the seller with a reference.

        The adapter navigates to the order, clicks the
        "Transferido, notificar al vendedor" control, posts the reference in the
        order chat and only returns once the resulting DOM shows the reference
        bubble.  Any navigation, interaction or confirmation failure raises a
        typed error, so an unknown close is never reported as success
        (R-BUY-009).

        Args:
            order_number: Provider order number.
            reference: Bank transaction reference to post in the order chat.

        Raises:
            ExternalServiceResponseError: ``binance_close_unconfirmed`` when the
                reference is empty, or when the DOM cannot confirm the close.
            ExternalServiceError: ``binance_session_expired`` when the session
                state is unusable, ``binance_browser_start_failed`` when the
                browser cannot start, or ``binance_close_unconfirmed`` when the
                interaction fails.
        """
        cleaned = reference.strip() if isinstance(reference, str) else ""
        if not cleaned:
            raise ExternalServiceResponseError(
                "Binance close requires a bank reference",
                service=_BROWSER_SERVICE,
                code="binance_close_unconfirmed",
            )

        context = await self._ensure_context()
        url = self._settings.binance_p2p_order_url_template.format(
            order_number=order_number
        )
        timeout = self._settings.binance_browser_timeout_ms
        page: Any | None = None
        try:
            page = await context.new_page()
            await page.goto(url, timeout=timeout)
            close_button = page.get_by_role(
                "button", name=_CLOSE_BUTTON_NAME
            ).first
            await close_button.click(timeout=timeout)
            chat = page.locator(_CHAT_INPUT_SELECTOR).first
            await chat.fill(cleaned, timeout=timeout)
            await chat.press(_CHAT_SEND_KEY, timeout=timeout)
            await page.get_by_text(cleaned, exact=False).first.wait_for(
                state="visible", timeout=timeout
            )
        except ExternalServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise ExternalServiceError(
                "Binance order close could not be confirmed",
                service=_BROWSER_SERVICE,
                code="binance_close_unconfirmed",
            ) from exc
        finally:
            await _close_quietly(page)

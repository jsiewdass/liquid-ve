"""Pure Banesco Online page contracts over captured markup.

This module is the single source of truth for the Banesco screens, frame names
and control selectors.  It also holds deterministic helpers that parse a
captured markup string into page-shape facts: visible security-question texts,
``select`` option sets (phone prefixes, document letters, destination banks),
the confirmation summary and the current screen.

The module is pure: it never imports Playwright, never reads settings and never
performs I/O.  Callers pass the markup captured by the browser adapter.

Frame architecture facts pinned from the live portal:

* the login form lives in the ``ctl00_cp_frmaplicacion`` iframe;
* the Pago Móvil UI (form, confirmation and Clave Dinámica) lives in the
  ``embedded`` iframe;
* ``aplicacion.aspx`` is only the outer shell, so these controls are never
  resolved against the top-level frame.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from html import unescape
from html.parser import HTMLParser
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from app.core.exceptions import (
    BanescoAccountLocked,
    BanescoAdModalBlocked,
    BanescoCaptchaDetected,
    BanescoLayoutUnexpected,
    BanescoLoginFailed,
    BanescoMaintenance,
    BanescoSessionExpired,
    IdTypeMissing,
    IdTypeUnsupported,
    PaymentDataUnavailable,
    PaymentRailError,
    UnsupportedPhonePrefix,
)
from app.domain.banesco import (
    CLAVE_DINAMICA_MAX_DIGITS,
    CLAVE_DINAMICA_MIN_DIGITS,
    QUESTION_ANSWER_SETTINGS,
    BanescoBankOption,
    BanescoPaymentTarget,
    QuestionKey,
    format_ves_amount,
    match_destination_bank_option,
    resolve_question_keys,
)

__all__ = [
    "Screen",
    "SelectOption",
    "ConfirmationSummary",
    "QuestionAnswer",
    "PaymentFormOptions",
    "PageLike",
    "BasePage",
    "LoginPage",
    "SecurityQuestionsPage",
    "PasswordPage",
    "AdModalPage",
    "MenuPage",
    "PagoMovilFormPage",
    "ConfirmationPage",
    "ClaveDinamicaPage",
    "LOGIN_FRAME_NAME",
    "PAGOMOVIL_FRAME_NAME",
    "FRAMES_BY_SCREEN",
    "SELECTORS_BY_SCREEN",
    "MENU_SELECTORS",
    "SCREEN_ANCHOR_SELECTORS",
    "AUTHENTICATED_MARKER_SELECTOR",
    "SCREEN_FIXTURES",
    "QUESTION_TEXT_SELECTORS",
    "CLAVE_DINAMICA_MIN_DIGITS",
    "CLAVE_DINAMICA_MAX_DIGITS",
    "CLAVE_DINAMICA_INPUT_MAXLENGTH",
    "detect_screen",
    "detect_login_error",
    "extract_question_texts",
    "extract_select_options",
    "parse_bank_options",
    "parse_phone_prefixes",
    "parse_id_types",
    "parse_confirmation_summary",
    "parse_question_answer_pairs",
]


class Screen(str, Enum):
    """Identity of one Banesco screen shape."""

    LOGIN_USER = "login_user"
    SECURITY_QUESTIONS = "security_questions"
    LOGIN_PASSWORD = "login_password"
    LOGIN_ERROR = "login_error"
    DASHBOARD_MODAL = "dashboard_modal"
    PAGOMOVIL_FORM = "pagomovil_form"
    CONFIRMATION = "confirmation"
    CLAVE_DINAMICA = "clave_dinamica"
    UNKNOWN = "unknown"


LOGIN_FRAME_NAME = "ctl00_cp_frmAplicacion"
"""Exact ``name`` attribute of the login iframe (case-sensitive)."""
"""Iframe that hosts every Banesco Online login screen."""

PAGOMOVIL_FRAME_NAME = "embedded"
"""Iframe that hosts the Pago Móvil form, confirmation and Clave Dinámica."""

FRAMES_BY_SCREEN: Mapping[Screen, str] = MappingProxyType(
    {
        Screen.LOGIN_USER: LOGIN_FRAME_NAME,
        Screen.SECURITY_QUESTIONS: LOGIN_FRAME_NAME,
        Screen.LOGIN_PASSWORD: LOGIN_FRAME_NAME,
        Screen.LOGIN_ERROR: LOGIN_FRAME_NAME,
        Screen.PAGOMOVIL_FORM: PAGOMOVIL_FRAME_NAME,
        Screen.CONFIRMATION: PAGOMOVIL_FRAME_NAME,
        Screen.CLAVE_DINAMICA: PAGOMOVIL_FRAME_NAME,
    }
)
"""Frame that must contain each screen; the modal lives in the outer shell."""

LOGIN_USER_SELECTORS: Mapping[str, str] = MappingProxyType(
    {
        "username": "#txtUsuario",
        "submit": "#bAceptar",
        "submit_alternate": "#bAceptar2",
    }
)

SECURITY_QUESTIONS_SELECTORS: Mapping[str, str] = MappingProxyType(
    {
        "first_question_text": "#lblPrimeraP",
        "first_answer": "#txtPrimeraR",
        "second_question_text": "#lblSegundaP",
        "second_answer": "#txtSegundaR",
        "submit": "#bAceptar",
        "back_rendered": "#bRegresar2",
        "back_legacy": "#bRegresar",
    }
)

LOGIN_PASSWORD_SELECTORS: Mapping[str, str] = MappingProxyType(
    {
        "username": "#txtUsuario",
        "password": "#txtClave",
        "frequent_machine": "#CBMachine",
        "submit": "#bAceptar",
    }
)

LOGIN_ERROR_SELECTORS: Mapping[str, str] = MappingProxyType(
    {
        "message": "#lblMensaje",
        "acknowledge": "#bAceptar",
    }
)

DASHBOARD_MODAL_SELECTORS: Mapping[str, str] = MappingProxyType(
    {
        "close": "#closeButton",
        "action": "#actionButton",
    }
)

PAGOMOVIL_FORM_SELECTORS: Mapping[str, str] = MappingProxyType(
    {
        "debit_account": "#inputGroupSelect01",
        "phone_prefix": "#pref",
        "phone_rest": "#tel",
        "id_type": "#NacCli",
        "id_digits": "#ced",
        "destination_bank": "#banco",
        "amount": "#monto",
        "concept": "#concepto",
        "beneficiary_name": "#nom",
        "frequent_payment": "#check",
        "duplicate_check": "#Duplicidad",
        "submit": "#enviar",
    }
)

CONFIRMATION_SELECTORS: Mapping[str, str] = MappingProxyType(
    {
        "summary_table": "#marTabla",
        "submit": "#butonPago",
    }
)

CLAVE_DINAMICA_SELECTORS: Mapping[str, str] = MappingProxyType(
    {
        "otp": "#otp",
        "submit": "#acceptar",
    }
)

SELECTORS_BY_SCREEN: Mapping[Screen, Mapping[str, str]] = MappingProxyType(
    {
        Screen.LOGIN_USER: LOGIN_USER_SELECTORS,
        Screen.SECURITY_QUESTIONS: SECURITY_QUESTIONS_SELECTORS,
        Screen.LOGIN_PASSWORD: LOGIN_PASSWORD_SELECTORS,
        Screen.LOGIN_ERROR: LOGIN_ERROR_SELECTORS,
        Screen.DASHBOARD_MODAL: DASHBOARD_MODAL_SELECTORS,
        Screen.PAGOMOVIL_FORM: PAGOMOVIL_FORM_SELECTORS,
        Screen.CONFIRMATION: CONFIRMATION_SELECTORS,
        Screen.CLAVE_DINAMICA: CLAVE_DINAMICA_SELECTORS,
    }
)
"""Pinned selector table per screen; actions never inline a selector."""

PAGOMOVIL_ENTRY_PATH = "/Mantis/WebSite/Aplicacion.aspx?opc=24"
"""Path the dashboard's own ``#ctl00_btnBPM`` link targets for Pago Móvil."""

AUTHENTICATED_MARKER_IDS: tuple[str, ...] = ("ctl00_btnBPM",)
"""Ids that prove the authenticated dashboard rendered."""

MENU_SELECTORS: Mapping[str, str] = MappingProxyType(
    {
        "menu": "#menu, #ctl00_ListMenuBtn",
        "menu_button": "#ctl00_ListMenuBtn",
        "pago_movil": "#ctl00_btnBPM",
        "enviar_pago": "text=Enviar pago",
    }
)
"""Authenticated menu anchors and the two Pago Móvil navigation entries.

The menu labels are pinned as accessible text pending the opt-in live smoke;
they are never guessed or fuzzy-matched and a mismatch fails closed.
"""

AUTHENTICATED_MARKER_SELECTOR = ", ".join(
    (
        DASHBOARD_MODAL_SELECTORS["close"],
        DASHBOARD_MODAL_SELECTORS["action"],
        MENU_SELECTORS["menu"],
        MENU_SELECTORS["menu_button"],
    )
)
"""Any one of these anchors proves the clave submission left the login area."""

SCREEN_ANCHOR_SELECTORS: Mapping[Screen, str] = MappingProxyType(
    {
        Screen.LOGIN_USER: LOGIN_USER_SELECTORS["username"],
        Screen.SECURITY_QUESTIONS: SECURITY_QUESTIONS_SELECTORS["first_answer"],
        Screen.LOGIN_PASSWORD: LOGIN_PASSWORD_SELECTORS["password"],
        Screen.LOGIN_ERROR: LOGIN_ERROR_SELECTORS["message"],
        Screen.DASHBOARD_MODAL: DASHBOARD_MODAL_SELECTORS["close"],
        Screen.PAGOMOVIL_FORM: PAGOMOVIL_FORM_SELECTORS["destination_bank"],
        Screen.CONFIRMATION: CONFIRMATION_SELECTORS["summary_table"],
        Screen.CLAVE_DINAMICA: CLAVE_DINAMICA_SELECTORS["otp"],
    }
)
"""Explicit wait anchor used before a screen shape assertion."""

SCREEN_FIXTURES: Mapping[Screen, str] = MappingProxyType(
    {
        Screen.LOGIN_USER: "01_login_user.html",
        Screen.SECURITY_QUESTIONS: "02_security_questions.html",
        Screen.LOGIN_PASSWORD: "03_login_password.html",
        Screen.LOGIN_ERROR: "04_login_error.html",
        Screen.DASHBOARD_MODAL: "05_dashboard_modal.html",
        Screen.PAGOMOVIL_FORM: "06_pagomovil_form.html",
        Screen.CONFIRMATION: "07_confirmation.html",
        Screen.CLAVE_DINAMICA: "08_clave_dinamica.html",
    }
)
"""Recorded, sanitized fixture that pins each screen shape."""

QUESTION_TEXT_SELECTORS: tuple[str, ...] = (
    SECURITY_QUESTIONS_SELECTORS["first_question_text"],
    SECURITY_QUESTIONS_SELECTORS["second_question_text"],
)

CLAVE_DINAMICA_INPUT_MAXLENGTH = 8
"""``maxlength`` pinned on the live ``#otp`` control."""

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ID_ATTRIBUTE_TEMPLATE = """\\bid\\s*=\\s*["']{element_id}["']"""
_TAG_RE = re.compile(r"<[^>]+>")

_CAPTCHA_MARKERS: tuple[str, ...] = (
    "captcha",
    "recaptcha",
    "no soy un robot",
    "anti-bot",
    "antibot",
)
_LOCKED_MARKERS: tuple[str, ...] = (
    "cuenta bloqueada",
    "usuario bloqueado",
    "acceso bloqueado",
    "ha sido bloqueado",
    "account locked",
    "locked account",
)
_MAINTENANCE_MARKERS: tuple[str, ...] = (
    "mantenimiento",
    "maintenance",
    "fuera de servicio",
)
_SESSION_MARKERS: tuple[str, ...] = (
    "sesion expirada",
    "sesion ha expirado",
    "ha expirado su sesion",
    "session expired",
)
_LOGIN_FAILED_MARKERS: tuple[str, ...] = (
    "usuario o clave invalida",
    "clave invalida",
    "clave incorrecta",
    "credenciales invalidas",
    "invalid credentials",
    "login failed",
)

_CONFIRMATION_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "nombre del beneficiario": "beneficiary_name",
        "numero de telefono del beneficiario": "beneficiary_phone",
        "numero de cedula del beneficiario": "beneficiary_document",
        "banco destino": "destination_bank",
        "monto": "amount_ves",
        "concepto": "concept",
    }
)


@dataclass(frozen=True, slots=True)
class SelectOption:
    """One ``<option>`` inside a captured ``select`` control.

    Attributes:
        value: Raw ``value`` attribute.
        label: Visible option text with whitespace collapsed.
    """

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class ConfirmationSummary:
    """Fields read from the captured confirmation screen.

    Attributes:
        beneficiary_name: Visible beneficiary name, or ``""`` when omitted.
        beneficiary_phone: Visible beneficiary phone text.
        beneficiary_document: Visible beneficiary document text.
        destination_bank: Visible destination-bank label.
        amount_ves: Parsed positive ``Decimal`` VES amount.
        concept: Visible concept text.
    """

    beneficiary_name: str
    beneficiary_phone: str
    beneficiary_document: str
    destination_bank: str
    amount_ves: Decimal
    concept: str


@dataclass(frozen=True, slots=True)
class PaymentFormOptions:
    """Live option sets read from the captured Pago Móvil form.

    Attributes:
        debit_accounts: Options offered by the single-account debit control.
        phone_prefixes: Non-empty values offered by the phone-prefix select.
        id_types: Non-empty document letters offered by the ID-type select.
        destination_banks: Reviewed destination-bank options read from
            ``#banco``.
    """

    debit_accounts: tuple[SelectOption, ...]
    phone_prefixes: tuple[str, ...]
    id_types: tuple[str, ...]
    destination_banks: tuple[BanescoBankOption, ...]


_PHONE_REST_RE = re.compile(r"^\d{7}$")
_ID_DIGITS_RE = re.compile(r"^\d{6,9}$")


def _strip_comments(markup: str) -> str:
    """Remove HTML comments so commented legacy controls are ignored.

    Args:
        markup: Raw captured markup.

    Returns:
        Markup without comment blocks.
    """
    return _COMMENT_RE.sub("", markup)


def _has_id(markup: str, element_id: str) -> bool:
    """Return whether an ``id`` attribute exists in the markup.

    Args:
        markup: Markup to search.
        element_id: Exact ``id`` value.

    Returns:
        ``True`` when the id attribute is present.
    """
    pattern = _ID_ATTRIBUTE_TEMPLATE.format(element_id=re.escape(element_id))
    return re.search(pattern, markup, re.IGNORECASE) is not None


def _input_tag_has_maxlength(
    markup: str, element_id: str, maxlength: int
) -> bool:
    """Return whether the ``input`` with ``element_id`` pins ``maxlength``.

    Args:
        markup: Captured markup.
        element_id: Exact ``id`` of the target input.
        maxlength: Required ``maxlength`` attribute value.

    Returns:
        ``True`` when the matched input tag carries the exact ``maxlength``.
    """
    tag_pattern = re.compile(
        rf'<input\b[^>]*\bid\s*=\s*["\']{re.escape(element_id)}["\'][^>]*>',
        re.IGNORECASE,
    )
    tag = tag_pattern.search(_strip_comments(markup))
    if tag is None:
        return False
    length_pattern = re.compile(
        rf'\bmaxlength\s*=\s*["\']{maxlength}["\']', re.IGNORECASE
    )
    return length_pattern.search(tag.group(0)) is not None


def detect_screen(markup: str) -> Screen:
    """Classify a captured markup string into a known Banesco screen.

    Detection uses stable ids from the recorded fixtures and ignores commented
    legacy controls.  Unrelated markup fails closed as :attr:`Screen.UNKNOWN`.

    Args:
        markup: Captured markup for one page or iframe.

    Returns:
        The detected :class:`Screen`.

    Raises:
        BanescoLayoutUnexpected: If ``markup`` is not non-empty text.
    """
    if not isinstance(markup, str) or not markup.strip():
        raise BanescoLayoutUnexpected("Captured markup is empty")
    cleaned = _strip_comments(markup)
    if _has_id(cleaned, "otp"):
        return Screen.CLAVE_DINAMICA
    if _has_id(cleaned, "butonPago"):
        return Screen.CONFIRMATION
    if (
        _has_id(cleaned, "banco")
        and _has_id(cleaned, "pref")
        and _has_id(cleaned, "NacCli")
    ):
        return Screen.PAGOMOVIL_FORM
    if _has_id(cleaned, "closeButton") or _has_id(cleaned, "actionButton"):
        return Screen.DASHBOARD_MODAL
    if _has_id(cleaned, "lblMensaje"):
        return Screen.LOGIN_ERROR
    if _has_id(cleaned, "txtPrimeraR") or _has_id(cleaned, "txtSegundaR"):
        return Screen.SECURITY_QUESTIONS
    if _has_id(cleaned, "txtClave") and _has_id(cleaned, "CBMachine"):
        return Screen.LOGIN_PASSWORD
    if _has_id(cleaned, "bAceptar2") or _has_id(cleaned, "txtUsuario"):
        return Screen.LOGIN_USER
    return Screen.UNKNOWN


class _QuestionLabelParser(HTMLParser):
    """Collect the text of the pinned security-question label spans."""

    def __init__(self, ids: frozenset[str]) -> None:
        """Initialize the parser for the requested label ids.

        Args:
            ids: Label element ids to capture.
        """
        super().__init__(convert_charrefs=True)
        self._ids = ids
        self._active_id: str | None = None
        self._depth = 0
        self._buffer: list[str] = []
        self.texts: list[tuple[str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Track span nesting and start capturing a requested label.

        Args:
            tag: Element name.
            attrs: Element attributes.
        """
        if self._active_id is not None:
            if tag == "span":
                self._depth += 1
            return
        if tag != "span":
            return
        element_id = dict(attrs).get("id")
        if element_id in self._ids:
            self._active_id = element_id
            self._depth = 1
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        """Finish capturing the active label span.

        Args:
            tag: Element name.
        """
        if self._active_id is None or tag != "span":
            return
        self._depth -= 1
        if self._depth != 0:
            return
        text = " ".join("".join(self._buffer).split())
        self.texts.append((self._active_id, text))
        self._active_id = None
        self._buffer = []

    def handle_data(self, data: str) -> None:
        """Collect text while a label is active.

        Args:
            data: Character data.
        """
        if self._active_id is not None:
            self._buffer.append(data)


class _SelectOptionsParser(HTMLParser):
    """Collect ``<option>`` entries inside one pinned ``select`` control."""

    def __init__(self, select_id: str) -> None:
        """Initialize the parser for one select id.

        Args:
            select_id: Exact ``id`` of the target select.
        """
        super().__init__(convert_charrefs=True)
        self._select_id = select_id
        self._in_select = False
        self._select_depth = 0
        self._in_option = False
        self._option_value = ""
        self._option_buffer: list[str] = []
        self.found = False
        self.options: list[SelectOption] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Track the target select and its option elements.

        Args:
            tag: Element name.
            attrs: Element attributes.
        """
        attributes = dict(attrs)
        if tag == "select":
            if not self._in_select and attributes.get("id") == self._select_id:
                self._in_select = True
                self.found = True
                self._select_depth = 1
            elif self._in_select:
                self._select_depth += 1
            return
        if self._in_select and tag == "option":
            self._in_option = True
            self._option_value = attributes.get("value") or ""
            self._option_buffer = []

    def handle_endtag(self, tag: str) -> None:
        """Close options and track the target select end.

        Args:
            tag: Element name.
        """
        if not self._in_select:
            return
        if tag == "option" and self._in_option:
            label = " ".join("".join(self._option_buffer).split())
            self.options.append(
                SelectOption(value=self._option_value, label=label)
            )
            self._in_option = False
            return
        if tag == "select":
            self._select_depth -= 1
            if self._select_depth == 0:
                self._in_select = False

    def handle_data(self, data: str) -> None:
        """Collect option text.

        Args:
            data: Character data.
        """
        if self._in_option:
            self._option_buffer.append(data)


def extract_question_texts(markup: str) -> tuple[str, ...]:
    """Read the two visible security-question texts in display order.

    The labels are read by their pinned ids, never by position or order.  A
    missing or duplicated label fails closed.

    Args:
        markup: Captured security-question markup.

    Returns:
        The two visible question texts in document order.

    Raises:
        BanescoLayoutUnexpected: If either label is missing or duplicated.
    """
    ids = tuple(selector.lstrip("#") for selector in QUESTION_TEXT_SELECTORS)
    parser = _QuestionLabelParser(frozenset(ids))
    parser.feed(markup)
    parser.close()
    if sorted(found_id for found_id, _ in parser.texts) != sorted(ids):
        raise BanescoLayoutUnexpected(
            "Security question labels are missing or duplicated"
        )
    return tuple(text for _, text in parser.texts)


def extract_select_options(markup: str, select_id: str) -> tuple[SelectOption, ...]:
    """Read every ``<option>`` from one captured select control.

    Args:
        markup: Captured markup containing the select.
        select_id: Exact ``id`` of the target select.

    Returns:
        The options in document order, including an empty-value placeholder.

    Raises:
        BanescoLayoutUnexpected: If the control or all of its options are
            missing.
    """
    parser = _SelectOptionsParser(select_id)
    parser.feed(markup)
    parser.close()
    if not parser.found:
        raise BanescoLayoutUnexpected(
            "Select control is missing", details={"select": select_id}
        )
    if not parser.options:
        raise BanescoLayoutUnexpected(
            "Select control has no options", details={"select": select_id}
        )
    return tuple(parser.options)


def parse_phone_prefixes(markup: str) -> tuple[str, ...]:
    """Read the non-empty ``#pref`` phone-prefix option values.

    Args:
        markup: Captured Pago Móvil form markup.

    Returns:
        Offered four-digit prefix values in document order.
    """
    return tuple(
        option.value
        for option in extract_select_options(markup, "pref")
        if option.value
    )


def parse_id_types(markup: str) -> tuple[str, ...]:
    """Read the non-empty ``#NacCli`` document-letter option values.

    Args:
        markup: Captured Pago Móvil form markup.

    Returns:
        Offered document letters in document order.
    """
    return tuple(
        option.value
        for option in extract_select_options(markup, "NacCli")
        if option.value
    )


def parse_bank_options(markup: str) -> tuple[BanescoBankOption, ...]:
    """Read the reviewed ``#banco`` destination-bank options.

    The empty ``Seleccione`` placeholder is skipped.  Each option carries the
    four-digit code as both ``numeric_code`` and ``value``.

    Args:
        markup: Captured Pago Móvil form markup.

    Returns:
        Destination-bank options in document order.

    Raises:
        BanescoLayoutUnexpected: If no destination bank can be parsed.
    """
    parsed = tuple(
        BanescoBankOption(
            numeric_code=option.value,
            label=option.label,
            value=option.value,
        )
        for option in extract_select_options(markup, "banco")
        if option.value
    )
    if not parsed:
        raise BanescoLayoutUnexpected("No destination-bank options were parsed")
    return parsed


class _ParagraphTextParser(HTMLParser):
    """Collect the visible text of every top-level paragraph."""

    def __init__(self) -> None:
        """Initialize an empty paragraph collector."""
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._buffer: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Track paragraph nesting.

        Args:
            tag: Element name.
            attrs: Element attributes.
        """
        if tag != "p":
            return
        if self._depth == 0:
            self._buffer = []
        self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        """Close the current paragraph and record its text.

        Args:
            tag: Element name.
        """
        if tag != "p" or self._depth == 0:
            return
        self._depth -= 1
        if self._depth == 0:
            self.paragraphs.append(" ".join("".join(self._buffer).split()))

    def handle_data(self, data: str) -> None:
        """Collect paragraph text.

        Args:
            data: Character data.
        """
        if self._depth > 0:
            self._buffer.append(data)


def _normalize_label(text: str) -> str:
    """Normalize a confirmation label for exact comparison.

    Args:
        text: Visible label text.

    Returns:
        Accent-free, whitespace-collapsed, casefolded text without a trailing
        colon.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    collapsed = " ".join(without_marks.casefold().split())
    return collapsed.rstrip(":").strip()


def _parse_ves_amount(text: str) -> Decimal:
    """Parse a Venezuelan-format amount into a positive ``Decimal``.

    Args:
        text: Visible amount such as ``"1500,00"`` or ``"2.500.000,00"``.

    Returns:
        The parsed positive amount.

    Raises:
        BanescoLayoutUnexpected: If the text is empty, malformed or not
            positive.
    """
    compact = " ".join(text.split())
    if not compact:
        raise BanescoLayoutUnexpected("Confirmation amount is empty")
    if "," in compact:
        integer, _, fraction = compact.rpartition(",")
        candidate = f"{integer.replace('.', '')}.{fraction}"
    elif compact.count(".") == 1 and len(compact.rsplit(".", 1)[1]) <= 2:
        candidate = compact
    else:
        candidate = compact.replace(".", "")
    try:
        amount = Decimal(candidate)
    except InvalidOperation as exc:
        raise BanescoLayoutUnexpected(
            "Confirmation amount is malformed"
        ) from exc
    if not amount.is_finite() or amount <= 0:
        raise BanescoLayoutUnexpected("Confirmation amount is not positive")
    return amount


def parse_confirmation_summary(markup: str) -> ConfirmationSummary:
    """Read the confirmation summary fields from captured markup.

    Labels are matched after accent/case normalization.  The beneficiary name
    is optional and may be an empty string; every other field is required.

    Args:
        markup: Captured confirmation-screen markup.

    Returns:
        The parsed :class:`ConfirmationSummary`.

    Raises:
        BanescoLayoutUnexpected: If a required label or its value is missing, or
            if the amount cannot be parsed as a positive ``Decimal``.
    """
    parser = _ParagraphTextParser()
    parser.feed(markup)
    parser.close()

    values: dict[str, str] = {}
    for index, paragraph in enumerate(parser.paragraphs):
        field = _CONFIRMATION_LABELS.get(_normalize_label(paragraph))
        if field is None:
            continue
        if index + 1 >= len(parser.paragraphs):
            raise BanescoLayoutUnexpected(
                "Confirmation label has no value", details={"field": field}
            )
        values.setdefault(field, parser.paragraphs[index + 1])

    missing = sorted(set(_CONFIRMATION_LABELS.values()) - set(values))
    if missing:
        raise BanescoLayoutUnexpected(
            "Confirmation summary is incomplete", details={"missing": missing}
        )

    return ConfirmationSummary(
        beneficiary_name=values["beneficiary_name"],
        beneficiary_phone=values["beneficiary_phone"],
        beneficiary_document=values["beneficiary_document"],
        destination_bank=values["destination_bank"],
        amount_ves=_parse_ves_amount(values["amount_ves"]),
        concept=values["concept"],
    )

@dataclass(frozen=True, slots=True)
class QuestionAnswer:
    """One security question paired with its adjacent answer control.

    Attributes:
        label_id: Pinned id of the visible question label.
        question_text: Visible question text.
        answer_selector: Selector of the answer input in the same form group.
    """

    label_id: str
    question_text: str
    answer_selector: str


def _visible_text(markup: str) -> str:
    """Return tag-free, entity-decoded visible text for error detection.

    Args:
        markup: Captured page markup.

    Returns:
        Collapsed visible text; empty when ``markup`` is not text.
    """
    if not isinstance(markup, str) or not markup.strip():
        return ""
    without_comments = _strip_comments(markup)
    without_tags = _TAG_RE.sub(" ", without_comments)
    return " ".join(unescape(without_tags).split())


def detect_login_error(
    markup: str, *, stage: str = "login"
) -> PaymentRailError | None:
    """Classify a captured login-area page into a typed fail-closed error.

    Detection is conservative and order-stable: captcha, locked account,
    maintenance, session expiry and finally rejected credentials.  The returned
    error carries only ``{"stage": stage}`` in its sanitized details; no page
    text, credential or question value is ever attached.

    Args:
        markup: Captured page markup.
        stage: Flow stage used for the safe detail, such as ``"login"``.

    Returns:
        The matching :class:`PaymentRailError`, or ``None`` when the page is not
        a recognized error page.
    """
    normalized = _normalize_label(_visible_text(markup))
    if not normalized:
        return None
    if any(marker in normalized for marker in _CAPTCHA_MARKERS):
        return BanescoCaptchaDetected(details={"stage": stage})
    if any(marker in normalized for marker in _LOCKED_MARKERS):
        return BanescoAccountLocked(details={"stage": stage})
    if any(marker in normalized for marker in _MAINTENANCE_MARKERS):
        return BanescoMaintenance(details={"stage": stage})
    if any(marker in normalized for marker in _SESSION_MARKERS):
        return BanescoSessionExpired(details={"stage": stage})
    if any(marker in normalized for marker in _LOGIN_FAILED_MARKERS):
        return BanescoLoginFailed(
            "Banesco Online credentials were rejected",
            details={"stage": stage},
        )
    return None


class _TableBucket:
    """Labels and answer inputs observed inside one table element."""

    __slots__ = ("answers", "labels", "order")

    def __init__(self, order: int) -> None:
        """Initialize an empty bucket at a document position.

        Args:
            order: Start offset used to order labels deterministically.
        """
        self.order = order
        self.labels: list[QuestionAnswer] = []
        self.answers: list[str] = []


class _QuestionPairParser(HTMLParser):
    """Pair security-question labels with inputs in the same table.

    The live markup wraps each question label and its answer input in the same
    inner table.  Pairing by nearest enclosing table is a real DOM adjacency
    rule; pairing by label/answer id would assume a fixed position.
    """

    def __init__(self) -> None:
        """Initialize an empty parser with the pinned ids."""
        super().__init__(convert_charrefs=True)
        self._label_ids = frozenset(
            selector.lstrip("#") for selector in QUESTION_TEXT_SELECTORS
        )
        self._answer_ids = frozenset(
            selector.lstrip("#")
            for selector in (
                SECURITY_QUESTIONS_SELECTORS["first_answer"],
                SECURITY_QUESTIONS_SELECTORS["second_answer"],
            )
        )
        self._stack: list[_TableBucket] = []
        self._tables: list[_TableBucket] = []
        self._order = 0
        self._active_label: QuestionAnswer | None = None
        self._active_bucket: _TableBucket | None = None
        self._span_depth = 0
        self._buffer: list[str] = []
        self.duplicate_labels: set[str] = set()
        self.answer_inputs: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Track tables, question labels and answer inputs.

        Args:
            tag: Element name.
            attrs: Element attributes.
        """
        attributes = dict(attrs)
        if tag == "table":
            bucket = _TableBucket(self._order)
            self._order += 1
            self._stack.append(bucket)
            self._tables.append(bucket)
            return
        if tag == "span":
            element_id = attributes.get("id")
            if self._active_label is None and element_id in self._label_ids:
                bucket = self._stack[-1] if self._stack else None
                if bucket is None:
                    raise ValueError(
                        "Security question label is outside a table"
                    )
                if any(
                    label.label_id == element_id for label in bucket.labels
                ):
                    self.duplicate_labels.add(element_id)
                self._active_label = QuestionAnswer(
                    label_id=element_id,
                    question_text="",
                    answer_selector="",
                )
                self._active_bucket = bucket
                bucket.labels.append(self._active_label)
                self._span_depth = 1
                self._buffer = []
            elif self._active_label is not None:
                self._span_depth += 1
            return
        if tag == "input" and self._active_label is None:
            element_id = attributes.get("id")
            if element_id in self._answer_ids and self._stack:
                self._stack[-1].answers.append(element_id)
                self.answer_inputs.append(element_id)

    def handle_endtag(self, tag: str) -> None:
        """Finish a question label and track table nesting.

        Args:
            tag: Element name.
        """
        if tag == "span" and self._active_label is not None:
            self._span_depth -= 1
            if self._span_depth == 0:
                text = " ".join("".join(self._buffer).split())
                self._active_label = QuestionAnswer(
                    label_id=self._active_label.label_id,
                    question_text=text,
                    answer_selector="",
                )
                bucket = self._active_bucket
                if bucket is not None:
                    bucket.labels[-1] = self._active_label
                self._active_label = None
                self._active_bucket = None
                self._buffer = []
            return
        if tag == "table" and self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        """Collect visible label text.

        Args:
            data: Character data.
        """
        if self._active_label is not None:
            self._buffer.append(data)


def parse_question_answer_pairs(markup: str) -> tuple[QuestionAnswer, ...]:
    """Pair the two pinned question labels with their co-indexed answer inputs.

    The live page renders both questions inside the **same** table, so grouping
    by ``<table>`` is ambiguous.  The controls are co-indexed by id instead:
    ``lblPrimeraP`` pairs with ``txtPrimeraR`` and ``lblSegundaP`` with
    ``txtSegundaR``.  Each id must appear exactly once; anything else fails
    closed rather than guessing.

    Args:
        markup: Captured security-question markup.

    Returns:
        The two pairs in fixed first/second order.

    Raises:
        BanescoLayoutUnexpected: On missing or duplicated labels or answer
            inputs, or when a question text is empty.
    """
    try:
        texts = extract_question_texts(markup)
    except BanescoLayoutUnexpected as exc:
        raise BanescoLayoutUnexpected(
            "Security question labels are missing or duplicated",
            details={"stage": "questions"},
        ) from exc

    if len(texts) != 2:
        raise BanescoLayoutUnexpected(
            "Exactly two security questions are required",
            details={"stage": "questions"},
        )

    cleaned = _strip_comments(markup)
    label_ids = (
        SECURITY_QUESTIONS_SELECTORS["first_question_text"].lstrip("#"),
        SECURITY_QUESTIONS_SELECTORS["second_question_text"].lstrip("#"),
    )
    answer_ids = (
        SECURITY_QUESTIONS_SELECTORS["first_answer"].lstrip("#"),
        SECURITY_QUESTIONS_SELECTORS["second_answer"].lstrip("#"),
    )

    for element_id in (*label_ids, *answer_ids):
        occurrences = len(
            re.findall(rf'id="{re.escape(element_id)}"', cleaned)
        )
        if occurrences != 1:
            raise BanescoLayoutUnexpected(
                "Security answer controls are missing or duplicated",
                details={"stage": "questions"},
            )

    return tuple(
        QuestionAnswer(
            label_id=label_ids[index],
            question_text=texts[index],
            answer_selector=f"#{answer_ids[index]}",
        )
        for index in range(2)
    )


@runtime_checkable
class PageLike(Protocol):
    """Narrow page protocol implemented by Playwright pages and test fakes."""

    url: str

    async def goto(self, url: str) -> None:
        """Navigate the page to ``url``."""
        ...

    async def content(self) -> str:
        """Return the current page markup."""
        ...

    async def fill(self, selector: str, value: str) -> None:
        """Fill one control."""
        ...

    async def click(self, selector: str) -> None:
        """Click one element."""
        ...

    async def select_option(self, selector: str, value: str) -> None:
        """Select one option in a dropdown."""
        ...

    async def wait_for_load_state(
        self, state: str = "domcontentloaded"
    ) -> None:
        """Wait for a load state other than ``networkidle``."""
        ...

    async def wait_for_selector(
        self, selector: str, *, timeout: int | None = None
    ) -> object:
        """Wait for one selector and return an opaque handle."""
        ...


def _safe_screen(markup: str) -> Screen:
    """Classify markup, treating empty content as unknown.

    Args:
        markup: Captured page markup.

    Returns:
        The detected screen or :attr:`Screen.UNKNOWN`.
    """
    if not isinstance(markup, str) or not markup.strip():
        return Screen.UNKNOWN
    return detect_screen(markup)


class BasePage:
    """Common fail-closed helpers shared by every Banesco page object.

    Attributes:
        screen: Screen this page object represents.
        stage: Flow stage named in safe error details.
        selectors: Pinned selector table for the screen.
    """

    screen: Screen = Screen.UNKNOWN
    stage: str = "unknown"
    selectors: Mapping[str, str] = MappingProxyType({})

    def __init__(self, page: PageLike, *, timeout_ms: int = 30000) -> None:
        """Store the page and the explicit interaction timeout.

        Args:
            page: Narrow page implementation.
            timeout_ms: Explicit timeout used by every expectation.
        """
        self._page = page
        self._timeout_ms = timeout_ms

    async def _expect(self, selector: str) -> None:
        """Wait for a selector or fail closed with a typed layout error.

        Args:
            selector: Selector that must become available.

        Raises:
            BanescoLayoutUnexpected: If the selector never resolves.
        """
        try:
            await self._page.wait_for_selector(
                selector, timeout=self._timeout_ms
            )
        except BanescoLayoutUnexpected:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary
            error = detect_login_error(
                await self._page.content(), stage=self.stage
            )
            if error is not None:
                raise error from exc
            raise BanescoLayoutUnexpected(
                "Expected Banesco control was not found",
                details={"stage": self.stage, "selector": selector},
            ) from exc

    async def _probe(self, selector: str, *, timeout_ms: int = 3000) -> bool:
        """Return whether a selector becomes visible within a short window.

        Unlike :meth:`_expect`, a missing selector is a normal outcome, so no
        typed error is raised.

        Args:
            selector: Selector to probe.
            timeout_ms: Maximum wait in milliseconds.

        Returns:
            ``True`` when the element became visible.
        """
        try:
            await self._page.wait_for_selector(selector, timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - absence is a valid outcome
            return False
        return True

    async def _await_screen(self, screen: Screen) -> None:
        """Wait for and verify one screen shape.

        Args:
            screen: Expected screen.

        Raises:
            PaymentRailError: A typed error page, or
                :class:`BanescoLayoutUnexpected` when the expected shape never
                appears.
        """
        markup = await self._page.content()
        if _safe_screen(markup) is screen:
            return
        anchor = SCREEN_ANCHOR_SELECTORS.get(screen)
        if anchor is not None:
            try:
                await self._page.wait_for_selector(
                    anchor, timeout=self._timeout_ms
                )
            except Exception as exc:  # noqa: BLE001 - provider boundary
                raise self._screen_failure(
                    screen, await self._page.content()
                ) from exc
        markup = await self._page.content()
        if _safe_screen(markup) is screen:
            return
        raise self._screen_failure(screen, markup)

    def _screen_failure(
        self, screen: Screen, markup: str
    ) -> PaymentRailError:
        """Build the most specific typed error for a missing screen.

        Args:
            screen: Expected screen.
            markup: Current page markup.

        Returns:
            A typed error page failure, a rejected-login error, or a generic
            layout failure.
        """
        error = detect_login_error(markup, stage=self.stage)
        if error is not None:
            return error
        if _safe_screen(markup) is Screen.LOGIN_ERROR:
            return BanescoLoginFailed(
                "Banesco Online login was rejected",
                details={"stage": self.stage},
            )
        return BanescoLayoutUnexpected(
            "Unexpected Banesco screen",
            details={
                "stage": self.stage,
                "expected": screen.value,
                "actual": _safe_screen(markup).value,
            },
        )

    async def assert_shape(self) -> None:
        """Assert the page currently shows this object's screen."""
        await self._await_screen(self.screen)


class LoginPage(BasePage):
    """The Banesco Online user-entry screen."""

    screen = Screen.LOGIN_USER
    stage = "login"
    selectors = LOGIN_USER_SELECTORS

    def __init__(
        self, page: PageLike, *, login_url: str, timeout_ms: int = 30000
    ) -> None:
        """Store the page, login URL and timeout.

        Args:
            page: Narrow page implementation.
            login_url: Configured absolute login URL.
            timeout_ms: Explicit interaction timeout.
        """
        super().__init__(page, timeout_ms=timeout_ms)
        self._login_url = login_url

    async def open(self) -> None:
        """Open the login URL and wait for the document to load.

        The user-entry controls live inside the ``ctl00_cp_frmAplicacion``
        iframe, so the top-level document is only a shell.  The login screen is
        asserted by :meth:`submit_user`, once the caller has resolved the frame.
        """
        await self._page.goto(self._login_url)
        await self._page.wait_for_load_state("domcontentloaded")

    async def submit_user(self, user: str) -> None:
        """Fill the user field and submit the entry step exactly once.

        The next screen is asserted by the caller after it re-resolves the login
        frame: the iframe replaces its document on submit, so a reference
        captured earlier may be stale.

        Args:
            user: Configured user identifier; never logged.

        Raises:
            BanescoLayoutUnexpected: When the user field is absent.
        """
        await self._expect(self.selectors["username"])
        await self._page.fill(self.selectors["username"], user)
        await self._expect(self.selectors["submit"])
        await self._page.click(self.selectors["submit"])
        # Surface captcha/lock/maintenance immediately, without asserting the
        # next screen: the caller re-resolves the frame for that.
        markup = await self._page.content()
        error = detect_login_error(markup, stage=self.stage)
        if error is not None:
            raise error



class SecurityQuestionsPage(BasePage):
    """The randomized two-of-four security-question screen."""

    screen = Screen.SECURITY_QUESTIONS
    stage = "questions"
    selectors = SECURITY_QUESTIONS_SELECTORS

    async def read_questions(self) -> tuple[QuestionAnswer, ...]:
        """Read and pair the two visible questions with their inputs.

        Returns:
            The pairs in label document order.
        """
        markup = await self._page.content()
        return parse_question_answer_pairs(markup)

    async def answer(
        self, answers: Mapping[QuestionKey, str | None]
    ) -> tuple[QuestionKey, QuestionKey]:
        """Resolve and fill both answers without assuming order or position.

        Args:
            answers: Configured answer by question key.  Values are never
                logged or attached to an error.

        Returns:
            The two recognized keys in document order.

        Raises:
            BanescoLayoutUnexpected: On unknown, repeated, wrong-count or
                ambiguously paired questions, or a missing configured answer.
        """
        pairs = await self.read_questions()
        keys = resolve_question_keys(
            [pair.question_text for pair in pairs], answers=answers
        )
        for pair, key in zip(pairs, keys, strict=True):
            value = answers.get(key)
            if not isinstance(value, str) or not value.strip():
                raise BanescoLayoutUnexpected(
                    "Missing configured security answer for "
                    f"{QUESTION_ANSWER_SETTINGS[key]}",
                    details={"setting": QUESTION_ANSWER_SETTINGS[key]},
                )
            await self._page.fill(pair.answer_selector, value)
        return keys

    async def submit(self) -> None:
        """Submit the answered challenge exactly once."""
        await self._expect(self.selectors["submit"])
        await self._page.click(self.selectors["submit"])


class PasswordPage(BasePage):
    """The Banesco Online password/key entry screen."""

    screen = Screen.LOGIN_PASSWORD
    stage = "password"
    selectors = LOGIN_PASSWORD_SELECTORS

    def __init__(
        self,
        page: PageLike,
        *,
        timeout_ms: int = 30000,
        outcome_page: PageLike | None = None,
    ) -> None:
        """Store the control page and the page that observes the outcome.

        The clave form lives inside the login iframe, but a successful submission
        replaces the whole document.  ``outcome_page`` is therefore the
        top-level page used to confirm that the login area was left.

        Args:
            page: Page-like object holding the clave controls.
            timeout_ms: Explicit interaction timeout.
            outcome_page: Page-like object used after submission; defaults to
                ``page``.
        """
        super().__init__(page, timeout_ms=timeout_ms)
        self._outcome_page = outcome_page if outcome_page is not None else page

    async def submit_clave(self, clave: str) -> None:
        """Fill the online clave and submit exactly once.

        Args:
            clave: Configured online-banking clave; never logged.

        Raises:
            PaymentRailError: A typed error page, a rejected clave, or a layout
                failure when the login area is never left.
        """
        await self._expect(self.selectors["password"])
        await self._page.fill(self.selectors["password"], clave)
        await self._expect(self.selectors["submit"])
        await self._page.click(self.selectors["submit"])
        outcome = self._outcome_page
        await outcome.wait_for_load_state("domcontentloaded")
        try:
            await outcome.wait_for_selector(
                AUTHENTICATED_MARKER_SELECTOR, timeout=self._timeout_ms
            )
        except Exception as exc:  # noqa: BLE001 - provider boundary
            markup = await outcome.content()
            error = detect_login_error(markup, stage=self.stage)
            if error is not None:
                raise error from exc
            raise BanescoLoginFailed(
                "Banesco Online clave was rejected",
                details={"stage": self.stage},
            ) from exc
        markup = await outcome.content()
        error = detect_login_error(markup, stage=self.stage)
        if error is not None:
            raise error


class AdModalPage(BasePage):
    """The post-login advertisement modal and menu reachability check."""

    screen = Screen.DASHBOARD_MODAL
    stage = "modal"
    selectors = DASHBOARD_MODAL_SELECTORS

    async def dismiss_if_present(self) -> bool:
        """Dismiss the advertisement modal once when it is present.

        Returns:
            ``True`` when a modal was dismissed, ``False`` when it was absent.

        Raises:
            BanescoAdModalBlocked: When a modal exists but cannot be dismissed.
        """
        markup = await self._page.content()
        if _safe_screen(markup) is not Screen.DASHBOARD_MODAL:
            return False
        close_selector = self.selectors["close"]
        if not _has_id(_strip_comments(markup), close_selector.lstrip("#")):
            raise BanescoAdModalBlocked(details={"stage": self.stage})
        try:
            await self._page.click(close_selector)
            await self._page.wait_for_load_state("domcontentloaded")
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise BanescoAdModalBlocked(details={"stage": self.stage}) from exc
        # The advertisement container keeps ``#closeButton`` in the DOM after it
        # is dismissed, so success is proven by a positive authenticated anchor.
        markup = await self._page.content()
        error = detect_login_error(markup, stage=self.stage)
        if error is not None:
            raise error
        if not any(marker in markup for marker in AUTHENTICATED_MARKER_IDS):
            raise BanescoAdModalBlocked(details={"stage": self.stage})
        return True

    async def assert_menu_reachable(self) -> None:
        """Assert the authenticated dashboard is present after modal handling.

        The advertisement container keeps ``#closeButton`` in the DOM after it
        is dismissed, so its presence alone does not mean the modal is still
        blocking.  The check therefore requires a **positive** authenticated
        anchor instead.

        Raises:
            BanescoAdModalBlocked: When no authenticated anchor is present.
        """
        markup = await self._page.content()
        error = detect_login_error(markup, stage=self.stage)
        if error is not None:
            raise error
        if not any(marker in markup for marker in AUTHENTICATED_MARKER_IDS):
            raise BanescoAdModalBlocked(details={"stage": self.stage})


class MenuPage(BasePage):
    """The authenticated menu: ``Pago Móvil`` then ``Enviar pago``."""

    stage = "menu"
    selectors = MENU_SELECTORS

    async def open_pago_movil(self, pago_movil_url: str | None = None) -> None:
        """Open the Pago Móvil product and reach its screen.

        The dashboard menu is a collapsible tree whose items are only visible
        after expansion, and the Pago Móvil UI loads inside the ``embedded``
        iframe.  The dashboard exposes a direct link (``#ctl00_btnBPM`` →
        ``Aplicacion.aspx?opc=24``), so when an absolute URL is supplied it is
        used directly; otherwise the menu is clicked for compatibility.

        Args:
            pago_movil_url: Absolute Pago Móvil URL; preferred when supplied.

        Raises:
            BanescoAdModalBlocked: When the dashboard is not authenticated.
            BanescoLayoutUnexpected: When the menu path does not reach the form.
        """
        if pago_movil_url:
            markup = await self._page.content()
            if not any(marker in markup for marker in AUTHENTICATED_MARKER_IDS):
                raise BanescoAdModalBlocked(details={"stage": "modal"})
            await self._page.goto(pago_movil_url)
            await self._page.wait_for_load_state("domcontentloaded")
            return

        try:
            await self._expect(self.selectors["menu"])
        except BanescoLayoutUnexpected as exc:
            raise BanescoAdModalBlocked(details={"stage": "modal"}) from exc
        try:
            await self._expect(self.selectors["pago_movil"])
            await self._page.click(self.selectors["pago_movil"])
            await self._expect(self.selectors["enviar_pago"])
            await self._page.click(self.selectors["enviar_pago"])
        except BanescoLayoutUnexpected as exc:
            raise BanescoLayoutUnexpected(
                "Pago Movil navigation is unavailable",
                details={"stage": self.stage},
            ) from exc
        await self._page.wait_for_load_state("domcontentloaded")
        await self._await_screen(Screen.PAGOMOVIL_FORM)


class PagoMovilFormPage(BasePage):
    """The Pago Móvil payment form.

    The form is filled exclusively from a validated
    :class:`~app.domain.banesco.BanescoPaymentTarget` and the live option sets
    read from the captured markup.  It never types a full phone into one control,
    never selects an option by index and never invents a beneficiary name.
    """

    screen = Screen.PAGOMOVIL_FORM
    stage = "form"
    selectors = PAGOMOVIL_FORM_SELECTORS

    async def read_option_sets(self) -> PaymentFormOptions:
        """Read the live debit-account, phone, ID and bank option sets.

        The ``#banco`` control is populated by client-side JavaScript after the
        document loads, so it is polled until it offers real options instead of
        only the ``Seleccione`` placeholder.

        Returns:
            The parsed :class:`PaymentFormOptions`.

        Raises:
            BanescoLayoutUnexpected: When the destination banks never populate.
        """
        markup = await self._page.content()
        destination_banks = self._parse_banks_or_empty(markup)
        if not destination_banks:
            destination_banks = await self._await_bank_options()
            markup = await self._page.content()
        return PaymentFormOptions(
            debit_accounts=extract_select_options(
                markup, self.selectors["debit_account"].lstrip("#")
            ),
            phone_prefixes=parse_phone_prefixes(markup),
            id_types=parse_id_types(markup),
            destination_banks=destination_banks,
        )

    @staticmethod
    def _parse_banks_or_empty(markup: str) -> tuple[BanescoBankOption, ...]:
        """Parse the bank options, returning an empty tuple when absent.

        Args:
            markup: Current form markup.

        Returns:
            The parsed options, or an empty tuple.
        """
        try:
            return parse_bank_options(markup)
        except BanescoLayoutUnexpected:
            return ()

    async def _await_bank_options(self) -> tuple[BanescoBankOption, ...]:
        """Poll the form until the destination-bank options populate.

        Returns:
            The populated options.

        Raises:
            BanescoLayoutUnexpected: When the control never populates.
        """
        deadline = time.monotonic() + (self._timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            options = self._parse_banks_or_empty(await self._page.content())
            if options:
                return options
        raise BanescoLayoutUnexpected(
            "Destination-bank options never populated",
            details={"stage": self.stage, "select": "banco"},
        )

    async def fill(
        self,
        target: BanescoPaymentTarget,
        *,
        beneficiary_name: str | None = None,
    ) -> None:
        """Fill every payment control from the target and the live options.

        Args:
            target: Validated two-control payment target.
            beneficiary_name: Optional counterparty name; when absent or blank
                the ``#nom`` control stays empty.

        Raises:
            UnsupportedPhonePrefix: If the phone prefix is not offered live.
            IdTypeMissing: If the document letter is absent.
            IdTypeUnsupported: If the letter is not offered live.
            PaymentDataUnavailable: If the remainder, digits, amount or concept
                is malformed.
            UnsupportedDestinationBank: If the destination bank is not reviewed
                or its live label changed.
            DestinationBankAmbiguous: If more than one live option matches.
        """
        options = await self.read_option_sets()
        await self._select_debit_account(options)
        await self._fill_phone(target, options)
        await self._fill_document(target, options)
        await self._select_destination_bank(target, options)
        await self._fill_amount(target)
        await self._fill_concept(target)
        await self._fill_beneficiary_name(beneficiary_name)

    async def _select_debit_account(self, options: PaymentFormOptions) -> None:
        """Select the single offered debit account.

        Args:
            options: Live form options.

        Raises:
            BanescoLayoutUnexpected: If the control does not offer exactly one
                account.
        """
        accounts = [
            option for option in options.debit_accounts if option.label.strip()
        ]
        if len(accounts) != 1:
            raise BanescoLayoutUnexpected(
                "Debit account control is not a single account",
                details={"stage": self.stage},
            )
        account = accounts[0]
        selector = self.selectors["debit_account"]
        try:
            await self._expect(selector)
        except BanescoLayoutUnexpected:
            # With a single offered account the control is often hidden and the
            # account is already preselected, so no interaction is required.
            return
        await self._page.select_option(selector, account.value or account.label)

    async def _fill_phone(
        self, target: BanescoPaymentTarget, options: PaymentFormOptions
    ) -> None:
        """Fill the phone prefix select and the remainder input separately.

        Args:
            target: Payment target.
            options: Live form options.

        Raises:
            UnsupportedPhonePrefix: If the prefix is not offered live.
            PaymentDataUnavailable: If the remainder is not seven digits.
        """
        prefix = target.phone_prefix
        rest = target.phone_rest
        if prefix not in options.phone_prefixes:
            raise UnsupportedPhonePrefix(details={"stage": "phone"})
        if not isinstance(rest, str) or not _PHONE_REST_RE.fullmatch(rest):
            raise PaymentDataUnavailable(
                "Beneficiary phone remainder must be exactly seven digits"
            )
        await self._expect(self.selectors["phone_prefix"])
        await self._page.select_option(self.selectors["phone_prefix"], prefix)
        await self._expect(self.selectors["phone_rest"])
        await self._page.fill(self.selectors["phone_rest"], rest)

    async def _fill_document(
        self, target: BanescoPaymentTarget, options: PaymentFormOptions
    ) -> None:
        """Fill the document letter select and the bare digits input.

        Args:
            target: Payment target.
            options: Live form options.

        Raises:
            IdTypeMissing: If the document letter is absent.
            IdTypeUnsupported: If the letter is not offered live.
            PaymentDataUnavailable: If the digits are not 6-9 bare digits.
        """
        id_type = target.id_type
        id_digits = target.id_digits
        if not isinstance(id_type, str) or not id_type.strip():
            raise IdTypeMissing(details={"stage": "document"})
        if id_type not in options.id_types:
            raise IdTypeUnsupported(details={"stage": "document"})
        if not isinstance(id_digits, str) or not _ID_DIGITS_RE.fullmatch(
            id_digits
        ):
            raise PaymentDataUnavailable(
                "Identity document digits must be 6-9 bare digits"
            )
        await self._expect(self.selectors["id_type"])
        await self._page.select_option(self.selectors["id_type"], id_type)
        await self._expect(self.selectors["id_digits"])
        await self._page.fill(self.selectors["id_digits"], id_digits)

    async def _select_destination_bank(
        self, target: BanescoPaymentTarget, options: PaymentFormOptions
    ) -> None:
        """Select the single reviewed live destination-bank option.

        Args:
            target: Payment target carrying the numeric code.
            options: Live form options.

        Raises:
            UnsupportedDestinationBank: If the code is not reviewed or its live
                label changed.
            DestinationBankAmbiguous: If more than one option matches.
        """
        matched = match_destination_bank_option(
            target.destination_bank_code, options.destination_banks
        )
        selector = self.selectors["destination_bank"]
        await self._expect(selector)
        await self._page.select_option(
            selector, matched.value or matched.numeric_code
        )

    async def _fill_amount(self, target: BanescoPaymentTarget) -> None:
        """Render and fill the Decimal VES amount.

        Args:
            target: Payment target.

        Raises:
            PaymentDataUnavailable: If the amount is not a positive finite
                ``Decimal``.
        """
        amount = target.amount_ves
        if (
            not isinstance(amount, Decimal)
            or not amount.is_finite()
            or amount <= 0
        ):
            raise PaymentDataUnavailable(
                "Amount must be a positive finite Decimal"
            )
        await self._expect(self.selectors["amount"])
        await self._page.fill(self.selectors["amount"], format_ves_amount(amount))

    async def _fill_concept(self, target: BanescoPaymentTarget) -> None:
        """Fill the non-empty concept text.

        Args:
            target: Payment target.

        Raises:
            PaymentDataUnavailable: If the concept is absent.
        """
        concept = target.concept
        if not isinstance(concept, str) or not concept.strip():
            raise PaymentDataUnavailable("Concept text is required")
        await self._expect(self.selectors["concept"])
        await self._page.fill(self.selectors["concept"], concept)

    async def _fill_beneficiary_name(
        self, beneficiary_name: str | None
    ) -> None:
        """Fill the optional beneficiary name control only when supplied.

        Args:
            beneficiary_name: Counterparty name, or ``None`` to leave it empty.
        """
        if beneficiary_name is None:
            return
        name = beneficiary_name.strip()
        if not name:
            return
        await self._expect(self.selectors["beneficiary_name"])
        await self._page.fill(self.selectors["beneficiary_name"], name)

    async def submit_first(self) -> None:
        """Perform the first ``Pagar`` submit exactly once.

        The real ``#enviar`` button lives inside the ``#ModalValidarDuplicidad``
        Bootstrap modal, which is normally hidden until the duplicate-validation
        trigger (``#Duplicidad``) is pressed.  In some states the trigger
        navigates straight to the confirmation screen instead, so the submit is
        only pressed when the modal actually exposes it; the caller then asserts
        the confirmation screen.

        Raises:
            BanescoLayoutUnexpected: When the duplicate trigger is absent.
        """
        await self._expect(self.selectors["duplicate_check"])
        await self._page.click(self.selectors["duplicate_check"])
        await self._page.wait_for_load_state("domcontentloaded")
        if await self._probe(self.selectors["submit"], timeout_ms=3000):
            await self._page.click(self.selectors["submit"])
            await self._page.wait_for_load_state("domcontentloaded")


class ConfirmationPage(BasePage):
    """The Pago Móvil confirmation screen and its second ``Pagar`` submit."""

    screen = Screen.CONFIRMATION
    stage = "confirmation"
    selectors = CONFIRMATION_SELECTORS

    def __init__(self, page: PageLike, *, timeout_ms: int = 30000) -> None:
        """Store the page and reset the observation/submit guards.

        Args:
            page: Narrow page implementation.
            timeout_ms: Explicit interaction timeout.
        """
        super().__init__(page, timeout_ms=timeout_ms)
        self._observed = False
        self._submitted = False

    async def assert_confirmation(self) -> None:
        """Observe the confirmation screen before any second submit.

        The observation is anchored on ``#butonPago``; a missing or unexpected
        confirmation raises a typed layout failure and leaves the second submit
        unavailable.

        Raises:
            PaymentRailError: A typed error page or
                :class:`BanescoLayoutUnexpected` when the confirmation is
                absent.
        """
        await self._expect(self.selectors["submit"])
        await self._await_screen(self.screen)
        self._observed = True

    async def submit_second(self) -> None:
        """Perform the second ``Pagar`` only after an observed confirmation.

        Raises:
            BanescoLayoutUnexpected: If the confirmation was not observed or the
                second submit already ran.  No click is recorded in either case.
        """
        if not self._observed:
            raise BanescoLayoutUnexpected(
                "Confirmation screen was not observed before the second submit",
                details={"stage": self.stage},
            )
        if self._submitted:
            raise BanescoLayoutUnexpected(
                "Second submit was already performed",
                details={"stage": self.stage},
            )
        await self._expect(self.selectors["submit"])
        await self._page.click(self.selectors["submit"])
        await self._page.wait_for_load_state("domcontentloaded")
        self._submitted = True


class ClaveDinamicaPage(BasePage):
    """The Clave Dinámica screen, detected only (no fill and no submit)."""

    screen = Screen.CLAVE_DINAMICA
    stage = "clave"
    selectors = CLAVE_DINAMICA_SELECTORS

    async def assert_present(self) -> None:
        """Detect the clave form by its ``#otp`` control and pinned length.

        This page object deliberately exposes no way to fill ``#otp`` or click
        ``#acceptar``; the operator key handoff is out of this slice.

        Raises:
            BanescoLayoutUnexpected: If the OTP control is missing or does not
                pin ``maxlength="8"``.
        """
        await self._expect(self.selectors["otp"])
        markup = await self._page.content()
        if not _input_tag_has_maxlength(
            markup, "otp", CLAVE_DINAMICA_INPUT_MAXLENGTH
        ):
            raise BanescoLayoutUnexpected(
                "Clave Dinamica form does not match the reviewed shape",
                details={"stage": self.stage},
            )

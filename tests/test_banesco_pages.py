"""Pure page-contract tests over the recorded Banesco DOM fixtures.

Every test here is offline: the fixtures under ``tests/fixtures/banesco/`` are
sanitized synthetic markup, no browser is started, no socket is opened and no
real credential is read.  The page-contract module is asserted to import
without Playwright.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.exceptions import (
    BanescoAccountLocked,
    BanescoAdModalBlocked,
    BanescoCaptchaDetected,
    BanescoLayoutUnexpected,
    BanescoLoginFailed,
    BanescoMaintenance,
    BanescoSessionExpired,
    DestinationBankAmbiguous,
    IdTypeMissing,
    IdTypeUnsupported,
    PaymentDataUnavailable,
    UnsupportedDestinationBank,
    UnsupportedPhonePrefix,
)
from app.domain.banesco import (
    BanescoBankOption,
    BanescoPaymentTarget,
    QuestionKey,
    resolve_question_keys,
)
from app.infrastructure.clients.banesco_pages import (
    CLAVE_DINAMICA_INPUT_MAXLENGTH,
    CLAVE_DINAMICA_MAX_DIGITS,
    CLAVE_DINAMICA_MIN_DIGITS,
    LOGIN_FRAME_NAME,
    MENU_SELECTORS,
    PAGOMOVIL_FRAME_NAME,
    SCREEN_FIXTURES,
    SELECTORS_BY_SCREEN,
    AdModalPage,
    BasePage,
    ClaveDinamicaPage,
    ConfirmationPage,
    ConfirmationSummary,
    LoginPage,
    MenuPage,
    PagoMovilFormPage,
    PasswordPage,
    PaymentFormOptions,
    QuestionAnswer,
    Screen,
    SecurityQuestionsPage,
    detect_login_error,
    detect_screen,
    extract_question_texts,
    extract_select_options,
    parse_bank_options,
    parse_confirmation_summary,
    parse_id_types,
    parse_phone_prefixes,
)

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "banesco"
_BANK_OPTIONS = _FIXTURE_DIR / "bank_options.json"

_TEXT_FIXTURES = tuple(
    name for name in SCREEN_FIXTURES.values() if name.endswith(".html")
)

_FAKE_ANSWERS = {
    QuestionKey.HOBBIE: "synthetic-hobby-answer",
    QuestionKey.CHILDHOOD_HERO: "synthetic-hero-answer",
    QuestionKey.FAVOURITE_DESSERT: "synthetic-dessert-answer",
    QuestionKey.COUNTRY_TO_VISIT: "synthetic-country-answer",
}


def _read_fixture(name: str) -> str:
    """Read one recorded fixture as UTF-8 markup.

    Args:
        name: File name under ``tests/fixtures/banesco``.

    Returns:
        The fixture markup.
    """
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


def _selector_is_present(markup: str, selector: str) -> bool:
    """Return whether a simple ``#id`` or ``[name="..."]`` selector is present.

    Comments are intentionally *not* stripped: the recorded capture keeps
    legacy/commented controls that the live evidence still names.

    Args:
        markup: Raw fixture markup.
        selector: Simple CSS selector used by the page contracts.

    Returns:
        ``True`` when the selector's attribute exists.
    """
    if selector.startswith("#"):
        element_id = re.escape(selector[1:])
        pattern = rf"""\bid\s*=\s*["']{element_id}["']"""
        return re.search(pattern, markup, re.IGNORECASE) is not None
    match = re.fullmatch(r"""\[name\s*=\s*["'](.+?)["']\]""", selector)
    if match is None:
        raise AssertionError(f"unsupported selector in contract: {selector}")
    name = re.escape(match.group(1))
    pattern = rf"""\bname\s*=\s*["']{name}["']"""
    return re.search(pattern, markup, re.IGNORECASE) is not None


def test_recorded_fixture_catalog_is_complete_and_sanitized() -> None:
    """The S2 catalog exists, is non-empty and drops the temporary live file."""
    assert len(SCREEN_FIXTURES) == 8
    assert len(set(SCREEN_FIXTURES.values())) == 8
    for screen, name in SCREEN_FIXTURES.items():
        path = _FIXTURE_DIR / name
        assert path.is_file(), f"missing fixture for {screen}: {name}"
        assert path.stat().st_size > 0
    assert _BANK_OPTIONS.is_file()
    assert not (_FIXTURE_DIR / "bank_options_live.json").exists()

    all_markup = "\n".join(_read_fixture(name) for name in _TEXT_FIXTURES)
    # The sanitization contract is materialized: synthetic PII only.
    assert "04141234567" in all_markup
    assert "NOMBRE APELLIDO EJEMPLO" in all_markup
    assert "Cuenta Electrónica" in all_markup


def test_every_selector_exists_in_its_fixture() -> None:
    """Every pinned selector is present in the fixture for its screen."""
    for screen, selectors in SELECTORS_BY_SCREEN.items():
        markup = _read_fixture(SCREEN_FIXTURES[screen])
        for role, selector in selectors.items():
            assert _selector_is_present(
                markup, selector
            ), f"{screen.value}.{role} -> {selector} not in {SCREEN_FIXTURES[screen]}"


def test_page_shape_parsers_over_recorded_html() -> None:
    """The pure parsers read the recorded Pago Móvil option sets."""
    markup = _read_fixture(SCREEN_FIXTURES[Screen.PAGOMOVIL_FORM])

    phone_options = extract_select_options(markup, "pref")
    assert tuple(option.value for option in phone_options) == (
        "0416",
        "0426",
        "0414",
        "0424",
        "0412",
        "0422",
    )
    assert parse_phone_prefixes(markup) == (
        "0416",
        "0426",
        "0414",
        "0424",
        "0412",
        "0422",
    )

    id_options = extract_select_options(markup, "NacCli")
    assert tuple(option.value for option in id_options) == ("V", "E", "J", "G", "R")
    assert parse_id_types(markup) == ("V", "E", "J", "G", "R")

    bank_options = extract_select_options(markup, "banco")
    # The first option is the empty "Seleccione" placeholder.
    assert bank_options[0].value == ""
    assert len(bank_options) == 26
    assert len(parse_bank_options(markup)) == 25


def test_question_texts_resolve_independent_of_order() -> None:
    """Two visible questions are read by text, never by position."""
    markup = _read_fixture(SCREEN_FIXTURES[Screen.SECURITY_QUESTIONS])
    texts = extract_question_texts(markup)
    assert texts == ("hobbie", "¿Qué país siempre has querido conocer?")

    assert resolve_question_keys(list(texts), answers=_FAKE_ANSWERS) == (
        QuestionKey.HOBBIE,
        QuestionKey.COUNTRY_TO_VISIT,
    )
    assert resolve_question_keys(
        list(reversed(texts)), answers=_FAKE_ANSWERS
    ) == (QuestionKey.COUNTRY_TO_VISIT, QuestionKey.HOBBIE)


def test_bank_options_parse_equals_reviewed_snapshot() -> None:
    """The parsed ``#banco`` options equal the reviewed versioned snapshot."""
    snapshot = json.loads(_BANK_OPTIONS.read_text(encoding="utf-8"))
    expected = {
        entry["numeric_code"]: BanescoBankOption(
            numeric_code=entry["numeric_code"],
            label=entry["label"],
            value=entry["value"],
        )
        for entry in snapshot["options"]
    }
    parsed = parse_bank_options(
        _read_fixture(SCREEN_FIXTURES[Screen.PAGOMOVIL_FORM])
    )
    assert {option.numeric_code: option for option in parsed} == expected
    assert len(parsed) == 25
    assert snapshot["version"] == 2
    assert "BANCO DE VENEZUELA" in {option.label for option in parsed}
    assert "BANESCO BANCO UNIVERSAL S.A.C.A." in {
        option.label for option in parsed
    }


def test_confirmation_parser_extracts_expected_fields() -> None:
    """The confirmation summary exposes every reviewed field as text/Decimal."""
    summary = parse_confirmation_summary(
        _read_fixture(SCREEN_FIXTURES[Screen.CONFIRMATION])
    )
    assert isinstance(summary, ConfirmationSummary)
    # The fixture's synthetic document value is deliberately fake.
    assert summary.beneficiary_name == ""
    assert summary.beneficiary_phone == "04141234567"
    assert summary.beneficiary_document == "V 12345678"
    assert summary.destination_bank == "BANESCO BANCO UNIVERSAL S.A.C.A."
    assert summary.amount_ves == Decimal("1500.00")
    assert summary.concept == "pago"


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("01_login_user.html", Screen.LOGIN_USER),
        ("02_security_questions.html", Screen.SECURITY_QUESTIONS),
        ("03_login_password.html", Screen.LOGIN_PASSWORD),
        ("04_login_error.html", Screen.LOGIN_ERROR),
        ("05_dashboard_modal.html", Screen.DASHBOARD_MODAL),
        ("06_pagomovil_form.html", Screen.PAGOMOVIL_FORM),
        ("07_confirmation.html", Screen.CONFIRMATION),
        ("08_clave_dinamica.html", Screen.CLAVE_DINAMICA),
    ],
)
def test_screen_detection_for_all_fixtures(
    fixture_name: str, expected: Screen
) -> None:
    """Each recorded fixture is classified as exactly one screen."""
    assert detect_screen(_read_fixture(fixture_name)) is expected


def test_screen_detection_returns_unknown_for_unrelated_markup() -> None:
    """Unrelated markup is never guessed into a known screen."""
    assert detect_screen("<html><body><p>hello</p></body></html>") is Screen.UNKNOWN


def test_frame_names_and_clave_bounds_are_pinned() -> None:
    """Frame names and Clave Dinámica bounds match the live capture."""
    assert LOGIN_FRAME_NAME == "ctl00_cp_frmAplicacion"
    assert PAGOMOVIL_FRAME_NAME == "embedded"
    assert (CLAVE_DINAMICA_MIN_DIGITS, CLAVE_DINAMICA_MAX_DIGITS) == (6, 8)
    assert CLAVE_DINAMICA_INPUT_MAXLENGTH == 8
    markup = _read_fixture(SCREEN_FIXTURES[Screen.CLAVE_DINAMICA])
    assert 'id="otp"' in markup
    assert 'maxlength="8"' in markup


def test_fixtures_contain_no_canary_or_real_looking_pii() -> None:
    """Fixtures carry no canary secret, credential or real-looking PII."""
    canary = "zz-canary-" + secrets.token_hex(16) + "-zz"
    forbidden = (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"Bearer\s+[A-Za-z0-9._-]{10,}"),
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        re.compile(r"\b(?:\d[ -]?){16,}\b"),
    )
    sensitive_ids = (
        "__VIEWSTATE",
        "__VIEWSTATEGENERATOR",
        "__EVENTVALIDATION",
        "huella",
        "lblURL",
        "lnkSitioSeguro",
        "lnkCandado",
    )
    for name in _TEXT_FIXTURES:
        text = _read_fixture(name)
        assert canary not in text
        for pattern in forbidden:
            assert pattern.search(text) is None, f"{name} matched {pattern.pattern}"
        for element_id in sensitive_ids:
            for match in re.finditer(
                rf'id="{re.escape(element_id)}"[^>]*value="([^"]*)"', text
            ):
                assert match.group(1) == "REDACTED", f"{name}:{element_id}"
        for tag in re.findall(r"""<input[^>]*type=["']password["'][^>]*>""", text, re.I):
            assert "value=" not in tag.lower(), f"{name}: password prefilled"


def _selector_present(markup: str, selector: str) -> bool:
    """Return whether a fake page can resolve a simple Playwright selector.

    Supports the selector shapes the page objects use: a comma-separated list
    of ``#id`` anchors, a bare ``#id`` and ``text=...``.

    Args:
        markup: Markup currently served by the fake page.
        selector: Selector requested by a page object.

    Returns:
        ``True`` when every comma-separated alternative is satisfied by at
        least one part.
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
        else:  # pragma: no cover - guards the test fake against new selectors
            raise AssertionError(f"fake page cannot resolve selector: {selector}")
    return False


class _RecordingPage:
    """``PageLike`` double that serves markup and records every action.

    Attributes:
        markup: Markup currently served by :meth:`content`.
        url: Last URL passed to :meth:`goto`.
        actions: Ordered tuples describing every recorded action.
        selector_checks: Selectors passed to :meth:`wait_for_selector`.
        transitions: Markup to serve after a matching selector is clicked.
    """

    def __init__(self, markup: str = "") -> None:
        """Initialize the fake with optional markup.

        Args:
            markup: Markup returned by :meth:`content`.
        """
        self.markup = markup
        self.url = ""
        self.actions: list[tuple[Any, ...]] = []
        self.selector_checks: list[str] = []
        self.transitions: dict[str, str] = {}

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
        """Record a click and apply any transition for the selector.

        Args:
            selector: Element selector.
        """
        self.actions.append(("click", selector))
        if selector in self.transitions:
            self.markup = self.transitions[selector]

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
        """Record a selector wait and fail when the selector is absent.

        Args:
            selector: Selector requested.
            timeout: Optional timeout in milliseconds.

        Returns:
            An opaque sentinel representing a resolved element.

        Raises:
            LookupError: When the selector is not in the served markup.
        """
        self.selector_checks.append(selector)
        if not _selector_present(self.markup, selector):
            raise LookupError(f"selector not found: {selector}")
        return object()


def _security_markup(first_label: str, second_label: str) -> str:
    """Build minimal two-question markup with adjacent answer inputs.

    Args:
        first_label: Visible first question text.
        second_label: Visible second question text.

    Returns:
        Synthetic security-question markup.
    """
    return (
        "<html><body><form>"
        "<table><tr><td>"
        f'<span id="lblPrimeraP">{first_label}</span>'
        '</td></tr><tr><td><input id="txtPrimeraR" type="password"></td></tr></table>'
        "<table><tr><td>"
        f'<span id="lblSegundaP">{second_label}</span>'
        '</td></tr><tr><td><input id="txtSegundaR" type="password"></td></tr></table>'
        '<input id="bAceptar" type="submit">'
        "</form></body></html>"
    )


def _error_markup(message: str) -> str:
    """Build minimal login-error markup carrying a visible message.

    Args:
        message: Visible error text.

    Returns:
        Synthetic error-page markup.
    """
    return (
        "<html><body><form>"
        f'<span id="lblMensaje">{message}</span>'
        '<input id="bAceptar" type="submit">'
        "</form></body></html>"
    )


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


async def test_page_like_fake_records_goto_fill_click_and_select() -> None:
    """The narrow ``PageLike`` shape drives a recording fake end to end."""
    page = _RecordingPage("<html><body><div id='menu'>Menu</div></body></html>")

    await page.goto("https://example.test/login")
    await page.fill("#txtUsuario", "synthetic-user")
    await page.click("#bAceptar")
    await page.select_option("#pref", "0414")

    assert page.actions == [
        ("goto", "https://example.test/login"),
        ("fill", "#txtUsuario", "synthetic-user"),
        ("click", "#bAceptar"),
        ("select_option", "#pref", "0414"),
    ]
    assert page.url == "https://example.test/login"

    base = BasePage(page)
    await base._expect("#menu")
    assert page.selector_checks == ["#menu"]
    with pytest.raises(BanescoLayoutUnexpected):
        await base._await_screen(Screen.PAGOMOVIL_FORM)


async def test_login_questions_and_clave_page_objects_fail_closed() -> None:
    """Login, question and clave page objects submit once and fail closed."""
    login_page = _RecordingPage(_read_fixture(SCREEN_FIXTURES[Screen.LOGIN_USER]))
    login_page.transitions["#bAceptar"] = _read_fixture(
        SCREEN_FIXTURES[Screen.SECURITY_QUESTIONS]
    )
    login = LoginPage(login_page, login_url="https://www.banesco.com/", timeout_ms=500)
    await login.open()
    await login.submit_user("synthetic-user")

    assert ("goto", "https://www.banesco.com/") in login_page.actions
    assert ("fill", "#txtUsuario", "synthetic-user") in login_page.actions
    assert ("click", "#bAceptar") in login_page.actions

    question_page = _RecordingPage(
        _read_fixture(SCREEN_FIXTURES[Screen.SECURITY_QUESTIONS])
    )
    questions = SecurityQuestionsPage(question_page, timeout_ms=500)
    pairs = await questions.read_questions()
    assert [pair.label_id for pair in pairs] == ["lblPrimeraP", "lblSegundaP"]
    assert [pair.answer_selector for pair in pairs] == ["#txtPrimeraR", "#txtSegundaR"]
    assert all(isinstance(pair, QuestionAnswer) for pair in pairs)

    keys = await questions.answer(_FAKE_ANSWERS)
    assert keys == (QuestionKey.HOBBIE, QuestionKey.COUNTRY_TO_VISIT)
    assert (
        "fill",
        "#txtPrimeraR",
        _FAKE_ANSWERS[QuestionKey.HOBBIE],
    ) in question_page.actions
    assert (
        "fill",
        "#txtSegundaR",
        _FAKE_ANSWERS[QuestionKey.COUNTRY_TO_VISIT],
    ) in question_page.actions
    question_page.transitions["#bAceptar"] = _read_fixture(
        SCREEN_FIXTURES[Screen.LOGIN_PASSWORD]
    )
    await questions.submit()
    assert (
        "click",
        "#bAceptar",
    ) in question_page.actions

    # Unknown, ambiguous and missing-answer challenges fail closed.
    unknown_page = _RecordingPage(
        _security_markup("unknown synthetic question", "hobbie")
    )
    with pytest.raises(BanescoLayoutUnexpected):
        await SecurityQuestionsPage(unknown_page, timeout_ms=500).answer(_FAKE_ANSWERS)

    repeated_page = _RecordingPage(_security_markup("hobbie", "hobbie"))
    with pytest.raises(BanescoLayoutUnexpected):
        await SecurityQuestionsPage(repeated_page, timeout_ms=500).answer(
            _FAKE_ANSWERS
        )

    with pytest.raises(BanescoLayoutUnexpected):
        await SecurityQuestionsPage(
            _RecordingPage(_security_markup("hobbie", "unknown")), timeout_ms=500
        ).answer(_FAKE_ANSWERS)

    ambiguous_page = _RecordingPage(
        "<html><body><table>"
        '<span id="lblPrimeraP">hobbie</span>'
        '<span id="lblSegundaP">¿Qué país siempre has querido conocer?</span>'
        '<input id="txtPrimeraR" type="password">'
        "</table></body></html>"
    )
    with pytest.raises(BanescoLayoutUnexpected):
        await SecurityQuestionsPage(ambiguous_page, timeout_ms=500).read_questions()

    single_page = _RecordingPage(
        "<html><body><table>"
        '<span id="lblPrimeraP">hobbie</span>'
        '<input id="txtPrimeraR" type="password">'
        "</table></body></html>"
    )
    with pytest.raises(BanescoLayoutUnexpected):
        await SecurityQuestionsPage(single_page, timeout_ms=500).read_questions()

    missing_answer_page = _RecordingPage(
        _read_fixture(SCREEN_FIXTURES[Screen.SECURITY_QUESTIONS])
    )
    with pytest.raises(BanescoLayoutUnexpected):
        await SecurityQuestionsPage(missing_answer_page, timeout_ms=500).answer(
            {QuestionKey.HOBBIE: None, QuestionKey.COUNTRY_TO_VISIT: "x"}
        )

    # Password submission fills once and clicks once, success and rejection.
    password_page = _RecordingPage(
        _read_fixture(SCREEN_FIXTURES[Screen.LOGIN_PASSWORD])
    )
    password_page.transitions["#bAceptar"] = _read_fixture(
        SCREEN_FIXTURES[Screen.DASHBOARD_MODAL]
    )
    await PasswordPage(password_page, timeout_ms=500).submit_clave("synthetic-clave")
    password_clicks = [
        action
        for action in password_page.actions
        if action[0] == "click" and action[1] == "#bAceptar"
    ]
    assert len(password_clicks) == 1
    assert ("fill", "#txtClave", "synthetic-clave") in password_page.actions

    rejected_page = _RecordingPage(
        _read_fixture(SCREEN_FIXTURES[Screen.LOGIN_PASSWORD])
    )
    rejected_page.transitions["#bAceptar"] = _read_fixture(
        SCREEN_FIXTURES[Screen.LOGIN_ERROR]
    )
    with pytest.raises(BanescoLoginFailed):
        await PasswordPage(rejected_page, timeout_ms=500).submit_clave(
            "synthetic-clave"
        )
    rejected_clicks = [
        action
        for action in rejected_page.actions
        if action[0] == "click" and action[1] == "#bAceptar"
    ]
    assert len(rejected_clicks) == 1


async def test_ad_modal_present_absent_and_undismissable() -> None:
    """Modal dismissal is one-shot, absent is a no-op, blocked is typed."""
    present_page = _RecordingPage(
        _read_fixture(SCREEN_FIXTURES[Screen.DASHBOARD_MODAL])
    )
    present_page.transitions["#closeButton"] = _MENU_MARKUP
    modal = AdModalPage(present_page, timeout_ms=500)
    assert await modal.dismiss_if_present() is True
    close_clicks = [
        action
        for action in present_page.actions
        if action[0] == "click" and action[1] == "#closeButton"
    ]
    assert len(close_clicks) == 1
    await modal.assert_menu_reachable()

    absent_page = _RecordingPage(_MENU_MARKUP)
    assert await AdModalPage(absent_page, timeout_ms=500).dismiss_if_present() is False
    assert not [action for action in absent_page.actions if action[0] == "click"]

    undismissable = _RecordingPage(
        '<html><body><div id="modalAnuncios">'
        '<button id="actionButton">Comenzar</button>'
        "</div></body></html>"
    )
    with pytest.raises(BanescoAdModalBlocked):
        await AdModalPage(undismissable, timeout_ms=500).dismiss_if_present()

    with pytest.raises(BanescoAdModalBlocked):
        await AdModalPage(
            _RecordingPage(_read_fixture(SCREEN_FIXTURES[Screen.DASHBOARD_MODAL])),
            timeout_ms=500,
        ).assert_menu_reachable()

    menu_page = _RecordingPage(_MENU_MARKUP)
    menu_page.transitions[MENU_SELECTORS["pago_movil"]] = _SUBMENU_MARKUP
    menu_page.transitions[MENU_SELECTORS["enviar_pago"]] = _read_fixture(
        SCREEN_FIXTURES[Screen.PAGOMOVIL_FORM]
    )
    await MenuPage(menu_page, timeout_ms=500).open_pago_movil()
    assert ("click", MENU_SELECTORS["pago_movil"]) in menu_page.actions
    assert ("click", MENU_SELECTORS["enviar_pago"]) in menu_page.actions
    assert detect_screen(menu_page.markup) is Screen.PAGOMOVIL_FORM

    wrong_product = _RecordingPage(_MENU_MARKUP)
    wrong_product.transitions[MENU_SELECTORS["pago_movil"]] = _SUBMENU_MARKUP
    wrong_product.transitions[MENU_SELECTORS["enviar_pago"]] = (
        "<html><body><p>Otro producto</p></body></html>"
    )
    with pytest.raises(BanescoLayoutUnexpected):
        await MenuPage(wrong_product, timeout_ms=500).open_pago_movil()


@pytest.mark.parametrize(
    ("message", "expected_type", "expected_code"),
    [
        ("Complete el captcha para continuar", BanescoCaptchaDetected, "banesco_captcha_detected"),
        ("Su cuenta bloqueada por medidas de seguridad", BanescoAccountLocked, "banesco_account_locked"),
        ("Servicio en mantenimiento programado", BanescoMaintenance, "banesco_maintenance"),
        ("Su sesión expirada, vuelva a ingresar", BanescoSessionExpired, "banesco_session_expired"),
        ("Usuario o clave inválida", BanescoLoginFailed, "banesco_login_failed"),
    ],
)
def test_captcha_locked_maintenance_and_session_expiry_are_typed(
    message: str, expected_type: type[Any], expected_code: str
) -> None:
    """Error pages map to the pinned typed codes with a safe stage detail."""
    error = detect_login_error(_error_markup(message), stage="login")

    assert isinstance(error, expected_type)
    assert error.code == expected_code
    assert error.service == "banesco_online"
    assert error.details == {"stage": "login"}
    assert message not in str(error.details)


def test_recorded_login_error_fixture_is_typed_as_login_failed() -> None:
    """The recorded wrong-credential page is a login failure, not a lockout."""
    error = detect_login_error(
        _read_fixture(SCREEN_FIXTURES[Screen.LOGIN_ERROR]), stage="login"
    )

    assert isinstance(error, BanescoLoginFailed)
    assert error.code == "banesco_login_failed"
    assert error.details == {"stage": "login"}


async def test_login_submission_raises_typed_error_page() -> None:
    """A captcha page returned after the user submit surfaces as typed."""
    page = _RecordingPage(_read_fixture(SCREEN_FIXTURES[Screen.LOGIN_USER]))
    page.transitions["#bAceptar"] = _error_markup("Complete el captcha")
    login = LoginPage(page, login_url="https://www.banesco.com/", timeout_ms=500)

    with pytest.raises(BanescoCaptchaDetected) as excinfo:
        await login.submit_user("synthetic-user")

    assert excinfo.value.code == "banesco_captcha_detected"
    assert excinfo.value.details == {"stage": "login"}


def test_page_contracts_import_without_playwright() -> None:
    """Importing the page contracts never pulls Playwright or a browser."""
    project_root = Path(__file__).resolve().parent.parent
    code = (
        "import sys; import app.infrastructure.clients.banesco_pages; "
        "assert 'playwright' not in sys.modules, 'playwright imported'; "
        "print('PAGES PURE OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "PAGES PURE OK" in result.stdout


def _payment_target(**overrides: Any) -> BanescoPaymentTarget:
    """Build a valid, synthetic payment target for the fake page.

    Args:
        **overrides: Field values replacing the valid defaults.

    Returns:
        A validated :class:`BanescoPaymentTarget`.
    """
    values: dict[str, Any] = {
        "phone_prefix": "0414",
        "phone_rest": "1234567",
        "id_type": "V",
        "id_digits": "12345678",
        "destination_bank_code": "0102",
        "destination_bank_option": "BANCO DE VENEZUELA",
        "amount_ves": Decimal("100.00"),
        "concept": "Pago",
    }
    values.update(overrides)
    return BanescoPaymentTarget(**values)


def _raw_target(**overrides: Any) -> SimpleNamespace:
    """Build an unvalidated target-like object to exercise page-level guards.

    The page objects must fail closed even if a caller hands them a target that
    bypassed the frozen DTO constructor.

    Args:
        **overrides: Field values replacing the valid defaults.

    Returns:
        A duck-typed target namespace.
    """
    values: dict[str, Any] = {
        "phone_prefix": "0414",
        "phone_rest": "1234567",
        "id_type": "V",
        "id_digits": "12345678",
        "destination_bank_code": "0102",
        "destination_bank_option": "BANCO DE VENEZUELA",
        "amount_ves": Decimal("100.00"),
        "concept": "Pago",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _clicks(page: _RecordingPage, selector: str) -> list[tuple[Any, ...]]:
    """Return recorded clicks for one selector.

    Args:
        page: Recording fake.
        selector: Element selector.

    Returns:
        Matching recorded click actions.
    """
    return [
        action
        for action in page.actions
        if action[0] == "click" and action[1] == selector
    ]


async def test_payment_form_reads_live_options_and_fills_two_controls() -> None:
    """The form reads live option sets and splits the phone across two controls."""
    form_markup = _read_fixture(SCREEN_FIXTURES[Screen.PAGOMOVIL_FORM])
    page = _RecordingPage(form_markup)
    form = PagoMovilFormPage(page, timeout_ms=500)

    options = await form.read_option_sets()
    assert isinstance(options, PaymentFormOptions)
    assert options.phone_prefixes == (
        "0416",
        "0426",
        "0414",
        "0424",
        "0412",
        "0422",
    )
    assert options.id_types == ("V", "E", "J", "G", "R")
    assert len(options.destination_banks) == 25
    assert len(options.debit_accounts) == 1
    assert "Cuenta Electrónica" in options.debit_accounts[0].label

    await form.fill(_payment_target())

    account = options.debit_accounts[0]
    assert ("select_option", "#inputGroupSelect01", account.value or account.label) in (
        page.actions
    )
    assert ("select_option", "#pref", "0414") in page.actions
    assert ("fill", "#tel", "1234567") in page.actions
    # The full phone is never typed into the remainder control.
    assert ("fill", "#tel", "04141234567") not in page.actions

    page.transitions["#enviar"] = _read_fixture(SCREEN_FIXTURES[Screen.CONFIRMATION])
    await form.submit_first()
    assert len(_clicks(page, "#enviar")) == 1

    # An unoffered live prefix fails closed before any submit.
    unoffered = _RecordingPage(form_markup)
    with pytest.raises(UnsupportedPhonePrefix) as excinfo:
        await PagoMovilFormPage(unoffered, timeout_ms=500).fill(
            _raw_target(phone_prefix="0399")
        )
    assert excinfo.value.code == "unsupported_phone_prefix"
    assert not _clicks(unoffered, "#enviar")

    # A remainder of the wrong length fails closed.
    short_rest = _RecordingPage(form_markup)
    with pytest.raises(PaymentDataUnavailable):
        await PagoMovilFormPage(short_rest, timeout_ms=500).fill(
            _raw_target(phone_rest="123")
        )
    assert not _clicks(short_rest, "#enviar")


async def test_destination_bank_selection_zero_one_many_fails_closed() -> None:
    """The destination bank is matched by reviewed label/code, never by index."""
    form_markup = _read_fixture(SCREEN_FIXTURES[Screen.PAGOMOVIL_FORM])

    supported = _RecordingPage(form_markup)
    await PagoMovilFormPage(supported, timeout_ms=500).fill(
        _payment_target(destination_bank_code="0134")
    )
    assert ("select_option", "#banco", "0134") in supported.actions
    assert not [
        action
        for action in supported.actions
        if action[0] == "select_option" and action[1] == "#banco" and action[2] == ""
    ]

    unknown_code = _RecordingPage(form_markup)
    with pytest.raises(UnsupportedDestinationBank):
        await PagoMovilFormPage(unknown_code, timeout_ms=500).fill(
            _raw_target(destination_bank_code="9999")
        )
    assert not _clicks(unknown_code, "#enviar")

    changed_label = _RecordingPage(
        form_markup.replace(
            '<option value="0102">BANCO DE VENEZUELA</option>',
            '<option value="0102">BANCO DISTINTO</option>',
        )
    )
    with pytest.raises(UnsupportedDestinationBank):
        await PagoMovilFormPage(changed_label, timeout_ms=500).fill(
            _payment_target(destination_bank_code="0102")
        )
    assert ("select_option", "#banco", "0102") not in changed_label.actions

    ambiguous = _RecordingPage(
        form_markup.replace(
            '<option value="0102">BANCO DE VENEZUELA</option>',
            '<option value="0102">BANCO DE VENEZUELA</option>'
            '<option value="0102">BANCO DE VENEZUELA</option>',
        )
    )
    with pytest.raises(DestinationBankAmbiguous):
        await PagoMovilFormPage(ambiguous, timeout_ms=500).fill(
            _payment_target(destination_bank_code="0102")
        )
    assert not _clicks(ambiguous, "#enviar")


async def test_document_amount_and_concept_fill_only_decimal_ves() -> None:
    """Document, amount and concept fill map to the reviewed controls."""
    form_markup = _read_fixture(SCREEN_FIXTURES[Screen.PAGOMOVIL_FORM])
    page = _RecordingPage(form_markup)
    form = PagoMovilFormPage(page, timeout_ms=500)

    await form.fill(_payment_target())

    assert ("select_option", "#NacCli", "V") in page.actions
    assert ("fill", "#ced", "12345678") in page.actions
    assert ("fill", "#monto", "100,00") in page.actions
    assert ("fill", "#concepto", "Pago") in page.actions
    # The optional name control stays untouched with no supplied name.
    assert not [
        action
        for action in page.actions
        if action[0] == "fill" and action[1] == "#nom"
    ]
    # The frequent-payment checkbox is never checked or clicked.
    assert not [
        action
        for action in page.actions
        if action[1] == "#check" and action[0] in ("fill", "click", "select_option")
    ]

    named = _RecordingPage(form_markup)
    await PagoMovilFormPage(named, timeout_ms=500).fill(
        _payment_target(), beneficiary_name="NOMBRE APELLIDO EJEMPLO"
    )
    assert ("fill", "#nom", "NOMBRE APELLIDO EJEMPLO") in named.actions

    missing_letter = _RecordingPage(form_markup)
    with pytest.raises(IdTypeMissing):
        await PagoMovilFormPage(missing_letter, timeout_ms=500).fill(
            _raw_target(id_type="")
        )
    assert not _clicks(missing_letter, "#enviar")

    unsupported_letter = _RecordingPage(form_markup)
    with pytest.raises(IdTypeUnsupported):
        await PagoMovilFormPage(unsupported_letter, timeout_ms=500).fill(
            _raw_target(id_type="P")
        )
    assert not _clicks(unsupported_letter, "#enviar")

    bad_digits = _RecordingPage(form_markup)
    with pytest.raises(PaymentDataUnavailable):
        await PagoMovilFormPage(bad_digits, timeout_ms=500).fill(
            _raw_target(id_digits="12")
        )

    zero_amount = _RecordingPage(form_markup)
    with pytest.raises(PaymentDataUnavailable):
        await PagoMovilFormPage(zero_amount, timeout_ms=500).fill(
            _raw_target(amount_ves=Decimal("0"))
        )
    assert not _clicks(zero_amount, "#enviar")

    float_amount = _RecordingPage(form_markup)
    with pytest.raises(PaymentDataUnavailable):
        await PagoMovilFormPage(float_amount, timeout_ms=500).fill(
            _raw_target(amount_ves=100.0)
        )

    infinite_amount = _RecordingPage(form_markup)
    with pytest.raises(PaymentDataUnavailable):
        await PagoMovilFormPage(infinite_amount, timeout_ms=500).fill(
            _raw_target(amount_ves=Decimal("Infinity"))
        )

    empty_concept = _RecordingPage(form_markup)
    with pytest.raises(PaymentDataUnavailable):
        await PagoMovilFormPage(empty_concept, timeout_ms=500).fill(
            _raw_target(concept="   ")
        )
    assert not _clicks(empty_concept, "#enviar")


async def test_double_pagar_requires_observed_confirmation() -> None:
    """The second Pagar only runs after an observed confirmation screen."""
    form_page = _RecordingPage(_read_fixture(SCREEN_FIXTURES[Screen.PAGOMOVIL_FORM]))
    form_page.transitions["#enviar"] = _read_fixture(
        SCREEN_FIXTURES[Screen.CONFIRMATION]
    )
    await PagoMovilFormPage(form_page, timeout_ms=500).submit_first()
    assert len(_clicks(form_page, "#enviar")) == 1

    confirmation_page = _RecordingPage(
        _read_fixture(SCREEN_FIXTURES[Screen.CONFIRMATION])
    )
    confirmation = ConfirmationPage(confirmation_page, timeout_ms=500)
    await confirmation.assert_confirmation()
    confirmation_page.transitions["#butonPago"] = _read_fixture(
        SCREEN_FIXTURES[Screen.CLAVE_DINAMICA]
    )
    await confirmation.submit_second()
    assert len(_clicks(confirmation_page, "#butonPago")) == 1

    unobserved = _RecordingPage(_read_fixture(SCREEN_FIXTURES[Screen.CONFIRMATION]))
    with pytest.raises(BanescoLayoutUnexpected):
        await ConfirmationPage(unobserved, timeout_ms=500).submit_second()
    assert not _clicks(unobserved, "#butonPago")

    missing_confirmation = _RecordingPage(
        _read_fixture(SCREEN_FIXTURES[Screen.PAGOMOVIL_FORM])
    )
    missing_confirmation.transitions["#enviar"] = (
        "<html><body><p>Pantalla inesperada</p></body></html>"
    )
    await PagoMovilFormPage(missing_confirmation, timeout_ms=500).submit_first()
    with pytest.raises(BanescoLayoutUnexpected):
        await ConfirmationPage(
            missing_confirmation, timeout_ms=500
        ).assert_confirmation()
    assert not _clicks(missing_confirmation, "#butonPago")


async def test_clave_page_detects_form_without_any_submit_surface() -> None:
    """Clave Dinámica detection only; the page exposes no submit surface."""
    assert not hasattr(ClaveDinamicaPage, "submit")
    assert not hasattr(ClaveDinamicaPage, "submit_key")

    clave_page = _RecordingPage(
        _read_fixture(SCREEN_FIXTURES[Screen.CLAVE_DINAMICA])
    )
    clave = ClaveDinamicaPage(clave_page, timeout_ms=500)
    await clave.assert_present()

    assert not [
        action
        for action in clave_page.actions
        if action[0] in ("fill", "click", "select_option")
    ]

    wrong_length = _RecordingPage(
        _read_fixture(SCREEN_FIXTURES[Screen.CLAVE_DINAMICA]).replace(
            'maxlength="8"', 'maxlength="6"'
        )
    )
    with pytest.raises(BanescoLayoutUnexpected):
        await ClaveDinamicaPage(wrong_length, timeout_ms=500).assert_present()

    missing_form = _RecordingPage("<html><body><p>otra pantalla</p></body></html>")
    with pytest.raises(BanescoLayoutUnexpected):
        await ClaveDinamicaPage(missing_form, timeout_ms=500).assert_present()

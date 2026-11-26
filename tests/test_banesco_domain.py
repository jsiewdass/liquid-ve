"""Pure-domain tests for the Banesco payment rail.

Every test here runs offline: no browser, no network, no settings object and no
real credential.  Values are synthetic fixtures only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.exceptions import (
    BanescoLayoutUnexpected,
    BanescoLoginFailed,
    ClaveDinamicaRejected,
    DestinationBankAmbiguous,
    IdTypeMissing,
    IdTypeUnsupported,
    LiquidVEException,
    PaymentDataUnavailable,
    PaymentRailError,
    UnsupportedDestinationBank,
    UnsupportedPhonePrefix,
)
from app.domain.banesco import (
    BANESCO_DESTINATION_BANK_OPTIONS,
    BANESCO_DESTINATION_BANK_OPTIONS_VERSION,
    CLAVE_DINAMICA_MAX_DIGITS,
    CLAVE_DINAMICA_MIN_DIGITS,
    QUESTION_ANSWER_SETTINGS,
    QUESTION_EXAMPLE_LABELS,
    BanescoBankOption,
    BanescoPaymentTarget,
    PAYMENT_RAIL_ERROR_CODES,
    QuestionKey,
    format_ves_amount,
    match_destination_bank_option,
    normalize_question,
    project_banesco_document,
    project_document,
    project_phone,
    resolve_question_keys,
    validate_clave_dinamica,
)

_BANK_OPTIONS_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "banesco" / "bank_options.json"
)

_FAKE_ANSWERS = {
    QuestionKey.HOBBIE: "synthetic-hobby-answer",
    QuestionKey.CHILDHOOD_HERO: "synthetic-hero-answer",
    QuestionKey.FAVOURITE_DESSERT: "synthetic-dessert-answer",
    QuestionKey.COUNTRY_TO_VISIT: "synthetic-country-answer",
}


def _two_question_texts(
    first: QuestionKey = QuestionKey.HOBBIE,
    second: QuestionKey = QuestionKey.FAVOURITE_DESSERT,
) -> list[str]:
    """Return two generic question labels for the requested keys.

    Args:
        first: First question key.
        second: Second question key.

    Returns:
        Two labels drawn from the sanitized example wording.
    """
    return [QUESTION_EXAMPLE_LABELS[first], QUESTION_EXAMPLE_LABELS[second]]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("What is your favourite hobby?", "what is your favourite hobby"),
        ("  WHAT   is your FAVOURITE hobby ...  ", "what is your favourite hobby"),
        ("\u00bfCu\u00e1l es tu pasatiempo favorito?", "\u00bfcual es tu pasatiempo favorito"),
        ("Which country would you like to visit:", "which country would you like to visit"),
        ("Who was your childhood hero.", "who was your childhood hero"),
    ],
)
def test_question_normalization_is_stable(raw: str, expected: str) -> None:
    """Normalize labels without guessing any content."""
    assert normalize_question(raw) == expected


def test_question_normalization_rejects_non_text() -> None:
    """A non-string label is a layout failure, never a silent skip."""
    with pytest.raises(BanescoLayoutUnexpected):
        normalize_question(None)  # type: ignore[arg-type]


def test_question_normalization_and_exactly_two_of_four_validation() -> None:
    """Accept exactly two distinct recognized questions and nothing else."""
    labels = _two_question_texts()
    resolved = resolve_question_keys(
        [labels[0].upper(), labels[1].replace("?", " : ")],
        answers=_FAKE_ANSWERS,
    )
    assert resolved == (QuestionKey.HOBBIE, QuestionKey.FAVOURITE_DESSERT)

    # Order must not matter and is not assumed by the resolver.
    reversed_labels = [labels[1], labels[0]]
    assert set(
        resolve_question_keys(reversed_labels, answers=_FAKE_ANSWERS)
    ) == {QuestionKey.HOBBIE, QuestionKey.FAVOURITE_DESSERT}


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (
            "hobbie",
            "¿Qué país siempre has querido conocer?",
            (QuestionKey.HOBBIE, QuestionKey.COUNTRY_TO_VISIT),
        ),
        (
            "  HOBBIE  ",
            "¿QUE PAIS SIEMPRE HAS QUERIDO CONOCER?",
            (QuestionKey.HOBBIE, QuestionKey.COUNTRY_TO_VISIT),
        ),
        (
            "¿Quién fue tu héroe de la infancia?",
            "¿Cuál es tu postre favorito?",
            (QuestionKey.CHILDHOOD_HERO, QuestionKey.FAVOURITE_DESSERT),
        ),
    ],
)
def test_spanish_question_aliases_resolve_real_live_wording(
    first: str, second: str, expected: tuple[QuestionKey, QuestionKey]
) -> None:
    """The pinned Spanish aliases match live wording accent/case tolerantly."""
    assert resolve_question_keys([first, second], answers=_FAKE_ANSWERS) == expected


@pytest.mark.parametrize(
    "texts",
    [
        [],
        [_two_question_texts()[0]],
        [
            QUESTION_EXAMPLE_LABELS[QuestionKey.HOBBIE],
            QUESTION_EXAMPLE_LABELS[QuestionKey.CHILDHOOD_HERO],
            QUESTION_EXAMPLE_LABELS[QuestionKey.FAVOURITE_DESSERT],
        ],
        [
            QUESTION_EXAMPLE_LABELS[QuestionKey.HOBBIE],
            QUESTION_EXAMPLE_LABELS[QuestionKey.HOBBIE],
        ],
        [
            QUESTION_EXAMPLE_LABELS[QuestionKey.HOBBIE],
            "What is the name of your first pet?",
        ],
    ],
    ids=["none", "one", "three", "repeated", "unknown"],
)
def test_question_resolution_fails_closed_on_wrong_shapes(texts: list[str]) -> None:
    """Wrong counts, repeats and unknown labels never resolve."""
    with pytest.raises(BanescoLayoutUnexpected):
        resolve_question_keys(texts, answers=_FAKE_ANSWERS)


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_question_resolution_fails_closed_on_missing_answers(bad: object) -> None:
    """A recognized key without a configured answer fails closed."""
    answers = dict(_FAKE_ANSWERS)
    answers[QuestionKey.COUNTRY_TO_VISIT] = bad  # type: ignore[assignment]
    labels = _two_question_texts(
        QuestionKey.HOBBIE, QuestionKey.COUNTRY_TO_VISIT
    )
    with pytest.raises(BanescoLayoutUnexpected) as excinfo:
        resolve_question_keys(labels, answers=answers)
    # The failure names the environment variable only, never a value.
    assert (
        QUESTION_ANSWER_SETTINGS[QuestionKey.COUNTRY_TO_VISIT]
        in str(excinfo.value)
    )


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("+584141234567", ("0414", "1234567")),
        ("0414-123-4567", ("0414", "1234567")),
        ("04161234567", ("0416", "1234567")),
    ],
)
def test_phone_projection_splits_into_two_controls(
    phone: str, expected: tuple[str, str]
) -> None:
    """Split a national phone into an offered prefix and seven digits."""
    assert project_phone(phone) == expected


def test_phone_projection_fails_closed() -> None:
    """An unoffered prefix or invalid phone never yields a partial projection."""
    with pytest.raises(UnsupportedPhonePrefix):
        project_phone("+584181234567")
    with pytest.raises(PaymentDataUnavailable):
        project_phone("not-a-phone")


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("V-12345678", ("V", "12345678")),
        ("E-12345678", ("E", "12345678")),
        ("J-12345678", ("J", "12345678")),
        ("G-12345678", ("G", "12345678")),
        ("R-12345678", ("R", "12345678")),
        ("r12345678", ("R", "12345678")),
        ("P-12345678", ("P", "12345678")),
    ],
)
def test_document_projection_normalizes_letter_and_digits(
    document: str, expected: tuple[str, str]
) -> None:
    """Normalize V/E/J/G/R/P letters and keep only bare digits."""
    assert project_document(document) == expected


def test_document_projection_never_defaults_to_v() -> None:
    """A missing letter is an error; unsupported letters are refused."""
    with pytest.raises(IdTypeMissing):
        project_document("12345678")
    with pytest.raises(IdTypeUnsupported):
        project_document("X-12345678")
    with pytest.raises(PaymentDataUnavailable):
        project_document("V-12")


def test_banesco_mapper_rejects_p_but_accepts_r() -> None:
    """P normalizes in the domain but is never accepted by the Banesco mapper."""
    assert project_banesco_document("R-12345678") == ("R", "12345678")
    with pytest.raises(IdTypeUnsupported):
        project_banesco_document("P-12345678")


def test_destination_bank_options_match_zero_one_or_many() -> None:
    """The versioned map matches its fixture and only exact reviewed labels."""
    snapshot = json.loads(_BANK_OPTIONS_FIXTURE.read_text(encoding="utf-8"))
    assert BANESCO_DESTINATION_BANK_OPTIONS_VERSION == snapshot["version"]
    expected = {
        entry["numeric_code"]: BanescoBankOption(
            numeric_code=entry["numeric_code"],
            label=entry["label"],
            value=entry["value"],
        )
        for entry in snapshot["options"]
    }
    assert dict(BANESCO_DESTINATION_BANK_OPTIONS) == expected
    assert set(BANESCO_DESTINATION_BANK_OPTIONS) == {
        entry["numeric_code"] for entry in snapshot["options"]
    }
    assert len(BANESCO_DESTINATION_BANK_OPTIONS) == 25
    assert BANESCO_DESTINATION_BANK_OPTIONS["0102"].label == "BANCO DE VENEZUELA"
    assert (
        BANESCO_DESTINATION_BANK_OPTIONS["0134"].label
        == "BANESCO BANCO UNIVERSAL S.A.C.A."
    )

    live = [
        BanescoBankOption("0134", "BANESCO BANCO UNIVERSAL S.A.C.A.", "0134"),
        BanescoBankOption("0102", "BANCO DE VENEZUELA", "0102"),
    ]
    matched = match_destination_bank_option("0102", live)
    assert matched.label == "BANCO DE VENEZUELA"
    # Matching tolerates whitespace and accents, never different wording.
    noisy = [BanescoBankOption("0102", "  BANCO   de  Venezuéla ", "0102")]
    assert match_destination_bank_option("0102", noisy).numeric_code == "0102"
    with pytest.raises(UnsupportedDestinationBank):
        match_destination_bank_option("0102", [live[0]])
    with pytest.raises(UnsupportedDestinationBank):
        match_destination_bank_option(
            "0102", [BanescoBankOption("0102", "BANCO DE VENEZUELA S.A.", "0102")]
        )
    with pytest.raises(UnsupportedDestinationBank):
        match_destination_bank_option("9999", live)
    with pytest.raises(DestinationBankAmbiguous):
        match_destination_bank_option(
            "0102",
            [
                BanescoBankOption("0102", "BANCO DE VENEZUELA", "0102"),
                BanescoBankOption("0102", "banco de venezuela", "0102"),
            ],
        )


def test_destination_bank_option_is_immutable() -> None:
    """The destination-bank option DTO cannot be mutated at runtime."""
    option = BANESCO_DESTINATION_BANK_OPTIONS["0102"]
    with pytest.raises((FrozenInstanceError, AttributeError)):
        option.label = "changed"  # type: ignore[misc]


def _valid_target(**overrides: object) -> BanescoPaymentTarget:
    """Build a valid payment target, applying optional overrides.

    Args:
        **overrides: Field values replacing the valid defaults.

    Returns:
        A constructed target.
    """
    values: dict[str, object] = {
        "phone_prefix": "0414",
        "phone_rest": "1234567",
        "id_type": "V",
        "id_digits": "12345678",
        "destination_bank_code": "0102",
        "destination_bank_option": "Banco de Venezuela",
        "amount_ves": Decimal("1500.50"),
        "concept": "Pago",
    }
    values.update(overrides)
    return BanescoPaymentTarget(**values)  # type: ignore[arg-type]


def test_amount_rendering_dto_invariants_and_decimal_only() -> None:
    """Render Decimal VES amounts and reject every malformed target."""
    assert format_ves_amount(Decimal("1500.50")) == "1500,50"
    assert format_ves_amount(Decimal("1500")) == "1500,00"

    target = _valid_target()
    assert target.amount_ves == Decimal("1500.50")
    assert not hasattr(target, "__dict__")
    with pytest.raises((FrozenInstanceError, AttributeError)):
        target.concept = "changed"  # type: ignore[misc]

    for bad_amount in (
        Decimal("0"),
        Decimal("-1.00"),
        Decimal("NaN"),
        Decimal("Infinity"),
        1500.5,
        1500,
    ):
        with pytest.raises(PaymentDataUnavailable):
            _valid_target(amount_ves=bad_amount)
    for bad_field in (
        {"phone_prefix": "0499"},
        {"phone_rest": "123456"},
        {"id_type": "P"},
        {"id_type": "X"},
        {"id_digits": "123"},
        {"destination_bank_code": "9999"},
        {"destination_bank_option": ""},
        {"concept": "   "},
    ):
        with pytest.raises(PaymentDataUnavailable):
            _valid_target(**bad_field)


_EXPECTED_RAIL_CODES = {
    "banesco_login_failed",
    "banesco_layout_unexpected",
    "banesco_captcha_detected",
    "banesco_account_locked",
    "banesco_maintenance",
    "banesco_session_expired",
    "banesco_ad_modal_blocked",
    "banesco_payment_rejected",
    "banesco_payment_outcome_unknown",
    "unsupported_phone_prefix",
    "id_type_missing",
    "id_type_unsupported",
    "payment_data_unavailable",
    "unsupported_destination_bank",
    "destination_bank_ambiguous",
    "clave_dinamica_timeout",
    "clave_dinamica_rejected",
    "receipt_amount_mismatch",
    "receipt_conflict",
    "receipt_unavailable",
    "evidence_upload_failed",
    "binance_close_failed",
    "appeal_already_filed",
    "release_timeout",
}


def test_payment_rail_error_codes_and_fail_closed_defaults() -> None:
    """Every rail failure is a typed, fail-closed ``LiquidVEException``."""
    assert set(PAYMENT_RAIL_ERROR_CODES) == _EXPECTED_RAIL_CODES
    for code, error_class in PAYMENT_RAIL_ERROR_CODES.items():
        error = error_class()
        assert error.code == code
        assert isinstance(error, PaymentRailError)
        assert isinstance(error, LiquidVEException)
        assert error.http_status >= 400
        assert error.message
        assert isinstance(error.details, dict)


def test_payment_rail_error_details_never_carry_secrets_tokens_or_html() -> None:
    """Sanitize detail maps so no credential, token, answer or HTML escapes."""
    error = BanescoLoginFailed(
        "login failed",
        details={
            "stage": "login",
            "password": "synthetic-password-canary",
            "token": "synthetic-token-canary",
            "security_answer": "synthetic-answer-canary",
            "clave_dinamica": "123456",
            "html": "<html><body>drift</body></html>",
            "raw": b"synthetic-bytes",
        },
    )
    assert error.details == {"stage": "login"}
    assert error.to_dict()["error"]["details"] == {"stage": "login"}
    assert error.service == "banesco_online"


def test_domain_module_is_pure_and_import_safe() -> None:
    """Importing the pure domain has no Playwright, I/O or settings object."""
    project_root = Path(__file__).resolve().parent.parent
    code = (
        "import sys; import app.domain.banesco; "
        "assert 'playwright' not in sys.modules, 'playwright imported'; "
        "assert 'app.core.config' not in sys.modules, 'settings imported'; "
        "print('DOMAIN PURE OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "DOMAIN PURE OK" in result.stdout


def test_destination_bank_option_value_change_fails_closed() -> None:
    """A reviewed label with a different live value is a drift failure."""
    with pytest.raises(UnsupportedDestinationBank):
        match_destination_bank_option(
            "0102", [BanescoBankOption("0102", "BANCO DE VENEZUELA", "9999")]
        )


@pytest.mark.parametrize("value", ["123456", "1234567", "12345678", " 12345678 "])
def test_clave_dinamica_accepts_six_to_eight_digits(value: str) -> None:
    """The live Clave field accepts 6-8 digits, not exactly six."""
    assert (
        CLAVE_DINAMICA_MIN_DIGITS,
        CLAVE_DINAMICA_MAX_DIGITS,
    ) == (6, 8)
    assert validate_clave_dinamica(value) == value.strip()


@pytest.mark.parametrize(
    "value",
    ["", "   ", "12345", "123456789", "12345a", "1234 56", None],
    ids=["empty", "blank", "five", "nine", "alpha", "space", "none"],
)
def test_clave_dinamica_rejects_out_of_range_or_non_digits(
    value: object,
) -> None:
    """A key outside 6-8 bare digits is rejected without echoing it."""
    with pytest.raises(ClaveDinamicaRejected):
        validate_clave_dinamica(value)  # type: ignore[arg-type]

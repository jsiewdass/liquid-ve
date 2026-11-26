"""Tests for the pure Binance P2P order-view parser.

The parser is stdlib only and must stay importable without Playwright.  Each
test reads a committed HTML snapshot, so the suite never opens a browser or a
socket.
"""

from __future__ import annotations

import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.ports import PaymentInstruction
from app.infrastructure.clients.binance_browser import parse_payment_instructions

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "binance"
_REPO_ROOT = Path(__file__).resolve().parents[1]

_CEDULA_BLOCK_RE = re.compile(
    r"<dt[^>]*>\s*C[ée]dula\s*</dt>\s*<dd[^>]*>.*?</dd>",
    re.IGNORECASE | re.DOTALL,
)


def _read_fixture(name: str) -> str:
    """Read one committed Binance order snapshot.

    Args:
        name: Fixture filename inside ``tests/fixtures/binance``.

    Returns:
        The fixture contents decoded as UTF-8.
    """
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_parse_bdv_order_fixture() -> None:
    """The BDV snapshot yields every field with the four-digit bank code."""
    instruction = parse_payment_instructions(
        _read_fixture("order_view_bdv.html"),
        order_number="ORD-BDV-0001",
    )

    assert isinstance(instruction, PaymentInstruction)
    assert instruction.order_number == "ORD-BDV-0001"
    assert instruction.bank_numeric_code == "0102"
    assert instruction.phone == "04141234567"
    assert instruction.id_number == "V-12345678"
    assert instruction.raw_fields["banco"] == "Banco de Venezuela (0102)"


def test_parsed_amount_is_decimal() -> None:
    """The parsed amount is an exact ``Decimal`` and never a float."""
    instruction = parse_payment_instructions(
        _read_fixture("order_view_bdv.html"),
        order_number="ORD-BDV-0001",
    )

    assert instruction is not None
    assert type(instruction.amount_ves) is Decimal
    assert instruction.amount_ves == Decimal("1500.00")


def test_parse_banesco_order_fixture() -> None:
    """The Banesco snapshot yields its own code and a bare-digit cedula."""
    instruction = parse_payment_instructions(
        _read_fixture("order_view_banesco.html"),
        order_number="ORD-BAN-0002",
    )

    assert instruction is not None
    assert instruction.bank_numeric_code == "0134"
    assert instruction.phone == "04141234567"
    assert instruction.id_number == "12345678"
    assert instruction.amount_ves == Decimal("1500.00")


def test_parse_grouped_amount_returns_exact_decimal() -> None:
    """A grouped amount is accepted and normalized to the same ``Decimal``."""
    html = _read_fixture("order_view_bdv.html").replace("1500,00", "1.500,00")

    instruction = parse_payment_instructions(html, order_number="ORD-BDV-0001")

    assert instruction is not None
    assert instruction.amount_ves == Decimal("1500.00")


def test_parse_missing_cedula_returns_none() -> None:
    """A snapshot without a cedula field never yields a partial instruction."""
    html = _CEDULA_BLOCK_RE.sub("", _read_fixture("order_view_bdv.html"))

    assert parse_payment_instructions(html, order_number="ORD-BDV-0001") is None


@pytest.mark.parametrize(
    "mangled_amount",
    ["1,500.00", "1500.00", "1.50,00", "1500,0", "1500,000", "0,00"],
)
def test_parse_mangled_amount_returns_none(mangled_amount: str) -> None:
    """A wrong decimal separator, grouping or value yields ``None``."""
    html = _read_fixture("order_view_bdv.html").replace("1500,00", mangled_amount)

    assert parse_payment_instructions(html, order_number="ORD-BDV-0001") is None


@pytest.mark.parametrize("html", ["", "   ", "<div>no labels here</div>"])
def test_parse_empty_or_unrelated_html_returns_none(html: str) -> None:
    """Empty or unrelated markup yields ``None`` instead of a guess."""
    assert parse_payment_instructions(html, order_number="ORD-BDV-0001") is None


def test_parser_import_does_not_import_playwright() -> None:
    """Importing the parser module must not import Playwright in ``sys.modules``."""
    probe = (
        "import sys, app.infrastructure.clients.binance_browser as m; "
        "assert 'playwright' not in sys.modules; "
        "assert hasattr(m, 'parse_payment_instructions'); "
        "print('browser-free OK')"
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "browser-free OK" in completed.stdout

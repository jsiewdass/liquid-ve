"""Offline inspector for the captured Banesco screens.

This tool does **not** drive a browser, does not log in and does not pay. It
runs the pure page contracts and domain mappers against the sanitized fixtures
so you can see exactly what the system already understands, and what it still
cannot do.

Usage (from the repository root)::

    .\\.venv\\Scripts\\python.exe scripts\\banesco_inspect.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.banesco import (
    BANESCO_PHONE_PREFIXES,
    BanescoPaymentTarget,
    format_ves_amount,
    project_banesco_document,
    project_phone,
)
from app.infrastructure.clients.banesco_pages import (
    FRAMES_BY_SCREEN,
    SCREEN_FIXTURES,
    SELECTORS_BY_SCREEN,
    Screen,
    detect_screen,
    extract_question_texts,
    parse_bank_options,
    parse_confirmation_summary,
    parse_id_types,
    parse_phone_prefixes,
)

FIXTURES = Path("tests/fixtures/banesco")


def mask(value: str) -> str:
    """Mask a value keeping only its length visible.

    Args:
        value: Sensitive text.

    Returns:
        A masked representation.
    """
    return f"<{len(value)} chars>"


def inspect_selectors() -> None:
    """Print selector coverage for every known screen."""
    print("\n== SELECTOR COVERAGE ==")
    for screen, fixture in SCREEN_FIXTURES.items():
        path = FIXTURES / fixture
        if not path.exists():
            print(f"  {screen.value:20} fixture missing: {fixture}")
            continue
        markup = path.read_text(encoding="utf-8")
        selectors = SELECTORS_BY_SCREEN[screen]
        missing = [key for key, sel in selectors.items() if sel.lstrip("#") not in markup]
        detected = detect_screen(markup)
        ok = "OK" if not missing else f"MISSING {missing}"
        match = "match" if detected is screen else f"DETECTED {detected.value}"
        print(
            f"  {screen.value:20} frame={FRAMES_BY_SCREEN.get(screen, 'outer shell'):24} "
            f"selectors={len(selectors)} {ok}  ({match})"
        )


def inspect_questions() -> None:
    """Print the security questions extracted from the real screen."""
    print("\n== SECURITY QUESTIONS ==")
    markup = (FIXTURES / SCREEN_FIXTURES[Screen.SECURITY_QUESTIONS]).read_text(
        encoding="utf-8"
    )
    for index, text in enumerate(extract_question_texts(markup), start=1):
        print(f"  {index}. {text}")


def inspect_form() -> None:
    """Print the live option sets of the Pago Movil form."""
    print("\n== PAGO MOVIL FORM OPTIONS ==")
    markup = (FIXTURES / SCREEN_FIXTURES[Screen.PAGOMOVIL_FORM]).read_text(
        encoding="utf-8"
    )
    prefixes = parse_phone_prefixes(markup)
    id_types = parse_id_types(markup)
    banks = parse_bank_options(markup)
    print(f"  phone prefixes ({len(prefixes)}): {list(prefixes)}")
    print(f"  document types ({len(id_types)}): {list(id_types)}")
    print(f"  destination banks ({len(banks)}):")
    for bank in banks[:6]:
        print(f"     {bank.numeric_code} -> {bank.label}")
    print("     ...")
    for code in ("0102", "0134", "0108"):
        found = next((b for b in banks if b.numeric_code == code), None)
        print(f"  lookup {code}: {found.label if found else 'NOT FOUND'}")


def inspect_confirmation() -> None:
    """Print the parsed confirmation summary and the projected target."""
    print("\n== CONFIRMATION SUMMARY (PII masked) ==")
    markup = (FIXTURES / SCREEN_FIXTURES[Screen.CONFIRMATION]).read_text(
        encoding="utf-8"
    )
    summary = parse_confirmation_summary(markup)
    print(f"  beneficiary name      : {mask(summary.beneficiary_name)}")
    print(f"  beneficiary phone     : {mask(summary.beneficiary_phone)}")
    print(f"  beneficiary document  : {mask(summary.beneficiary_document)}")
    print(f"  destination bank      : {summary.destination_bank}")
    print(f"  amount (Decimal)      : {summary.amount_ves}")
    print(f"  amount as Banesco text: {format_ves_amount(summary.amount_ves)}")
    print(f"  concept               : {summary.concept}")


def inspect_target_projection() -> None:
    """Show how a Binance order becomes two-control form values."""
    print("\n== TARGET PROJECTION (synthetic order) ==")
    target = BanescoPaymentTarget(
        phone_prefix=project_phone("04221234567")[0],
        phone_rest=project_phone("04221234567")[1],
        id_type=project_banesco_document("V-12345678")[0],
        id_digits=project_banesco_document("V-12345678")[1],
        destination_bank_code="0102",
        destination_bank_option="BANCO DE VENEZUELA",
        amount_ves=Decimal("1500.00"),
        concept="pago p2p",
    )
    print(f"  phone select : {target.phone_prefix}")
    print(f"  phone input  : {target.phone_rest}")
    print(f"  doc select   : {target.id_type}")
    print(f"  doc input    : {target.id_digits}")
    print(f"  bank select  : {target.destination_bank_code} ({target.destination_bank_option})")
    print(f"  amount input : {format_ves_amount(target.amount_ves)}")
    print(f"  concept      : {target.concept}")
    print(f"  offered prefixes: {sorted(BANESCO_PHONE_PREFIXES)}")


def main() -> int:
    """Run every offline inspection.

    Returns:
        Process exit code.
    """
    print("=" * 66)
    print("Banesco rail - OFFLINE inspection (no browser, no network, no payment)")
    print("=" * 66)
    print("\nWhat this proves: the system can identify each screen, read the real")
    print("controls, resolve the questions, map a bank code and project a payment.")
    print("What it does NOT do: drive the portal. That is PR 4-5, not built yet.")
    inspect_selectors()
    inspect_questions()
    inspect_form()
    inspect_confirmation()
    inspect_target_projection()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

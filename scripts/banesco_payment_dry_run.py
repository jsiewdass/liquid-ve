"""Dry run of the Banesco payment flow, stopping at the Clave Dinámica form.

It performs the whole prefix of the purchase payment:

    login -> security questions -> clave -> ad modal -> Pago Móvil ->
    form fill -> Pagar -> confirmation -> Pagar -> Clave Dinámica form

It **stops there**. It never types the dynamic key, never clicks "Aceptar" and
therefore never moves money. No payment data is persisted.

Usage (from the repository root)::

    .\\.venv\\Scripts\\python.exe scripts\\banesco_payment_dry_run.py \\
        --phone 04141234567 --document V12345678 --bank banesco --amount 1.00

Add ``--keep-open`` to inspect the Clave Dinámica screen, and ``--screenshot``
to save an image of it under ``data/debug`` (gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from app.core.config import Settings  # noqa: E402
from app.core.exceptions import PaymentRailError  # noqa: E402
from app.domain.banesco import (  # noqa: E402
    BANESCO_DESTINATION_BANK_OPTIONS,
    BanescoPaymentTarget,
    match_destination_bank_option,
    project_banesco_document,
    project_phone,
)
from app.infrastructure.clients.banesco_browser import BanescoBrowser  # noqa: E402
from app.infrastructure.clients.banesco_pages import detect_screen  # noqa: E402
from banesco_login_check import dump_failure_diagnostics  # noqa: E402

RESULT_PATH = REPO_ROOT / "data" / "debug" / "banesco-payment-dry-run.json"
SHOT_PATH = REPO_ROOT / "data" / "debug" / "banesco-clave-dinamica.png"


def resolve_bank_code(value: str) -> str:
    """Resolve a bank argument into a four-digit Banesco destination code.

    Args:
        value: Four-digit code, or a fragment of the reviewed bank label.

    Returns:
        The four-digit code.

    Raises:
        SystemExit: When no single reviewed bank matches.
    """
    text = value.strip()
    if re.fullmatch(r"\d{4}", text):
        if text in BANESCO_DESTINATION_BANK_OPTIONS:
            return text
        raise SystemExit(f"Bank code {text} is not in the reviewed map")

    needle = text.casefold()
    matches = [
        code
        for code, option in BANESCO_DESTINATION_BANK_OPTIONS.items()
        if needle in option.label.casefold()
    ]
    if len(matches) != 1:
        raise SystemExit(
            f"Bank {value!r} is ambiguous or unknown; pass the 4-digit code"
        )
    return matches[0]


def build_target(args: argparse.Namespace) -> BanescoPaymentTarget:
    """Build the validated two-control target from CLI arguments.

    Args:
        args: Parsed CLI arguments.

    Returns:
        The validated payment target.
    """
    code = resolve_bank_code(args.bank)
    option = match_destination_bank_option(
        code, tuple(BANESCO_DESTINATION_BANK_OPTIONS.values())
    )
    prefix, rest = project_phone(args.phone)
    id_type, id_digits = project_banesco_document(args.document)
    try:
        amount = Decimal(args.amount.replace(",", "."))
    except InvalidOperation as exc:
        raise SystemExit(f"Invalid amount: {args.amount!r}") from exc
    return BanescoPaymentTarget(
        phone_prefix=prefix,
        phone_rest=rest,
        id_type=id_type,
        id_digits=id_digits,
        destination_bank_code=code,
        destination_bank_option=option.label,
        amount_ves=amount,
        concept=args.concept,
    )


def mask(value: str) -> str:
    """Mask a sensitive value keeping only its length.

    Args:
        value: Sensitive text.

    Returns:
        A masked representation.
    """
    return f"<{len(value)} chars>"


async def run(args: argparse.Namespace) -> int:
    """Execute the dry run.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code.
    """
    target = build_target(args)
    settings = Settings(
        banesco_rail_enabled=False,
        telegram_enabled=False,
        banesco_browser_headless=args.headless,
    )

    print("Banesco payment DRY RUN - stops before the dynamic key")
    print(f"  login url     : {settings.banesco_online_login_url}")
    print(f"  headless      : {settings.banesco_browser_headless}")
    print(f"  phone         : {target.phone_prefix} {mask(target.phone_rest)}")
    print(f"  document      : {target.id_type} {mask(target.id_digits)}")
    print(f"  bank          : {target.destination_bank_code} ({target.destination_bank_option})")
    print(f"  amount        : {target.amount_ves}")
    print(f"  concept       : {target.concept}")
    print()

    browser = BanescoBrowser(settings)
    result: dict[str, object] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "outcome": "unknown",
    }
    exit_code = 2
    try:
        await browser.fill_and_submit_payment(target)
        page = browser.page
        stage = "unknown"
        url = "(unknown)"
        if page is not None:
            url = page.url
            frames = getattr(page, "frames", [])
            detected_frames = []
            if isinstance(frames, list):
                for frame in frames:
                    try:
                        content = await frame.content()
                    except Exception:  # noqa: BLE001 - diagnostics only
                        continue
                    detected = detect_screen(content)
                    if detected.value != "unknown":
                        detected_frames.append(detected)
            if detected_frames:
                stage = detected_frames[-1].value
            else:
                stage = detect_screen(await page.content()).value
        print("=" * 62)
        print("  LISTO - detenido en el formulario de Clave Dinamica")
        print("=" * 62)
        print(f"  final url     : {url}")
        print(f"  detected stage: {stage}")
        print("  NO se escribio la clave y NO se envio ningun pago")
        result.update({"outcome": "ok", "final_url": url, "stage": stage})

        if args.screenshot and page is not None:
            shooter = getattr(page, "screenshot", None)
            if callable(shooter):
                try:
                    SHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
                    await shooter(path=str(SHOT_PATH))
                    print(f"  screenshot    : {SHOT_PATH}")
                    result["screenshot"] = str(SHOT_PATH)
                except Exception as exc:  # noqa: BLE001 - optional evidence
                    print(f"  screenshot failed: {type(exc).__name__}")
            else:
                print("  screenshot unavailable on this page object")
        exit_code = 0
    except PaymentRailError as exc:
        print("=" * 62)
        print("  DETENIDO por un error tipado")
        print("=" * 62)
        print(f"  code    : {getattr(exc, 'code', 'unknown')}")
        print(f"  details : {getattr(exc, 'details', {})}")
        result.update(
            {
                "outcome": "failed",
                "code": getattr(exc, "code", "unknown"),
                "details": getattr(exc, "details", {}),
            }
        )
        exit_code = 1
        await dump_failure_diagnostics(browser)
    except Exception as exc:  # noqa: BLE001 - operator-facing diagnostic
        print("=" * 62)
        print("  DETENIDO por un error inesperado")
        print("=" * 62)
        print(f"  type: {type(exc).__name__}")
        print(f"  text: {exc}")
        result.update({"outcome": "error", "type": type(exc).__name__})
        exit_code = 2
        await dump_failure_diagnostics(browser)
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
            RESULT_PATH.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"\nResult written to {RESULT_PATH}")
        except OSError as exc:
            print(f"\nCould not write result: {exc}")
        if args.keep_open and browser.page is not None:
            print("\nEl navegador sigue abierto. NO escribas la clave.")
            await asyncio.to_thread(
                input, "Presiona Enter aqui para cerrarlo y terminar... "
            )
        await browser.aclose()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the dry run.

    Args:
        argv: Optional CLI arguments.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", required=True, help="Beneficiary phone.")
    parser.add_argument(
        "--document", required=True, help="Beneficiary document, e.g. V12345678."
    )
    parser.add_argument(
        "--bank", required=True, help="Bank 4-digit code or label fragment."
    )
    parser.add_argument("--amount", required=True, help="Amount in VES, e.g. 100.00.")
    parser.add_argument("--concept", default="pago", help="Payment concept.")
    parser.add_argument(
        "--headless", action="store_true", help="Run without a visible window."
    )
    parser.add_argument(
        "--keep-open", action="store_true", help="Keep the browser open until Enter."
    )
    parser.add_argument(
        "--screenshot", action="store_true", help="Save a screenshot of the clave form."
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

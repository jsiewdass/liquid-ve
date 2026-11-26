"""Live login check for the Banesco Online rail (PR 4).

This script performs **only** the login prefix of the flow:

    open login -> user -> the two security questions -> password -> ad modal

It stops on the authenticated dashboard. It never opens a product, never
fills a payment control and never clicks a payment button.

Credentials are read from ``.env`` and are never printed.

Usage (from the repository root)::

    .\\.venv\\Scripts\\python.exe scripts\\banesco_login_check.py
    .\\.venv\\Scripts\\python.exe scripts\\banesco_login_check.py --headless
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402
from app.core.exceptions import PaymentRailError  # noqa: E402
from app.infrastructure.clients.banesco_browser import BanescoBrowser  # noqa: E402
from app.infrastructure.clients.banesco_pages import detect_screen  # noqa: E402

RESULT_PATH = Path("data/debug/banesco-login-result.json")
FAIL_DIR = Path("data/debug/banesco-fail")


async def dump_failure_diagnostics(browser: BanescoBrowser) -> None:
    """Save every frame of the current page when the login fails.

    Args:
        browser: Driver holding the live page, if any.
    """
    page = browser.page
    if page is None:
        print("  (no page available for diagnostics)")
        return
    frames = getattr(page, "frames", None)
    if not isinstance(frames, list):
        print("  (page exposes no frames list)")
        return
    try:
        FAIL_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"  (cannot create {FAIL_DIR}: {exc})")
        return

    report: list[dict[str, object]] = []
    for index, frame in enumerate(frames):
        try:
            markup = await frame.content()
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            report.append({"index": index, "error": type(exc).__name__})
            continue
        try:
            url = frame.url
        except Exception:  # noqa: BLE001
            url = "?"
        try:
            name = frame.name
        except Exception:  # noqa: BLE001
            name = "?"
        screen = detect_screen(markup)
        try:
            sanitized = await frame.evaluate(
                "() => {"
                " const c = document.documentElement.cloneNode(true);"
                " c.querySelectorAll('script, style, noscript').forEach(n => n.remove());"
                " c.querySelectorAll('input').forEach(n => {"
                "   if (n.hasAttribute('value')) n.setAttribute('value','REDACTED'); });"
                " c.querySelectorAll('*').forEach(n => {"
                "   Array.from(n.attributes||[]).forEach(a => {"
                "     if (a.name.toLowerCase().startsWith('on')) n.removeAttribute(a.name);"
                "   });"
                " });"
                " return c.outerHTML; }"
            )
        except Exception:  # noqa: BLE001
            sanitized = markup
        path = FAIL_DIR / f"{index:02d}_{screen.value}.html"
        try:
            path.write_text(str(sanitized), encoding="utf-8")
        except OSError as exc:
            print(f"  (cannot write {path}: {exc})")
        report.append(
            {
                "index": index,
                "name": name,
                "url": url,
                "screen": screen.value,
                "bytes": len(markup),
                "has_txtUsuario": "txtUsuario" in markup,
                "has_txtPrimeraR": "txtPrimeraR" in markup,
                "has_txtClave": "txtClave" in markup,
                "has_otp": 'id="otp"' in markup,
                "file": path.name,
            }
        )

    try:
        (FAIL_DIR / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nDiagnostics written to {FAIL_DIR.resolve()}")
    except OSError as exc:
        print(f"  (cannot write report: {exc})")



def build_settings(headless: bool) -> Settings:
    """Build settings with the rail switches forced off for a login probe.

    The login driver only needs the URL and the credentials; ``banesco_rail_enabled``
    and ``telegram_enabled`` gate dispatch in later slices, so forcing them off
    here keeps this check independent of the Telegram configuration.

    Args:
        headless: Whether Chromium should run without a visible window.

    Returns:
        The settings instance for this probe.
    """
    return Settings(
        banesco_rail_enabled=False,
        telegram_enabled=False,
        banesco_browser_headless=headless,
    )


def write_result(payload: dict[str, object]) -> None:
    """Persist the probe outcome for later inspection.

    Args:
        payload: JSON-serializable result without credentials or PII.
    """
    try:
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nResult written to {RESULT_PATH}")
    except OSError as exc:
        print(f"\nCould not write {RESULT_PATH}: {exc}")


async def run(headless: bool, keep_open: bool) -> int:
    """Execute the login probe.

    Args:
        headless: Whether Chromium should run without a visible window.
        keep_open: Pause before closing the browser so it can be inspected.

    Returns:
        Process exit code.
    """
    settings = build_settings(headless)
    started_at = datetime.now(timezone.utc)
    print("Banesco login check")
    print(f"  login url     : {settings.banesco_online_login_url}")
    print(f"  headless      : {settings.banesco_browser_headless}")
    print(f"  timeout (ms)  : {settings.banesco_browser_timeout_ms}")
    print("  credentials   : loaded from .env (values never printed)")
    print()

    browser = BanescoBrowser(settings)
    exit_code = 2
    result: dict[str, object] = {
        "started_at": started_at.isoformat(),
        "login_url": settings.banesco_online_login_url,
    }
    try:
        await browser.login()
        page = browser.page
        markup = await page.content() if page is not None else ""
        screen = detect_screen(markup)
        url = page.url if page is not None else "(unknown)"
        print("=" * 58)
        print("  LOGIN OK - la automatizacion entro correctamente")
        print("=" * 58)
        print(f"  final url     : {url}")
        print(f"  detected stage: {screen.value}")
        print(f"  markup bytes  : {len(markup)}")
        result.update(
            {"outcome": "ok", "final_url": url, "stage": screen.value}
        )
        exit_code = 0
    except PaymentRailError as exc:
        print("=" * 58)
        print("  LOGIN FAILED - la automatizacion se detuvo")
        print("=" * 58)
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
        print("=" * 58)
        print("  LOGIN FAILED - error inesperado")
        print("=" * 58)
        print(f"  type: {type(exc).__name__}")
        print(f"  text: {exc}")
        result.update({"outcome": "error", "type": type(exc).__name__})
        exit_code = 2
        await dump_failure_diagnostics(browser)
    finally:
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_result(result)
        if keep_open and browser.page is not None:
            print("\nEl navegador sigue abierto para inspeccionarlo.")
            await asyncio.to_thread(
                input, "Presiona Enter aqui para cerrarlo y terminar... "
            )
        await browser.aclose()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the probe.

    Args:
        argv: Optional CLI arguments.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium without a visible window.",
    )
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Keep the browser open until Enter is pressed.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(headless=args.headless, keep_open=args.keep_open))


if __name__ == "__main__":
    raise SystemExit(main())

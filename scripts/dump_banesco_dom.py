"""Passive DOM capture helper for the Banesco Online payment rail.

This is a **one-off operator tool**, not part of the application. It does not
log in, does not read credentials, does not fill forms and never submits a
payment. It only observes a browser window that *you* drive manually and saves
a sanitized snapshot of every distinct screen so the Playwright page objects can
be built against the real DOM.

Unlike a normal Playwright run, the browser is launched as a **detached
process** with a remote debugging port and a persistent profile. When this
script exits the browser keeps running, so the next development phase can
reconnect to the exact same session and screen with ``connect_over_cdp``:

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        page = browser.contexts[0].pages[0]

Usage (from the repository root)::

    .\\.venv\\Scripts\\python.exe scripts\\dump_banesco_dom.py

Then, in the window that opens:

1. Log in manually (user, security questions, password).
2. Close the advertisement modal.
3. Open ``Pago Movil`` -> ``Enviar pago``.
4. Fill the form with a **token amount** (for example ``0,01``) to a
   beneficiary you control.
5. Press ``Pagar`` and reach the confirmation screen.
6. **STOP at the Clave Dinamica form. Do not enter the key.**

Every distinct screen is written to ``data/debug/banesco/`` (gitignored).
Capture stops by itself when the Clave Dinamica form is detected; pressing
``Enter`` in the terminal also stops it. The browser stays open either way.

Sanitization performed before writing:

* ``<script>`` and ``<style>`` elements are removed.
* Every ``<input>`` ``value`` attribute is replaced with ``REDACTED``.
* ``<textarea>`` content is replaced with ``REDACTED``.
* All inline ``on*`` event handler attributes are removed.

Query strings and fragments are stripped from the recorded URL log.

IMPORTANT: review the generated files before sharing them. The sanitizer
removes obvious secrets and form values, but visible text such as account
names, balances or beneficiary data can still appear in the markup.

SECURITY: the debugging port grants full control over the logged-in banking
session to any local process that can reach it. It binds to ``127.0.0.1`` only,
but close the browser when you are done developing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://www.banesconline.com/Mantis/WebSite/Login.aspx"
DEFAULT_OUT = Path("data/debug/banesco")
DEFAULT_PROFILE = Path("data/debug/banesco-profile")
DEFAULT_PORT = 9222
DEFAULT_INTERVAL = 1.0
_TARGET_RE = re.compile(r"clave[\s_\-:]*din[aá]mica", re.IGNORECASE)
"""Matches the Clave Dinamica screen in visible text or control identifiers."""

_MAX_SNAPSHOTS = 200

_SKIP_URL_PREFIXES = (
    "chrome://",
    "chrome-untrusted://",
    "devtools://",
    "about:",
    "edge://",
)

_MIN_MARKUP_CHARS = 200
"""Documents smaller than this are browser internals, not real screens."""


def _target_reached(markup: str) -> bool:
    """Return whether the captured markup is the Clave Dinamica screen.

    Args:
        markup: Sanitized page HTML.

    Returns:
        ``True`` when the screen mentions the Clave Dinamica form.
    """
    return _TARGET_RE.search(markup) is not None


_SANITIZE_JS = """
() => {
  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll('script, style, noscript, link[rel="stylesheet"]')
    .forEach((node) => node.remove());
  clone.querySelectorAll('input').forEach((node) => {
    if (node.hasAttribute('value')) {
      node.setAttribute('value', 'REDACTED');
    }
  });
  clone.querySelectorAll('textarea').forEach((node) => {
    node.textContent = 'REDACTED';
  });
  clone.querySelectorAll('*').forEach((node) => {
    Array.from(node.attributes || []).forEach((attr) => {
      if (attr.name.toLowerCase().startsWith('on')) {
        node.removeAttribute(attr.name);
      }
    });
  });
  return clone.outerHTML;
}
"""


def _slug(value: str) -> str:
    """Build a filesystem-safe slug from a URL.

    Args:
        value: URL or page title.

    Returns:
        A lowercase slug with non-alphanumeric characters collapsed.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (cleaned or "page")[:60]


def _strip_url(url: str) -> str:
    """Remove query string and fragment from a URL.

    Args:
        url: Raw browser URL.

    Returns:
        The URL without secrets that may travel in the query string.
    """
    return url.split("#", 1)[0].split("?", 1)[0]


def _sanitize(target: Any) -> str | None:
    """Return the sanitized document markup for a page or frame.

    Args:
        target: Playwright page or frame object.

    Returns:
        The sanitized HTML, or ``None`` when the document is not readable.
    """
    try:
        return target.evaluate(_SANITIZE_JS)
    except Exception:  # noqa: BLE001 - navigation races are expected and safe
        return None


_STRUCTURAL_ATTR_RE = re.compile(r'(?:id|name|for)="([^"]*)"', re.IGNORECASE)


def _structural_signature(markup: str) -> str:
    """Build a signature that ignores cosmetic DOM churn.

    Banesco pages rotate banners and counters, so an exact markup hash changes
    on every poll. The identity of a screen is its control structure instead:
    the set of ``id``/``name``/``for`` attribute values plus the number of form
    controls.

    Args:
        markup: Sanitized page or frame HTML.

    Returns:
        A stable signature for the screen's structure.
    """
    attributes = sorted(set(_STRUCTURAL_ATTR_RE.findall(markup)))
    counts = (
        markup.count("<input"),
        markup.count("<select"),
        markup.count("<textarea"),
        markup.count("<button"),
        markup.count("<form"),
        markup.count("<iframe"),
    )
    payload = f"{counts}|" + "|".join(attributes)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _save(
    out_dir: Path, index: int, url: str, frame_label: str, markup: str
) -> Path:
    """Write one sanitized snapshot.

    Args:
        out_dir: Destination directory.
        index: Sequential snapshot number.
        url: Frame URL used in the filename.
        frame_label: Frame name or index, disambiguating nested documents.
        markup: Sanitized HTML content.

    Returns:
        The written path.
    """
    path = out_dir / f"{index:03d}_{frame_label}_{_slug(_strip_url(url))}.html"
    path.write_text(markup, encoding="utf-8")
    return path


def _resolve_executable(preferred: str, override: str | None) -> str | None:
    """Resolve a usable Chromium executable.

    The pinned Playwright revision may not be downloaded. Because this tool
    launches the browser itself and attaches over CDP, any installed Chromium
    build works, so the newest available revision is used as a fallback.

    Args:
        preferred: The executable path Playwright expects.
        override: Operator-provided executable path, if any.

    Returns:
        A path to an existing executable, or ``None`` when none is available.
    """
    if override:
        candidate = Path(override).expanduser()
        return str(candidate) if candidate.is_file() else None

    pinned = Path(preferred)
    if pinned.is_file():
        return str(pinned)

    roots = list(pinned.parents)
    if len(roots) >= 3:
        browser_root = roots[2]
        available = sorted(
            browser_root.glob("chromium-*/chrome-win/chrome.exe"),
            key=lambda item: item.parent.parent.name,
            reverse=True,
        )
        for candidate in available:
            if candidate.is_file():
                return str(candidate)
    return None


def _cdp_endpoint(port: int) -> str:
    """Build the CDP HTTP endpoint for a debugging port.

    Args:
        port: Remote debugging port.

    Returns:
        The local endpoint URL.
    """
    return f"http://127.0.0.1:{port}"


def _cdp_ready(port: int) -> bool:
    """Return whether a CDP endpoint is answering.

    Args:
        port: Remote debugging port.

    Returns:
        ``True`` when the version endpoint responds with JSON.
    """
    try:
        with urllib.request.urlopen(
            f"{_cdp_endpoint(port)}/json/version", timeout=1.0
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("webSocketDebuggerUrl"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _launch_detached(executable: str, port: int, profile: Path) -> subprocess.Popen[bytes]:
    """Launch a detached Chromium with remote debugging enabled.

    Args:
        executable: Chromium executable path.
        port: Remote debugging port.
        profile: Persistent user data directory.

    Returns:
        The detached process handle.
    """
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        executable,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
    ]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    return subprocess.Popen(  # noqa: S603 - executable comes from Playwright
        args,
        close_fds=True,
        creationflags=creationflags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_cdp(port: int, timeout: float = 30.0) -> bool:
    """Poll until the CDP endpoint answers or the timeout elapses.

    Args:
        port: Remote debugging port.
        timeout: Maximum seconds to wait.

    Returns:
        ``True`` when the endpoint became ready.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _cdp_ready(port):
            return True
        time.sleep(0.25)
    return False


def _stop_requested() -> bool:
    """Return whether the operator asked to stop capturing.

    Returns:
        ``True`` when Enter was pressed in the terminal (Windows) or stdin
        reached end of file.
    """
    if sys.platform == "win32":
        try:
            import msvcrt
        except ImportError:  # pragma: no cover - defensive
            return False
        if msvcrt.kbhit():
            key = msvcrt.getwch()
            if key in ("\r", "\n", "\x1b"):
                return True
        return False
    try:
        import select

        readable, _, _ = select.select([sys.stdin], [], [], 0)
    except (OSError, ValueError):  # pragma: no cover - non-tty
        return False
    if not readable:
        return False
    sys.stdin.readline()
    return True


def main(argv: list[str] | None = None) -> int:
    """Run the passive capture loop.

    Args:
        argv: Optional CLI arguments.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Initial URL to open.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output directory.")
    parser.add_argument(
        "--profile", default=str(DEFAULT_PROFILE), help="Persistent profile directory."
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="CDP port.")
    parser.add_argument(
        "--executable",
        default=None,
        help="Explicit Chromium executable path (overrides auto-detection).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="Seconds between DOM checks.",
    )
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright is not installed. Run:\n"
            "  .\\.venv\\Scripts\\python.exe -m pip install playwright\n"
            "  .\\.venv\\Scripts\\python.exe -m playwright install chromium",
            file=sys.stderr,
        )
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output directory: {out_dir.resolve()}")
    print(f"Profile (session survives restarts): {Path(args.profile).resolve()}")

    try:
        with sync_playwright() as playwright:
            launched_here = False
            if not _cdp_ready(args.port):
                executable = _resolve_executable(
                    playwright.chromium.executable_path, args.executable
                )
                if executable is None:
                    print(
                        "No Chromium build was found.\n"
                        "Install the pinned revision with:\n"
                        "  .\\.venv\\Scripts\\python.exe -m playwright install chromium\n"
                        "or point at an existing build with --executable.",
                        file=sys.stderr,
                    )
                    return 2
                print(f"Using Chromium: {executable}")
                try:
                    _launch_detached(executable, args.port, Path(args.profile))
                except (FileNotFoundError, OSError) as exc:
                    print(
                        f"Could not start Chromium ({type(exc).__name__}: {exc}).\n"
                        "Run: .\\.venv\\Scripts\\python.exe -m playwright install chromium",
                        file=sys.stderr,
                    )
                    return 2
                print(f"Launching Chromium on CDP port {args.port} ...")
                if not _wait_for_cdp(args.port):
                    print(
                        "Chromium did not expose its debugging port in time.",
                        file=sys.stderr,
                    )
                    return 1
                launched_here = True
            else:
                print(f"Reusing the browser already listening on port {args.port}.")

            browser = playwright.chromium.connect_over_cdp(_cdp_endpoint(args.port))

            # Never create a context on a browser we did not launch: doing so
            # opens an isolated incognito window while the operator keeps
            # working in the real one, which silently captures nothing.
            context_deadline = time.monotonic() + 20
            while not browser.contexts and time.monotonic() < context_deadline:
                time.sleep(0.25)
            if not browser.contexts:
                print(
                    "The browser exposed no context. Open a tab in it and retry.",
                    file=sys.stderr,
                )
                return 1

            if launched_here:
                landing = browser.contexts[0]
                if not landing.pages:
                    try:
                        landing.new_page().goto(
                            args.url, wait_until="domcontentloaded"
                        )
                    except Exception:  # noqa: BLE001 - best effort only
                        pass

            print("\nDrive the window manually. The capture stops by itself as")
            print("soon as the Clave Dinamica form appears. Do NOT enter the key.\n")

            seen: set[str] = set()
            url_log: list[str] = []
            saved: list[tuple[int, str, Path]] = []
            stop_reason = ""
            last_heartbeat = 0.0

            while not stop_reason:
                if _stop_requested():
                    stop_reason = "operator pressed Enter"
                    break
                pages = [
                    page
                    for context in browser.contexts
                    for page in context.pages
                    if not page.is_closed()
                ]
                if not pages:
                    stop_reason = "no open pages left"
                    break
                if time.monotonic() - last_heartbeat >= 10:
                    last_heartbeat = time.monotonic()
                    frame_count = 0
                    for candidate in pages:
                        try:
                            frame_count += len(candidate.frames)
                        except Exception:  # noqa: BLE001 - page closing
                            continue
                    print(
                        f"  ... watching {len(browser.contexts)} context(s), "
                        f"{len(pages)} page(s), {frame_count} frame(s)"
                    )
                for page in pages:
                    if stop_reason:
                        break
                    try:
                        page_url = _strip_url(page.url)
                    except Exception:  # noqa: BLE001 - page navigating
                        page_url = ""
                    if page_url.startswith(_SKIP_URL_PREFIXES):
                        continue
                    frames = []
                    for position, frame in enumerate(page.frames):
                        try:
                            frames.append((position, frame))
                        except Exception:  # noqa: BLE001 - detached mid-iteration
                            continue
                    for position, frame in frames:
                        if stop_reason:
                            break
                        markup = _sanitize(frame)
                        if not markup or len(markup) < _MIN_MARKUP_CHARS:
                            continue
                        signature = _structural_signature(markup)
                        if signature in seen:
                            continue
                        seen.add(signature)
                        index = len(saved) + 1
                        try:
                            frame_name = frame.name or ""
                        except Exception:  # noqa: BLE001 - detached frame
                            frame_name = ""
                        label = _slug(frame_name) if frame_name else f"f{position}"
                        try:
                            current_url = _strip_url(frame.url)
                        except Exception:  # noqa: BLE001 - detached frame
                            current_url = page_url
                        path = _save(out_dir, index, current_url, label, markup)
                        saved.append((index, current_url, path))
                        url_log.append(f"{index:03d} [{label}] {current_url}")
                        print(f"  captured {path.name}  ({current_url})")
                        if _target_reached(markup):
                            if len(saved) <= 1:
                                print(
                                    "  (Clave Dinamica is the first screen seen;"
                                    " log out and rerun to capture the login flow)"
                                )
                            else:
                                stop_reason = "Clave Dinamica screen detected"
                                break
                        if len(saved) >= _MAX_SNAPSHOTS:
                            stop_reason = f"snapshot cap reached ({_MAX_SNAPSHOTS})"
                            break
                if stop_reason:
                    break
                for _ in range(int(max(args.interval, 0.2) * 10)):
                    time.sleep(0.1)
                    if _stop_requested():
                        stop_reason = "operator pressed Enter"
                        break
                else:
                    continue
                break

            print(f"\nCapture stopped: {stop_reason}.")

            (out_dir / "_session.txt").write_text(
                "URLs observed (query strings stripped):\n"
                + "\n".join(url_log)
                + "\n",
                encoding="utf-8",
            )
            browser.close()
    except KeyboardInterrupt:
        print("\nInterrupted by operator.")
    except Exception as exc:  # noqa: BLE001 - one-off tool, surface the reason
        print(f"Capture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"\nDone. {len(saved)} unique screen(s) saved to {out_dir.resolve()}")
    print("The browser is STILL OPEN and the session is preserved.")
    print(f"Next phase reconnects with connect_over_cdp('{_cdp_endpoint(args.port)}').")
    print("Review the files for personal data before sharing them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

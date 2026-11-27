"""Run the Telegram → Playwright test locally. A real payment requires --execute-payment."""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from dotenv import load_dotenv
from pydantic import SecretStr
from pydantic_settings import BaseSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.core.exceptions import ConflictError, ExternalServiceError, PaymentRailError  # noqa: E402
from app.domain.banesco import BanescoPaymentTarget  # noqa: E402
from app.infrastructure.clients.banesco_browser import BanescoBrowser, BanescoReceipt  # noqa: E402
from app.infrastructure.clients.telegram_bot import TelegramBot  # noqa: E402
from app.infrastructure.receipts import store_receipt  # noqa: E402

if TYPE_CHECKING:
    from app.core.config import Settings

_DEMO_CODE = "123456"
_JOURNAL = REPO_ROOT / "data" / "banesco-telegram-pilot.sqlite3"


class LocalTelegramSettings(BaseSettings):
    telegram_bot_token: SecretStr
    telegram_operator_chat_id: int
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_clave_timeout_seconds: int = 30

    def check(self) -> None:
        if not re.fullmatch(
            r"[0-9]+:[A-Za-z0-9_-]+", self.telegram_bot_token.get_secret_value()
        ):
            raise ValueError("TELEGRAM_BOT_TOKEN no tiene el formato esperado.")
        if self.telegram_operator_chat_id <= 0:
            raise ValueError(
                "TELEGRAM_OPERATOR_CHAT_ID debe ser el número de tu chat privado."
            )
        if not 1 <= self.telegram_clave_timeout_seconds <= 30:
            raise ValueError("TELEGRAM_CLAVE_TIMEOUT_SECONDS debe estar entre 1 y 30.")
        if self.telegram_api_base_url.rstrip("/") != "https://api.telegram.org":
            raise ValueError("Este script usa la API oficial de Telegram.")


def reserve_attempt(
    journal: Path, key: str, amount: Decimal, settings: Settings
) -> None:
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Usa un monto con hasta dos decimales.")
    if amount < settings.min_tx_amount_ves or amount > min(
        settings.max_buy_amount_ves, settings.max_tx_amount_ves
    ):
        raise ValueError("El monto está fuera de los límites configurados.")
    journal.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(journal, isolation_level=None)) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS attempts ("
            "key TEXT PRIMARY KEY, utc_day TEXT NOT NULL, cents INTEGER NOT NULL, "
            "result TEXT NOT NULL, receipt_sha256 TEXT)"
        )
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM attempts WHERE key = ?", (key,)).fetchone():
            raise ConflictError(
                "Ese intento ya existe. Verifica el banco antes de hacer otro pago."
            )
        day = datetime.now(timezone.utc).date().isoformat()
        (used,) = db.execute(
            "SELECT COALESCE(SUM(cents), 0) FROM attempts WHERE utc_day = ?", (day,)
        ).fetchone()
        cents = int(amount * 100)
        cap = min(settings.daily_pay_limit_ves, settings.daily_limit_ves)
        if used + cents > int(cap * 100):
            raise ValueError("El monto supera el tope diario de este piloto.")
        db.execute(
            "INSERT INTO attempts VALUES (?, ?, ?, 'started', NULL)", (key, day, cents)
        )
        db.commit()


def record_result(
    journal: Path, key: str, result: str, receipt: str | None = None
) -> None:
    with closing(sqlite3.connect(journal)) as db, db:
        db.execute(
            "UPDATE attempts SET result = ?, receipt_sha256 = ? WHERE key = ?",
            (result, receipt, key),
        )


async def relay_payment(
    settings: Settings,
    target: BanescoPaymentTarget,
    telegram: TelegramBot,
    *,
    browser=BanescoBrowser,
    keep_open: bool = False,
) -> BanescoReceipt:
    async with browser(settings) as bank:
        try:
            await bank.fill_and_submit_payment(target)
            code = await telegram.request_code(
                f"Banesco solicita el código para el pago de Bs. {target.amount_ves} "
                f"a {target.destination_bank_option}, teléfono terminado en {target.phone_rest[-4:]}."
            )
            try:
                deadline = time.monotonic() + settings.banesco_browser_timeout_ms / 1000
                await bank.authorize_payment(code, deadline)
            finally:
                del code
            return await bank.read_receipt()
        finally:
            if keep_open:
                await asyncio.to_thread(
                    input, "Revisa la pantalla. Presiona Enter para cerrar Banesco. "
                )


async def run(args: argparse.Namespace) -> int:
    if args.env_file:
        env_file = args.env_file
    else:
        env_file = REPO_ROOT / ".env"
        if not env_file.is_file():
            env_file = REPO_ROOT.parent / ".env"
    load_dotenv(env_file, interpolate=False)

    try:
        bot_settings = LocalTelegramSettings()
        bot_settings.check()
    except ValueError:
        print(
            "Revisa TELEGRAM_BOT_TOKEN, TELEGRAM_OPERATOR_CHAT_ID y el plazo de Telegram en tu .env."
        )
        return 2

    async with httpx.AsyncClient(timeout=12.0, trust_env=False) as client:
        telegram = TelegramBot(bot_settings, client)
        if not args.execute_payment:
            print("Esperando tu respuesta en Telegram. Usa el código de prueba 123456.")
            try:
                answer = await telegram.request_code(
                    "Prueba local de liquidVE. Introduce el código de prueba 123456."
                )
            except asyncio.TimeoutError:
                print("Terminó el plazo para responder.")
                return 2
            except ExternalServiceError:
                print(
                    "Telegram no respondió. Comprueba el chat del bot y vuelve a ejecutar la prueba."
                )
                return 2
            if answer != _DEMO_CODE:
                print("La respuesta llegó, pero no coincide con el código de prueba.")
                return 2
            print("Prueba correcta. liquidVE recibió tu respuesta de Telegram.")
            return 0

        try:
            from app.core.config import Settings
            from scripts.banesco_payment_dry_run import build_target

            settings = Settings(
                banesco_rail_enabled=False,
                telegram_enabled=True,
                **({"banesco_browser_headless": False} if args.keep_open else {}),
            )
            target = build_target(args)
            reserve_attempt(_JOURNAL, args.attempt_id, target.amount_ves, settings)
        except ConflictError as exc:
            print(exc.message)
            return 2
        except (ValueError, PaymentRailError):
            print("Revisa el destino, el monto y los límites de pago en tu .env.")
            return 2
        print("Esperando una respuesta en Telegram para autorizar el pago de Banesco.")
        try:
            receipt = await relay_payment(
                settings, target, telegram, keep_open=args.keep_open
            )
            digest = store_receipt(
                settings.receipt_storage_dir, target.amount_ves, receipt
            )
            record_result(_JOURNAL, args.attempt_id, "confirmed", digest)
        except (Exception, asyncio.CancelledError) as exc:
            record_result(_JOURNAL, args.attempt_id, "review")
            print(
                f"Resultado pendiente de revisión ({type(exc).__name__}). Verifica los movimientos en Banesco."
            )
            return 2
        print(
            f"Pago confirmado. Comprobante: {Path(settings.receipt_storage_dir) / (digest + '.png')}"
        )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--execute-payment",
        action="store_true",
        help="Autoriza un pago real después de validarlo en Telegram.",
    )
    parser.add_argument("--attempt-id", help="Identificador único de la operación.")
    parser.add_argument("--phone")
    parser.add_argument("--document")
    parser.add_argument("--bank")
    parser.add_argument("--amount")
    parser.add_argument("--concept", default="pago")
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="Mantiene Banesco abierto para inspección manual.",
    )
    args = parser.parse_args()
    if args.execute_payment and not all(
        (args.attempt_id, args.phone, args.document, args.bank, args.amount)
    ):
        parser.error(
            "--execute-payment requiere --attempt-id, --phone, --document, --bank y --amount."
        )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

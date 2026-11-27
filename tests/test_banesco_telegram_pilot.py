"""Exercise the local Telegram path with a simulated bank and Telegram API."""

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from decimal import Decimal

import httpx
import pytest

from app.core.exceptions import ConflictError
from app.domain.banesco import BanescoPaymentTarget
from app.infrastructure.clients.banesco_browser import BanescoReceipt
from app.infrastructure.clients.telegram_bot import TelegramBot
from scripts.banesco_telegram_payment import relay_payment, reserve_attempt

_TARGET = BanescoPaymentTarget(
    "0414",
    "1234567",
    "V",
    "12345678",
    "0134",
    "BANESCO BANCO UNIVERSAL S.A.C.A.",
    Decimal("100.00"),
    "pago",
)


def update(chat, reply, code, *, update_id=1, sender=None, chat_type="private"):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat, "type": chat_type},
            "from": {"id": chat if sender is None else sender},
            "reply_to_message": {"message_id": reply},
            "text": code,
        },
    }


async def test_only_the_private_reply_to_the_current_prompt_reaches_playwright(
    test_settings, caplog
):
    chat = test_settings.telegram_operator_chat_id
    steps = []

    class FakeBank:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def fill_and_submit_payment(self, target):
            assert target == _TARGET
            steps.append("prepare_bank")

        async def authorize_payment(self, code, _deadline):
            assert code == "012345"
            steps.append("submit_once")

        async def read_receipt(self):
            steps.append("read_receipt")
            return BanescoReceipt("789012", _TARGET.amount_ves, b"synthetic")

    async def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/sendMessage"):
            steps.append("send_notice")
            assert body["chat_id"] == chat
            assert body["reply_markup"]["force_reply"]
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "chat": {"id": chat, "type": "private"},
                        "message_id": 40,
                    },
                },
            )
        if not body.get("offset"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        update(chat + 1, 40, "888888", update_id=1),
                        update(chat, 39, "888888", update_id=2),
                        update(chat, 40, "xx", update_id=3),
                        update(chat, 40, "888888", update_id=4, sender=chat + 1),
                        update(chat, 40, "888888", update_id=5, chat_type="group"),
                    ],
                },
            )
        assert body["offset"] == 6
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    update(chat, 40, "012345", update_id=6),
                    update(chat, 40, "111111", update_id=7),
                ],
            },
        )

    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        receipt = await relay_payment(
            test_settings,
            _TARGET,
            TelegramBot(test_settings, client),
            browser=lambda _: FakeBank(),
        )
    assert receipt.amount == _TARGET.amount_ves
    assert steps == ["prepare_bank", "send_notice", "submit_once", "read_receipt"]
    assert "012345" not in caplog.text
    assert test_settings.telegram_bot_token.get_secret_value() not in caplog.text


async def test_telegram_reply_window_ends_without_a_code(test_settings):
    test_settings.telegram_clave_timeout_seconds = 1
    chat = test_settings.telegram_operator_chat_id

    async def handler(request):
        if request.url.path.endswith("/sendMessage"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "chat": {"id": chat, "type": "private"},
                        "message_id": 1,
                    },
                },
            )
        await asyncio.sleep(2)
        raise AssertionError("the request must have been cancelled")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(asyncio.TimeoutError):
            await TelegramBot(test_settings, client).request_code("Código de prueba")


def test_duplicate_attempt_keeps_its_reservation(test_settings, tmp_path):
    journal = tmp_path / "pilot.sqlite3"
    reserve_attempt(journal, "first", Decimal("1500.00"), test_settings)
    with pytest.raises(ConflictError):
        reserve_attempt(journal, "first", Decimal("1500.00"), test_settings)
    test_settings.daily_pay_limit_ves = Decimal("2000.00")
    with pytest.raises(ValueError, match="tope diario"):
        reserve_attempt(journal, "other", Decimal("600.00"), test_settings)


def test_telegram_cli_can_load_without_banking_configuration(tmp_path):
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "banesco_telegram_payment.py"
    )
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env={"DEBUG": "release"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--execute-payment" in result.stdout

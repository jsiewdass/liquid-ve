"""Offline integration tests for the authenticated manual payment path."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.exceptions import BanescoPaymentOutcomeUnknown, ExternalServiceError
from app.domain.enums import OrderType
from app.domain.ports import PaymentInstruction
from app.infrastructure.clients.banesco_browser import BanescoReceipt
from app.infrastructure.clients.banesco_pages import parse_receipt
from app.infrastructure.clients.binance_client import BinanceC2COrder
from app.infrastructure.clients.telegram_bot import TelegramBot
from app.infrastructure.database import get_session
from app.infrastructure.models import BankTransaction, P2POrder
from app.main import create_app
from app.services import banesco_payment
from tests.conftest import _override_session

ORDER = "ORDER-DEMO-001"
AMOUNT = Decimal("1500.00")
PNG = b"\x89PNG\r\n\x1a\nsynthetic-test-image"
PAY = f"/api/v1/orders/buy/{ORDER}/pay"
STATUS = f"/api/v1/orders/buy/{ORDER}/payment"


class FakeBank:
    def __init__(self) -> None:
        self.started = 0
        self.codes: list[str] = []
        self.receipt = BanescoReceipt("987654321", AMOUNT, PNG)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def fill_and_submit_payment(self, target):
        self.started += 1
        assert target.amount_ves == AMOUNT

    async def authorize_payment(self, code, deadline):
        assert time.monotonic() < deadline
        self.codes.append(code)

    async def read_receipt(self):
        return self.receipt


class FakeNotice:
    def __init__(self) -> None:
        self.delivery: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    async def request_authorization(self, order, url):
        await self.delivery.put((order, url))

    async def receive(self):
        order, url = await asyncio.wait_for(self.delivery.get(), timeout=2)
        assert order == ORDER
        link = urlsplit(url)
        assert not link.query
        return link.path, {"Authorization": f"Bearer {link.fragment}"}


@pytest.fixture()
async def pilot(
    test_settings, engine, fake_binance, fake_browser, monkeypatch, tmp_path
):
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def scope():
        async with factory.begin() as session:
            yield session

    monkeypatch.setattr(banesco_payment, "session_scope", scope)
    test_settings.operator_public_url = "https://operator.example"
    test_settings.receipt_storage_dir = str(tmp_path / "receipts")
    notice = FakeNotice()
    bank = FakeBank()
    payments = banesco_payment.BanescoPayments(
        test_settings, notice, browser=lambda _: bank
    )
    application = create_app(test_settings)
    application.state.banesco_payments = payments
    application.state.binance = fake_binance
    application.state.browser = fake_browser
    application.dependency_overrides[get_session] = _override_session(engine)
    fake_binance.register(
        BinanceC2COrder(
            order_number=ORDER,
            trade_type=OrderType.BUY,
            asset="USDT",
            fiat="VES",
            price=Decimal("150.00"),
            amount=Decimal("10"),
            total_price=AMOUNT,
            status="PENDING_PAYMENT",
            created_at=datetime.now(timezone.utc),
        )
    )
    fake_browser.instruction = PaymentInstruction(
        order_number=ORDER,
        bank_numeric_code="0134",
        phone="04141234567",
        id_number="V12345678",
        amount_ves=AMOUNT,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="https://operator.example",
    ) as client:
        operator = {"X-API-Key": test_settings.internal_api_key.get_secret_value()}
        try:
            yield client, operator, payments, bank, notice, factory
        finally:
            await payments.aclose()


async def test_payment_authorization_is_one_time_and_persists_receipt(
    pilot, test_settings
):
    client, operator, payments, bank, notice, factory = pilot
    assert (await client.post(PAY)).status_code == 401
    start = await client.post(PAY, headers=operator)
    assert start.status_code == 202
    assert start.json()["status"] == "SENDING_PAYMENT"
    path, auth = await notice.receive()
    page = await client.get(path)
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert (await client.get(f"{path}/details")).status_code == 410
    details = await client.get(f"{path}/details", headers=auth)
    assert details.json()["amount"] == str(AMOUNT)
    assert 0 < details.json()["milliseconds_left"] <= 30_000
    assert details.json()["phone_last4"] == "4567"
    assert "12345678" not in details.text

    invalid = await client.post(path, headers=auth, json={"code": "do-not-echo"})
    assert invalid.status_code == 422
    assert "do-not-echo" not in invalid.text
    first, second = await asyncio.gather(
        *(client.post(path, headers=auth, json={"code": "012345"}) for _ in range(2))
    )
    assert sorted((first.status_code, second.status_code)) == [202, 410]
    await asyncio.wait_for(payments._task, timeout=2)

    result = (await client.get(STATUS, headers=operator)).json()
    assert result["status"] == "PAYMENT_CONFIRMED"
    async with factory() as session:
        payment = await session.scalar(select(BankTransaction))
        assert payment.amount == AMOUNT
        assert payment.verified
        assert payment.bank_reference == "987654321"
        order = await session.scalar(select(P2POrder))
        assert order.payment_slot is None
    receipt = (
        Path(test_settings.receipt_storage_dir) / f'{result["receipt_sha256"]}.png'
    )
    assert receipt.read_bytes() == PNG
    if os.name == "posix":
        # POSIX permissions do not exist on Windows; the write is still atomic.
        assert receipt.stat().st_mode & 0o077 == 0
    assert (await client.post(PAY, headers=operator)).status_code == 200
    assert bank.started == 1
    assert bank.codes == ["012345"]


async def test_expired_entry_window_parks_order_without_bank_submission(
    pilot, test_settings
):
    client, operator, payments, bank, notice, _ = pilot
    test_settings.telegram_clave_timeout_seconds = 1
    assert (await client.post(PAY, headers=operator)).status_code == 202
    path, auth = await notice.receive()
    assert (await client.get(f"{path}/details", headers=auth)).status_code == 200
    await asyncio.wait_for(payments._task, timeout=2)
    assert (
        await client.post(path, headers=auth, json={"code": "123456"})
    ).status_code == 410
    result = (await client.get(STATUS, headers=operator)).json()
    assert result["status"] == "MANUAL_REVIEW"
    assert result["manual_review_reason"] == "operator_entry_timeout"
    assert bank.codes == []


async def test_copy_window_starts_on_form_open_and_cannot_be_reset(
    pilot, test_settings
):
    client, operator, payments, bank, notice, _ = pilot
    test_settings.telegram_clave_timeout_seconds = 1
    assert (await client.post(PAY, headers=operator)).status_code == 202
    path, auth = await notice.receive()
    await asyncio.sleep(1.05)
    assert (
        await client.post(path, headers=auth, json={"code": "012345"})
    ).status_code == 410

    first = await client.get(f"{path}/details", headers=auth)
    assert first.status_code == 200
    assert first.json()["milliseconds_left"] > 750
    await asyncio.sleep(0.05)
    second = await client.get(f"{path}/details", headers=auth)
    assert second.status_code == 200
    assert 0 < second.json()["milliseconds_left"] < first.json()["milliseconds_left"]
    assert (
        await client.post(path, headers=auth, json={"code": "012345"})
    ).status_code == 202
    await asyncio.wait_for(payments._task, timeout=2)
    assert bank.codes == ["012345"]


async def test_unopened_link_has_its_own_timeout(pilot, test_settings):
    client, operator, payments, bank, notice, _ = pilot
    test_settings.operator_link_timeout_seconds = 1
    assert (await client.post(PAY, headers=operator)).status_code == 202
    path, auth = await notice.receive()
    await asyncio.wait_for(payments._task, timeout=2)
    assert (await client.get(f"{path}/details", headers=auth)).status_code == 410
    result = (await client.get(STATUS, headers=operator)).json()
    assert result["manual_review_reason"] == "operator_link_timeout"
    assert bank.codes == []


async def test_receipt_amount_mismatch_blocks_confirmation_and_retry(pilot):
    client, operator, payments, bank, notice, factory = pilot
    bank.receipt = BanescoReceipt("987654321", AMOUNT + 1, PNG)
    assert (await client.post(PAY, headers=operator)).status_code == 202
    path, auth = await notice.receive()
    assert (await client.get(f"{path}/details", headers=auth)).status_code == 200
    assert (
        await client.post(path, headers=auth, json={"code": "123456"})
    ).status_code == 202
    await asyncio.wait_for(payments._task, timeout=2)
    result = (await client.get(STATUS, headers=operator)).json()
    assert result["status"] == "MANUAL_REVIEW"
    assert result["manual_review_reason"] == "receipt_amount_mismatch"
    async with factory() as session:
        assert await session.scalar(select(BankTransaction)) is None
    assert (await client.post(PAY, headers=operator)).status_code == 200
    assert bank.started == 1


async def test_interrupted_attempt_closes_link_and_cannot_restart(pilot):
    client, operator, payments, bank, notice, _ = pilot
    assert (await client.post(PAY, headers=operator)).status_code == 202
    path, auth = await notice.receive()
    await payments.aclose()
    result = (await client.get(STATUS, headers=operator)).json()
    assert result["manual_review_reason"] == "payment_interrupted"
    assert (
        await client.post(path, headers=auth, json={"code": "123456"})
    ).status_code == 410
    assert (await client.post(PAY, headers=operator)).status_code == 200
    assert bank.codes == []


async def test_sms_webhook_has_been_removed(pilot):
    client, _, _, _, _, _ = pilot
    assert (await client.post("/api/v1/webhooks/sms")).status_code == 404


def test_receipt_requires_success_heading_and_exact_fields():
    receipt = "<p>Comprobante de Pago Móvil</p><p>Referencia:</p><p>789012</p><p>Monto:</p><p>1.500,00</p>"
    assert parse_receipt(receipt) == ("789012", AMOUNT)
    with pytest.raises(BanescoPaymentOutcomeUnknown):
        parse_receipt(receipt + '<input id="otp">')
    with pytest.raises(BanescoPaymentOutcomeUnknown):
        parse_receipt(receipt + "<p>Referencia:</p><p>123456</p>")
    with pytest.raises(BanescoPaymentOutcomeUnknown):
        parse_receipt(receipt.replace("Comprobante de Pago Móvil", "Confirmación"))


async def test_telegram_send_keeps_token_out_of_logs(test_settings, caplog):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(503)

    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBot(test_settings, client)
        with pytest.raises(ExternalServiceError) as exc:
            await bot.request_authorization(
                ORDER, "https://operator.example/page#one-use"
            )
    assert len(requests) == 1
    payload = requests[0].content.decode()
    assert "Abrir autorización" in payload
    assert "#one-use" in payload
    credential = test_settings.telegram_bot_token.get_secret_value()
    assert credential not in caplog.text
    assert credential not in str(exc.value)


async def test_telegram_notification_fails_closed_on_delivery_error(pilot, monkeypatch):
    client, operator, payments, bank, notice, _ = pilot

    async def unavailable(*_):
        raise ExternalServiceError("Notice unavailable", service="telegram")

    monkeypatch.setattr(notice, "request_authorization", unavailable)
    assert (await client.post(PAY, headers=operator)).status_code == 202
    await asyncio.wait_for(payments._task, timeout=2)
    result = (await client.get(STATUS, headers=operator)).json()
    assert result["manual_review_reason"] == "telegram_notice_failed"
    assert bank.codes == []

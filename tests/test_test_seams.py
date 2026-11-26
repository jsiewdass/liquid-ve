"""One-engine ASGI and provider-double test seams.

These tests pin the contract of the shared fixtures used by the webhook and
direction-guard suites: the ASGI client must read and write the *same*
in-memory engine the assertions use, and it must never initialise or mutate the
module-global engine that the plain ``client`` fixture owns.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.enums import OrderStatus, OrderType
from app.domain.ports import PaymentInstruction
from app.infrastructure import database
from app.infrastructure.clients.binance_client import (
    BinanceC2CClient,
    BinanceC2COrder,
)
from app.infrastructure.models import P2POrder, WebhookEvent


def _cachicamo_body(event_id: str, amount: str = "1500.00") -> bytes:
    """Build a signed-body candidate Cachicamo payload.

    Args:
        event_id: Provider event identifier.
        amount: Fiat amount in VES.

    Returns:
        The JSON body encoded as bytes.
    """
    payload = {
        "event_id": event_id,
        "event_type": "PAYMENT_RECEIVED",
        "bank_reference": f"REF-{event_id}",
        "amount_ves": amount,
        "payer_phone": "+584141234567",
        "bank_code": "BANESCO",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(payload).encode("utf-8")


async def test_app_client_shares_the_override_engine(
    app_client: httpx.AsyncClient,
    signed_cachicamo_headers: object,
    session: AsyncSession,
) -> None:
    """A webhook written by the ASGI app is visible on the assertion session."""
    body = _cachicamo_body("evt-seam-shared")
    headers = signed_cachicamo_headers(body, event_id="evt-seam-shared")  # type: ignore[operator]

    response = await app_client.post(
        "/api/v1/webhooks/cachicamo", content=body, headers=headers
    )

    assert response.status_code == 202
    result = await session.execute(
        select(WebhookEvent).where(
            WebhookEvent.provider == "cachicamo",
            WebhookEvent.external_event_id == "evt-seam-shared",
        )
    )
    event = result.scalar_one_or_none()
    assert event is not None
    assert event.payload["event_id"] == "evt-seam-shared"


async def test_app_client_does_not_touch_module_global_engine(
    app_client: httpx.AsyncClient,
) -> None:
    """Building and using the ASGI client leaves the global engine untouched."""
    before = database.engine

    response = await app_client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert database.engine is before


async def test_fake_binance_records_and_returns_orders(
    fake_binance: object,
) -> None:
    """The Binance double records every call and always returns a real DTO."""
    detail = await fake_binance.get_order_detail("ORD-SEAM")  # type: ignore[attr-defined]

    assert isinstance(detail, BinanceC2COrder)
    assert detail.order_number == "ORD-SEAM"
    assert detail.trade_type is OrderType.SELL

    await fake_binance.release_order("ORD-SEAM")  # type: ignore[attr-defined]
    await fake_binance.mark_order_as_paid("ORD-SEAM")  # type: ignore[attr-defined]

    assert fake_binance.detail_calls == ["ORD-SEAM"]  # type: ignore[attr-defined]
    assert fake_binance.release_calls == ["ORD-SEAM"]  # type: ignore[attr-defined]
    assert fake_binance.mark_paid_calls == ["ORD-SEAM"]  # type: ignore[attr-defined]


async def test_seed_order_is_deterministic(seed_order: object) -> None:
    """Two seeded orders share the defaults while staying independently persisted."""
    first = await seed_order(order_number="ORD-SEED-1")  # type: ignore[operator]
    second = await seed_order(order_number="ORD-SEED-2")  # type: ignore[operator]

    assert isinstance(first, P2POrder) and isinstance(second, P2POrder)
    assert first.status is OrderStatus.PENDING
    assert first.order_type is OrderType.SELL
    assert first.amount_fiat == Decimal("1500.00")
    assert first.amount_asset == Decimal("10.00000000")
    assert first.price == Decimal("150.00000000")
    assert second.amount_fiat == first.amount_fiat
    assert second.price == first.price
    assert first.id != second.id

    buy = await seed_order(  # type: ignore[operator]
        order_number="ORD-SEED-BUY",
        order_type=OrderType.BUY,
        amount_fiat=Decimal("2500.00"),
    )
    assert buy.order_type is OrderType.BUY
    assert buy.amount_fiat == Decimal("2500.00")


@pytest.mark.parametrize("amount", ["1.00", "9999.99"])
async def test_seed_order_accepts_override_amounts(
    seed_order: object, amount: str
) -> None:
    """The override path is exercised with distinct, non-trivial amounts."""
    order = await seed_order(  # type: ignore[operator]
        order_number=f"ORD-SEED-{amount}", amount_fiat=Decimal(amount)
    )

    assert order.amount_fiat == Decimal(amount)


def test_fake_browser_conforms_to_binance_browser_port(fake_browser: object) -> None:
    """The double structurally matches the port without an isinstance check."""
    extract = fake_browser.extract_payment_instruction  # type: ignore[attr-defined]
    notify = fake_browser.notify_paid  # type: ignore[attr-defined]

    assert inspect.iscoroutinefunction(extract)
    assert inspect.iscoroutinefunction(notify)
    assert list(inspect.signature(extract).parameters) == ["order_number"]
    assert list(inspect.signature(notify).parameters) == ["order_number", "reference"]


async def test_fake_browser_records_ordered_events(fake_browser: object) -> None:
    """Extraction and notification append ordered, scriptable events."""
    instruction = PaymentInstruction(
        order_number="ORD-SEAM",
        bank_numeric_code="0102",
        phone="04141234567",
        id_number="V-12345678",
        amount_ves=Decimal("1500.00"),
    )
    fake_browser.instruction = instruction  # type: ignore[attr-defined]

    returned = await fake_browser.extract_payment_instruction(  # type: ignore[attr-defined]
        "ORD-SEAM"
    )
    await fake_browser.notify_paid("ORD-SEAM", "REF-0001")  # type: ignore[attr-defined]

    assert returned is instruction
    assert fake_browser.events == [  # type: ignore[attr-defined]
        ("extract", 1),
        ("notify", 1, "REF-0001"),
    ]


async def test_fake_browser_scripts_errors(fake_browser: object) -> None:
    """Scripted errors are raised after the event is recorded."""
    fake_browser.extract_error = RuntimeError("extract boom")  # type: ignore[attr-defined]
    fake_browser.notify_error = RuntimeError("notify boom")  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="extract boom"):
        await fake_browser.extract_payment_instruction(  # type: ignore[attr-defined]
            "ORD-SEAM"
        )
    with pytest.raises(RuntimeError, match="notify boom"):
        await fake_browser.notify_paid(  # type: ignore[attr-defined]
            "ORD-SEAM", "REF-0001"
        )

    assert fake_browser.events == [  # type: ignore[attr-defined]
        ("extract", 1),
        ("notify", 1, "REF-0001"),
    ]


async def test_fake_sms_records_idempotency_key(fake_sms: object) -> None:
    """The SMS double records the destination short code and the wire key."""
    result = await fake_sms.send_sms(  # type: ignore[attr-defined]
        phone_number="2661",
        message="Pagar 0102 04141234567 12345678 1500,00",
        idempotency_key="ORD-SEAM:payout",
    )

    assert result.message_id == "msg-1"
    assert fake_sms.calls == [  # type: ignore[attr-defined]
        ("2661", "Pagar 0102 04141234567 12345678 1500,00", "ORD-SEAM:payout")
    ]


def test_fake_browserless_is_none(fake_browserless: None) -> None:
    """The browserless double is an explicit ``None``."""
    assert fake_browserless is None


@respx.mock
async def test_binance_history_route_matches_presigned_query(
    test_settings: Settings,
) -> None:
    """The history route helper matches the pre-signed query string."""
    route = respx.post(
        url__regex=r".*/sapi/v1/c2c/orderMatch/listUserOrderHistory\?.*"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    async with BinanceC2CClient(test_settings) as client:
        orders = await client.get_order_history(trade_type=OrderType.BUY)

    assert orders == []
    assert len(route.calls) == 1
    assert route.calls[0].request.url.params["tradeType"] == "BUY"


def test_shared_seams_are_offline_browser_free_and_network_free() -> None:
    """Importing the Banesco seams never imports Playwright or opens a socket."""
    repo_root = Path(__file__).resolve().parents[1]
    tests_dir = Path(__file__).resolve().parent
    probe = r"""
import socket
import sys

sys.path.insert(0, r"{tests_dir}")
sys.path.insert(0, r"{repo_root}")


class _NoSocket:
    def __init__(self, *args, **kwargs):
        raise AssertionError("a socket was opened")


socket.socket = _NoSocket

import tests.conftest as seams  # noqa: E402

assert "playwright" not in sys.modules, "conftest imported playwright"
page = seams.FakePage("login.html")
context = seams.FakeBanescoContext(page)
notifier = seams.FakeOperatorNotifier()
telegram = seams.FakeTelegramBotClient()
assert page.fixture_name == "login.html"
assert context.page is page
assert notifier is not None
assert telegram is not None
print("OFFLINE_SEAMS_OK")
""".format(tests_dir=str(tests_dir), repo_root=str(repo_root))

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "OFFLINE_SEAMS_OK" in result.stdout


def test_banesco_fixture_catalog_note_documents_names_and_sanitization() -> None:
    """The catalog note names every recorded fixture and states the sanitization rule."""
    from tests.conftest import BANESCO_FIXTURE_CATALOG

    note = Path(__file__).parent / "fixtures" / "banesco" / "README.md"
    text = note.read_text(encoding="utf-8")

    for name in BANESCO_FIXTURE_CATALOG:
        assert name in text
    assert "sanitized" in text.lower()
    assert "credential" in text.lower()


async def test_fake_page_and_context_record_screen_actions() -> None:
    """The fake page records every action and the context closes offline."""
    from tests.conftest import FakeBanescoContext, FakePage

    page = FakePage("login.html")

    async with FakeBanescoContext(page) as context:
        assert await context.new_page() is page
        await page.goto("https://example.test/login")
        await page.fill("#user", "test-user")
        await page.select_option("#bank", "0102")
        await page.click("#submit")
        screenshot = await page.screenshot("receipt.png")

    assert screenshot == b"fake-receipt-bytes"
    assert page.actions == [
        ("goto", "https://example.test/login"),
        ("fill", "#user", "test-user"),
        ("select_option", "#bank", "0102"),
        ("click", "#submit"),
        ("screenshot", "receipt.png"),
    ]
    assert context.closed is True


async def test_fake_operator_notifier_scripts_correlated_replies() -> None:
    """The confirmation double replays scripted replies in order."""
    from tests.conftest import FakeOperatorNotifier

    notifier = FakeOperatorNotifier()
    notifier.replies.extend(["123456", None])

    first = await notifier.request_confirmation(
        order_number="ORD-SEAM",
        reason="CLAVE",
        prompt="Send the clave",
    )
    second = await notifier.request_confirmation(
        order_number="ORD-SEAM",
        reason="ID_TYPE",
        prompt="Send the ID letter",
    )

    assert first == "123456"
    assert second is None
    assert [request["reason"] for request in notifier.requests] == ["CLAVE", "ID_TYPE"]


async def test_fake_telegram_transport_records_and_replays_updates() -> None:
    """The Telegram double records sends and drains scripted updates."""
    from tests.conftest import FakeTelegramBotClient

    fake = FakeTelegramBotClient()
    fake.updates.append({"update_id": 7, "message": {"text": "123456"}})

    sent = await fake.send_message(
        chat_id=123456789,
        text="Send the clave",
        protect_content=True,
    )
    updates = await fake.get_updates(offset=6, timeout=1)

    assert sent["ok"] is True
    assert fake.sent[0]["chat_id"] == 123456789
    assert fake.sent[0]["protect_content"] is True
    assert updates == [{"update_id": 7, "message": {"text": "123456"}}]
    assert fake.get_updates_calls == [{"offset": 6, "timeout": 1}]


async def test_telegram_bot_api_helper_intercepts_without_a_socket() -> None:
    """The respx Bot API helper answers sendMessage/getUpdates offline."""
    from tests.conftest import telegram_bot_api_mock

    with telegram_bot_api_mock() as (send_route, updates_route):
        async with httpx.AsyncClient() as client:
            sent = await client.post(
                "https://api.telegram.org/bottest-token/sendMessage",
                json={"chat_id": 1, "text": "hello"},
            )
            updates = await client.post(
                "https://api.telegram.org/bottest-token/getUpdates",
                json={"offset": 0, "timeout": 1},
            )

    assert sent.status_code == 200
    assert updates.status_code == 200
    assert len(send_route.calls) == 1
    assert len(updates_route.calls) == 1


def test_frozen_clock_helper_freezes_utc() -> None:
    """The freezegun helper pins the UTC clock for expiry assertions."""
    from tests.conftest import frozen_clock

    with frozen_clock("2026-11-25T12:00:00+00:00"):
        assert datetime.now(timezone.utc) == datetime(
            2026, 11, 25, 12, 0, tzinfo=timezone.utc
        )

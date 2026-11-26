"""Shared pytest fixtures.

No test requires network access.  Environment variables are populated before
any application import so the cached module-level settings can be built even
when no ``.env`` file exists.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-api-key-0123456789abcdef")
os.environ.setdefault("BINANCE_API_KEY", "test-binance-api-key")
os.environ.setdefault("BINANCE_API_SECRET", "test-binance-api-secret")
os.environ.setdefault("CACHICAMO_BASE_URL", "http://localhost:9001")
os.environ.setdefault("CACHICAMO_API_KEY", "test-cachicamo-api-key")
os.environ.setdefault(
    "CACHICAMO_WEBHOOK_SECRET",
    "test-cachicamo-webhook-secret-0123456789abcdef",
)
os.environ.setdefault("SMS_GATEWAY_BASE_URL", "http://100.64.0.10:8080")
os.environ.setdefault("SMS_GATEWAY_TOKEN", "test-sms-gateway-token")
os.environ.setdefault("MAX_TX_AMOUNT_VES", "5000.00")
os.environ.setdefault("DAILY_LIMIT_VES", "20000.00")
os.environ.setdefault("MAX_USDT_PER_ORDER", "1000.00000000")
os.environ.setdefault("MAX_BUY_AMOUNT_VES", "5000.00")
os.environ.setdefault("DAILY_PAY_LIMIT_VES", "20000.00")
# Deprecated alias kept for one release; both names resolve to the same cap.
os.environ.setdefault("DAILY_SMS_LIMIT_VES", "20000.00")
os.environ.setdefault("PAYMENT_CONFIRMATION_TIMEOUT_SECONDS", "300")
os.environ.setdefault("BANESCO_RAIL_ENABLED", "false")
os.environ.setdefault("BANESCO_ONLINE_LOGIN_URL", "https://www.banesco.com/")
os.environ.setdefault("BANESCO_ONLINE_USER", "test-banesco-user")
os.environ.setdefault("BANESCO_ONLINE_PASSWORD", "test-banesco-password")
os.environ.setdefault("BANESCO_SECURITY_ANSWER_HOBBIE", "test-banesco-answer-hobbie")
os.environ.setdefault(
    "BANESCO_SECURITY_ANSWER_CHILDHOOD_HERO", "test-banesco-answer-childhood-hero"
)
os.environ.setdefault(
    "BANESCO_SECURITY_ANSWER_FAVOURITE_DESSERT", "test-banesco-answer-dessert"
)
os.environ.setdefault(
    "BANESCO_SECURITY_ANSWER_COUNTRY_TO_VISIT", "test-banesco-answer-country"
)
os.environ.setdefault("BANESCO_BROWSER_HEADLESS", "true")
os.environ.setdefault("BANESCO_BROWSER_TIMEOUT_MS", "30000")
os.environ.setdefault("BANESCO_MAX_CONCURRENT_PAYMENTS", "1")
os.environ.setdefault("BANESCO_CLAVE_RETRY_LIMIT", "0")
os.environ.setdefault("BANESCO_PAYMENT_CONCEPT", "Pago")
os.environ.setdefault("BANESCO_DEFAULT_ID_TYPE", "")
os.environ.setdefault("TELEGRAM_ENABLED", "false")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-bot-token")
os.environ.setdefault("TELEGRAM_OPERATOR_CHAT_ID", "123456789")
os.environ.setdefault("TELEGRAM_API_BASE_URL", "https://api.telegram.org")
os.environ.setdefault("TELEGRAM_CLAVE_TIMEOUT_SECONDS", "300")
os.environ.setdefault("TELEGRAM_ID_TYPE_TIMEOUT_SECONDS", "300")
os.environ.setdefault("TELEGRAM_POLL_TIMEOUT_SECONDS", "30")
os.environ.setdefault("TELEGRAM_MAX_CLARIFICATIONS", "2")
os.environ.setdefault("RECEIPT_STORAGE_DIR", "./data/receipts")
os.environ.setdefault("RECEIPT_RETENTION_DAYS", "30")
os.environ.setdefault("RELEASE_WATCHER_ENABLED", "false")
os.environ.setdefault("RELEASE_POLL_INTERVAL_SECONDS", "30")
os.environ.setdefault("RELEASE_TIMEOUT_SECONDS", "900")
os.environ.setdefault("RELEASE_APPEAL_MODE", "auto")
os.environ.setdefault("BUY_PAYMENT_RAIL", "banesco")

from contextlib import contextmanager  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
import re  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import respx  # noqa: E402
from freezegun import freeze_time  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.security import compute_hmac_signature  # noqa: E402
from app.domain.enums import BankCode, OrderStatus, OrderType, PaymentVerificationStatus  # noqa: E402
from app.domain.ports import PaymentInstruction  # noqa: E402
from app.infrastructure.clients.binance_client import BinanceC2COrder  # noqa: E402
from app.infrastructure.clients.cachicamo_client import (  # noqa: E402
    CachicamoVerificationResult,
)
from app.infrastructure.clients.sms_gateway import SmsSendResult  # noqa: E402
from app.infrastructure.database import (  # noqa: E402
    create_all,
    dispose_engine,
    get_session,
    init_engine,
)
from app.infrastructure.models import Base, P2POrder  # noqa: E402
from app.main import create_app  # noqa: E402

_TEST_CACHICAMO_WEBHOOK_SECRET = "test-cachicamo-webhook-secret-0123456789abcdef"


class FakeBinance:
    """Records Binance calls and returns deterministic order details."""

    def __init__(self) -> None:
        """Initialize an empty recording double."""
        self.orders: dict[str, BinanceC2COrder] = {}
        self.detail_calls: list[str] = []
        self.history_calls: list[dict[str, Any]] = []
        self.mark_paid_calls: list[str] = []
        self.release_calls: list[str] = []

    def register(self, detail: BinanceC2COrder) -> BinanceC2COrder:
        """Register a canned detail returned by :meth:`get_order_detail`.

        Args:
            detail: Order detail to return for its order number.

        Returns:
            The registered detail.
        """
        self.orders[detail.order_number] = detail
        return detail

    async def get_order_detail(self, order_number: str) -> BinanceC2COrder:
        """Record the lookup and return a canned or synthesized detail.

        Args:
            order_number: Provider order number.

        Returns:
            The registered detail, or a deterministic ``SELL`` default.
        """
        self.detail_calls.append(order_number)
        registered = self.orders.get(order_number)
        if registered is not None:
            return registered
        return BinanceC2COrder(
            order_number=order_number,
            trade_type=OrderType.SELL,
            asset="USDT",
            fiat="VES",
            price=Decimal("150.00000000"),
            amount=Decimal("10.00000000"),
            total_price=Decimal("1500.00"),
            status="PENDING",
            counterparty_nickname="counterparty",
            created_at=datetime.now(timezone.utc),
        )

    async def get_order_history(self, **kwargs: Any) -> list[BinanceC2COrder]:
        """Record the history query and return every registered order.

        Args:
            **kwargs: Query arguments accepted for signature compatibility.

        Returns:
            The registered orders.
        """
        self.history_calls.append(dict(kwargs))
        return list(self.orders.values())

    async def mark_order_as_paid(self, order_number: str) -> None:
        """Record a mark-as-paid call.

        Args:
            order_number: Provider order number.
        """
        self.mark_paid_calls.append(order_number)

    async def release_order(self, order_number: str) -> None:
        """Record a crypto-release call.

        Args:
            order_number: Provider order number.
        """
        self.release_calls.append(order_number)


class FakeCachicamo:
    """Verifies every payment so the sell path can complete offline."""

    async def verify_incoming_payment(
        self,
        *,
        bank_reference: str,
        amount_ves: Decimal,
        payer_phone: str | None = None,
        bank_code: BankCode | None = None,
    ) -> CachicamoVerificationResult:
        """Return a positive verification without any HTTP traffic.

        Args:
            bank_reference: Bank reference of the payment.
            amount_ves: Received amount in VES.
            payer_phone: Optional payer phone.
            bank_code: Optional originating bank.

        Returns:
            A ``VERIFIED`` verification result.
        """
        return CachicamoVerificationResult(
            status=PaymentVerificationStatus.VERIFIED,
            bank_reference=bank_reference,
            amount_ves=amount_ves,
            bank_code=bank_code,
            verified_at=datetime.now(timezone.utc),
        )


class FakeBrowser:
    """Scriptable structural double for the ``BinanceBrowser`` port.

    Attributes:
        instruction: Instruction returned by extraction once configured.
        extract_error: Optional error raised by extraction after the event is
            recorded.
        notify_error: Optional error raised by notification after the event is
            recorded.
        events: Ordered ``("extract", n)`` / ``("notify", n, reference)``
            events, where ``n`` is the 1-based ordinal of that method call.
    """

    def __init__(self) -> None:
        """Initialize an unconfigured double."""
        self.instruction: PaymentInstruction | None = None
        self.extract_error: Exception | None = None
        self.notify_error: Exception | None = None
        self.events: list[tuple[object, ...]] = []
        self._extract_count = 0
        self._notify_count = 0

    async def extract_payment_instruction(
        self, order_number: str
    ) -> PaymentInstruction:
        """Record the extraction and return the scripted instruction.

        Args:
            order_number: Provider order number (recorded only through events).

        Returns:
            The configured instruction.

        Raises:
            RuntimeError: If no instruction was configured.
            Exception: The scripted ``extract_error`` when set.
        """
        self._extract_count += 1
        self.events.append(("extract", self._extract_count))
        if self.extract_error is not None:
            raise self.extract_error
        if self.instruction is None:
            raise RuntimeError("fake_browser.instruction is not configured")
        return self.instruction

    async def notify_paid(self, order_number: str, reference: str) -> None:
        """Record the notification and return or raise the scripted outcome.

        Args:
            order_number: Provider order number (recorded only through events).
            reference: Bank transaction reference.

        Raises:
            Exception: The scripted ``notify_error`` when set.
        """
        self._notify_count += 1
        self.events.append(("notify", self._notify_count, reference))
        if self.notify_error is not None:
            raise self.notify_error


class FakeSms:
    """Records outgoing SMS calls and scripts gateway failures.

    Attributes:
        calls: Recorded ``(phone_number, message, idempotency_key)`` tuples.
        errors: Exceptions raised in order on the next sends.
        on_send: Optional async hook invoked on every send before its outcome.
    """

    def __init__(self) -> None:
        """Initialize an empty recording double."""
        self.calls: list[tuple[str, str, str]] = []
        self.errors: list[Exception] = []
        self.on_send: Callable[[str], Awaitable[None]] | None = None

    async def send_sms(
        self,
        *,
        phone_number: str,
        message: str,
        idempotency_key: str,
    ) -> SmsSendResult:
        """Record the send and return a deterministic result.

        Args:
            phone_number: Destination short code.
            message: Command body.
            idempotency_key: Stable key for this message.

        Returns:
            A deterministic ``SENT`` result.

        Raises:
            Exception: The next scripted ``errors`` entry, when present.
        """
        self.calls.append((phone_number, message, idempotency_key))
        if self.on_send is not None:
            await self.on_send(phone_number)
        if self.errors:
            raise self.errors.pop(0)
        return SmsSendResult(
            message_id=f"msg-{len(self.calls)}",
            status="SENT",
            sent_at=datetime.now(timezone.utc),
        )


BANESCO_FIXTURE_CATALOG: tuple[str, ...] = (
    "login.html",
    "security_questions_a.html",
    "security_questions_b.html",
    "ad_modal.html",
    "payment_form.html",
    "confirmation.html",
    "clave_form.html",
    "receipt.html",
    "error_captcha.html",
    "error_locked.html",
    "error_maintenance.html",
    "error_session_expired.html",
    "bank_options.json",
)
"""Sanitized Banesco DOM fixtures owned by the S2 slice.

The names and the sanitization contract are fixed here; S2 materializes the
files under ``tests/fixtures/banesco/``.  No credential, real account, real
phone, real cedula or real reference may appear in a fixture.
"""


class FakePage:
    """Records Playwright-shaped page actions without importing a browser.

    Attributes:
        fixture_name: Recorded fixture this page represents, if any.
        actions: Ordered tuples describing every recorded action.
        screenshot_bytes: Deterministic bytes returned by :meth:`screenshot`.
    """

    def __init__(self, fixture_name: str | None = None) -> None:
        """Initialize a fake page bound to an optional recorded fixture.

        Args:
            fixture_name: Name from :data:`BANESCO_FIXTURE_CATALOG`.

        Raises:
            ValueError: If the fixture name is not part of the catalog.
        """
        if fixture_name is not None and fixture_name not in BANESCO_FIXTURE_CATALOG:
            raise ValueError(f"unknown Banesco fixture: {fixture_name}")
        self.fixture_name = fixture_name
        self.actions: list[tuple[Any, ...]] = []
        self.screenshot_bytes = b"fake-receipt-bytes"

    async def goto(self, url: str) -> None:
        """Record a navigation.

        Args:
            url: Destination URL.
        """
        self.actions.append(("goto", url))

    async def fill(self, selector: str, value: str) -> None:
        """Record a field fill.

        Args:
            selector: Element selector.
            value: Value typed into the field.
        """
        self.actions.append(("fill", selector, value))

    async def select_option(self, selector: str, value: str) -> None:
        """Record a dropdown selection.

        Args:
            selector: Element selector.
            value: Selected option value.
        """
        self.actions.append(("select_option", selector, value))

    async def click(self, selector: str) -> None:
        """Record a click.

        Args:
            selector: Element selector.
        """
        self.actions.append(("click", selector))

    async def screenshot(self, path: str | None = None) -> bytes:
        """Record a screenshot and return deterministic bytes.

        Args:
            path: Optional screenshot destination.

        Returns:
            The canned receipt bytes.
        """
        self.actions.append(("screenshot", path))
        return self.screenshot_bytes


class FakeBanescoContext:
    """Serves fake pages and records context lifecycle without a browser.

    Attributes:
        page: The page returned by :meth:`new_page`.
        created_pages: Every page handed out by :meth:`new_page`.
        closed: Whether :meth:`close` has run.
    """

    def __init__(self, page: FakePage | None = None) -> None:
        """Initialize a context around a single fake page.

        Args:
            page: Page to serve; a ``login.html`` page is created by default.
        """
        self.page = page if page is not None else FakePage("login.html")
        self.created_pages: list[FakePage] = []
        self.closed = False

    async def new_page(self) -> FakePage:
        """Return and record the served page.

        Returns:
            The configured fake page.
        """
        self.created_pages.append(self.page)
        return self.page

    async def close(self) -> None:
        """Mark the context closed."""
        self.closed = True

    async def __aenter__(self) -> "FakeBanescoContext":
        """Enter the asynchronous context.

        Returns:
            The context itself.
        """
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Leave the asynchronous context and close it.

        Args:
            *exc_info: Ignored exception details.
        """
        await self.close()


class FakeOperatorNotifier:
    """Scriptable in-memory confirmation provider.

    Attributes:
        replies: Queued replies returned in order by
            :meth:`request_confirmation`; ``None`` models a timeout.
        requests: Recorded request payloads.
        error: Optional error raised instead of returning a reply.
    """

    def __init__(self) -> None:
        """Initialize an empty, unscripted notifier."""
        self.replies: list[str | None] = []
        self.requests: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def request_confirmation(
        self,
        *,
        order_number: str,
        reason: str,
        prompt: str,
        timeout_seconds: int | None = None,
    ) -> str | None:
        """Record a confirmation request and return the next scripted reply.

        Args:
            order_number: Order the challenge belongs to.
            reason: Challenge reason such as ``CLAVE`` or ``ID_TYPE``.
            prompt: Operator-facing message text.
            timeout_seconds: Optional expiry window.

        Returns:
            The next scripted reply, or ``None`` to model a timeout.

        Raises:
            Exception: The scripted ``error`` when set.
        """
        self.requests.append(
            {
                "order_number": order_number,
                "reason": reason,
                "prompt": prompt,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        if self.replies:
            return self.replies.pop(0)
        return None


class FakeTelegramBotClient:
    """In-memory Telegram Bot API transport.

    Attributes:
        sent: Recorded ``sendMessage`` payloads.
        updates: Scripted updates drained by :meth:`get_updates`.
        get_updates_calls: Recorded ``getUpdates`` arguments.
    """

    def __init__(self) -> None:
        """Initialize an empty transport."""
        self.sent: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.get_updates_calls: list[dict[str, Any]] = []

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        protect_content: bool = False,
    ) -> dict[str, Any]:
        """Record a message send and return a Bot API-shaped result.

        Args:
            chat_id: Destination chat id.
            text: Message body.
            reply_markup: Optional reply markup.
            protect_content: Whether Telegram must protect the content.

        Returns:
            A minimal successful Bot API response.
        """
        payload = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
            "protect_content": protect_content,
        }
        self.sent.append(payload)
        return {
            "ok": True,
            "result": {
                "message_id": len(self.sent),
                "chat": {"id": chat_id},
                "text": text,
            },
        }

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """Record a poll and drain the scripted updates.

        Args:
            offset: Acknowledgement offset.
            timeout: Long-poll timeout in seconds.

        Returns:
            The scripted updates, drained after the call.
        """
        self.get_updates_calls.append({"offset": offset, "timeout": timeout})
        pending, self.updates = self.updates, []
        return pending


@contextmanager
def telegram_bot_api_mock(
    base_url: str = "https://api.telegram.org",
) -> Iterator[tuple[respx.Route, respx.Route]]:
    """Intercept the Telegram Bot API with ``respx`` instead of a socket.

    Args:
        base_url: Bot API base URL to intercept.

    Yields:
        The ``sendMessage`` and ``getUpdates`` routes.
    """
    escaped = re.escape(base_url)
    with respx.mock(assert_all_called=False) as router:
        send_route = router.post(url__regex=rf"{escaped}/bot[^/]+/sendMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"message_id": 1}}
            )
        )
        updates_route = router.post(url__regex=rf"{escaped}/bot[^/]+/getUpdates").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": []})
        )
        yield send_route, updates_route


@contextmanager
def frozen_clock(moment: str = "2026-11-25T12:00:00+00:00") -> Iterator[Any]:
    """Freeze the clock for challenge and release-deadline assertions.

    Args:
        moment: ISO-8601 instant accepted by ``freezegun``.

    Yields:
        The ``freezegun`` controller so tests can move time forward.
    """
    with freeze_time(moment) as frozen:
        yield frozen


def _override_session(engine: AsyncEngine):
    """Build a ``get_session`` replacement bound to ``engine``.

    Mirrors the production dependency: commit on success and roll back on
    error, with expire-on-commit disabled.

    Args:
        engine: Engine the request session must use.

    Returns:
        An async generator dependency yielding one session per request.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with factory() as request_session:
            try:
                yield request_session
                await request_session.commit()
            except Exception:
                await request_session.rollback()
                raise

    return _session


@pytest.fixture()
def test_settings(tmp_path: Path) -> Settings:
    """Build deterministic settings for the test suite.

    Args:
        tmp_path: Per-test temporary directory.

    Returns:
        A fully populated settings object.
    """
    return Settings(
        app_name="liquidVE-test",
        app_env="local",
        debug=True,
        secret_key="test-secret-key-0123456789abcdef0123456789abcdef",
        internal_api_key="test-internal-api-key-0123456789abcdef",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        binance_api_key="test-binance-api-key",
        binance_api_secret="test-binance-api-secret",
        cachicamo_base_url="http://localhost:9001",
        cachicamo_api_key="test-cachicamo-api-key",
        cachicamo_webhook_secret="test-cachicamo-webhook-secret-0123456789abcdef",
        sms_gateway_base_url="http://100.64.0.10:8080",
        sms_gateway_token="test-sms-gateway-token",
        max_tx_amount_ves=Decimal("5000.00"),
        daily_limit_ves=Decimal("20000.00"),
        max_usdt_per_order=Decimal("1000.00000000"),
        max_buy_amount_ves=Decimal("5000.00"),
        daily_pay_limit_ves=Decimal("20000.00"),
        daily_sms_limit_ves=Decimal("20000.00"),
        payment_confirmation_timeout_seconds=300,
        banesco_rail_enabled=False,
        banesco_online_login_url="https://www.banesco.com/",
        banesco_online_user="test-banesco-user",
        banesco_online_password="test-banesco-password",
        banesco_security_answer_hobbie="test-banesco-answer-hobbie",
        banesco_security_answer_childhood_hero="test-banesco-answer-childhood-hero",
        banesco_security_answer_favourite_dessert="test-banesco-answer-dessert",
        banesco_security_answer_country_to_visit="test-banesco-answer-country",
        banesco_browser_headless=True,
        banesco_browser_timeout_ms=30000,
        banesco_max_concurrent_payments=1,
        banesco_clave_retry_limit=0,
        banesco_payment_concept="Pago",
        banesco_default_id_type="",
        telegram_enabled=False,
        telegram_bot_token="test-telegram-bot-token",
        telegram_operator_chat_id=123456789,
        telegram_api_base_url="https://api.telegram.org",
        telegram_clave_timeout_seconds=300,
        telegram_id_type_timeout_seconds=300,
        telegram_poll_timeout_seconds=30,
        telegram_max_clarifications=2,
        receipt_storage_dir=str(tmp_path / "receipts"),
        receipt_retention_days=30,
        release_watcher_enabled=False,
        release_poll_interval_seconds=30,
        release_timeout_seconds=900,
        release_appeal_mode="auto",
        buy_payment_rail="banesco",
        log_json=False,
    )


@pytest.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    """Create an in-memory engine with the schema applied.

    Yields:
        An engine bound to a single shared in-memory connection.
    """
    test_engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield test_engine
    finally:
        await test_engine.dispose()


@pytest.fixture()
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Provide a session bound to the in-memory test engine.

    Yields:
        An async session with expire-on-commit disabled.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as test_session:
        yield test_session


@pytest.fixture()
async def client(test_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """Provide an HTTP client bound to the ASGI application.

    Note:
        ``respx`` can intercept adapter traffic in other tests; this fixture
        never performs real network calls.

    Yields:
        A client talking to the in-process application.
    """
    init_engine(test_settings)
    await create_all()
    application = create_app(test_settings)
    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        await dispose_engine()


@pytest.fixture()
def fake_binance() -> FakeBinance:
    """Provide a recording Binance double.

    Returns:
        A fresh double per test.
    """
    return FakeBinance()


@pytest.fixture()
def fake_cachicamo() -> FakeCachicamo:
    """Provide a Cachicamo double that verifies every payment.

    Returns:
        A fresh double per test.
    """
    return FakeCachicamo()


@pytest.fixture()
def fake_browser() -> FakeBrowser:
    """Provide a scriptable ``BinanceBrowser`` double.

    Returns:
        A fresh double per test.
    """
    return FakeBrowser()


@pytest.fixture()
def fake_sms() -> FakeSms:
    """Provide a recording SMS gateway double.

    Returns:
        A fresh double per test.
    """
    return FakeSms()


@pytest.fixture()
def fake_browserless() -> None:
    """Provide a missing browser so extraction must degrade safely.

    Returns:
        ``None``; no browser adapter is available.
    """
    return None


@pytest.fixture()
async def app_client(
    test_settings: Settings,
    engine: AsyncEngine,
    fake_binance: FakeBinance,
    fake_cachicamo: FakeCachicamo,
) -> AsyncIterator[httpx.AsyncClient]:
    """Provide an ASGI client sharing the assertion database.

    The application is built *without* running its lifespan, so no socket,
    Chromium or module-global engine is touched.  Requests and assertions bind
    to the same in-memory engine through the ``get_session`` override.

    Args:
        test_settings: Deterministic settings.
        engine: In-memory engine shared with the ``session`` fixture.
        fake_binance: Recording Binance double.
        fake_cachicamo: Verifying Cachicamo double.

    Yields:
        A client talking to the in-process application.
    """
    application = create_app(test_settings)
    application.state.binance = fake_binance
    application.state.cachicamo = fake_cachicamo
    application.state.sms = object()
    application.state.browser = None
    application.dependency_overrides[get_session] = _override_session(engine)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


@pytest.fixture()
def seed_order(session: AsyncSession):
    """Provide a deterministic order factory.

    Args:
        session: Session bound to the in-memory engine.

    Returns:
        A coroutine function that persists one order and returns it.
    """

    async def _seed(**kwargs: Any) -> P2POrder:
        defaults: dict[str, Any] = {
            "order_number": "ORD-SEED",
            "order_type": OrderType.SELL,
            "status": OrderStatus.PENDING,
            "asset": "USDT",
            "fiat_currency": "VES",
            "price": Decimal("150.00000000"),
            "amount_asset": Decimal("10.00000000"),
            "amount_fiat": Decimal("1500.00"),
            "bank_code": BankCode.BANESCO,
            "counterparty_name": "counterparty",
            "counterparty_phone": "+584141234567",
        }
        defaults.update(kwargs)
        order = P2POrder(**defaults)
        session.add(order)
        await session.commit()
        return order

    return _seed


@pytest.fixture()
def signed_cachicamo_headers():
    """Provide a signer for Cachicamo webhook headers.

    Returns:
        A callable building the signature, timestamp and event-id headers.
    """

    def _sign(body: bytes, *, event_id: str) -> dict[str, str]:
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        signature = compute_hmac_signature(
            _TEST_CACHICAMO_WEBHOOK_SECRET, body, timestamp=timestamp
        )
        return {
            "Content-Type": "application/json",
            "X-Cachicamo-Signature": f"sha256={signature}",
            "X-Cachicamo-Timestamp": timestamp,
            "X-Cachicamo-Event-Id": event_id,
        }

    return _sign


def pytest_configure(config: Any) -> None:
    """Register the asyncio marker for collections that need it.

    Args:
        config: Pytest configuration object.
    """
    config.addinivalue_line("markers", "integration: marks integration-style tests")

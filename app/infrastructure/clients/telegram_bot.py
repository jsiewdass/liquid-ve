"""Telegram operator notifications and replies for the local payment pilot."""

import asyncio
import logging
import re
import time
from typing import Any, Protocol

import httpx

from pydantic import SecretStr
from app.core.exceptions import ClaveDinamicaRejected, ExternalServiceError
from app.domain.banesco import validate_clave_dinamica


class _RedactBotURL(logging.Filter):
    _path = re.compile(r"/bot[^/\s]+/(sendMessage|getUpdates)")

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        redacted = self._path.sub(r"/bot<redacted>/\1", rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


_REDACT_BOT_URL = _RedactBotURL()


class TelegramSettings(Protocol):
    @property
    def telegram_bot_token(self) -> SecretStr | None: ...

    @property
    def telegram_operator_chat_id(self) -> int | None: ...

    @property
    def telegram_api_base_url(self) -> str: ...

    @property
    def telegram_clave_timeout_seconds(self) -> int: ...


class TelegramBot:
    def __init__(self, settings: TelegramSettings, client: httpx.AsyncClient) -> None:
        logging.getLogger("httpx").addFilter(_REDACT_BOT_URL)
        self._settings = settings
        self._client = client

    async def _call(self, method: str, body: dict[str, Any]) -> Any:
        token = self._settings.telegram_bot_token
        endpoint = self._settings.telegram_api_base_url.rstrip("/")
        try:
            if token is not None:
                response = await self._client.post(
                    f"{endpoint}/bot{token.get_secret_value()}/{method}",
                    json=body,
                    follow_redirects=False,
                )
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("ok") is True:
                    return payload.get("result")
        except (httpx.HTTPError, ValueError):
            pass  # Transport errors can contain the credential-bearing URL.
        raise self._error()

    @staticmethod
    def _error() -> ExternalServiceError:
        return ExternalServiceError(
            "No se pudo completar la consulta a Telegram", service="telegram"
        )

    async def request_authorization(self, order_number: str, url: str) -> None:
        await self._call(
            "sendMessage",
            {
                "chat_id": self._settings.telegram_operator_chat_id,
                "text": f"Orden {order_number}: Banesco espera tu autorización. Abre el enlace ahora.",
                "protect_content": True,
                "link_preview_options": {"is_disabled": True},
                "reply_markup": {
                    "inline_keyboard": [[{"text": "Abrir autorización", "url": url}]]
                },
            },
        )

    async def request_code(self, prompt: str) -> str:
        """Accept one valid private reply to this exact Telegram message."""
        chat_id = self._settings.telegram_operator_chat_id
        seconds = self._settings.telegram_clave_timeout_seconds
        notice = await self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": f"{prompt}\nResponde a este mensaje dentro de {seconds} segundos.",
                "protect_content": True,
                "reply_markup": {
                    "force_reply": True,
                    "input_field_placeholder": "Código",
                },
            },
        )
        if (
            not isinstance(notice, dict)
            or not isinstance(notice.get("chat"), dict)
            or notice["chat"].get("type") != "private"
            or notice["chat"].get("id") != chat_id
            or type(notice.get("message_id")) is not int
            or notice["message_id"] <= 0
        ):
            raise self._error()

        deadline = time.monotonic() + seconds
        offset = None
        while (remaining := deadline - time.monotonic()) > 0:
            params: dict[str, Any] = {
                "allowed_updates": ["message"],
                "timeout": min(5, int(remaining)),
            }
            if offset is not None:
                params["offset"] = offset
            updates = await asyncio.wait_for(
                self._call("getUpdates", params), timeout=remaining
            )
            if not isinstance(updates, list):
                raise self._error()
            for update in updates:
                if (
                    not isinstance(update, dict)
                    or type(update.get("update_id")) is not int
                ):
                    continue
                offset = max(offset or 0, update["update_id"] + 1)
                code = self._operator_reply(
                    update.get("message"), chat_id, notice["message_id"]
                )
                if code is not None and time.monotonic() < deadline:
                    return code
            if not updates:
                await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))
        raise asyncio.TimeoutError

    @staticmethod
    def _operator_reply(
        message: Any, chat_id: int | None, prompt_id: int
    ) -> str | None:
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        sender = message.get("from")
        replied_to = message.get("reply_to_message")
        if (
            not isinstance(chat, dict)
            or chat.get("type") != "private"
            or chat.get("id") != chat_id
            or not isinstance(sender, dict)
            or sender.get("id") != chat_id
            or not isinstance(replied_to, dict)
            or replied_to.get("message_id") != prompt_id
        ):
            return None
        try:
            return validate_clave_dinamica(message.get("text"))
        except ClaveDinamicaRejected:
            return None

"""Structured logging with sensitive-data masking.

Loguru is configured once during application startup.  When
``settings.log_json`` is enabled a serialized JSON sink is used; the patcher
injects request-scoped metadata and masks sensitive values *before*
serialization so the JSON payload stays valid.
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Mapping

from loguru import logger

from app.core.config import Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    # loguru does not re-export ``Logger`` at package level; the private path
    # is the supported way to reference it for type checking only.
    from loguru._logger import Logger

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
app_env_var: ContextVar[str] = ContextVar("app_env", default="-")

SECRET_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "secret_key",
        "api_key",
        "apikey",
        "authorization",
        "x-api-key",
        "xapikey",
        "signature",
        "private_key",
    }
)

IDENTIFIER_KEYS: frozenset[str] = frozenset(
    {
        "phone",
        "phone_number",
        "payer_phone",
        "destination_phone",
        "cedula",
        "id_number",
        "national_id",
        "bank_reference",
        "reference",
        "account_number",
        "card_number",
    }
)

SENSITIVE_KEYS: frozenset[str] = SECRET_KEYS | IDENTIFIER_KEYS

_CEDULA_PATTERN = re.compile(r"\b([VEJGP])-?(\d{6,9})\b", re.IGNORECASE)
_PHONE_PATTERN = re.compile(r"(\+?58|0)?(4\d{2})[- .]?(\d{3})[- .]?(\d{4})\b")

_logging_configured = False


def _normalize_key(key: str) -> str:
    """Normalize a mapping key for sensitive-name matching.

    Args:
        key: Raw mapping key.

    Returns:
        Lowercase key without separators.
    """
    return key.lower().replace("-", "").replace("_", "")


def _mask_identifier(value: str, visible: int = 4) -> str:
    """Mask an identifier but keep a short suffix for correlation."""
    if not value:
        return value
    if len(value) <= visible:
        return "***"
    return f"{'*' * (len(value) - visible)}{value[-visible:]}"


def _mask_value(key: str, value: Any) -> Any:
    """Mask a single value according to the sensitivity of its key.

    Args:
        key: Parent mapping key.
        value: Value to mask.

    Returns:
        A masked string, or the original value when the key is not sensitive.
    """
    if not isinstance(value, str) or not value:
        return value
    normalized = _normalize_key(key)
    secret_keys = {_normalize_key(item) for item in SECRET_KEYS}
    identifier_keys = {_normalize_key(item) for item in IDENTIFIER_KEYS}
    if normalized in secret_keys:
        return "***"
    if normalized in identifier_keys:
        return _mask_identifier(value)
    if normalized == "message":
        return _mask_message(value)
    return value


def mask_sensitive(record: dict[str, Any]) -> dict[str, Any]:
    """Recursively mask sensitive values inside a log record.

    The function returns a new mapping and never mutates the input in place
    so it is safe to call from the loguru patcher.

    Args:
        record: Log record mapping (loguru ``record`` or ``record["extra"]``).

    Returns:
        A sanitized copy of the mapping.
    """
    sanitized: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, Mapping):
            sanitized[key] = mask_sensitive(dict(value))
        elif isinstance(value, (list, tuple)):
            sanitized[key] = [
                mask_sensitive(dict(item)) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            sanitized[key] = _mask_value(key, value)
    return sanitized


def _mask_message(message: str) -> str:
    """Mask cÃƒÂ©dulas and Venezuelan phone numbers inside free text.

    Args:
        message: Raw log message.

    Returns:
        The message with identifiers masked.
    """
    masked = _CEDULA_PATTERN.sub(lambda match: f"{match.group(1)}-****", message)

    def _phone_replacer(match: re.Match[str]) -> str:
        digits = match.group(0)
        return _mask_identifier(re.sub(r"[ .-]", "", digits), visible=4)

    return _PHONE_PATTERN.sub(_phone_replacer, masked)


def _patch_record(record: dict[str, Any]) -> None:
    """Loguru patcher injecting context and masking sensitive data.

    Args:
        record: Mutable loguru record.  Extra fields and the rendered message
            are sanitized before the sink serializes them.
    """
    extra = record.setdefault("extra", {})
    extra.setdefault("request_id", request_id_var.get())
    extra.setdefault("app_env", app_env_var.get())
    record["extra"] = mask_sensitive(dict(extra))
    if isinstance(record.get("message"), str):
        record["message"] = _mask_message(record["message"])


def configure_logging(settings: Settings) -> None:
    """Configure loguru sinks and intercept standard library logging.

    Args:
        settings: Active application settings.
    """
    global _logging_configured
    app_env_var.set(settings.app_env)
    logger.remove()
    if settings.log_json:
        sink: Any = sys.stdout
        logger.add(
            sink,
            level=settings.log_level,
            serialize=True,
            enqueue=True,
            backtrace=settings.debug,
            diagnose=False,
            patcher=_patch_record,
        )
    else:
        logger.add(
            sys.stderr,
            level=settings.log_level,
            colorize=True,
            enqueue=True,
            backtrace=settings.debug,
            diagnose=False,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{extra[request_id]}</cyan> | <level>{message}</level>"
            ),
            patcher=_patch_record,
        )
    _logging_configured = True
    intercept_standard_logging()


class _InterceptHandler(logging.Handler):
    """Route standard library log records into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        """Forward a stdlib log record to loguru.

        Args:
            record: Standard library log record.
        """
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def intercept_standard_logging() -> None:
    """Redirect stdlib and uvicorn logging through loguru."""
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers = [_InterceptHandler()]
        stdlib_logger.propagate = False


def bind_request_context(request_id: str) -> None:
    """Bind a request identifier to the current context.

    Args:
        request_id: Identifier propagated through logs and audit rows.
    """
    request_id_var.set(request_id)


def new_request_id() -> str:
    """Generate a new correlation identifier.

    Returns:
        A hex encoded UUID4.
    """
    return uuid.uuid4().hex


def get_logger(name: str | None = None) -> Logger:
    """Return a loguru logger bound to ``name``.

    Args:
        name: Optional component name added to the log context.

    Returns:
        A loguru logger instance.
    """
    if name:
        return logger.bind(component=name)
    return logger


__all__ = [
    "request_id_var",
    "SENSITIVE_KEYS",
    "mask_sensitive",
    "configure_logging",
    "intercept_standard_logging",
    "bind_request_context",
    "get_logger",
    "new_request_id",
]

"""Regression tests for structured logging configuration.

``configure_logging`` must boot in both sink modes and register the masking
patcher globally on loguru 0.7.3, where ``add()`` accepts no ``patcher`` kwarg.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from loguru import logger

from app.core import logging as logging_module
from app.core.config import Settings
from app.core.logging import configure_logging, mask_sensitive


@pytest.fixture(autouse=True)
def _restore_loguru(monkeypatch: pytest.MonkeyPatch):
    """Reset logging state so the JSON and text cases are order-independent.

    Args:
        monkeypatch: Fixture used to reset the module guard flag.

    Yields:
        Control back to the test, then restores a clean loguru state.
    """
    monkeypatch.setattr(logging_module, "_logging_configured", False, raising=False)
    yield
    logger.remove()
    logger.configure(patcher=None)
    logger.add(sys.stderr)


@pytest.mark.parametrize("log_json", [True, False])
def test_configure_logging_applies_global_patcher(
    log_json: bool, test_settings: Settings
) -> None:
    """Both sink modes boot without raising and the patcher masks fields."""
    settings = test_settings.model_copy(update={"log_json": log_json})

    configure_logging(settings)

    # Drop the configured sink to keep test output clean; a global patcher
    # survives ``logger.remove()`` and still applies to this probe sink.
    logger.remove()
    records: list[dict[str, Any]] = []
    sink_id = logger.add(
        lambda message: records.append(message.record),
        format="{message}",
        level="TRACE",
        enqueue=False,
    )
    try:
        logger.info("payer cedula V-12345678 phone +584141234567")
    finally:
        logger.remove(sink_id)

    assert len(records) == 1
    record = records[0]
    assert record["extra"]["request_id"] == "-"
    assert record["extra"]["app_env"] == settings.app_env
    assert "V-12345678" not in record["message"]
    assert "V-****" in record["message"]


def test_new_sensitive_keys_and_receipt_text_are_masked(
    test_settings: Settings,
) -> None:
    """New rail secrets, R- documents and receipt payloads never render."""
    masked = mask_sensitive(
        {
            "security_answer": "synthetic-answer",
            "answer": "synthetic-answer",
            "clave": "synthetic-clave",
            "clave_dinamica": "123456",
            "receipt_text": "synthetic-receipt-text",
            "receipt_bytes": b"synthetic-receipt-bytes",
            "document": "R-12345678",
            "documents": ["R-12345678", "V-12345678"],
        }
    )
    for key in (
        "security_answer",
        "answer",
        "clave",
        "clave_dinamica",
        "receipt_text",
        "receipt_bytes",
    ):
        assert masked[key] == "***"
    assert masked["document"] == "R-****"
    assert masked["documents"] == ["R-****", "V-****"]

    settings = test_settings.model_copy(update={"log_json": True})
    configure_logging(settings)
    logger.remove()
    records: list[dict[str, Any]] = []
    sink_id = logger.add(
        lambda message: records.append(message.record),
        format="{message}",
        level="TRACE",
        enqueue=False,
    )
    try:
        logger.bind(
            security_answer="synthetic-answer",
            answer="synthetic-answer",
            clave="synthetic-clave",
            clave_dinamica="123456",
            receipt_text="synthetic-receipt-text",
            receipt_bytes=b"synthetic-receipt-bytes",
        ).info("document R-12345678 rendered")
    finally:
        logger.remove(sink_id)

    assert len(records) == 1
    blob = f"{records[0]['message']} {records[0]['extra']!r}"
    for value in (
        "synthetic-answer",
        "synthetic-clave",
        "123456",
        "synthetic-receipt-text",
        "synthetic-receipt-bytes",
        "R-12345678",
    ):
        assert value not in blob
    assert "R-****" in records[0]["message"]

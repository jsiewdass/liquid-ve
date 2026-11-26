"""Secrets-hygiene canary tests.

A synthetic canary is generated at runtime so the literal value never exists in
the repository.  It is pushed through the logging, audit-masking, database-text
and Engram-payload paths and must never survive.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest
from loguru import logger

from app.core import logging as logging_module
from app.core.config import Settings
from app.core.logging import configure_logging, mask_sensitive

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OPENSPEC_ROOT = _REPO_ROOT / "openspec" / "changes"
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".atl",
        "data",
        "node_modules",
        ".playwright",
        "test-results",
    }
)
_SKIP_SUFFIXES = frozenset(
    {
        ".pyc",
        ".pyo",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".db",
        ".sqlite3",
        ".woff",
        ".woff2",
        ".ttf",
    }
)


def _new_canary() -> str:
    """Return a fresh high-entropy canary that exists only in memory.

    Returns:
        A synthetic marker that never appears literally on disk.
    """
    return "zz-canary-" + secrets.token_hex(16) + "-zz"


@pytest.fixture(autouse=True)
def _restore_loguru() -> Iterator[None]:
    """Reset loguru state after each test.

    Yields:
        Control back to the test, then restores a clean logger.
    """
    logging_module._logging_configured = False
    yield
    logger.remove()
    logger.configure(patcher=None)
    logger.add(sys.stderr)


def _iter_repository_text_files(root: Path) -> Iterator[Path]:
    """Yield likely-text files under ``root`` while skipping caches.

    Args:
        root: Directory to scan.

    Yields:
        Paths of files that can be scanned as UTF-8 text.
    """
    for path in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        yield path


def _scan_tree_for(canary: str, root: Path) -> list[str]:
    """Return relative paths of files that contain ``canary``.

    Args:
        canary: Value that must not appear.
        root: Directory to scan.

    Returns:
        Relative paths of leaking files (empty when the canary is absent).
    """
    leaks: list[str] = []
    for path in _iter_repository_text_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if canary in text:
            leaks.append(str(path.relative_to(root)))
    return leaks


def _redacted_observation_body(sensitive: dict[str, Any]) -> str:
    """Render the redacted body shape used by Engram saves.

    Args:
        sensitive: Candidate observation fields.

    Returns:
        A JSON body in which sensitive keys are already masked.
    """
    return json.dumps(mask_sensitive(dict(sensitive)), sort_keys=True)


def test_no_canary_reaches_logs_audit_db_text_columns_artifacts_or_engram_payload(
    test_settings: Settings,
) -> None:
    """The canary never survives logs, audit, DB text, repo or Engram bodies."""
    canary = _new_canary()
    document = "R-12345678"

    settings = test_settings.model_copy(update={"log_json": True})
    configure_logging(settings)
    logger.remove()
    records: list[Any] = []
    sink_id = logger.add(
        lambda message: records.append(message.record),
        format="{message}",
        level="TRACE",
        enqueue=False,
    )
    try:
        logger.bind(
            security_answer=canary,
            answer=canary,
            clave=canary,
            clave_dinamica=canary,
            receipt_text=canary,
            receipt_bytes=canary.encode("utf-8"),
            token=canary,
        ).info("payment attempt for {}", document)
    finally:
        logger.remove(sink_id)

    assert len(records) == 1
    record = records[0]
    logged_blob = f"{record['message']} {record['extra']!r}"
    assert canary not in logged_blob
    assert document not in record["message"]

    audit_payload = mask_sensitive(
        {
            "security_answer": canary,
            "answer": canary,
            "clave": canary,
            "clave_dinamica": canary,
            "document": document,
        }
    )
    assert canary not in json.dumps(audit_payload, default=str)

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE audit_logs (payload TEXT)")
        connection.execute(
            "INSERT INTO audit_logs (payload) VALUES (?)",
            (json.dumps(audit_payload, default=str),),
        )
        rows = connection.execute("SELECT payload FROM audit_logs").fetchall()
    finally:
        connection.close()
    stored_text = "".join(row[0] for row in rows)
    assert canary not in stored_text
    assert document not in stored_text

    assert _scan_tree_for(canary, _REPO_ROOT) == []
    assert _scan_tree_for(canary, _OPENSPEC_ROOT) == []

    engram_body = _redacted_observation_body(
        {
            "title": "payment attempt",
            "security_answer": canary,
            "clave_dinamica": canary,
            "content": "redacted progress body",
        }
    )
    assert canary not in engram_body

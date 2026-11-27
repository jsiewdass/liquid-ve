"""Tests for the Alembic revisions, including the ``0003`` data migration.

Alembic's online path calls :func:`asyncio.run`, so every test here is a plain
sync function: a pytest-asyncio test would already own the event loop and the
``command.upgrade``/``command.downgrade`` calls would raise
``RuntimeError: asyncio.run() cannot be called from a running event loop``.

The ``0003`` revision is data-only: it renames ``SENDING_SMS`` and ``VES_SMS``
in place and reverses the rename on downgrade while collapsing the new payment
tail to ``MANUAL_REVIEW``.  These tests drive the real Alembic command API
against throwaway SQLite files and inspect the result with the stdlib
``sqlite3`` driver, independently of the ORM enums.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.core.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

PREVIOUS_REVISION = "20261123_0002"
BASE_REVISION = "20260101_0001"

# The five nullable columns added by revision ``20261123_0002``.
TELEMETRY_COLUMNS: frozenset[str] = frozenset(
    {
        "counterparty_id_number",
        "sms_message_id",
        "sms_sent_at",
        "bank_short_code",
        "bank_confirmed_at",
    }
)

# A few pre-existing columns that must survive a downgrade untouched.
CORE_COLUMNS: frozenset[str] = frozenset(
    {"order_number", "bank_reference", "idempotency_key", "status"}
)


def _alembic_config() -> Config:
    """Return an Alembic config bound to this repository.

    Returns:
        A config whose script location is resolved absolutely so the test does
        not depend on the current working directory.
    """
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return config


def _query(
    db_path: Path, sql: str, params: Sequence[object] = ()
) -> list[tuple[object, ...]]:
    """Run a read-only SQL statement against the throwaway database.

    Args:
        db_path: SQLite database file to inspect.
        sql: SQL text with ``?`` placeholders.
        params: Positional parameter values.

    Returns:
        The fetched rows.
    """
    with closing(sqlite3.connect(str(db_path))) as connection:
        return connection.execute(sql, tuple(params)).fetchall()


def _execute(db_path: Path, sql: str, params: Sequence[object] = ()) -> None:
    """Run one writing SQL statement and commit.

    Args:
        db_path: SQLite database file to mutate.
        sql: SQL text with ``?`` placeholders.
        params: Positional parameter values.
    """
    with closing(sqlite3.connect(str(db_path))) as connection:
        connection.execute(sql, tuple(params))
        connection.commit()


def _table_info(db_path: Path, table: str = "p2p_orders") -> dict[str, sqlite3.Row]:
    """Return ``PRAGMA table_info`` rows keyed by column name.

    Args:
        db_path: SQLite database file to inspect.
        table: Table whose columns are read.

    Returns:
        Mapping of column name to its ``PRAGMA table_info`` row.
    """
    with closing(sqlite3.connect(str(db_path))) as connection:
        cursor = connection.execute(f"PRAGMA table_info({table})")
        cursor.row_factory = sqlite3.Row
        rows = cursor.fetchall()
    return {row["name"]: row for row in rows}


def _row_count(db_path: Path, table: str = "p2p_orders") -> int:
    """Return the number of rows in ``table``.

    Args:
        db_path: SQLite database file to inspect.
        table: Table whose rows are counted.

    Returns:
        The table row count.
    """
    return int(_query(db_path, f"SELECT COUNT(*) FROM {table}")[0][0])


def _insert_order(db_path: Path, order_number: str, status: str) -> None:
    """Insert a minimally valid ``p2p_orders`` row with ``status``.

    Args:
        db_path: SQLite database file to mutate.
        order_number: Unique provider order number.
        status: Raw status string stored in the ``VARCHAR(32)`` column.
    """
    _execute(
        db_path,
        "INSERT INTO p2p_orders ("
        "order_number, order_type, status, asset, fiat_currency, price, "
        "amount_asset, amount_fiat, risk_checked, version"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            order_number,
            "BUY",
            status,
            "USDT",
            "VES",
            "150.0",
            "10.0",
            "1500.00",
            0,
            1,
        ),
    )


def _order_status(db_path: Path, order_number: str) -> str:
    """Return one order's stored status string.

    Args:
        db_path: SQLite database file to inspect.
        order_number: Provider order number.

    Returns:
        The raw status value.
    """
    rows = _query(
        db_path,
        "SELECT status FROM p2p_orders WHERE order_number = ?",
        (order_number,),
    )
    return str(rows[0][0])


def _insert_audit(
    db_path: Path,
    message: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    payload: str | None = None,
) -> int:
    """Insert an ``audit_logs`` row carrying status columns and payload.

    Args:
        db_path: SQLite database file to mutate.
        message: Human-readable message.
        from_status: Optional source status string.
        to_status: Optional destination status string.
        payload: Optional raw JSON text stored verbatim.

    Returns:
        The new audit row primary key.
    """
    with closing(sqlite3.connect(str(db_path))) as connection:
        cursor = connection.execute(
            "INSERT INTO audit_logs ("
            "event, actor, from_status, to_status, message, payload"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "STATUS_CHANGED",
                "SYSTEM",
                from_status,
                to_status,
                message,
                payload,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def _audit_statuses(db_path: Path, audit_id: int) -> tuple[str | None, str | None, str]:
    """Return ``(from_status, to_status, payload)`` for one audit row.

    Args:
        db_path: SQLite database file to inspect.
        audit_id: Audit row primary key.

    Returns:
        The two status columns and the raw payload text.
    """
    rows = _query(
        db_path,
        "SELECT from_status, to_status, payload FROM audit_logs WHERE id = ?",
        (audit_id,),
    )
    from_status, to_status, payload = rows[0]
    return (
        None if from_status is None else str(from_status),
        None if to_status is None else str(to_status),
        "" if payload is None else str(payload),
    )


def _insert_ledger(
    db_path: Path, ledger_date: str, currency: str, volume: str, count: int
) -> None:
    """Insert a ``daily_volume_ledgers`` row.

    Args:
        db_path: SQLite database file to mutate.
        ledger_date: ISO date string.
        currency: Ledger channel.
        volume: Decimal total as a string.
        count: Transaction count.
    """
    _execute(
        db_path,
        "INSERT INTO daily_volume_ledgers ("
        "ledger_date, currency, total_volume, transaction_count"
        ") VALUES (?, ?, ?, ?)",
        (ledger_date, currency, volume, count),
    )


def _ledger_rows(db_path: Path) -> dict[tuple[str, str], tuple[Decimal, int]]:
    """Return ledger rows keyed by ``(date, currency)`` with Decimal volume.

    Args:
        db_path: SQLite database file to inspect.

    Returns:
        Mapping of ``(ledger_date, currency)`` to ``(total_volume, count)``.
    """
    rows = _query(
        db_path,
        "SELECT ledger_date, currency, total_volume, transaction_count "
        "FROM daily_volume_ledgers",
    )
    return {
        (str(date), str(currency)): (Decimal(str(volume)), int(count))
        for date, currency, volume, count in rows
    }


@pytest.fixture()
def migration_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point application settings at a throwaway SQLite file.

    The ``DATABASE_URL`` environment variable is set before importing Alembic's
    environment (which reads ``get_settings()``) and the settings cache is
    cleared both before and after so no other test sees the temporary URL.

    Args:
        tmp_path: Per-test temporary directory.
        monkeypatch: Environment patcher restored at teardown.

    Yields:
        Path to the temporary database file.
    """
    db_path = tmp_path / "migration_check.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()
    try:
        yield db_path
    finally:
        get_settings.cache_clear()


def test_upgrade_head_adds_nullable_telemetry_columns(migration_db: Path) -> None:
    """``upgrade head`` adds exactly the five nullable telemetry columns."""
    command.upgrade(_alembic_config(), "head")

    columns = _table_info(migration_db)

    assert TELEMETRY_COLUMNS <= set(columns)
    for name in TELEMETRY_COLUMNS:
        assert columns[name]["notnull"] == 0, f"{name} must be nullable"


def test_telemetry_downgrade_drops_columns_and_keeps_rows(migration_db: Path) -> None:
    """Dropping ``0002`` removes the telemetry columns without touching rows."""
    config = _alembic_config()
    command.upgrade(config, "head")

    _insert_order(migration_db, "ORD-MIG", "PENDING")
    assert _row_count(migration_db) == 1
    assert TELEMETRY_COLUMNS <= set(_table_info(migration_db))

    command.downgrade(config, BASE_REVISION)

    columns_after = _table_info(migration_db)
    assert TELEMETRY_COLUMNS.isdisjoint(columns_after)
    assert CORE_COLUMNS <= set(columns_after)
    assert _row_count(migration_db) == 1

    command.upgrade(config, "head")

    assert TELEMETRY_COLUMNS <= set(_table_info(migration_db))
    assert _row_count(migration_db) == 1


def test_status_rename_upgrade_and_new_tail_downgrade(migration_db: Path) -> None:
    """``0003`` renames the old status and collapses the new tail on downgrade."""
    config = _alembic_config()
    command.upgrade(config, PREVIOUS_REVISION)

    _insert_order(migration_db, "ORD-SMS", "SENDING_SMS")
    _insert_order(migration_db, "ORD-DONE", "COMPLETED")
    _insert_order(migration_db, "ORD-REVIEW", "MANUAL_REVIEW")
    sms_audit = _insert_audit(
        migration_db,
        "sms parked",
        from_status="SENDING_SMS",
        to_status="MANUAL_REVIEW",
        payload='{"note": "SENDING_SMS persisted historically"}',
    )
    to_sms_audit = _insert_audit(
        migration_db,
        "entered sms",
        from_status="VERIFYING_CACHICAMO",
        to_status="SENDING_SMS",
    )

    command.upgrade(config, "head")

    assert _order_status(migration_db, "ORD-SMS") == "SENDING_PAYMENT"
    assert _order_status(migration_db, "ORD-DONE") == "COMPLETED"
    assert _order_status(migration_db, "ORD-REVIEW") == "MANUAL_REVIEW"

    from_status, to_status, payload = _audit_statuses(migration_db, sms_audit)
    assert from_status == "SENDING_PAYMENT"
    assert to_status == "MANUAL_REVIEW"
    # Historical audit payload JSON is append-only and must not be rewritten.
    assert "SENDING_SMS" in payload
    assert _audit_statuses(migration_db, to_sms_audit)[1] == "SENDING_PAYMENT"

    _insert_order(migration_db, "ORD-AWAIT", "AWAITING_RELEASE")
    _insert_order(migration_db, "ORD-APPEAL", "APPEALED")
    await_audit = _insert_audit(
        migration_db,
        "awaiting release",
        from_status="SENDING_PAYMENT",
        to_status="AWAITING_RELEASE",
    )
    appeal_audit = _insert_audit(
        migration_db,
        "appealed",
        from_status="MANUAL_REVIEW",
        to_status="APPEALED",
    )

    command.downgrade(config, PREVIOUS_REVISION)

    # The new tail collapses to MANUAL_REVIEW, never back to SENDING_SMS.
    assert _order_status(migration_db, "ORD-AWAIT") == "MANUAL_REVIEW"
    assert _order_status(migration_db, "ORD-APPEAL") == "MANUAL_REVIEW"
    # Paid/completed orders are never mapped back to the stale name.
    assert _order_status(migration_db, "ORD-DONE") == "COMPLETED"
    assert _order_status(migration_db, "ORD-SMS") == "SENDING_SMS"
    assert _audit_statuses(migration_db, await_audit)[1] == "MANUAL_REVIEW"
    assert _audit_statuses(migration_db, appeal_audit)[1] == "MANUAL_REVIEW"

    statuses = {str(row[0]) for row in _query(migration_db, "SELECT status FROM p2p_orders")}
    assert "SENDING_PAYMENT" not in statuses
    assert "AWAITING_RELEASE" not in statuses
    assert "APPEALED" not in statuses


def test_ledger_rename_aggregates_collisions_and_rolls_back(migration_db: Path) -> None:
    """``0003`` merges same-date channels, renames the rest and reverses cleanly."""
    config = _alembic_config()
    command.upgrade(config, PREVIOUS_REVISION)

    # Same-date collision: both channels already exist for one UTC day.
    _insert_ledger(migration_db, "2026-03-08", "VES_SMS", "100.00", 2)
    _insert_ledger(migration_db, "2026-03-08", "VES_PAY", "50.00", 1)
    # Rename-only date.
    _insert_ledger(migration_db, "2026-03-09", "VES_SMS", "200.00", 5)
    # UTC rollover: rows on either side of midnight stay separate days.
    _insert_ledger(migration_db, "2026-03-10", "VES_SMS", "300.00", 6)
    _insert_ledger(migration_db, "2026-03-11", "VES_SMS", "50.00", 1)

    command.upgrade(config, "head")

    rows = _ledger_rows(migration_db)
    assert rows[("2026-03-08", "VES_PAY")] == (Decimal("150.00"), 3)
    assert ("2026-03-08", "VES_SMS") not in rows
    assert rows[("2026-03-09", "VES_PAY")] == (Decimal("200.00"), 5)
    assert rows[("2026-03-10", "VES_PAY")] == (Decimal("300.00"), 6)
    assert rows[("2026-03-11", "VES_PAY")] == (Decimal("50.00"), 1)
    assert not any(currency == "VES_SMS" for _, currency in rows)
    assert len({date for date, _ in rows}) == 4

    # Downgrade guard: a stale VES_SMS row on a date that already has VES_PAY
    # must aggregate into the restored channel instead of violating UNIQUE.
    _insert_ledger(migration_db, "2026-03-09", "VES_SMS", "25.00", 1)

    command.downgrade(config, PREVIOUS_REVISION)

    rows = _ledger_rows(migration_db)
    assert rows[("2026-03-09", "VES_SMS")] == (Decimal("225.00"), 6)
    assert ("2026-03-09", "VES_PAY") not in rows
    assert rows[("2026-03-08", "VES_SMS")] == (Decimal("150.00"), 3)
    assert rows[("2026-03-10", "VES_SMS")] == (Decimal("300.00"), 6)
    assert rows[("2026-03-11", "VES_SMS")] == (Decimal("50.00"), 1)
    assert not any(currency == "VES_PAY" for _, currency in rows)


def test_upgrade_head_and_downgrade_across_legacy_revisions(
    migration_db: Path,
) -> None:
    """The full chain upgrades, seeds, downgrades to the old payout schema and re-upgrades cleanly."""
    config = _alembic_config()
    command.upgrade(config, "head")

    _insert_order(migration_db, "ORD-FRESH-PAY", "SENDING_PAYMENT")
    _insert_order(migration_db, "ORD-FRESH-AWAIT", "AWAITING_RELEASE")
    _insert_order(migration_db, "ORD-FRESH-APPEAL", "APPEALED")
    _insert_order(migration_db, "ORD-FRESH-DONE", "COMPLETED")
    _insert_ledger(migration_db, "2026-11-25", "VES_PAY", "1500.00", 1)

    command.downgrade(config, PREVIOUS_REVISION)

    assert _order_status(migration_db, "ORD-FRESH-PAY") == "SENDING_SMS"
    assert _order_status(migration_db, "ORD-FRESH-AWAIT") == "MANUAL_REVIEW"
    assert _order_status(migration_db, "ORD-FRESH-APPEAL") == "MANUAL_REVIEW"
    assert _order_status(migration_db, "ORD-FRESH-DONE") == "COMPLETED"
    rows = _ledger_rows(migration_db)
    assert rows[("2026-11-25", "VES_SMS")] == (Decimal("1500.00"), 1)
    assert ("2026-11-25", "VES_PAY") not in rows
    # ``0003`` is data-only: the telemetry columns stay in place.
    assert TELEMETRY_COLUMNS <= set(_table_info(migration_db))

    command.upgrade(config, "head")

    assert _order_status(migration_db, "ORD-FRESH-PAY") == "SENDING_PAYMENT"
    # A collapsed tail stays parked; it never resumes the payment rail.
    assert _order_status(migration_db, "ORD-FRESH-AWAIT") == "MANUAL_REVIEW"
    assert _order_status(migration_db, "ORD-FRESH-APPEAL") == "MANUAL_REVIEW"
    assert _order_status(migration_db, "ORD-FRESH-DONE") == "COMPLETED"
    rows = _ledger_rows(migration_db)
    assert rows[("2026-11-25", "VES_PAY")] == (Decimal("1500.00"), 1)
    assert ("2026-11-25", "VES_SMS") not in rows


def test_banesco_migration_preserves_existing_payment_history(migration_db: Path) -> None:
    config = _alembic_config()
    command.upgrade(config, "20261125_0003")
    _insert_order(migration_db, "ORD-HISTORICAL", "COMPLETED")
    _execute(
        migration_db,
        "UPDATE p2p_orders SET sms_message_id = ?, bank_reference = ? WHERE order_number = ?",
        ("historical-message", "prior-bank-ref", "ORD-HISTORICAL"),
    )
    command.upgrade(config, "head")
    assert {"receipt_sha256", "payment_slot"} <= set(_table_info(migration_db))
    _insert_order(migration_db, "ORD-BANESCO-PAID", "PAYMENT_CONFIRMED")
    command.downgrade(config, "-1")
    rows = _query(
        migration_db,
        "SELECT sms_message_id, bank_reference FROM p2p_orders WHERE order_number = 'ORD-HISTORICAL'",
    )
    assert rows == [("historical-message", "prior-bank-ref")]
    assert _order_status(migration_db, "ORD-BANESCO-PAID") == "MANUAL_REVIEW"

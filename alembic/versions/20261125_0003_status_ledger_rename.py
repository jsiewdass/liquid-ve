"""Rename the BUY payment status and the outgoing ledger channel in place.

Revision ID: 20261125_0003
Revises: 20261123_0002
Create Date: 2026-11-25 00:00:00

This revision is data-only.  The status columns are plain ``VARCHAR(32)`` with
no CHECK constraint and the ledger channel is a plain ``VARCHAR(8)``, so the
rename needs no DDL.

Upgrade
    ``SENDING_SMS`` -> ``SENDING_PAYMENT`` on ``p2p_orders.status``,
    ``audit_logs.from_status`` and ``audit_logs.to_status``.  On the ledger, a
    same-date ``VES_SMS``/``VES_PAY`` collision is aggregated into ``VES_PAY``
    before the stale row is deleted, then every remaining ``VES_SMS`` row is
    renamed.  ``UNIQUE(ledger_date, currency)`` is preserved.

Downgrade
    Collapse ``AWAITING_RELEASE`` and ``APPEALED`` to ``MANUAL_REVIEW`` first,
    then reverse the rename.  A paid or completed order never returns to
    ``SENDING_SMS`` and the historical audit payload JSON is never rewritten.
    The ledger reverses with the same aggregation guard.

All statements are parameterised and executed through :func:`op.get_bind`, so
the revision is portable across SQLite and PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261125_0003"
down_revision: str | None = "20261123_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUS = "SENDING_SMS"
NEW_STATUS = "SENDING_PAYMENT"
COLLAPSED_STATUSES: tuple[str, ...] = ("AWAITING_RELEASE", "APPEALED")
COLLAPSED_TARGET = "MANUAL_REVIEW"

OLD_CHANNEL = "VES_SMS"
NEW_CHANNEL = "VES_PAY"

# Every column that stores an ``OrderStatus`` value.
_STATUS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("p2p_orders", "status"),
    ("audit_logs", "from_status"),
    ("audit_logs", "to_status"),
)


def _rename_statuses(bind: sa.engine.Connection, *, old: str, new: str) -> None:
    """Rename every stored status value from ``old`` to ``new``.

    Args:
        bind: Migration connection.
        old: Status value being replaced.
        new: Replacement status value.
    """
    for table, column in _STATUS_COLUMNS:
        statement = sa.text(
            f"UPDATE {table} SET {column} = :new WHERE {column} = :old"  # noqa: S608
        )
        bind.execute(statement, {"new": new, "old": old})


def _collapse_payment_tail(bind: sa.engine.Connection) -> None:
    """Collapse the new payment tail to ``MANUAL_REVIEW`` on downgrade.

    ``AWAITING_RELEASE`` and ``APPEALED`` have no representation in the old
    status vocabulary, so a downgrade parks them for a human instead of mapping
    a paid order back onto ``SENDING_SMS``.

    Args:
        bind: Migration connection.
    """
    for table, column in _STATUS_COLUMNS:
        statement = sa.text(
            f"UPDATE {table} SET {column} = :target "  # noqa: S608
            f"WHERE {column} IN :collapsed"
        ).bindparams(sa.bindparam("collapsed", expanding=True))
        bind.execute(
            statement,
            {"target": COLLAPSED_TARGET, "collapsed": list(COLLAPSED_STATUSES)},
        )


def _merge_and_rename_channel(
    bind: sa.engine.Connection, *, source: str, target: str
) -> None:
    """Move ledger rows from ``source`` to ``target``, aggregating collisions.

    For every UTC ledger date that holds both channels, ``source`` volume and
    count are added into ``target`` and the stale ``source`` row is deleted.
    Every remaining ``source`` row is then renamed to ``target``.  The
    ``UNIQUE(ledger_date, currency)`` anchor therefore never trips.

    Args:
        bind: Migration connection.
        source: Channel being retired.
        target: Channel receiving the volume.
    """
    collision_dates = bind.execute(
        sa.text(
            "SELECT source.ledger_date FROM daily_volume_ledgers AS source "
            "JOIN daily_volume_ledgers AS target "
            "ON target.ledger_date = source.ledger_date "
            "AND target.currency = :target "
            "WHERE source.currency = :source"
        ),
        {"source": source, "target": target},
    ).fetchall()

    aggregate = sa.text(
        "UPDATE daily_volume_ledgers "
        "SET total_volume = total_volume + ("
        "SELECT total_volume FROM daily_volume_ledgers "
        "WHERE ledger_date = :ledger_date AND currency = :source), "
        "transaction_count = transaction_count + ("
        "SELECT transaction_count FROM daily_volume_ledgers "
        "WHERE ledger_date = :ledger_date AND currency = :source) "
        "WHERE ledger_date = :ledger_date AND currency = :target"
    )
    delete_source = sa.text(
        "DELETE FROM daily_volume_ledgers "
        "WHERE ledger_date = :ledger_date AND currency = :source"
    )
    for (ledger_date,) in collision_dates:
        params = {
            "ledger_date": ledger_date,
            "source": source,
            "target": target,
        }
        bind.execute(aggregate, params)
        bind.execute(delete_source, params)

    bind.execute(
        sa.text(
            "UPDATE daily_volume_ledgers SET currency = :target "
            "WHERE currency = :source"
        ),
        {"source": source, "target": target},
    )


def upgrade() -> None:
    """Rename the status and ledger channel to the payment-rail vocabulary."""
    bind = op.get_bind()
    _rename_statuses(bind, old=OLD_STATUS, new=NEW_STATUS)
    _merge_and_rename_channel(bind, source=OLD_CHANNEL, target=NEW_CHANNEL)


def downgrade() -> None:
    """Collapse the new tail and reverse the status and ledger rename."""
    bind = op.get_bind()
    _collapse_payment_tail(bind)
    _rename_statuses(bind, old=NEW_STATUS, new=OLD_STATUS)
    _merge_and_rename_channel(bind, source=NEW_CHANNEL, target=OLD_CHANNEL)

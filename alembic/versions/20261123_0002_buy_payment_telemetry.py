"""Add buy-payment telemetry columns to ``p2p_orders``.

Revision ID: 20261123_0002
Revises: 20260101_0001
Create Date: 2026-11-23 00:00:00

Five nullable, side-effect-free columns record the SMS payout telemetry used by
the buy flow.  No index, backfill, constraint or data migration is added: all
columns are nullable, the existing ``ix_p2p_orders_status`` already serves the
candidate query, and nothing reads these columns until later revisions ship.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261123_0002"
down_revision: str | None = "20260101_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "p2p_orders"

# Column definitions in upgrade order; the downgrade drops them in reverse.
_TELEMETRY_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("counterparty_id_number", sa.String(length=32)),
    ("sms_message_id", sa.String(length=128)),
    ("sms_sent_at", sa.DateTime(timezone=True)),
    ("bank_short_code", sa.String(length=16)),
    ("bank_confirmed_at", sa.DateTime(timezone=True)),
)
COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in _TELEMETRY_COLUMNS)


def upgrade() -> None:
    """Add the five nullable telemetry columns to ``p2p_orders``."""
    for name, type_ in _TELEMETRY_COLUMNS:
        op.add_column(TABLE_NAME, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    """Drop the telemetry columns in reverse order using batch mode.

    Batch mode rebuilds the SQLite table so the drop is safe on the default
    test backend while remaining a plain ``ALTER TABLE`` on PostgreSQL.
    """
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        for name in reversed(COLUMN_NAMES):
            batch_op.drop_column(name)

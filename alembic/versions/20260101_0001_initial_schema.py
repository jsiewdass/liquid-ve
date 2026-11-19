"""Initial schema for liquidVE.

Revision ID: 20260101_0001
Revises:
Create Date: 2026-01-01 00:00:00

Enum columns are plain ``VARCHAR`` on purpose: the ORM models use
``Enum(..., native_enum=False)`` without creating CHECK constraints, so plain
strings keep autogenerate stable across SQLite and PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260101_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _bigint_pk() -> sa.types.TypeEngine:
    """Return the portable autoincrementing primary key type.

    Returns:
        ``BIGINT`` on PostgreSQL and ``INTEGER`` on SQLite so autoincrement
        keeps working.
    """
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")

def _json_type() -> sa.types.TypeEngine:
    """Return a portable JSON type that uses JSONB on PostgreSQL.

    Returns:
        ``JSONB`` on PostgreSQL and ``JSON`` elsewhere.
    """
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    """Create every liquidVE table."""
    op.create_table(
        "p2p_orders",
        sa.Column("id", _bigint_pk(), primary_key=True, autoincrement=True),
        sa.Column("order_number", sa.String(length=64), nullable=False),
        sa.Column("external_order_id", sa.String(length=64), nullable=True),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("asset", sa.String(length=16), nullable=False),
        sa.Column("fiat_currency", sa.String(length=8), nullable=False),
        sa.Column("price", sa.Numeric(24, 8), nullable=False),
        sa.Column("amount_asset", sa.Numeric(24, 8), nullable=False),
        sa.Column("amount_fiat", sa.Numeric(24, 2), nullable=False),
        sa.Column("counterparty_name", sa.String(length=120), nullable=True),
        sa.Column("counterparty_phone", sa.String(length=32), nullable=True),
        sa.Column("bank_code", sa.String(length=32), nullable=True),
        sa.Column("bank_reference", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("risk_checked", sa.Boolean(), nullable=False),
        sa.Column("manual_review_reason", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("raw_payload", _json_type(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("price > 0", name="ck_p2p_orders_price_positive"),
        sa.CheckConstraint("amount_asset > 0", name="ck_p2p_orders_amount_asset_positive"),
        sa.CheckConstraint("amount_fiat > 0", name="ck_p2p_orders_amount_fiat_positive"),
        sa.UniqueConstraint("bank_reference", name="uq_p2p_orders_bank_reference"),
        sa.UniqueConstraint("idempotency_key", name="uq_p2p_orders_idempotency_key"),
    )
    op.create_index("ix_p2p_orders_order_number", "p2p_orders", ["order_number"], unique=True)
    op.create_index("ix_p2p_orders_status", "p2p_orders", ["status"], unique=False)

    op.create_table(
        "bank_transactions",
        sa.Column("id", _bigint_pk(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_id",
            _bigint_pk(),
            sa.ForeignKey(
                "p2p_orders.id",
                ondelete="CASCADE",
                name="fk_bank_transactions_order_id",
            ),
            nullable=False,
        ),
        sa.Column("external_reference", sa.String(length=64), nullable=True),
        sa.Column("bank_reference", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("bank_code", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(24, 2), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("payer_phone", sa.String(length=32), nullable=True),
        sa.Column("payer_name", sa.String(length=120), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("verification_status", sa.String(length=32), nullable=True),
        sa.Column("raw_payload", _json_type(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_bank_transactions_amount_positive"),
    )
    op.create_index(
        "ix_bank_transactions_bank_reference",
        "bank_transactions",
        ["bank_reference"],
        unique=True,
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", _bigint_pk(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_id",
            _bigint_pk(),
            sa.ForeignKey("p2p_orders.id", ondelete="SET NULL", name="fk_audit_logs_order_id"),
            nullable=True,
        ),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", _json_type(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_audit_logs_event", "audit_logs", ["event"], unique=False)
    op.create_index(
        "ix_audit_logs_order_id_created_at",
        "audit_logs",
        ["order_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", _bigint_pk(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_event_id", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("payload", _json_type(), nullable=True),
        sa.UniqueConstraint(
            "provider",
            "external_event_id",
            name="uq_webhook_events_provider_event_id",
        ),
    )

    op.create_table(
        "daily_volume_ledgers",
        sa.Column("id", _bigint_pk(), primary_key=True, autoincrement=True),
        sa.Column("ledger_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column(
            "total_volume",
            sa.Numeric(24, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "transaction_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "ledger_date",
            "currency",
            name="uq_daily_volume_ledger_date_currency",
        ),
    )


def downgrade() -> None:
    """Drop every liquidVE table in reverse dependency order."""
    op.drop_table("daily_volume_ledgers")
    op.drop_table("webhook_events")
    op.drop_index("ix_audit_logs_order_id_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_event", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_bank_transactions_bank_reference", table_name="bank_transactions")
    op.drop_table("bank_transactions")
    op.drop_index("ix_p2p_orders_status", table_name="p2p_orders")
    op.drop_index("ix_p2p_orders_order_number", table_name="p2p_orders")
    op.drop_table("p2p_orders")

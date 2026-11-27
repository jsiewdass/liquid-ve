"""Track the active Banesco payment slot and receipt checksum."""

import sqlalchemy as sa
from alembic import op

revision = "20261127_0004"
down_revision = "20261125_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("p2p_orders", sa.Column("payment_slot", sa.Integer(), nullable=True))
    op.add_column(
        "p2p_orders", sa.Column("receipt_sha256", sa.String(64), nullable=True)
    )
    op.create_index(
        "ix_p2p_orders_payment_slot", "p2p_orders", ["payment_slot"], unique=True
    )


def downgrade() -> None:
    orders = sa.table(
        "p2p_orders", sa.column("status"), sa.column("manual_review_reason")
    )
    op.execute(
        orders.update()
        .where(orders.c.status == "PAYMENT_CONFIRMED")
        .values(
            status="MANUAL_REVIEW",
            manual_review_reason="banesco_receipt_schema_downgraded",
        )
    )
    audit = sa.table("audit_logs", sa.column("from_status"), sa.column("to_status"))
    for status in (audit.c.from_status, audit.c.to_status):
        op.execute(
            audit.update()
            .where(status == "PAYMENT_CONFIRMED")
            .values({status: "MANUAL_REVIEW"})
        )
    op.drop_index("ix_p2p_orders_payment_slot", table_name="p2p_orders")
    with op.batch_alter_table("p2p_orders") as batch:
        batch.drop_column("receipt_sha256")
        batch.drop_column("payment_slot")

"""SQLAlchemy 2.0 declarative models.

Money is stored as :class:`sqlalchemy.Numeric` and mapped to
:class:`decimal.Decimal`; floats are never used.  A deterministic naming
convention keeps Alembic autogenerate stable across environments.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.enums import (
    AuditActor,
    AuditEvent,
    BankCode,
    BankTransactionDirection,
    OrderStatus,
    OrderType,
    PaymentVerificationStatus,
)

JSONType = JSON().with_variant(JSONB, "postgresql")

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with a deterministic constraint naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Adds timezone-aware creation and update timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


def _bigint_pk() -> Any:
    """Return a BigInteger primary key type with a SQLite-compatible variant.

    Returns:
        A type that maps to ``INTEGER PRIMARY KEY`` on SQLite so autoincrement
        keeps working there, and to ``BIGINT`` on PostgreSQL.
    """
    return BigInteger().with_variant(Integer, "sqlite")


class P2POrder(TimestampMixin, Base):
    """A P2P trade tracked by the engine.

    ``order_number`` is the idempotency anchor for order creation and
    ``bank_reference`` is unique per order once a payment is matched.
    """

    __tablename__ = "p2p_orders"
    __table_args__ = (
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("amount_asset > 0", name="amount_asset_positive"),
        CheckConstraint("amount_fiat > 0", name="amount_fiat_positive"),
        Index("ix_p2p_orders_status", "status"),
        Index("ix_p2p_orders_payment_slot", "payment_slot", unique=True),
    )

    id: Mapped[int] = mapped_column(_bigint_pk(), primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    external_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType, native_enum=False, length=16, name="order_type"),
        nullable=False,
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=32, name="order_status"),
        nullable=False,
        default=OrderStatus.PENDING,
    )
    asset: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    fiat_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="VES")
    price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    amount_asset: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    amount_fiat: Mapped[Decimal] = mapped_column(Numeric(24, 2), nullable=False)
    counterparty_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    counterparty_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    counterparty_id_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bank_code: Mapped[BankCode | None] = mapped_column(
        Enum(BankCode, native_enum=False, length=32, name="p2p_order_bank_code"),
        nullable=True,
    )
    bank_reference: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    # Historical telemetry. The current payment rail does not write these fields.
    sms_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sms_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    bank_short_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bank_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    payment_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    receipt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    risk_checked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )

    bank_transactions: Mapped[list["BankTransaction"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(
        back_populates="order",
        lazy="selectin",
        passive_deletes=True,
    )

    __mapper_args__ = {"version_id_col": version}

    def __repr__(self) -> str:
        return (
            f"<P2POrder id={self.id} order_number={self.order_number!r} "
            f"status={self.status} amount_fiat={self.amount_fiat}>"
        )


class BankTransaction(Base):
    """A bank movement used to reconcile an order.

    ``bank_reference`` is the financial idempotency anchor and is unique.
    """

    __tablename__ = "bank_transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_bank_transactions_bank_reference", "bank_reference", unique=True),
    )

    id: Mapped[int] = mapped_column(_bigint_pk(), primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("p2p_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bank_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[BankTransactionDirection] = mapped_column(
        Enum(
            BankTransactionDirection,
            native_enum=False,
            length=16,
            name="bank_transaction_direction",
        ),
        nullable=False,
    )
    bank_code: Mapped[BankCode] = mapped_column(
        Enum(BankCode, native_enum=False, length=32, name="bank_transaction_bank_code"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="VES")
    payer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payer_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_status: Mapped[PaymentVerificationStatus | None] = mapped_column(
        Enum(
            PaymentVerificationStatus,
            native_enum=False,
            length=32,
            name="payment_verification_status",
        ),
        nullable=True,
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    order: Mapped["P2POrder"] = relationship(back_populates="bank_transactions", lazy="selectin")

    def __repr__(self) -> str:
        masked_reference = f"***{self.bank_reference[-4:]}" if self.bank_reference else None
        return (
            f"<BankTransaction id={self.id} order_id={self.order_id} "
            f"reference={masked_reference!r} amount={self.amount}>"
        )


class AuditLog(Base):
    """Append-only audit trail entry."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_order_id_created_at", "order_id", "created_at"),)

    id: Mapped[int] = mapped_column(_bigint_pk(), primary_key=True, autoincrement=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("p2p_orders.id", ondelete="SET NULL"),
        nullable=True,
    )
    event: Mapped[AuditEvent] = mapped_column(
        Enum(AuditEvent, native_enum=False, length=32, name="audit_event"),
        nullable=False,
        index=True,
    )
    actor: Mapped[AuditActor] = mapped_column(
        Enum(AuditActor, native_enum=False, length=16, name="audit_actor"),
        nullable=False,
    )
    from_status: Mapped[OrderStatus | None] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=32, name="audit_from_status"),
        nullable=True,
    )
    to_status: Mapped[OrderStatus | None] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=32, name="audit_to_status"),
        nullable=True,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    order: Mapped["P2POrder | None"] = relationship(back_populates="audit_logs", lazy="selectin")

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} event={self.event} actor={self.actor} "
            f"order_id={self.order_id}>"
        )


class WebhookEvent(Base):
    """Deduplicated record of an inbound webhook.

    The ``(provider, external_event_id)`` unique constraint is the replay
    protection mechanism.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "external_event_id",
            name="uq_webhook_events_provider_event_id",
        ),
    )

    id: Mapped[int] = mapped_column(_bigint_pk(), primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<WebhookEvent id={self.id} provider={self.provider!r} "
            f"event={self.external_event_id!r} processed={self.processed}>"
        )


class DailyVolumeLedger(Base):
    """Per-day, per-currency volume used for atomic limit accounting."""

    __tablename__ = "daily_volume_ledgers"
    __table_args__ = (
        UniqueConstraint(
            "ledger_date",
            "currency",
            name="uq_daily_volume_ledger_date_currency",
        ),
    )

    id: Mapped[int] = mapped_column(_bigint_pk(), primary_key=True, autoincrement=True)
    ledger_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    total_volume: Mapped[Decimal] = mapped_column(
        Numeric(24, 2),
        nullable=False,
        default=Decimal("0.00"),
        server_default=text("0"),
    )
    transaction_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<DailyVolumeLedger id={self.id} date={self.ledger_date} "
            f"currency={self.currency} total_volume={self.total_volume}>"
        )


ALL_MODELS: tuple[type[Base], ...] = (
    P2POrder,
    BankTransaction,
    AuditLog,
    WebhookEvent,
    DailyVolumeLedger,
)

__all__ = [
    "Base",
    "TimestampMixin",
    "JSONType",
    "P2POrder",
    "BankTransaction",
    "AuditLog",
    "WebhookEvent",
    "DailyVolumeLedger",
    "ALL_MODELS",
]

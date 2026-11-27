"""Register Binance BUY orders and prepare authenticated manual payments."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DuplicateOrderError, InvalidStateTransitionError
from app.core.logging import request_id_var
from app.domain.enums import AuditActor, AuditEvent, OrderStatus, OrderType
from app.infrastructure.clients.binance_client import BinanceC2CClient, BinanceC2COrder
from app.infrastructure.models import AuditLog, P2POrder

PENDING_PAYMENT = "PENDING_PAYMENT"


def validate_buy(detail: BinanceC2COrder, order_number: str) -> None:
    if detail.order_number != order_number or detail.trade_type is not OrderType.BUY:
        raise InvalidStateTransitionError(
            "A matching BUY order is required", code="direction_mismatch"
        )
    if (
        detail.status != PENDING_PAYMENT
        or detail.fiat != "VES"
        or detail.asset != "USDT"
    ):
        raise InvalidStateTransitionError(
            "The USDT/VES order is no longer awaiting payment",
            code="external_status_not_pending_payment",
        )


def record_status(
    session: AsyncSession,
    order: P2POrder,
    status: OrderStatus,
    actor: AuditActor,
    event: AuditEvent,
    message: str,
) -> None:
    old_status = order.status
    if not old_status.can_transition_to(status):
        raise InvalidStateTransitionError(
            "The order cannot advance from its current status"
        )
    order.status = status
    session.add(
        AuditLog(
            order_id=order.id,
            event=event,
            actor=actor,
            from_status=old_status,
            to_status=status,
            message=message,
            request_id=request_id_var.get(),
        )
    )


class BuyFlow:
    def __init__(self, session: AsyncSession, *, binance: BinanceC2CClient) -> None:
        self._session = session
        self._binance = binance

    async def execute(self, order_number: str) -> P2POrder:
        """Register a BUY order without starting any payment."""
        existing = await self._session.scalar(
            select(P2POrder.id).where(P2POrder.order_number == order_number)
        )
        if existing is not None:
            raise DuplicateOrderError("The order is already registered")
        detail = await self._binance.get_order_detail(order_number)
        validate_buy(detail, order_number)
        return await self.register(detail)

    async def register(self, detail: BinanceC2COrder) -> P2POrder:
        order = P2POrder(
            order_number=detail.order_number,
            external_order_id=detail.order_number,
            order_type=detail.trade_type,
            status=OrderStatus.PENDING,
            asset=detail.asset,
            fiat_currency=detail.fiat,
            price=detail.price,
            amount_asset=detail.amount,
            amount_fiat=detail.total_price,
            counterparty_name=detail.counterparty_nickname,
            raw_payload=detail.model_dump(mode="json"),
        )
        self._session.add(order)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise DuplicateOrderError("The order is already registered") from exc
        self._session.add(
            AuditLog(
                order_id=order.id,
                event=AuditEvent.ORDER_CREATED,
                actor=AuditActor.SYSTEM,
                to_status=OrderStatus.PENDING,
                message="Buy order registered from Binance",
                request_id=request_id_var.get(),
            )
        )
        await self._session.commit()
        return order

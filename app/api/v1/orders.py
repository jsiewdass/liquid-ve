"""Authenticated operator endpoint for the BUY payout lifecycle.

The endpoint is the only human-in-the-loop path into the BUY pipeline: it is
gated by the internal API key, it advances a parked order through legal
transitions only, and it returns lifecycle data without ever exposing the
amount, the counterparty phone or the cedula.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    ConflictError,
    ExternalServiceError,
    InvalidStateTransitionError,
    NotFoundError,
    UnauthorizedError,
)
from app.core.logging import get_logger, request_id_var
from app.core.security import require_internal_api_key
from app.domain.enums import AuditActor, AuditEvent, OrderStatus, OrderType
from app.domain.schemas import BuyPayRequest, BuyPayResponse
from app.infrastructure.database import get_session
from app.infrastructure.models import AuditLog, P2POrder
from app.services.buy_flow import BuyFlow

logger = get_logger("orders")

router = APIRouter(prefix="/orders", tags=["orders"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_AWAITING_OPERATOR = "awaiting_operator_approval"
_PENDING_PAYMENT_STATUS = "PENDING_PAYMENT"
_PIPELINE_STATUSES = frozenset(
    {
        OrderStatus.PENDING,
        OrderStatus.PAYMENT_DETECTED,
        OrderStatus.VERIFYING_CACHICAMO,
    }
)
_TERMINAL_STATUSES = frozenset({OrderStatus.COMPLETED, OrderStatus.FAILED})


async def require_operator_key(
    request: Request,
    session: SessionDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Enforce the internal API key and audit a rejected attempt.

    The audit row is committed before the error is re-raised so a refused
    operator action is never lost (R-BUY-005).

    Args:
        request: Current request.
        session: Request-scoped database session.
        x_api_key: Internal API key header.

    Raises:
        UnauthorizedError: When the key is missing or invalid.
    """
    try:
        await require_internal_api_key(x_api_key)
    except UnauthorizedError:
        session.add(
            AuditLog(
                order_id=None,
                event=AuditEvent.WEBHOOK_REJECTED,
                actor=AuditActor.SYSTEM,
                message="Operator endpoint rejected: unauthorized",
                payload={
                    "endpoint": "orders.buy_pay",
                    "path": request.url.path,
                },
                request_id=request_id_var.get(),
            )
        )
        await session.commit()
        raise


def _request_settings(request: Request) -> Settings:
    """Return the settings attached to the app, falling back to the cache.

    Args:
        request: Current request.

    Returns:
        Active settings.
    """
    attached = getattr(request.app.state, "settings", None)
    return attached if isinstance(attached, Settings) else get_settings()


def _clients(request: Request) -> tuple[Any, Any, Any, Any]:
    """Return the startup clients, tolerating a lifespan-free app.

    Args:
        request: Current request.

    Returns:
        A ``(binance, cachicamo, sms, browser)`` tuple.
    """
    state = request.app.state
    return (
        getattr(state, "binance", None),
        getattr(state, "cachicamo", None),
        getattr(state, "sms", None),
        getattr(state, "browser", None),
    )


def _response(order: P2POrder, outcome: str) -> BuyPayResponse:
    """Build the PII-free operator response for an order.

    Args:
        order: Persisted order.
        outcome: Lifecycle outcome vocabulary value.

    Returns:
        The response model instance.
    """
    return BuyPayResponse(
        order_number=order.order_number,
        status=order.status,
        outcome=outcome,  # type: ignore[arg-type]
        manual_review_reason=order.manual_review_reason,
        bank_short_code=order.bank_short_code,
        sms_message_id=order.sms_message_id,
    )


@router.post(
    "/buy/{order_number}/pay",
    status_code=202,
    response_model=BuyPayResponse,
    dependencies=[Depends(require_operator_key)],
    summary="Approve and dispatch a BUY payout",
)
async def pay_buy_order(
    order_number: str,
    request: Request,
    response: Response,
    session: SessionDep,
    payload: BuyPayRequest | None = None,
) -> BuyPayResponse:
    """Approve a parked BUY order or resume an in-flight one.

    ``MANUAL_REVIEW`` with ``awaiting_operator_approval`` advances through
    ``MANUAL_REVIEW -> PENDING`` and runs the pipeline with the human approval;
    an in-flight order runs the pipeline; ``SENDING_PAYMENT`` is a no-op.  Terminal
    orders and SELL orders are refused.

    Args:
        order_number: Provider order number.
        request: Current request (client and settings access).
        response: Response object used to switch between 200 and 202.
        session: Request-scoped database session.
        payload: Optional operator metadata; never echoed back.

    Returns:
        The operator-facing order view.

    Raises:
        NotFoundError: When the order does not exist.
        InvalidStateTransitionError: When the order is not a BUY order.
        ConflictError: When the order is terminal.
    """
    # PostgreSQL locks the row so concurrent submissions serialise; SQLite
    # ignores the clause, so the status preconditions below are the guard.
    result = await session.execute(
        select(P2POrder)
        .where(P2POrder.order_number == order_number)
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError(f"Order {order_number} was not found")
    if order.order_type is not OrderType.BUY:
        raise InvalidStateTransitionError(
            f"Order {order_number} is not a BUY order",
            code="direction_mismatch",
            details={"order_type": order.order_type.value},
        )
    if order.status in _TERMINAL_STATUSES:
        raise ConflictError(
            f"Order {order_number} is in a terminal status",
            code="terminal_status",
            details={"status": order.status.value},
        )
    if order.status is OrderStatus.SENDING_PAYMENT:
        response.status_code = 200
        return _response(order, "noop")

    binance, cachicamo, sms, browser = _clients(request)
    if binance is None:
        raise ExternalServiceError(
            "Binance client is unavailable", service="binance"
        )
    detail = await binance.get_order_detail(order_number)
    if detail.status != _PENDING_PAYMENT_STATUS:
        raise InvalidStateTransitionError(
            f"Provider order {order_number} is no longer awaiting payment",
            code="external_status_not_pending_payment",
            details={"status": detail.status},
        )
    flow = BuyFlow(
        session,
        settings=_request_settings(request),
        binance=binance,
        cachicamo=cachicamo,
        sms=sms,
        browser=browser,
    )
    if order.status is OrderStatus.MANUAL_REVIEW:
        if order.manual_review_reason != _AWAITING_OPERATOR:
            response.status_code = 200
            return _response(order, "manual_review")
        await flow._transition(
            order,
            OrderStatus.PENDING,
            AuditActor.OPERATOR,
            AuditEvent.STATUS_CHANGED,
            "Operator approved the payout",
        )
    elif order.status not in _PIPELINE_STATUSES:
        raise ConflictError(
            f"Order {order_number} cannot be paid from {order.status.value}",
            code="terminal_status",
            details={"status": order.status.value},
        )
    if order.status not in _PIPELINE_STATUSES:
        # A concurrent submission may have advanced the row already.
        response.status_code = 200
        return _response(order, "noop")
    if sms is None:
        raise ExternalServiceError(
            "SMS gateway is unavailable", service="sms_gateway"
        )
    order = await flow._dispatch(
        order,
        sms=sms,
        operator_approved=True,
        actor=AuditActor.OPERATOR,
    )
    response.status_code = 202
    outcome = "manual_review" if order.status is OrderStatus.MANUAL_REVIEW else "dispatched"
    logger.info("Operator payout for order {} ended in {}", order_number, order.status.value)
    return _response(order, outcome)


__all__ = ["router"]

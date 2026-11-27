"""Inbound webhooks for Cachicamo.

Idempotency contract: an inbound webhook is recorded in ``webhook_events``
under the ``(provider, external_event_id)`` unique constraint *before* any
business handler runs.  For Cachicamo, the unique ``bank_reference`` on
``bank_transactions`` is the financial anchor: money movement is only possible
after that insert commits, so a replay necessarily fails before any
non-idempotent Binance call.  Replays always receive HTTP 202, never 500.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import LiquidVEException
from app.core.logging import get_logger, request_id_var
from app.core.security import (
    CachicamoWebhookVerifier,
    hash_payload,
)
from app.domain.enums import AuditActor, AuditEvent, OrderStatus, OrderType
from app.domain.schemas import (
    CachicamoWebhookPayload,
    WebhookAcceptedResponse,
)
from app.infrastructure.database import get_session
from app.infrastructure.models import AuditLog, P2POrder, WebhookEvent
from app.services.sell_flow import SellFlow

logger = get_logger("webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_CACHICAMO_PROVIDER = "cachicamo"


def _request_settings(request: Request) -> Settings:
    """Return settings attached to the app, falling back to the cache.

    Args:
        request: Current request.

    Returns:
        Active settings.
    """
    attached = getattr(request.app.state, "settings", None)
    return attached if isinstance(attached, Settings) else get_settings()


def _clients(request: Request) -> tuple[Any, Any]:
    """Return the clients created during application startup.

    Args:
        request: Current request.

    Returns:
        A ``(binance, cachicamo)`` tuple; elements may be ``None`` when
        the app was created without a lifespan (for example in tests).
    """
    return (
        getattr(request.app.state, "binance", None),
        getattr(request.app.state, "cachicamo", None),
    )


async def _store_event(session: AsyncSession, event: WebhookEvent) -> bool:
    """Insert a webhook event, returning ``False`` on a replay.

    Args:
        session: Active session.
        event: Event to persist.

    Returns:
        ``True`` when the row was inserted, ``False`` when it already existed.
    """
    session.add(event)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return False
    return True


@router.post(
    "/cachicamo",
    status_code=202,
    response_model=WebhookAcceptedResponse,
    summary="Receive a Cachicamo payment notification",
)
async def cachicamo_webhook(
    request: Request,
    session: SessionDep,
) -> WebhookAcceptedResponse:
    """Handle a signed Cachicamo webhook.

    Args:
        request: Raw request (body and headers are verified as received).
        session: Database session dependency.

    Returns:
        The acceptance envelope; ``duplicate=True`` for replays.

    Raises:
        SignatureVerificationError: If the signature is invalid.
        ReplayAttackError: If the timestamp is stale.
    """
    app_settings = _request_settings(request)
    raw_body = await request.body()
    verifier = CachicamoWebhookVerifier(app_settings)
    verifier.verify(raw_body, request.headers)
    payload = CachicamoWebhookPayload.model_validate_json(raw_body)
    event_id = payload.event_id or verifier.extract_event_id(request.headers)
    if not event_id:
        event_id = hash_payload(raw_body)[:64]

    stored = await _store_event(
        session,
        WebhookEvent(
            provider=_CACHICAMO_PROVIDER,
            external_event_id=event_id,
            payload_hash=hash_payload(raw_body),
            signature_valid=True,
            processed=False,
            payload=payload.model_dump(mode="json"),
        ),
    )
    if not stored:
        logger.info("Replayed Cachicamo webhook rejected as duplicate")
        return WebhookAcceptedResponse(
            accepted=True,
            event_id=event_id,
            duplicate=True,
            message="Webhook already processed",
        )

    message = await _dispatch_cachicamo(request, session, payload)
    await _mark_processed(session, _CACHICAMO_PROVIDER, event_id, None)
    return WebhookAcceptedResponse(accepted=True, event_id=event_id, message=message)


async def _mark_processed(
    session: AsyncSession,
    provider: str,
    external_event_id: str,
    error: str | None,
) -> None:
    """Mark a stored webhook event as processed.

    Args:
        session: Active session.
        provider: Provider name.
        external_event_id: Provider event identifier.
        error: Optional processing error message.
    """
    result = await session.execute(
        select(WebhookEvent).where(
            WebhookEvent.provider == provider,
            WebhookEvent.external_event_id == external_event_id,
        )
    )
    event = result.scalar_one_or_none()
    if event is None:
        return
    event.processed = error is None
    event.processing_error = error
    await session.flush()


async def _reject_non_sell_order(
    session: AsyncSession,
    order: P2POrder,
) -> str | None:
    """Reject a non-SELL order that reached the sell-side dispatch.

    The SQL predicate makes this unreachable in normal operation; it is the
    defence-in-depth half of R-BUY-011, so a caller that bypasses the query can
    still never advance a BUY order.  A ``WEBHOOK_REJECTED`` audit entry with
    reason ``direction_mismatch`` is written before returning.

    Args:
        session: Active request session.
        order: Loaded candidate order.

    Returns:
        The no-match message for a non-SELL order, ``None`` when it is SELL.
    """
    if order.order_type is OrderType.SELL:
        return None
    session.add(
        AuditLog(
            order_id=order.id,
            event=AuditEvent.WEBHOOK_REJECTED,
            actor=AuditActor.WEBHOOK,
            from_status=order.status,
            to_status=order.status,
            message="Cachicamo webhook rejected: direction mismatch",
            payload={"reason": "direction_mismatch"},
            request_id=request_id_var.get(),
        )
    )
    await session.flush()
    logger.warning("Cachicamo webhook rejected a non-SELL order")
    return "Payment stored; no matching order is waiting for it"


async def _dispatch_cachicamo(
    request: Request,
    session: AsyncSession,
    payload: CachicamoWebhookPayload,
) -> str:
    """Forward a verified payment to the sell flow.

    Matching is direction-first: only ``SELL`` orders are candidates, because a
    Cachicamo payment advances the order and can release crypto.  Within that
    set the oldest pending order with the same fiat amount is selected.

    Args:
        request: Current request (used to read the app clients).
        session: Active session.
        payload: Verified webhook payload.

    Returns:
        A human readable acceptance message.
    """
    # TODO(integration): tighten matching with payer phone and bank account
    # rules once Cachicamo exposes them.
    result = await session.execute(
        select(P2POrder)
        .where(
            P2POrder.order_type == OrderType.SELL,
            P2POrder.status.in_(
                [OrderStatus.PENDING, OrderStatus.PAYMENT_DETECTED]
            ),
            P2POrder.amount_fiat == payload.amount_ves,
        )
        .order_by(P2POrder.created_at.asc())
        .limit(1)
    )
    order = result.scalar_one_or_none()
    if order is None:
        return "Payment stored; no matching order is waiting for it"
    rejection = await _reject_non_sell_order(session, order)
    if rejection is not None:
        return rejection
    binance, cachicamo = _clients(request)
    flow = SellFlow(
        session,
        settings=_request_settings(request),
        binance=binance,
        cachicamo=cachicamo,
    )
    try:
        await flow.handle_incoming_payment(
            order_number=order.order_number,
            bank_reference=payload.bank_reference,
            amount_ves=payload.amount_ves,
            payer_phone=payload.payer_phone,
            bank_code=payload.bank_code,
        )
    except LiquidVEException as exc:
        await _mark_processed(
            session,
            _CACHICAMO_PROVIDER,
            payload.event_id,
            f"{exc.code}: {exc.message}",
        )
        logger.warning("Cachicamo webhook dispatch failed: {}", type(exc).__name__)
        return "Payment stored; processing requires manual review"
    return "Payment processed"


__all__ = ["router"]

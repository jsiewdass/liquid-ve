"""Operator endpoints for the manual Banesco payment flow."""

import json
import re
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ExternalServiceError,
    InvalidStateTransitionError,
    NotFoundError,
    UnauthorizedError,
)
from app.core.logging import request_id_var
from app.core.security import require_internal_api_key
from app.domain.enums import AuditActor, AuditEvent, OrderStatus, OrderType
from app.domain.schemas import BuyPayResponse
from app.infrastructure.database import get_session
from app.infrastructure.models import AuditLog, P2POrder
from app.services.banesco_payment import BanescoPayments, PendingAuthorization
from app.services.buy_flow import BuyFlow, validate_buy

router = APIRouter(prefix="/orders", tags=["orders"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]

_PRIVATE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


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
        await require_internal_api_key(
            x_api_key, app_settings=request.app.state.settings
        )
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


def _payments(request: Request) -> BanescoPayments:
    payments = getattr(request.app.state, "banesco_payments", None)
    if payments is None:
        raise HTTPException(status_code=503, detail="Banesco payments are disabled")
    return payments


def _order_result(order: P2POrder, *, started: bool = False) -> BuyPayResponse:
    outcome = "dispatched" if started else "noop"
    if order.status is OrderStatus.MANUAL_REVIEW:
        outcome = "manual_review"
    return BuyPayResponse(
        order_number=order.order_number,
        status=order.status,
        outcome=outcome,
        manual_review_reason=order.manual_review_reason,
        receipt_sha256=order.receipt_sha256,
    )


@router.post(
    "/buy/{order_number}/pay",
    status_code=202,
    response_model=BuyPayResponse,
    dependencies=[Depends(require_operator_key)],
)
async def pay_buy_order(
    order_number: str,
    request: Request,
    response: Response,
    session: SessionDep,
) -> BuyPayResponse:
    """Manually start a payment for a current Binance BUY order."""
    payments = _payments(request)
    order = await session.scalar(
        select(P2POrder).where(P2POrder.order_number == order_number)
    )
    if order is not None:
        if order.order_type is not OrderType.BUY:
            raise InvalidStateTransitionError(
                "The order must be a BUY", code="direction_mismatch"
            )
        if order.status is not OrderStatus.PENDING or order.idempotency_key is not None:
            response.status_code = 200
            return _order_result(order)

    binance = getattr(request.app.state, "binance", None)
    browser = getattr(request.app.state, "browser", None)
    if binance is None or browser is None:
        raise ExternalServiceError(
            "Binance order access is unavailable", service="binance"
        )
    detail = await binance.get_order_detail(order_number)
    validate_buy(detail, order_number)
    if order is None:
        order = await BuyFlow(session, binance=binance).register(detail)
    if (order.amount_fiat, order.amount_asset, order.price) != (
        detail.total_price,
        detail.amount,
        detail.price,
    ):
        raise InvalidStateTransitionError("The Binance order amount or rate changed")
    instruction = await browser.extract_payment_instruction(order_number)
    # Recheck provider state after scraping, before any bank action.
    latest = await binance.get_order_detail(order_number)
    validate_buy(latest, order_number)
    if (detail.total_price, detail.amount, detail.price) != (
        latest.total_price,
        latest.amount,
        latest.price,
    ):
        raise InvalidStateTransitionError("The Binance order amount or rate changed")
    await payments.start(session, order, instruction)
    return _order_result(order, started=order.status is OrderStatus.SENDING_PAYMENT)


@router.get(
    "/buy/{order_number}/payment",
    response_model=BuyPayResponse,
    dependencies=[Depends(require_operator_key)],
)
async def payment_status(order_number: str, session: SessionDep) -> BuyPayResponse:
    order = await session.scalar(
        select(P2POrder).where(
            P2POrder.order_number == order_number, P2POrder.order_type == OrderType.BUY
        )
    )
    if order is None:
        raise NotFoundError("The BUY order is not registered")
    return _order_result(order)


_AUTHORIZATION_PAGE = """<!doctype html>
<html lang="es">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Autorizar Pago Móvil</title>
<main>
  <h1>Autorizar Pago Móvil</h1>
  <p id="summary" aria-live="polite">Verificando el enlace…</p>
  <form hidden>
    <label for="code">Código de Banesco</label>
    <input id="code" type="password" inputmode="numeric" autocomplete="one-time-code"
           pattern="[0-9]{6,8}" maxlength="8" required>
    <button>Autorizar pago</button>
    <p id="entry-time"></p>
  </form>
</main>
<script nonce="__NONCE__">
  const token = location.hash.slice(1);
  history.replaceState(null, "", location.pathname);
  const headers = {"Authorization": "Bearer " + token};
  const summary = document.querySelector("#summary");
  const form = document.querySelector("form");
  const code = document.querySelector("#code");
  const entryTime = document.querySelector("#entry-time");
  const expired = "La autorización expiró o ya se usó.";
  async function load() {
    try {
      const reply = await fetch(location.pathname + "/details", {headers, cache: "no-store"});
      if (!reply.ok) { summary.textContent = expired; return; }
      const details = await reply.json();
      summary.textContent = "Orden " + details.order_number + " · Bs. " + details.amount
        + " · " + details.bank + " · teléfono terminado en " + details.phone_last4;
      form.hidden = false;
      const deadline = performance.now() + details.milliseconds_left;
      function updateCountdown() {
        if (form.hidden) return;
        const remaining = deadline - performance.now();
        if (remaining <= 0) {
          form.hidden = true;
          code.value = "";
          summary.textContent = "Terminó el tiempo para ingresar el código.";
          return;
        }
        entryTime.textContent = "Tiempo para ingresar el código: " + Math.ceil(remaining / 1000) + " s";
        setTimeout(updateCountdown, Math.min(1000, remaining));
      }
      updateCountdown();
    } catch { summary.textContent = "No se pudo consultar el estado del enlace."; }
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    form.hidden = true;
    const body = JSON.stringify({code: code.value});
    code.value = "";
    try {
      const reply = await fetch(location.pathname, {
        method: "POST", body, headers: {...headers, "Content-Type": "application/json"}
      });
      summary.textContent = reply.ok ? "Código recibido. Banesco está verificando el pago." : expired;
    } catch { summary.textContent = "No se confirmó la recepción. Consulta el estado de la orden."; }
  });
  if (token) load(); else summary.textContent = expired;
</script>
</html>"""


def _authorization(
    request: Request,
    *,
    open_form: bool = False,
) -> tuple[BanescoPayments, str, PendingAuthorization]:
    payments = _payments(request)
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ")
    pending = None
    if header.startswith("Bearer ") and re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        pending = (
            payments.open_authorization(token) if open_form else payments.pending(token)
        )
    if pending is None:
        raise HTTPException(
            status_code=410,
            detail="Authorization expired or already used",
            headers=_PRIVATE_HEADERS,
        )
    return payments, token, pending


@router.get("/buy/authorize", include_in_schema=False)
async def authorization_page(request: Request) -> HTMLResponse:
    _payments(request)
    nonce = secrets.token_urlsafe(18)
    return HTMLResponse(
        _AUTHORIZATION_PAGE.replace("__NONCE__", nonce),
        headers={
            **_PRIVATE_HEADERS,
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; "
                "form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )


@router.get("/buy/authorize/details", include_in_schema=False)
async def authorization_details(request: Request) -> JSONResponse:
    _, _, pending = _authorization(request, open_form=True)
    assert pending.entry_deadline is not None
    return JSONResponse(
        {
            "order_number": pending.order_number,
            "amount": str(pending.target.amount_ves),
            "bank": pending.target.destination_bank_option,
            "phone_last4": pending.target.phone_rest[-4:],
            "milliseconds_left": max(
                0, int((pending.entry_deadline - time.monotonic()) * 1000)
            ),
        },
        headers=_PRIVATE_HEADERS,
    )


@router.post("/buy/authorize", status_code=202, include_in_schema=False)
async def authorize_payment(request: Request) -> JSONResponse:
    payments, token, _ = _authorization(request)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 128:
            raise HTTPException(
                status_code=413,
                detail="Invalid authorization body",
                headers=_PRIVATE_HEADERS,
            )
        body.extend(chunk)
    try:
        payload = json.loads(body)
        code = payload.get("code") if isinstance(payload, dict) else None
    except (ValueError, UnicodeError):
        code = None
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6,8}", code):
        raise HTTPException(
            status_code=422,
            detail="Invalid authorization code format",
            headers=_PRIVATE_HEADERS,
        )
    if not payments.authorize(token, code):
        raise HTTPException(
            status_code=410,
            detail="Authorization expired or already used",
            headers=_PRIVATE_HEADERS,
        )
    return JSONResponse({"accepted": True}, status_code=202, headers=_PRIVATE_HEADERS)

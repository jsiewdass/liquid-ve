# buy-flow Specification

## Purpose

BUY-side orchestration: detect open BUY orders, decide whether paying is safe, dispatch one Pago Móvil command, confirm it from the bank reply, and close on Binance. This capability writes order state and moves real money, so every requirement declares its idempotency and audit contract.

Invariants: money is `Decimal` only; an order MUST be durably committed to `SENDING_SMS` before any gateway call; no path MAY reach `COMPLETED` without a confirmed bank reply **and** a successful seller notification; a money command MUST NOT be auto-retried when its fate is unknown; every unknown, incomplete, or ambiguous outcome fails closed to `MANUAL_REVIEW` with a machine-readable reason; every state change and reservation appends an audit entry before the matching external side effect.

## Requirements

### Requirement: BUY order detection and status mapping

- **ID**: R-BUY-001

The system MUST detect open BUY orders (worker tick and operator trigger), select only orders whose trade type is BUY and whose external status is `PENDING_PAYMENT`, and map that status to local `PENDING` without adding an enum member. Non-BUY orders MUST be ignored.

**Idempotency**: `order_number` is UNIQUE; re-detection MUST create no row, no duplicate audit entry, and no state change.
**Audit**: One `ORDER_CREATED` per new order with the provider number and mapped status; re-detection emits nothing.

#### Scenario: PENDING_PAYMENT maps to PENDING

- GIVEN Binance reports a BUY order in `PENDING_PAYMENT`
- WHEN a detection tick runs
- THEN one order is persisted as `PENDING` with an `ORDER_CREATED` audit entry

#### Scenario: Repeat detection is inert

- GIVEN an already persisted `order_number`
- WHEN detection runs again
- THEN no second row and no second `ORDER_CREATED` entry are created

#### Scenario: Sell order is ignored

- GIVEN an order whose trade type is SELL
- WHEN detection evaluates it
- THEN nothing is persisted or audited

### Requirement: Idempotent processing and resume

- **ID**: R-BUY-002

Processing the same `order_number` twice MUST NOT produce a second payment attempt. An order in `SENDING_SMS` MUST be resumed reusing the original idempotency key; an order in a terminal status, or in `MANUAL_REVIEW` without the explicit operator approval of R-BUY-005, MUST be refused with a typed already-processed error and no side effect.

**Idempotency**: `resume(order_number)` is idempotent — repeating it while `SENDING_SMS` MUST NOT send an SMS or reserve budget again.
**Audit**: Each resume emits one `STATUS_CHANGED`; a refused duplicate emits a rejection audit entry and no `SMS_SENT`.

#### Scenario: Second processing sends nothing

- GIVEN an order in `SENDING_SMS`
- WHEN processing is invoked again
- THEN the order resumes and zero additional gateway calls occur

#### Scenario: Completed order is refused

- GIVEN an order in `COMPLETED`
- WHEN processing is invoked again
- THEN a typed already-processed error is returned and state is unchanged

### Requirement: Payment data extraction

- **ID**: R-BUY-003

The system MUST obtain bank numeric code, phone, cedula, and exact VES amount through the `BinanceBrowser` port and MUST persist bank code, short code, and counterparty identity number. A failed or incomplete extraction MUST move the order to `MANUAL_REVIEW` with reason `payment_data_unavailable` and MUST NOT dispatch. Payment data MUST NOT be inferred from the counterparty phone or any fallback source.

**Idempotency**: The persisted instruction is reused on resume instead of re-extracting.
**Audit**: `STATUS_CHANGED` to `MANUAL_REVIEW` on incomplete data; `EXTERNAL_ERROR` on a port failure; both with PII masked.

#### Scenario: Complete instruction is persisted

- GIVEN a BUY order exposing bank, phone, cedula, and amount
- WHEN extraction runs
- THEN a `Decimal` amount and resolvable bank code are persisted on the order

#### Scenario: Incomplete data blocks dispatch

- GIVEN an extraction result missing the cedula
- WHEN the order is processed
- THEN it moves to `MANUAL_REVIEW` with reason `payment_data_unavailable` and no gateway call occurs

### Requirement: Pre-dispatch risk circuit breakers

- **ID**: R-BUY-004

After extraction the system MUST evaluate every breaker below and MUST block on the first failure:

| Breaker | Rule | Blocked reason |
|---|---|---|
| Per-order cap | `amount_ves` ≤ `MAX_BUY_AMOUNT_VES` and `amount_asset` ≤ `MAX_USDT_PER_ORDER` | `amount_over_cap` |
| Daily SMS cap | UTC-day reservation on the `VES_SMS` ledger channel plus `amount_ves` ≤ `DAILY_SMS_LIMIT_VES` | `daily_sms_cap_exceeded` |
| Implied-rate tolerance | `abs(amount_ves / amount_asset − order.price) / order.price` ≤ `RATE_TOLERANCE_PCT` | `rate_out_of_tolerance` |
| Supported bank | a profile resolves for the extracted numeric code | `unsupported_destination_bank` |

**Idempotency**: Assessment is read-only; reservation happens only on the allow path, keyed by `(ledger_date, "VES_SMS")` so concurrent dispatches cannot both pass. Reservation is released only on a confirmed permanent dispatch failure and MUST NOT be released on timeout.
**Audit**: One `RISK_BLOCKED` per block with reason and limits snapshot; each release emits `STATUS_CHANGED` naming the amount.

#### Scenario: Over-cap amount is blocked

- GIVEN an order above `MAX_BUY_AMOUNT_VES`
- WHEN risk evaluation runs
- THEN it moves to `MANUAL_REVIEW` with reason `amount_over_cap` and no gateway call occurs

#### Scenario: Mangled scrape fails the rate check

- GIVEN an extracted amount implying a rate outside `RATE_TOLERANCE_PCT` of the order price
- WHEN risk evaluation runs
- THEN it moves to `MANUAL_REVIEW` with reason `rate_out_of_tolerance`

#### Scenario: Daily cap respects the UTC rollover

- GIVEN the current UTC day's `VES_SMS` reservation is at its cap
- WHEN the ledger date advances one UTC day
- THEN a new reservation is permitted up to the limit

#### Scenario: Unsupported bank never dispatches

- GIVEN an order whose bank has no profile
- WHEN risk evaluation runs
- THEN it moves to `MANUAL_REVIEW` with reason `unsupported_destination_bank`

### Requirement: Operator-approval gate

- **ID**: R-BUY-005

With `BUY_AUTO_PAY_ENABLED=false` (default), a risk-approved order MUST park in `MANUAL_REVIEW` with reason `awaiting_operator_approval`, MUST reserve no budget, and MUST NOT dispatch. A resume action authenticated with the internal key MUST advance it through legal transitions only and MUST be rejected when unauthenticated.

**Idempotency**: Repeating the resume on an order that already left `MANUAL_REVIEW` MUST return its state without a second dispatch or reservation.
**Audit**: Approval emits `STATUS_CHANGED` with the operator actor; an unauthorized attempt emits `WEBHOOK_REJECTED`.

#### Scenario: Gate off parks the order

- GIVEN `BUY_AUTO_PAY_ENABLED=false` and a risk-approved order
- WHEN processing completes
- THEN it is `MANUAL_REVIEW` with reason `awaiting_operator_approval` and zero calls or reservations occur

#### Scenario: Approval resumes exactly once

- GIVEN a parked order and a valid internal key
- WHEN the resume action is submitted twice
- THEN it advances legally once and the second submission is a no-op

#### Scenario: Unauthenticated approval is rejected

- GIVEN a parked order without a valid internal key
- WHEN the resume action is submitted
- THEN it is rejected and the order is unchanged

### Requirement: Durable dispatch of the bank command

- **ID**: R-BUY-006

The system MUST commit `SENDING_SMS`, the reserved amount, and the attempt record before calling the gateway, MUST send the rendered command (R-BANK-006) to the primary short code with `Idempotency-Key: "{order_number}:payout"`. On a classified not-delivered failure it MAY fall back once to the next short code. On an unknown outcome (timeout or unparseable 5xx) it MUST NOT retry, MUST keep `SENDING_SMS`, and MUST record `EXTERNAL_ERROR`. A permanent failure MUST release the reservation and move to `MANUAL_REVIEW`.

**Idempotency**: The gateway call is non-idempotent; retries exist only for the classified fallback and reuse the same key. A crash after commit leaves a resumable `SENDING_SMS` order.
**Audit**: `SMS_SENT` on acceptance with short code and masked phone, otherwise `EXTERNAL_ERROR`; command content MUST be masked in logs and audit.

#### Scenario: State is durable before the call

- GIVEN an order entering dispatch
- WHEN the process is interrupted right after commit
- THEN a persisted `SENDING_SMS` order with the reservation exists and no `COMPLETED` state does

#### Scenario: Unknown outcome is not retried

- GIVEN the gateway returns an unparseable 5xx
- WHEN dispatch ends
- THEN exactly one request was made and the order stays `SENDING_SMS` with `EXTERNAL_ERROR`

#### Scenario: Not-delivered failure falls back once

- GIVEN the primary short code is classified as not delivered
- WHEN dispatch retries
- THEN one attempt is made to the next short code with the same idempotency key

### Requirement: Bank-reply correlation and confirmation

- **ID**: R-BUY-007

An inbound reply MUST be deduplicated by `(provider="sms_gateway", external_event_id=message_id)`, parsed with the sender's bank profile, and correlated only against `SENDING_SMS` orders of that bank whose amount matches and whose dispatch falls inside `sms_confirmation_timeout_seconds`. Exactly one candidate is required. Zero candidates MUST change nothing; two or more MUST raise an operator alert; a sender outside the profile's short codes MUST NOT confirm anything; only a parsed success with exactly one candidate MAY confirm.

**Idempotency**: A replayed webhook MUST return HTTP 202 with `duplicate=True` and MUST NOT create a second transaction or state change.
**Audit**: `WEBHOOK_RECEIVED` per accepted delivery, `SMS_RECEIVED` on confirmation, `WEBHOOK_REJECTED` with outcome `ambiguous` or `no_match` otherwise.

#### Scenario: Single candidate confirms

- GIVEN one `SENDING_SMS` order matching the reply's bank, amount, and window
- WHEN the success reply is processed
- THEN the order is confirmed with outcome `confirmed`

#### Scenario: Ambiguous match confirms nothing

- GIVEN two `SENDING_SMS` orders matching bank and amount
- WHEN the success reply is processed
- THEN no order changes state and an operator alert is raised

#### Scenario: Foreign sender cannot confirm

- GIVEN a sender that is not a short code of the order's bank profile
- WHEN the reply is processed
- THEN no confirmation occurs and the outcome is `no_match`

#### Scenario: Replay is idempotent

- GIVEN an already processed `message_id`
- WHEN the webhook is delivered again
- THEN it returns HTTP 202 with `duplicate=True` and writes no transaction

### Requirement: Outgoing bank transaction and reference uniqueness

- **ID**: R-BUY-008

A confirmed reply MUST write exactly one `BankTransaction` with `direction=OUTGOING` linked to the order, carrying the `Decimal` amount, bank code, reference, and raw reply, and MUST store the reference on the order. A UNIQUE `bank_reference` collision MUST become a typed duplicate-reference error moving the order to `MANUAL_REVIEW`, never an unhandled server error.

**Idempotency**: At most one `OUTGOING` transaction per order; a repeated or colliding reference MUST NOT create a second one.
**Audit**: `PAYMENT_VERIFIED` with the masked reference; a collision emits `STATUS_CHANGED` with reason `duplicate_bank_reference`.

#### Scenario: Confirmation writes one outgoing transaction

- GIVEN a confirmed reply with reference `123456789`
- WHEN it is persisted
- THEN exactly one `OUTGOING` row exists for the order with that reference

#### Scenario: Duplicate reference goes to review

- GIVEN a reference already present on another order
- WHEN it is persisted
- THEN the order moves to `MANUAL_REVIEW` with reason `duplicate_bank_reference` and no server error is returned

### Requirement: Binance close and COMPLETED boundary

- **ID**: R-BUY-009

After confirmation the system MUST notify the seller with the reference through the browser port's `notify_paid` operation and MUST reach `COMPLETED` only after that notification succeeds. A failed or unparseable close MUST move the order to `MANUAL_REVIEW` with reason `binance_close_failed` and MUST NOT produce `COMPLETED`. No other path — including timeout or retry — MAY produce `COMPLETED`.

**Idempotency**: The close MUST run at most once per confirmed order; after an unknown outcome it MUST NOT be re-invoked automatically and only an operator action may re-attempt it.
**Audit**: `STATUS_CHANGED` to `COMPLETED` with the masked reference, or the failure reason otherwise.

#### Scenario: Success closes the order

- GIVEN a confirmed order and a successful seller notification
- WHEN the close completes
- THEN the order is `COMPLETED` with one `STATUS_CHANGED` entry holding the masked reference

#### Scenario: Failed close never completes

- GIVEN a confirmed order and a failing notification
- WHEN the close is attempted
- THEN the order is `MANUAL_REVIEW` with reason `binance_close_failed` and no `COMPLETED` entry exists

#### Scenario: Unknown close outcome is not retried

- GIVEN a close whose outcome cannot be determined
- WHEN processing ends
- THEN it is not re-invoked automatically and the order awaits operator action

### Requirement: Fail-closed timeout semantics

- **ID**: R-BUY-010

When no reply arrives within `sms_confirmation_timeout_seconds`, the system MUST NOT resend the command, MUST NOT release the reservation, and MUST NOT reach `COMPLETED`. It MUST retain the `SENDING_SMS` evidence and flag the order; any review transition MUST carry reason `sms_confirmation_timeout`.

**Idempotency**: Timeout checks are repeatable without changing money state; no check resends or unreserves.
**Audit**: One `STATUS_CHANGED` or `EXTERNAL_ERROR` flag per window; no duplicate flag.

#### Scenario: Silence is never success

- GIVEN a `SENDING_SMS` order with no reply past the window
- WHEN the sweep runs
- THEN it is flagged `sms_confirmation_timeout` while the reservation is retained and nothing is resent

### Requirement: Direction guard between flows

- **ID**: R-BUY-011

The sell-side inbound payment path MUST select only orders whose type is SELL. A BUY order MUST never be advanced, confirmed, or released by that path, and crypto release MUST never be invoked for a BUY order. When only BUY candidates match, the path MUST return a no-match acknowledgement.

**Idempotency**: The guard is a pure filter evaluated before any state change; re-delivery remains a no-op for BUY orders.
**Audit**: `WEBHOOK_REJECTED` with reason `direction_mismatch`; no `PAYMENT_VERIFIED` and no release audit for the BUY order.

#### Scenario: Equal amounts do not cross directions

- GIVEN one BUY and one SELL order in `PENDING` with the same amount
- WHEN the sell-side payment webhook arrives
- THEN only the SELL order advances and release is never invoked for the BUY order

#### Scenario: Only a BUY candidate yields no match

- GIVEN the only matching order is a BUY order
- WHEN the sell-side webhook arrives
- THEN a no-match acknowledgement is returned and state is unchanged

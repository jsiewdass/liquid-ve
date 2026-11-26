# Delta for buy-flow

## ADDED Requirements

### Requirement: Extended payment lifecycle state machine

- **ID**: R-BUY-012

Order lifecycle MUST use `PENDING`, `PAYMENT_DETECTED`, `VERIFYING_CACHICAMO`, `SENDING_PAYMENT`, `AWAITING_RELEASE`, `APPEALED`, `COMPLETED`, `FAILED`, and `MANUAL_REVIEW`, with these legal edges: `PENDING → {PAYMENT_DETECTED, FAILED, MANUAL_REVIEW}`; `PAYMENT_DETECTED → {VERIFYING_CACHICAMO, MANUAL_REVIEW, FAILED}`; `VERIFYING_CACHICAMO → {SENDING_PAYMENT, COMPLETED, MANUAL_REVIEW, FAILED}`; `SENDING_PAYMENT → {AWAITING_RELEASE, MANUAL_REVIEW, FAILED}`; `AWAITING_RELEASE → {COMPLETED, APPEALED, MANUAL_REVIEW, FAILED}`; `APPEALED → {COMPLETED, FAILED, MANUAL_REVIEW}`; `MANUAL_REVIEW → {PENDING, FAILED}`; `COMPLETED` and `FAILED` terminal. `SENDING_PAYMENT → COMPLETED` MUST NOT exist. `AWAITING_RELEASE` MUST require a non-null bank reference and confirmation timestamp. `APPEALED` MUST be entered only after a durable appeal attempt identity is committed. `MANUAL_REVIEW → PENDING` MUST be allowed only for pre-payment reasons (`awaiting_operator_approval`, `payment_data_unavailable`, or a risk block) and MUST NOT be allowed for `clave_dinamica_timeout`, `payment_outcome_unknown`, `receipt_amount_mismatch`, or a manual `release_timeout`.

**Idempotency**: Each transition is atomic and guarded; re-applying a transition that already occurred MUST be a no-op.
**Audit**: One `STATUS_CHANGED` per transition with the from/to status and the reason.

#### Scenario: Legal tail reaches release

- GIVEN an order in `SENDING_PAYMENT` with a captured receipt and evidence uploaded
- WHEN the lifecycle advances
- THEN it enters `AWAITING_RELEASE` and never `COMPLETED` directly

#### Scenario: Direct completion is rejected

- GIVEN an order in `SENDING_PAYMENT`
- WHEN a transition to `COMPLETED` is attempted
- THEN the transition is refused and the status is unchanged

#### Scenario: Payment-related review cannot resume

- GIVEN a `MANUAL_REVIEW` order parked with `payment_outcome_unknown`
- WHEN a resume to `PENDING` is attempted
- THEN the resume is refused and no payment is dispatched

### Requirement: Receipt persistence and evidence integrity

- **ID**: R-BUY-013

The system MUST persist, for every captured receipt, a relative path under restricted gitignored storage, the SHA-256 digest, the reference, the `Decimal` amount, and the paid-at timestamp. It MUST expose only the digest and a masked reference through API responses and audit payloads, MUST NOT store raw image bytes or raw receipt text in the database, and MUST enforce the configured retention. A differing digest for an order already holding a receipt MUST park `MANUAL_REVIEW` with `receipt_conflict`; an amount mismatch MUST park with `receipt_amount_mismatch`; neither MUST overwrite the stored evidence.

**Idempotency**: The digest is the dedupe anchor; an identical recapture is a no-op and at most one receipt record exists per order.
**Audit**: `PAYMENT_CAPTURED` with the digest and masked reference; conflicts and mismatches emit `STATUS_CHANGED` with the reason and no PII.

#### Scenario: Metadata is persisted without raw content

- GIVEN a captured receipt with a digest, reference, and matching amount
- WHEN it is persisted
- THEN the order carries the digest, masked-able reference, amount, and timestamp, and no raw bytes or text

#### Scenario: Conflicting digest never overwrites

- GIVEN an order that already has a receipt with digest A
- WHEN a receipt with digest B is captured
- THEN the order parks with `receipt_conflict` and digest A remains stored

#### Scenario: Amount mismatch parks

- GIVEN a receipt whose parsed amount differs from the reserved amount
- WHEN persistence runs
- THEN the order parks with `receipt_amount_mismatch` and is not treated as paid

### Requirement: Evidence upload before release

- **ID**: R-BUY-014

Before entering `AWAITING_RELEASE` the system MUST upload the receipt image to the Binance order chat and submit the payment notification exactly once, and MUST set the release deadline only after both succeed. An upload or notification failure MUST park `MANUAL_REVIEW` with `evidence_upload_failed` and MUST NOT enter `AWAITING_RELEASE` and MUST NOT file an appeal.

**Idempotency**: The upload and notification run at most once per receipt; an unknown upload outcome MUST NOT be retried automatically and only an operator action may re-attempt it.
**Audit**: `EVIDENCE_UPLOADED` on success with the digest and masked reference; `EXTERNAL_ERROR` with `evidence_upload_failed` otherwise.

#### Scenario: Success enters the release tail

- GIVEN a persisted receipt and a reachable order chat
- WHEN the upload and notification succeed
- THEN the order enters `AWAITING_RELEASE` with a release deadline

#### Scenario: Upload failure blocks release and appeal

- GIVEN an upload that fails or returns an unknown outcome
- WHEN the flow finalizes
- THEN the order parks with `evidence_upload_failed` and no appeal is filed

### Requirement: Release watcher, status mapping, and timeout policy

- **ID**: R-BUY-015

The system MUST poll Binance order detail for `AWAITING_RELEASE` orders at the configured interval and MUST map status conservatively: observed release → `COMPLETED`; already `APPEALING` → `APPEALED` without filing another appeal; `CANCELLED` or `EXPIRED` → `MANUAL_REVIEW` with `external_order_cancelled`; still paid or pending → continue waiting; any unknown status → `MANUAL_REVIEW` with `external_status_unknown`. When the release deadline passes, the system MUST apply `RELEASE_APPEAL_MODE`: `auto` → `APPEALED` and invoke R-BUY-016; `alert` → `MANUAL_REVIEW` with `release_timeout`; `off` → no appeal and no completion, leaving the order flagged for the operator.

**Idempotency**: Polling is read-only; each status change occurs once; repeated ticks produce no additional completion or appeal decision.
**Audit**: `RELEASE_DETECTED` on release, `APPEAL_SKIPPED` when release wins, and one `STATUS_CHANGED` or timeout flag per order.

#### Scenario: Release completes the order

- GIVEN an `AWAITING_RELEASE` order whose Binance status reports release
- WHEN the watcher polls
- THEN the order becomes `COMPLETED`

#### Scenario: Unknown status parks

- GIVEN an `AWAITING_RELEASE` order whose Binance status is unrecognized
- WHEN the watcher polls
- THEN the order parks with `external_status_unknown` and no completion occurs

#### Scenario: Timeout honors the configured mode

- GIVEN an `AWAITING_RELEASE` order past its deadline and `RELEASE_APPEAL_MODE=alert`
- WHEN the watcher evaluates it
- THEN the order parks with `release_timeout` and no appeal is filed

### Requirement: At-most-once automatic appeal

- **ID**: R-BUY-016

The system MUST file at most one appeal per order and MUST NOT appeal while `RELEASE_APPEAL_MODE=off`. Before any appeal click it MUST durably commit a UNIQUE appeal attempt identity and `APPEALED`, and MUST re-check the Binance status immediately before filing so that an observed release yields `COMPLETED` and an `APPEAL_SKIPPED` audit entry instead of an appeal. An unknown appeal outcome MUST NOT be retried automatically and MUST require operator verification. A kill switch MUST stop new appeals.

**Idempotency**: The appeal attempt identity is UNIQUE; duplicate or concurrent attempts are refused, and an order already showing `APPEALING` is never clicked again.
**Audit**: `APPEAL_FILED` with the attempt identity and masked reference; `APPEAL_SKIPPED` with the reason; every refusal is audited.

#### Scenario: Appeal fires once

- GIVEN an `AWAITING_RELEASE` order past its deadline with `RELEASE_APPEAL_MODE=auto`
- WHEN the appeal path runs
- THEN exactly one appeal is filed with a durable attempt identity and the status becomes `APPEALED`

#### Scenario: Release wins the race

- GIVEN an order whose release is observed during the pre-click re-check
- WHEN the appeal path re-checks status
- THEN it skips the appeal, records `APPEAL_SKIPPED`, and completes the order

#### Scenario: Off mode disables appeal

- GIVEN `RELEASE_APPEAL_MODE=off` and a past-deadline order
- WHEN the watcher evaluates it
- THEN no appeal is filed and the order is left flagged for the operator

#### Scenario: Unknown appeal outcome is not auto-retried

- GIVEN an appeal whose submit outcome is unknown
- WHEN processing ends
- THEN the order remains `APPEALED` for operator verification and no second click occurs

## MODIFIED Requirements

### Requirement: Idempotent processing and resume

- **ID**: R-BUY-002

Processing the same `order_number` twice MUST NOT produce a second payment attempt. An order in `SENDING_PAYMENT` or `AWAITING_RELEASE` MUST be resumed without invoking the payment rail again; an order in a terminal status, or in `MANUAL_REVIEW` without the explicit operator approval of R-BUY-005, MUST be refused with a typed already-processed error and no side effect. A `MANUAL_REVIEW` order MUST be resumable to `PENDING` only for a pre-payment reason; a payment-related park MUST NOT be resumed into a new payment.
(Previously: resumed `SENDING_SMS` and made no distinction between pre-payment and payment-related `MANUAL_REVIEW` parks.)

**Idempotency**: `resume(order_number)` is idempotent — repeating it while `SENDING_PAYMENT` or `AWAITING_RELEASE` MUST NOT invoke the rail, send a command, or reserve budget again.
**Audit**: Each resume emits one `STATUS_CHANGED`; a refused duplicate emits a rejection audit entry and no `PAYMENT_INITIATED`.

#### Scenario: Second processing invokes no rail

- GIVEN an order in `SENDING_PAYMENT`
- WHEN processing is invoked again
- THEN the order resumes and zero payment-rail invocations occur

#### Scenario: Completed order is refused

- GIVEN an order in `COMPLETED`
- WHEN processing is invoked again
- THEN a typed already-processed error is returned and state is unchanged

#### Scenario: Payment-related park refuses resume

- GIVEN a `MANUAL_REVIEW` order parked with `clave_dinamica_timeout`
- WHEN an operator approval resume is submitted
- THEN it is refused and no payment attempt is created

### Requirement: Payment data extraction

- **ID**: R-BUY-003

The system MUST obtain bank numeric code, phone, cedula digits, the document letter when the order page exposes it, and exact VES amount through the `BinanceBrowser` port and MUST persist bank code and counterparty identity. The document letter MUST NOT be defaulted or inferred from any other source; when it is absent or ambiguous the order MUST move to `MANUAL_REVIEW` with `id_type_missing` and MUST NOT dispatch. Any other failed or incomplete extraction MUST move the order to `MANUAL_REVIEW` with reason `payment_data_unavailable` and MUST NOT dispatch, and payment data MUST NOT be inferred from the counterparty phone or any fallback source.
(Previously: required only bank code, phone, cedula, and amount, with no document-letter handling.)

**Idempotency**: The persisted instruction is reused on resume instead of being re-extracted.
**Audit**: `STATUS_CHANGED` to `MANUAL_REVIEW` on incomplete data or a missing letter; `EXTERNAL_ERROR` on a port failure; all with PII masked.

#### Scenario: Complete instruction including letter is persisted

- GIVEN a BUY order exposing bank, phone, cedula with a letter, and amount
- WHEN extraction runs
- THEN a `Decimal` amount, resolvable bank code, and the document letter are persisted on the order

#### Scenario: Incomplete data blocks dispatch

- GIVEN an extraction result missing the cedula
- WHEN the order is processed
- THEN it moves to `MANUAL_REVIEW` with reason `payment_data_unavailable` and no payment-rail invocation occurs

#### Scenario: Missing letter never defaults

- GIVEN an extraction result with document digits but no letter
- WHEN the order is processed
- THEN it moves to `MANUAL_REVIEW` with `id_type_missing` and does not submit a payment

### Requirement: Pre-dispatch risk circuit breakers

- **ID**: R-BUY-004

After extraction the system MUST evaluate every breaker below and MUST block on the first failure:

| Breaker | Rule | Blocked reason |
|---|---|---|
| Per-order cap | `amount_ves` ≤ `MAX_BUY_AMOUNT_VES` and `amount_asset` ≤ `MAX_USDT_PER_ORDER` | `amount_over_cap` |
| Daily outgoing cap | UTC-day reservation on the `VES` and `VES_PAY` ledger channels plus `amount_ves` ≤ `DAILY_PAY_LIMIT_VES` | `daily_pay_cap_exceeded` |
| Implied-rate tolerance | `abs(amount_ves / amount_asset − order.price) / order.price` ≤ `RATE_TOLERANCE_PCT` | `rate_out_of_tolerance` |
| Supported bank | a profile resolves for the extracted numeric code | `unsupported_destination_bank` |

A dispatched BUY MUST reserve both `VES` and `VES_PAY`; the double cap semantics MUST be preserved and `VES_PAY` MUST replace the former SMS channel.

**Idempotency**: Assessment is read-only; reservation happens only on the allow path, keyed by `(ledger_date, "VES")` and `(ledger_date, "VES_PAY")` atomically so concurrent dispatches cannot both pass. Reservation is released only on a confirmed permanent pre-submission failure and MUST NOT be released on timeout.
**Audit**: One `RISK_BLOCKED` per block with reason and limits snapshot; each release emits `STATUS_CHANGED` naming the amount and channel.

#### Scenario: Over-cap amount is blocked

- GIVEN an order above `MAX_BUY_AMOUNT_VES`
- WHEN risk evaluation runs
- THEN it moves to `MANUAL_REVIEW` with reason `amount_over_cap` and no rail invocation occurs

#### Scenario: Mangled scrape fails the rate check

- GIVEN an extracted amount implying a rate outside `RATE_TOLERANCE_PCT` of the order price
- WHEN risk evaluation runs
- THEN it moves to `MANUAL_REVIEW` with reason `rate_out_of_tolerance`

#### Scenario: Daily cap respects the UTC rollover

- GIVEN the current UTC day's `VES_PAY` reservation is at its cap
- WHEN the ledger date advances one UTC day
- THEN a new reservation is permitted up to the limit

#### Scenario: Both channels are reserved

- GIVEN an allow-path order that dispatches
- WHEN the reservation commits
- THEN both `VES` and `VES_PAY` carry the amount for the same ledger date

#### Scenario: Unsupported bank never dispatches

- GIVEN an order whose bank has no profile
- WHEN risk evaluation runs
- THEN it moves to `MANUAL_REVIEW` with reason `unsupported_destination_bank`

### Requirement: Durable payment-rail invocation

- **ID**: R-BUY-006

The system MUST commit `SENDING_PAYMENT`, the reserved amount, and a durable payment attempt identity before invoking the payment rail, MUST invoke the rail at most once per attempt, and MUST pass the order's idempotency key as `"{order_number}:payment"`. There MUST be no short-code fallback and no second rail invocation. On an unknown outcome (timeout, missing receipt, or ambiguous page state) it MUST NOT retry, MUST keep the order out of `COMPLETED`, and MUST park `MANUAL_REVIEW` with `payment_outcome_unknown`. A classified permanent pre-submission failure MUST release the reservation and move to `MANUAL_REVIEW`.
(Previously: dispatched a rendered SMS command to a bank short code with a one-time fallback to the next short code.)

**Idempotency**: The rail call is non-idempotent; retries do not exist. The attempt identity is UNIQUE, and a crash after commit leaves a `SENDING_PAYMENT` order that cannot auto-reinvoke the rail.
**Audit**: `PAYMENT_INITIATED` on invocation with the attempt identity and masked phone, `PAYMENT_CAPTURED` on receipt capture, otherwise `EXTERNAL_ERROR`; target and command content MUST be masked.

#### Scenario: State is durable before the call

- GIVEN an order entering payment
- WHEN the process is interrupted right after commit
- THEN a persisted `SENDING_PAYMENT` order with the reservation and attempt identity exists and no `COMPLETED` state does

#### Scenario: Unknown outcome is not retried

- GIVEN the rail returns an unknown outcome after submission
- WHEN dispatch ends
- THEN exactly one rail invocation occurred and the order parks with `payment_outcome_unknown`

#### Scenario: Permanent failure releases the reservation

- GIVEN a classified permanent pre-submission failure
- WHEN dispatch ends
- THEN the reservation is released and the order moves to `MANUAL_REVIEW`

### Requirement: Outgoing bank transaction and reference uniqueness

- **ID**: R-BUY-008

A captured receipt MUST write exactly one `BankTransaction` with `direction=OUTGOING` linked to the order, carrying the `Decimal` amount, bank code, receipt reference, and receipt digest, and MUST store the reference on the order and set the bank confirmation timestamp. A UNIQUE `bank_reference` collision MUST become a typed duplicate-reference error moving the order to `MANUAL_REVIEW`, never an unhandled server error.
(Previously: the transaction and reference were produced by an inbound bank-SMS confirmation.)

**Idempotency**: At most one `OUTGOING` transaction per order; a repeated capture or a colliding reference MUST NOT create a second one.
**Audit**: `PAYMENT_VERIFIED` with the masked reference and digest; a collision emits `STATUS_CHANGED` with reason `duplicate_bank_reference`.

#### Scenario: Receipt writes one outgoing transaction

- GIVEN a captured receipt with reference `123456789`
- WHEN it is persisted
- THEN exactly one `OUTGOING` row exists for the order with that reference

#### Scenario: Duplicate reference goes to review

- GIVEN a reference already present on another order
- WHEN it is persisted
- THEN the order moves to `MANUAL_REVIEW` with reason `duplicate_bank_reference` and no server error is returned

### Requirement: Binance evidence boundary and COMPLETED

- **ID**: R-BUY-009

After a captured receipt the system MUST upload the receipt and notify the seller through the browser port and MUST NOT reach `COMPLETED` at that point; it MUST enter `AWAITING_RELEASE`. `COMPLETED` MUST be reachable only from `AWAITING_RELEASE` or `APPEALED` after an observed counterparty release, and a direct `SENDING_PAYMENT → COMPLETED` transition MUST never occur. A failed or unparseable upload or notification MUST move the order to `MANUAL_REVIEW` with `evidence_upload_failed` or `binance_close_failed` and MUST NOT produce `COMPLETED`. No timeout or retry MAY produce `COMPLETED`.
(Previously: notification succeeded and immediately transitioned `SENDING_SMS → COMPLETED`.)

**Idempotency**: The upload and notification run at most once per receipt; after an unknown outcome they MUST NOT be re-invoked automatically and only an operator action may re-attempt. Completion occurs exactly once when release is observed.
**Audit**: `EVIDENCE_UPLOADED` on success, `RELEASE_DETECTED` and `STATUS_CHANGED` to `COMPLETED` with the masked reference, or the failure reason otherwise.

#### Scenario: Evidence upload enters awaiting release

- GIVEN a captured receipt and a successful upload and notification
- WHEN the flow finalizes
- THEN the order is `AWAITING_RELEASE` and not `COMPLETED`

#### Scenario: Failed evidence never completes

- GIVEN a captured receipt and a failing upload
- WHEN the flow finalizes
- THEN the order is `MANUAL_REVIEW` with `evidence_upload_failed` and no `COMPLETED` entry exists

#### Scenario: Release completes exactly once

- GIVEN an `AWAITING_RELEASE` order whose release is observed
- WHEN the transition runs
- THEN the order is `COMPLETED` with one `STATUS_CHANGED` entry holding the masked reference

#### Scenario: Unknown upload outcome is not retried

- GIVEN an upload whose outcome cannot be determined
- WHEN processing ends
- THEN it is not re-invoked automatically and the order awaits operator action

### Requirement: Fail-closed timeout semantics

- **ID**: R-BUY-010

When the Clave Dinámica is not received within its configured window, or the receipt is not captured within the payment confirmation window, the system MUST NOT resend, MUST NOT release the reservation, and MUST NOT reach `COMPLETED`. It MUST retain the `SENDING_PAYMENT` evidence and flag the order with `clave_dinamica_timeout` or `payment_confirmation_timeout`. A restart while a payment is in flight MUST expire pending challenges and park the order in `MANUAL_REVIEW` with `payment_interrupted`, and MUST NOT auto-resume the payment.
(Previously: covered an SMS confirmation window and retained `SENDING_SMS` evidence.)

**Idempotency**: Timeout checks and sweeps are repeatable without changing money state; no check resends, un-reserves, or resumes a payment.
**Audit**: One `STATUS_CHANGED` or `EXTERNAL_ERROR` flag per window; no duplicate flag.

#### Scenario: Silence is never success

- GIVEN a `SENDING_PAYMENT` order whose key or receipt never arrives
- WHEN the sweep runs
- THEN it is flagged with the matching timeout reason while the reservation is retained and nothing is resent

#### Scenario: Restart parks an in-flight payment

- GIVEN a process restart while an order is in `SENDING_PAYMENT`
- WHEN startup sweep runs
- THEN pending challenges expire and the order parks with `payment_interrupted`

## REMOVED Requirements

### Requirement: Bank-reply correlation and confirmation

- **ID**: R-BUY-007

(Reason: BUY no longer receives an inbound bank SMS; confirmation now comes from receipt capture — R-BANESCO-013 and R-BUY-008 — and the SMS rail is decommissioned.)
(Migration: webhook dedupe, candidate matching, and reply parsing are removed with the SMS BUY path; the `(provider, external_event_id)` uniqueness anchor remains for other webhooks.)

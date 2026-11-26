# Exploration: Flow B — Buy USDT via Pago Móvil SMS (`buy-usdt-sms-payment-flow`)

- **Phase**: explore (read-only)
- **Date**: 2026-11-23
- **Artifact store**: openspec + Engram (`sdd/buy-usdt-sms-payment-flow/explore`)
- **Baseline**: `33 passed` with `.venv\Scripts\python.exe -m pytest -q` (note: the global interpreter has no pytest; the suite must run from the repo virtualenv)
- **Product source**: `PROJECT_SPEC.md:27-35` (Flow B)
- **Scope of this document**: gap analysis only. No application code, tests or config were written.

---

## 1. What already exists for Flow B

### 1.1 Order states and transitions

`app/domain/enums.py:20-73`

| Element | Location | Status vs. Flow B |
|---|---|---|
| `OrderStatus.{PENDING, PAYMENT_DETECTED, VERIFYING_CACHICAMO, SENDING_SMS, COMPLETED, FAILED, MANUAL_REVIEW}` | `enums.py:20-29` | Present. `SENDING_SMS` exists exactly as the spec names it. |
| `ALLOWED_TRANSITIONS`, `can_transition_to`, `is_terminal` | `enums.py:31-73` | Present. `PENDING → PAYMENT_DETECTED → VERIFYING_CACHICAMO → SENDING_SMS` is legal; `SENDING_SMS → COMPLETED` is legal; `SENDING_SMS → MANUAL_REVIEW/FAILED` is legal. |
| `PENDING_PAYMENT` | — | **Absent.** Flow B step 1 names the Binance-side status; there is no local member. |
| `VERIFYING_CACHICAMO` | `enums.py:25` | Present but semantically wrong for a buy: buying USDT has nothing to verify at Cachicamo. `BuyFlow` walks through it anyway (`buy_flow.py:335-342`) purely to satisfy the shared transition table. |
| `BankCode` (18 members + `OTHER`) | `enums.py:76-96` | Present but name-only: **no 4-digit numeric code, no SMS short code**. |
| `BankCode.from_aliases` | `enums.py:103-126` | Resolves free-form *names* only (`"banesco"`, `"BdV"`); cannot resolve `"0102"`. |
| `BankCode.display_name` | `enums.py:98-101`, `129-148` | Present. Contains mojibake in `_BANK_ALIAS_ENTRIES` (`enums.py:179,186`) — **known, out of scope**. |
| `BankTransactionDirection.OUTGOING` | `enums.py:196-200` | Present and unused — the correct enum value for an outgoing buy payment. |
| `PaymentVerificationStatus` | `enums.py:203-210` | Present (`VERIFIED/NOT_FOUND/AMOUNT_MISMATCH/PENDING/REJECTED`). |
| `AuditEvent.{SMS_SENT, SMS_RECEIVED, PAYMENT_VERIFIED, WEBHOOK_RECEIVED, WEBHOOK_REJECTED, RISK_BLOCKED, EXTERNAL_ERROR}` | `enums.py:230-242` | Every audit event Flow B needs already exists. |
| `OrderType` docstring | `enums.py:9-17` | **Inverted vs. reality**: it says `BUY` = "we receive VES and release crypto", but `BuyFlow` *pays* VES. Neither flow validates `detail.trade_type`, so a BUY order can enter `SellFlow` and vice versa (`buy_flow.py:379`, `sell_flow.py:361`). |

### 1.2 Buy orchestration

`app/services/buy_flow.py` (540 lines)

| Capability | Location | Reality |
|---|---|---|
| Client injection (settings/binance/cachicamo/sms) | `:51-73`, `_clients` `:75-94` | Good seam for fakes; already supports injection. |
| Order idempotency by `order_number` | `:116-132`, `:374` | Present, but **rejects** instead of resuming (see §2.6). |
| Fetch order detail from Binance C2C API | `:375` | Present (`BinanceC2CClient.get_order_detail`). |
| Persist order + `ORDER_CREATED` audit | `:376-403` | Present. |
| Risk gate | `:404-417` (via `_assess_risk` `:275-314`) | Partial (see §5). |
| USDT cap | `:418-428` | Present. |
| Phone fallback / manual review | `:429-440` | Present. |
| Three-step transition into `SENDING_SMS` | `_advance_to_sending` `:316-350` | Present but with copy that misdescribes a buy ("payout intent detected", ":333"). |
| Commit before money movement | `:441-444` | Present — `SENDING_SMS` is durable before `send_sms`. |
| Build + send SMS with `Idempotency-Key = "{order_number}:payout"` | `:445-457` | Present, but the message body is not a bank command (see §3). |
| Provider failure → audit `EXTERNAL_ERROR`, stay in `SENDING_SMS` | `:458-467` | Present and correct in intent. |
| Mark `COMPLETED` | `:469-477` | **Wrong boundary**: transitions to `COMPLETED` immediately after the gateway *accepts* the SMS. Flow B requires waiting for the bank reply and closing on Binance first. |
| Inbound SMS handler | `handle_incoming_sms` `:481-522` | Best-effort substring match only (see §4). |
| PII masking helper | `_mask_sender` `:526-537` | Present. |
| Direct tests | — | **Zero.** |

### 1.3 SMS gateway adapter

`app/infrastructure/clients/sms_gateway.py`

| Capability | Location | Reality |
|---|---|---|
| `POST {base}/messages` with bearer + `Idempotency-Key`, `idempotent=False` | `:96-141` | Present and correct for a non-retryable money side effect. |
| `SmsSendResult(message_id, status, sent_at)` | `:43-49` | Present. |
| `GET /messages` inbox polling | `:143-200` | Present. |
| `IncomingSms(message_id, sender, body, received_at)` | `:52-59` | Present. |
| `health_check` | `:202-204` | Present. |
| `build_payment_sms(...)` | `:206-235` | **Wrong artifact**: emits `"liquidVE | Pago movil <Bank name>: envia Bs 1,500.00 al +58412... Referencia: <order>. Concepto: ..."`. It is a human instruction, not the bank command `Pagar 0102 04141234567 12345678 1500,00`. It also formats money US-style (`1,500.00`) instead of VE-style (`1500,00`), and has no cedula, no numeric bank code, no short code. |

### 1.4 Binance adapter

`app/infrastructure/clients/binance_client.py`

| Capability | Location | Reality |
|---|---|---|
| HMAC-signed request builder | `:153-168`, `_sign_query` `:44-56` | Present, pure, unit-testable. |
| `get_order_history` | `:184-221` | Present; no `tradeType`/`orderStatus` filter arguments. |
| `get_order_detail` | `:223-244` | Present. |
| `mark_order_as_paid` | `:246-259` | Thin POST wrapper. Endpoint unverified (`TODO(integration)` `:36-41`), no response validation, never called anywhere, **no test** → effectively a stub. |
| `release_order` | `:261-281` | Same class of stub; module docstring states only merchant accounts can use it (`:8-13`). |
| `BinanceC2COrder` DTO | `:82-129` | Has `order_number/trade_type/asset/fiat/price/amount/total_price/status/nickname/created_at`. **No bank code, phone, cedula or payer name** — by design, since the C2C API does not expose counterparty bank data. `status` is an unvalidated raw string (`:120`). |

### 1.5 Persistence

`app/infrastructure/models.py`, migration `alembic/versions/20260101_0001_initial_schema.py`

| Column / constraint | Location | Relevant to Flow B |
|---|---|---|
| `p2p_orders.order_number` UNIQUE | `models.py:105` / migration `:86` | Idempotency anchor. Present. |
| `p2p_orders.status` (`Enum(native_enum=False)`, `VARCHAR(32)`) | `models.py:111-115` | Adding an enum member later needs **no column migration** (no CHECK constraint is emitted). |
| `p2p_orders.bank_code` nullable | `models.py:123-126` | Present, but never populated by `BuyFlow`. |
| `p2p_orders.bank_reference` UNIQUE nullable | `models.py:127` | Present — the natural place to store the reference extracted from the bank reply. |
| `p2p_orders.counterparty_phone` | `models.py:122` | Present. `counterparty_id_number` (cedula) is **absent**. |
| `p2p_orders.raw_payload` JSON/JSONB | `models.py:132`, `JSONType` `:46` | Escape hatch: can carry the Playwright extraction verbatim without a migration. |
| Optimistic locking (`version` + `version_id_col`) | `models.py:133-138`, `:151` | Present — useful for concurrent webhook + worker updates. |
| `bank_transactions` (`order_id NOT NULL`, `bank_reference` UNIQUE index, `direction`, `bank_code`, `amount`, `payer_phone`, `payer_name`, `verified`, `verification_status`, `raw_payload`) | `models.py:160-226` | Structurally **sufficient** for an OUTGOING buy payment row. Nothing writes OUTGOING rows today. |
| `webhook_events` UNIQUE `(provider, external_event_id)` + `payload` | `models.py:275-309` | Replay protection + raw inbound SMS storage. Present. |
| `daily_volume_ledgers` UNIQUE `(ledger_date, currency)`, `total_volume`, `transaction_count` | `models.py:312-350` | One row per day/currency. Usable for an SMS cap with a namespaced `currency` value (see §5). |
| Missing telemetry columns | — | `sms_message_id`, `sms_sent_at`, `bank_confirmed_at`, `counterparty_id_number`, `bank_short_code`: all absent. |

### 1.6 Webhooks

`app/api/v1/webhooks.py`, mounted at `/api/v1` (`app/main.py:217-218`)

| Capability | Location | Reality |
|---|---|---|
| `POST /api/v1/webhooks/sms` | `:155-220` | **Exists.** Bearer auth via `require_bearer_token` (`:160`), body = `SmsInboundWebhookPayload`, dedupe on `(provider="sms_gateway", external_event_id=message_id)`, then `BuyFlow.handle_incoming_sms`, always HTTP 202. |
| `POST /api/v1/webhooks/cachicamo` | `:98-152` | HMAC + timestamp verification, dedupe, dispatches to `SellFlow`. |
| `SmsInboundWebhookPayload` | `app/domain/schemas.py:149-155` | `message_id, sender, body, received_at`. |
| Webhook accepted envelope | `schemas.py:186-192` | `accepted/event_id/duplicate/message`. |
| Client resolution from `app.state` | `:62-76` | Returns `None` outside a real lifespan — important for tests (§7). |

### 1.7 Configuration inventory (`app/core/config.py`)

| Setting | Location | Needed by Flow B? |
|---|---|---|
| `max_tx_amount_ves` | `:89` | Partially (direction-agnostic) |
| `min_tx_amount_ves` | `:90` | No |
| `daily_limit_ves` | `:91` | Partially (shared, direction-agnostic) |
| `max_usdt_per_order` | `:92` | Yes (already enforced) |
| `manual_review_on_limit` | `:93` | Yes |
| `sms_gateway_base_url` | `:74` | Yes |
| `sms_gateway_token` | `:75` | Yes |
| `sms_gateway_timeout_seconds` | `:76` | Yes |
| `sms_gateway_poll_interval_seconds` | `:77` | Yes |
| `sms_poller_enabled` | `:78` | Yes |
| `sms_gateway_shared_secret` | `:79` | Declared but **referenced nowhere** (`grep`: only `config.py:79`). |
| `binance_api_key/secret/base_url/recv_window/asset/fiat` | `:57-62` | Yes |
| `http_*` policy | `:82-86` | Yes |

**Settings the spec requires but that do not exist** (validated names to add):

- `MAX_BUY_AMOUNT_VES` → `max_buy_amount_ves`
- `DAILY_SMS_LIMIT_VES` → `daily_sms_limit_ves`
- rate margin → no name exists; needs e.g. `max_implied_rate_ves_per_usdt` (+ optional `min_implied_rate_ves_per_usdt`)
- SMS confirmation window → `sms_confirmation_timeout_seconds`
- Playwright adapter → `binance_browser_headless`, `binance_browser_timeout_ms`, `binance_session_state_path`, `binance_browser_base_url`
- buy-order detection worker → `buy_worker_enabled`, `buy_worker_poll_interval_seconds`
- optional manual gate → `buy_auto_pay_enabled` (depends on open question Q5)

Note `Settings._validate_money_amounts` (`:136-165`) and `_validate_consistency` (`:167-193`) must be extended for any new decimal setting, and `.env.example:73-85` plus `conftest.py:29-31,68-71` must be updated in lockstep or `Settings()` will fail to construct.

### 1.8 Test infrastructure

`tests/conftest.py`

| Fixture | Location | Notes |
|---|---|---|
| env defaults before imports | `:12-31` | Required because `Settings` is cached at import. |
| `test_settings` | `:49-77` | File-backed SQLite in `tmp_path`. |
| `engine` | `:80-97` | **In-memory SQLite + `StaticPool` + `create_all`** — the documented integration pattern. |
| `session` | `:100-109` | Bound to `engine`, `expire_on_commit=False`, `autoflush=False`. |
| `client` | `:112-130` | ASGI client; calls `init_engine` + `create_all` + `create_app`; **does not override `get_session`** and **does not run lifespan** (so `app.state.{binance,cachicamo,sms}` are unset). |
| `pytest_configure` | `:133-141` | Registers `integration` marker. |

`respx==0.22.0`, `freezegun==1.5.1`, `pytest-cov` are installed (`requirements.txt:35-37`) but **no current test uses respx or freezegun**.

Existing coverage: `tests/test_enums.py` (state machine + bank aliases, includes mojibake fixtures), `tests/test_health.py`, `tests/test_idempotency.py` (DB-level UNIQUE anchors), `tests/test_security.py` (HMAC/masking). Nothing touches `BuyFlow`, `SmsGatewayClient`, `BinanceC2CClient` or `webhooks.py`.

---

## 2. What is missing or stubbed

Ordered by money risk.

1. **No Playwright layer at all.** `requirements.txt:23` declares `playwright==1.49.1`; `Dockerfile:44-48` documents (commented) a Chromium stage; `binance_client.py:8-13,267-271` and `README.md:69` say it is the "next increment"; `.gitignore:65-70` ignores artifacts. **No `binance_browser.py` exists** and there is no port/protocol for it.
2. **No BUY order detection at all.** `main.py:96-98` starts only `_run_sms_poller` (`:36-69`), which polls the SMS inbox. There is no worker that scans Binance for open buy orders and no API route to trigger `BuyFlow.execute` (`main.py:217-218` registers only `health` and `webhooks`). **`BuyFlow.execute` is unreachable in production.**
3. **Buy closes too early.** `buy_flow.py:469-477` sets `COMPLETED` right after the gateway returns `message_id`. There is no "wait for bank reply" state, no reference extraction, no Binance `mark_order_as_paid`, no chat message. Flow B steps 4-5 are entirely absent.
4. **SMS body is not a bank command.** `sms_gateway.py:206-235` (§3).
5. **No counterparty payment data.** No way to obtain `bank numeric code / phone / cedula / exact amount` from Binance: the API DTO lacks them (`binance_client.py:82-129`) and nothing extracts them from the UI. `p2p_orders.bank_code` is consequently always `None`, which makes `buy_flow.py:448` fall back to `BankCode.OTHER` and lose the bank entirely.
6. **SMS retry path contradicts its own docstring.** `buy_flow.py:6-11` promises an idempotent retry with the same key, but `:374` raises `DuplicateOrderError` for any already-persisted `order_number`. `p2p_orders.idempotency_key` (`models.py:128`) is never populated. Consequence: a transient gateway failure parks the order in `SENDING_SMS` with no supported way to resume it.
7. **Inbound confirmation cannot confirm anything.** `handle_incoming_sms` (`buy_flow.py:481-522`) only (a) substring-matches `order_number` or `f"{amount:,.2f}"` against the last 100 orders in `MANUAL_REVIEW`/`SENDING_SMS`, newest first, and (b) moves `MANUAL_REVIEW → PENDING` or appends an audit row. It never extracts a reference, never writes a `BankTransaction`, never transitions `SENDING_SMS → COMPLETED`, never calls Binance.
8. **No bank-reply parser.** No regex/registry for `"Pago Móvil exitoso ... Referencia: 123456789"` nor for failure texts; `sender` (the bank short code) is unused except for masking (`:510`).
9. **SMS webhook is bearer-only.** `webhooks.py:160` compares against `internal_api_key`; `sms_gateway_shared_secret` is dead config. Acceptable (`internal_api_key` is a real shared secret + `message_id` dedupe), but the unused setting invites a false sense of HMAC protection.
10. **`PENDING_PAYMENT` unnamed/absent** (§1.1) and `VERIFYING_CACHICAMO` abused as a buy stepping stone (`buy_flow.py:343-350`).
11. **Duplicated risk logic** — `buy_flow.py:275-314` and `sell_flow.py:295-337` are functionally identical (only docstring wording differs). Adding buy-specific caps to one will silently diverge from the other.
12. **`_load_or_create_ledger` hardcodes `"VES"`** in four places per copy (`buy_flow.py:231-273`, `sell_flow.py:240-293`), so it cannot track a second counter without refactoring.
13. **No pagination/filter params on `get_order_history`** (`binance_client.py:184-208`) — a detection worker needs `tradeType=BUY` and status filtering server-side, or it will burn rate limit scanning every order.
14. **`OrderType` is never enforced against the flow** (`buy_flow.py:379`, `sell_flow.py:361`).
15. **No telemetry columns** for SMS/confirmation (§1.5), so reconciliation must dig through `audit_logs.payload`.
16. **`main.py` has no SMS-confirmation timeout sweep.** An order that never receives a reply stays in `SENDING_SMS` forever.

---

## 3. Bank formatting: current state and minimal data-driven design

### 3.1 Current state

- **No bank-code → template mapping exists anywhere.** `grep` for `0102`, `0134`, `2661`, `2662`, `2846` across `app/`, `tests/`, `.env.example` returns nothing.
- `build_payment_sms` (`sms_gateway.py:206-235`) is the only message builder; it is a static method with a single Spanish template, no bank branch, no short code, no cedula.
- `BankCode` values are *names* (`"BANESCO"`), never numeric codes.
- `SmsGatewayClient.send_sms(phone_number=...)` (`:96-102`) is currently passed the **counterparty phone** (`buy_flow.py:453-454`). For a bank command the destination must be the **bank short code** (e.g. `2661`), which is a completely different routing decision.
- Amount formatting for VE is absent: `f"{amount_ves:,.2f}"` yields `1,500.00`; the spec requires `1500,00`.
- Phone formatting for the command is absent: `validate_venezuelan_phone` returns E.164 `+584141234567` (`schemas.py:18-48`); the command needs national `04141234567`.
- Cedula validation exists (`validate_venezuelan_id`, `schemas.py:51-71`, returns `V-12345678`) but is never used, and the command needs the digits only.

### 3.2 Minimal data-driven design (recommendation)

Add a **pure domain module** `app/domain/banks.py` (no I/O, no settings, no framework imports):

```python
@dataclass(frozen=True, slots=True)
class BankSmsProfile:
    bank: BankCode
    numeric_code: str                     # "0102"
    short_codes: tuple[str, ...]          # ("2661", "2662") — primary first, then fallbacks
    command_template: str                 # "Pagar {numeric_code} {phone_national} {cedula_digits} {amount_ve}"
    success_pattern: str                  # r"Referencia[: ]+(?P<reference>\d{6,12})"
    failure_patterns: tuple[str, ...]     # ("fondos insuficientes", "no procesado", ...)
```

Plus three pure functions:

- `resolve_bank(raw: str) -> BankSmsProfile | None` accepting numeric code, enum name, or free-form alias (delegating to `BankCode.from_aliases`).
- `format_ves_amount(value: Decimal) -> str` → integer part + 2 decimals, **no thousands grouping**, comma decimal: `Decimal("1500")` → `"1500,00"`. Deterministic and matches the spec example exactly.
- `render_bank_command(profile, *, phone, cedula, amount) -> str` with `phone` → national `0414...` digits and `cedula` → digits only.

Registry: `BANK_SMS_PROFILES: Mapping[str, BankSmsProfile]` keyed by numeric code, and `SUPPORTED_BANKS: frozenset[BankCode]`.

**First banks to support (as the spec demands):**

| Bank | Numeric code | Short codes | Priority |
|---|---|---|---|
| Banco de Venezuela | `0102` | `2661`, `2662` | First |
| Banesco | `0134` | `2846` | Second |
| anything else | — | — | **Refuse**: park in `MANUAL_REVIEW` with reason `unsupported_destination_bank` |

Rationale for refusing unknown banks: a malformed command to a bank short code is an *unrecoverable* money action (it can debit the wrong account or be silently dropped). Failing closed is cheaper than guessing.

Short-code dispatch: `SmsGatewayClient.send_sms(phone_number=profile.short_codes[0], ...)`; on a typed retryable failure move to `short_codes[1]`, then `MANUAL_REVIEW`. This belongs in the dispatch service, not in the adapter.

Where to put the numeric codes: **not** in `enums.py` (it holds the known-mojibake alias table and must not be touched here). A new `banks.py` keeps `BankCode` as a stable identity and layers routing data on top.

---

## 4. The inbound confirmation loop

### 4.1 How a bank reply actually arrives

```
Bank ──SMS──▶ Android phone (SIM) ──gateway inbox──▶ two possible paths:
  A) gateway HTTP POST  →  POST /api/v1/webhooks/sms   (push, bearer-authenticated)
  B) backend polls      →  SmsGatewayClient.fetch_inbox() every 15s when sms_poller_enabled
```

Both paths converge on `BuyFlow.handle_incoming_sms` (`webhooks.py:205-213`; poller at `main.py:53-67`). Path A is the spec's "webhook del SMS entrante" (`PROJECT_SPEC.md:34`). Path B already exists and is the only thing wired by default (`sms_poller_enabled` defaults to `False`).

### 4.2 What `POST /webhooks/sms` does today

1. `require_bearer_token` (`webhooks.py:160`, `security.py:require_bearer_token`) — compares to `internal_api_key`.
2. Parses `SmsInboundWebhookPayload` (`schemas.py:149-155`).
3. Inserts a `WebhookEvent(provider="sms_gateway", external_event_id=message_id)`; duplicate → 202 `duplicate=True` (`webhooks.py:176-193`).
4. Builds a `BuyFlow` with clients from `app.state` and calls `handle_incoming_sms`.
5. Marks the event processed and returns 202 with `"SMS stored; no matching order"` or `"SMS matched an order"`.

No status code, body or audit detail reports *what* matched, so an operator cannot distinguish "confirmation applied" from "stored and ignored".

### 4.3 Is correlation possible with the current schema?

**Yes, mechanically** — no schema change is strictly required:

| Need | Existing carrier |
|---|---|
| Dedupe the reply | `webhook_events (provider, external_event_id)` (`models.py:283-288`) |
| Keep the raw bank text/parser provenance | `webhook_events.payload`, `bank_transactions.raw_payload` (`models.py:303,206`) |
| Record the outgoing payment | `bank_transactions` with `direction=OUTGOING` (`enums.py:196-200`), `order_id`, `amount`, `bank_code`, `raw_payload` |
| Store the extracted reference | `bank_transactions.bank_reference` (UNIQUE index, `models.py:169`) and/or `p2p_orders.bank_reference` (UNIQUE, `models.py:127`) |
| Bound the search to in-flight orders | `p2p_orders.status == SENDING_SMS` + `updated_at` |
| Audit the confirmation | `AuditEvent.SMS_RECEIVED` + `PaymentVerificationStatus.VERIFIED` |

**What makes correlation hard today (must be fixed in the slices):**

1. **Wrong key.** Bank replies carry neither `order_number` nor a US-formatted amount, so both current match conditions fail (`buy_flow.py:503-504`).
2. **No in-flight identity.** Nothing links the outbound `SmsSendResult.message_id` or send timestamp to the order in a queryable column; it lives only inside an `audit_logs.payload` JSON blob (`buy_flow.py:468`).
3. **Unsafe ordering.** Candidates are ordered `created_at DESC` (`:498`) and the *first* hit wins. For a money confirmation the matcher must be: filter to `SENDING_SMS` + same bank + amount within tolerance + within `sms_confirmation_timeout_seconds`, and require **exactly one** candidate; if 0 or ≥2, do nothing and raise an operator alert.
4. **Partial-amount failures.** Bank success SMS may report a different amount (commission/fee) or a truncated format. Amount tolerance policy is a design decision (recommend: exact match on the requested VES amount after VE-format parsing, else `AMOUNT_MISMATCH`).
5. **Global uniqueness of `bank_reference`.** Two banks can theoretically emit the same reference string; the UNIQUE index is cross-bank. Low probability, but it means a collision surfaces as an `IntegrityError` mid-flow — must be converted to `DuplicateBankReferenceError` → `MANUAL_REVIEW`, never to a 500.
6. **Sender direction.** `sender` must be validated against the destination short codes for that bank, otherwise a spoofed/irrelevant SMS could complete an order. Store `sender_masked` only.

### 4.4 Target confirmation sequence (for the spec phase)

```
SENDING_SMS order ──bank reply──▶ POST /webhooks/sms
  ├─ dedupe on message_id (202 on replay)
  ├─ resolve bank by sender short code → BankSmsProfile
  ├─ parse: reference | failure_reason
  ├─ select exactly one SENDING_SMS order (bank + amount + window)
  ├─ success → add BankTransaction(dir=OUTGOING, ref, verification_status=PENDING)
  │             + set p2p_orders.bank_reference (UNIQUE → DuplicateBankReferenceError on replay)
  │             + audit SMS_RECEIVED / PAYMENT_VERIFIED
  │             → then close on Binance (§4.5)
  └─ failure → MANUAL_REVIEW with parsed reason, ledger reservation released
```

### 4.5 Binance close

Flow B step 5 needs `Playwright`: click *"Transferido, notificar al vendedor"* and post the reference in the order chat. `BinanceC2CClient.mark_order_as_paid` (`binance_client.py:246-259`) is the API analogue but is documented as merchant-only (`:8-13`) and unverified. Therefore the close must go through the **same browser port** as the extraction, not through the C2C client. `COMPLETED` must only be written **after** the click succeeds; on failure → `MANUAL_REVIEW` with `failure_reason` (mirroring `sell_flow.py:500-513`'s release-failure pattern).

---

## 5. Risk and limits

### 5.1 What exists vs. what the spec requires

| Spec requirement | Existing setting | Gap |
|---|---|---|
| amount ≤ `MAX_BUY_AMOUNT_VES` | `max_tx_amount_ves` (`config.py:89`) | Present but **direction-agnostic**; no buy-specific ceiling. Add `max_buy_amount_ves` and keep `max_tx_amount_ves` as the shared floor/ceiling. |
| daily accumulated ≤ `DAILY_SMS_LIMIT_VES` | `daily_limit_ves` (`config.py:91`) | Present but shared by buy and sell, and *not* an SMS-channel cap. Add `daily_sms_limit_ves`. |
| implied rate within margin | — | **Absent entirely.** No rate field in `RiskAssessment` (`schemas.py:158-165`), no setting, no reference price source. |
| not already paid (idempotency) | `order_number` UNIQUE + `_ensure_order_idempotent` | Present, but it blocks resume instead of detecting "already paid" (§2.6). |
| USDT ceiling | `max_usdt_per_order` (`config.py:92`) | Present, enforced at `buy_flow.py:418-428`. |
| per-bank SMS cap (~Bs 100,000/day per `PROJECT_SPEC.md:9`) | — | Absent. Needed if multiple bank accounts are used. |

`RiskAssessment.limits` (`schemas.py:165`) is a `dict[str, str]` snapshot copied into the audit payload (`buy_flow.py:414`), so adding keys is backward compatible.

### 5.2 How `daily_volume_ledgers` works today

- One row per `(ledger_date UTC, currency)` (`models.py:316-322`); `currency` is `String(8)`.
- `_load_or_create_ledger` (`buy_flow.py:221-273`) creates it idempotently: `SELECT … FOR UPDATE` on PostgreSQL, `INSERT … ON CONFLICT DO NOTHING` then `SELECT` on SQLite.
- `_assess_risk` (`:275-314`) computes `projected = total_volume + amount`, rejects if over `daily_limit_ves`, and **only increments on the allowed path**.
- Both flows share the same `currency="VES"` row, so a buy consumes the sell budget.
- There is **no release/decrement** anywhere: once reserved, a failed SMS keeps the volume consumed permanently.

### 5.3 Can it back the SMS daily cap?

**Yes, with a caveat.** Three options:

| Option | Change | Pros | Cons |
|---|---|---|---|
| **A. Namespaced `currency` key** (`"VES_SMS"`) | `_load_or_create_ledger(currency=...)` refactor only, **no migration** | Zero schema risk; reuses the exact locking logic; `String(8)` fits (`VES_SMS` = 7 chars) | Overloads `currency` semantics; reporting/tests must know the pseudo-currency |
| **B. Add `channel` column** + UNIQUE `(ledger_date, currency, channel)` | New Alembic migration + backfill default `'ALL'` | Semantically clean; extensible | Migration + index/constraint rename; touches archived DDL |
| **C. New `sms_daily_ledgers` table** | New migration + duplicated ledger logic | Perfect separation | Most code; two counters to keep consistent |

**Recommendation: A for the first slice**, documented explicitly as a namespaced counter (`"VES_SMS"`), with a follow-up to B if a third channel appears. It keeps the money-risk slices migration-free.

Additionally, the SMS counter must gain a **release path** that today's ledger lacks:

- Reserve on dispatch (so two concurrent sends cannot both pass the cap).
- On a confirmed bank failure or a typed permanent gateway error, decrement `total_volume` (and `transaction_count`) and move to `MANUAL_REVIEW`.
- On timeout with no bank reply, **do not** auto-release: the money may already have moved. Keep the reservation and park in `MANUAL_REVIEW` — fail closed.

### 5.4 Implied-rate margin

`implied_rate = amount_fiat / amount_asset` (both already persisted, `models.py:119-120`). Two possible references:

- **Order-intrinsic** (recommended, no new provider): compare `implied_rate` to the order's own `price` (`models.py:118`). Since a well-formed order has `total_price == price × amount`, this is an *extraction integrity* check that catches a mangled UI scrape (e.g. wrong decimal separator), which is the dominant risk of Playwright extraction.
- **Market-relative** (needs a new provider/ticker): compare against a reference Bs/USDT rate. Larger scope; also racy.

Recommend order-intrinsic first with a tolerance setting (e.g. 1%), and treat market-relative as a separate change.

---

## 6. Slice plan for `auto-chain` (review budget 800 lines/PR)

Constraint order: **S1 → S4**, **S2 → S4**, **S3 → S4**, **S5 → S6**, **(S1,S2,S3,S5,S6) → S7**.

Every slice below is autonomous: clear start, clear finish, its own verification, trivially revertible.

### S1 — Bank SMS domain data + formatting (pure functions)

- **Start**: no bank profile registry; wrong message format.
- **Finish**: `app/domain/banks.py` with `BankSmsProfile`, `BANK_SMS_PROFILES` (BDV `0102`/`2661`,`2662`; Banesco `0134`/`2846`), `resolve_bank`, `format_ves_amount`, `render_bank_command`, `national_digits`, `cedula_digits`. No I/O.
- **Verification**: pure unit tests (exact strings, `1500,00`, round-trips, unsupported bank → `None`).
- **Fakes**: none. **Real credentials**: none.
- **Est. size**: ~180–260 lines (code + tests).

### S2 — Risk policy extraction + buy caps + SMS ledger channel

- **Start**: duplicated `_assess_risk`, no buy/SMS-specific caps, no rate check.
- **Finish**: shared `app/services/risk_policy.py` (or `app/domain/risk.py`) used by **both** flows; parameterized `_load_or_create_ledger(currency=...)`; new settings `max_buy_amount_ves`, `daily_sms_limit_ves`, `max_implied_rate_ves_per_usdt`, `rate_tolerance_pct`; `RiskAssessment.limits` gains the new keys; reservation + explicit `release_*` method.
- **Verification**: unit tests per limit boundary with freezegun at the UTC date rollover; one regression test proving `SellFlow` behaviour is unchanged; settings-consistency tests.
- **Fakes**: none (in-memory SQLite only). **Real credentials**: none.
- **Est. size**: ~320–420 lines (touches two 500-line services).
- **Risk**: refactoring `SellFlow` is the highest-blast-radius step; consider "add shared policy, delegate from both, delete copies" as two commits inside the PR.

### S3 — Browser port + Playwright adapter + extraction parser

- **Start**: no Playwright code, no payment-instruction model.
- **Finish**: `PaymentInstruction` DTO (`bank_numeric_code`, `phone`, `cedula`, `amount_ves`, `order_number`), `BinanceBrowser` `Protocol` (port), `PlaywrightBinanceBrowser` adapter (lazy `import playwright`, storage-state auth, timeouts from settings), and a **pure HTML parser** (`parse_payment_instructions(html) -> PaymentInstruction`) that the adapter calls.
- **Verification**: parser tests against committed HTML fixtures for BDV and Banesco order views; port conformance test via a fake; **no browser in CI**.
- **Fakes**: `FakeBinanceBrowser`. **Real credentials**: needed only for a manual smoke run of the adapter.
- **Est. size**: ~380–480 lines (adapter + fixtures). Fixtures do not count toward the review budget as authored logic, but the HTML files do add diff noise — split fixture files into a follow-up commit if the budget is tight.

### S4 — Buy dispatch service (PENDING → SENDING_SMS) with bank command

- **Start**: `BuyFlow.execute` sends a human sentence to the counterparty phone and completes immediately.
- **Finish**: new Alembic revision adding `p2p_orders.counterparty_id_number`, `sms_message_id`, `sms_sent_at`, `bank_short_code`; extraction via the S3 port; `bank_code` persisted; unsupported bank → `MANUAL_REVIEW`; risk via S2; bank command via S1; short-code fallback; **no `COMPLETED`** — stops at `SENDING_SMS`; idempotent resume (`resume(order_number)` for orders already in `SENDING_SMS`); failure keeps `SENDING_SMS` and records `EXTERNAL_ERROR`; ledger reservation released on permanent failure.
- **Verification**: `FakeBinanceBrowser` + `FakeSmsGateway` + in-memory SQLite; respx tests for the real `SmsGatewayClient` (path, bearer header, `Idempotency-Key`, 500 → typed error, no retry); `DuplicateOrderError` vs. resume semantics; migration up/down test.
- **Fakes**: yes for browser and SMS. **Real credentials**: only for the manual end-to-end smoke.
- **Est. size**: ~350–450 lines (service + migration + tests).
- **Note**: this is where the existing "COMPLETED too early" behaviour is *removed*, so it must land before S5/S6 or the state machine is incoherent.

### S5 — Inbound bank-reply confirmation + reference extraction

- **Start**: `handle_incoming_sms` substring matching; no parser, no `BankTransaction`.
- **Finish**: `app/domain/bank_replies.py` parser (success/failure patterns per bank); `BuyFlow.confirm_bank_payment(...)` writing `BankTransaction(direction=OUTGOING, verification_status=PENDING)` + `p2p_orders.bank_reference` + audit; exactly-one-candidate matcher with sender validation and `sms_confirmation_timeout_seconds`; webhook response distinguishes `matched/confirmed/ambiguous/no_match`; duplicate reference → `DuplicateBankReferenceError` → `MANUAL_REVIEW` (never 500).
- **Verification**: webhook integration tests over ASGI with `get_session` overridden and `app.state` fakes; parser tests from real-ish SMS samples; ambiguity → no state change; replay → 202 `duplicate=True`.
- **Fakes**: fake gateway payloads (no respx needed; the webhook is inbound). **Real credentials**: real SMS samples are desirable but not required to build.
- **Est. size**: ~300–380 lines.

### S6 — Binance close step (mark transferred + chat reference)

- **Start**: nothing calls Binance after confirmation.
- **Finish**: `BinanceBrowser.notify_paid(order_number, reference)` (click + chat), `SENDING_SMS → COMPLETED` **only after** success, `failure_reason` + `MANUAL_REVIEW` on failure, `AuditEvent.STATUS_CHANGED` payload with masked reference.
- **Verification**: fake port assertions on call order and arguments; state-machine tests for success/failure; explicit test that a Binance failure never yields `COMPLETED`.
- **Fakes**: yes. **Real credentials**: manual smoke only.
- **Est. size**: ~160–240 lines.

### S7 — Buy-order detection worker + operator trigger

- **Start**: `BuyFlow` unreachable; only the SMS poller runs.
- **Finish**: `buy_worker_enabled` / `buy_worker_poll_interval_seconds`; a worker using `get_order_history` with BUY filtering that skips known `order_number`s and enforces `order_type == BUY`; an operator endpoint (e.g. `POST /api/v1/orders/buy/{order_number}/pay`) guarded by `require_internal_api_key`; lifespan wiring mirroring `_run_sms_poller` (`main.py:36-69,96-98`) with cancellation-safe shutdown; optional timeout sweep for orders stuck in `SENDING_SMS`.
- **Verification**: respx for the history call; worker tick test with a fake clock; endpoint auth tests; shutdown cancellation test.
- **Fakes**: yes. **Real credentials**: none for CI.
- **Est. size**: ~180–260 lines.

### What needs real credentials / real hardware

| Slice | Fakes sufficient? | Real dependency |
|---|---|---|
| S1, S2, S5 (parser+matcher), S7 | Yes | none |
| S3 | Parser yes; adapter no | Binance login/session, Playwright Chromium |
| S4 | Yes | Android gateway + Tailscale + SIM for a true send |
| S6 | Yes | live Binance UI click |
| End-to-end Flow B | No | all of the above + real bank short code + real money |

**Total forecast**: ~1,870–2,490 authored lines across 7 slices. No single slice is designed to exceed 480 lines, so `auto-chain` is viable without an `size:exception`. This forecast must be re-validated by `sdd-tasks`.

---

## 7. Test strategy for strict TDD (`python -m pytest -q`)

### 7.1 Non-negotiable test seams

1. **`BinanceBrowser` port — REQUIRED.** Introduce a `typing.Protocol` (place under `app/domain/ports.py`, since the domain must not import infrastructure) with at minimum:
   ```python
   class BinanceBrowser(Protocol):
       async def extract_payment_instruction(self, order_number: str) -> PaymentInstruction: ...
       async def notify_paid(self, order_number: str, reference: str) -> None: ...
   ```
   Why it is mandatory: (a) CI cannot run Chromium/session login; (b) it keeps `BuyFlow` identical whether the close is done by Playwright or a future merchant API; (c) it matches the existing adapter pattern (`BaseAsyncHTTPClient` ABC + concrete client) without forcing `services` to import Playwright. The Playwright implementation must live in `app/infrastructure/clients/binance_browser.py` and import `playwright` **lazily** so collection never requires the browser binaries.
2. **Flow construction seam in the webhook.** `webhooks.py:197-204` builds `BuyFlow` inline with clients from `app.state`. Add a FastAPI dependency (e.g. `get_buy_flow(request, session) -> BuyFlow`) so tests can `app.dependency_overrides[get_buy_flow] = ...`. Without this, an SMS webhook test either performs real network calls or duplicates handler logic.
3. **`get_session` override.** The `client` fixture (`conftest.py:112-130`) calls `init_engine` + `create_all` against a file DB but never overrides `get_session`, while the `session` fixture runs on a *separate in-memory engine*. Any webhook test must do `app.dependency_overrides[get_session] = lambda: session`. Add this as a shared fixture (`app_client` + `override_session`) early in S5.

### 7.2 Fakes and fixtures to add

| Fixture / fake | Purpose |
|---|---|
| `fake_binance` | Records `get_order_detail/get_order_history/mark_order_as_paid/release_order`; returns a `BinanceC2COrder`. |
| `fake_browser` | Implements `BinanceBrowser`; records extraction + `notify_paid` calls; can raise a typed error. |
| `fake_sms` | Returns a controllable `SmsSendResult`; records `send_sms` args; can raise `RetryableExternalServiceError`. |
| `respx` routes (SMS) | `POST {sms_base}/messages` → 200 `{"message_id","status","sent_at"}`; assert `Idempotency-Key` and bearer header; 500 → `RetryableExternalServiceError` **without** retry (non-idempotent); malformed body → `ExternalServiceResponseError`. |
| `respx` routes (Binance) | `GET .../getUserOrderDetail` with signed query; 200 envelope → `BinanceC2COrder`; 401 → `UnauthorizedError`; 500 retried for idempotent GETs (assert call count). |
| `tests/fixtures/binance/order_view_{bdv,banesco}.html` | Offline HTML samples for the extraction parser; keeps selector logic testable without a browser. |
| `tests/fixtures/sms/bank_reply_{success,failure}_{bdv,banesco}.txt` | Bank reply bodies for the reply parser and matcher. |
| `seed_order(**kwargs)` factory | Build orders in specific statuses with deterministic amounts/phones; replaces the private `_order` helper pattern from `tests/test_idempotency.py:22-40`. |
| `freezegun` (`freeze_time`) | UTC date rollover for ledger caps, and `sms_confirmation_timeout_seconds` window tests. |
| `app_client` + `override_session` | Webhook integration over `httpx.ASGITransport` with in-memory SQLite and fake `app.state` clients. |

Existing fixtures to reuse as-is: `engine` (in-memory SQLite + `StaticPool`), `session`, `test_settings`. `test_settings` will need the new decimal settings once S2 lands, or `Settings()` validation fails.

### 7.3 TDD ordering inside each slice

1. **S1**: pure-string tests first (no fakes at all). This is the cheapest place to start and establishes the RED-GREEN rhythm.
2. **S2**: boundary tests per limit with freezegun; then a characterization test that pins current `SellFlow` behaviour *before* the shared-policy refactor (protect the existing flow).
3. **S3**: parser tests from HTML fixtures first, then port-conformance test with the fake, then a thin adapter test that is skipped when Chromium is absent (`pytest.importorskip("playwright")` + an explicit `@pytest.mark.integration`).
4. **S4**: fake-driven state tests (`SENDING_SMS` persisted before send, no `COMPLETED`, resume path, ledger released on permanent failure), then respx tests for the real adapter.
5. **S5**: matcher tests (0, 1, many candidates; wrong sender; expired window), then webhook integration.
6. **S6**: success/failure/ordering assertions; explicit "never COMPLETED without a successful notify".
7. **S7**: respx history call + worker tick + authz.

**Hard rule for this change**: every test must pass with the network stack unavailable. Any test that would touch Binance, the Android gateway, or Chromium must be explicitly marked `integration` and excluded from the default `-q` run.

---

## 8. Open questions (product decisions that change the design)

Deliberately capped at 5. Everything else was resolved from `PROJECT_SPEC.md`.

1. **Bank reply wording per bank.** Do you have 2–3 real success and failure SMS bodies for BDV and Banesco? The parser design depends on whether the reference appears as `Referencia: 123456789`, `Ref. 123456789`, or a fixed-width block, and whether failure texts are uniform. *Fallback if unavailable*: conservative regexes + `MANUAL_REVIEW` on any unparsed reply (safe, but every failure becomes manual work).
2. **Cedula in the command.** Does the BDV/Banesco command take the bare digits (`12345678`) or the `V-`/`E-` prefix? The spec example (`PROJECT_SPEC.md:33`) shows digits only, but that is one bank. This changes the template data per bank.
3. **Implied-rate reference.** Is the margin an *extraction integrity* check against the order's own Binance price, or a *market* check against an external Bs/USDT reference (new provider + new failure mode)? This is the difference between ~40 and ~250 lines in S2.
4. **What "close" means on Binance for a standard account.** Is clicking *"Transferido, notificar al vendedor"* + posting the reference sufficient to consider the buy settled, or must the flow also wait for the seller's release (i.e. `COMPLETED` should mean "seller notified" vs. "crypto received")? This determines whether S6 needs a post-notification wait/verification step and whether `release_order` is ever used.
5. **Automatic vs. operator-approved payment.** Flow A has `AUTO_RELEASE_ENABLED`; Flow B's spec says the worker pays automatically, but wiring real money on an unattended worker is irreversible. Do we need a manual approval gate (e.g. `buy_auto_pay_enabled=false` → park at `PAYMENT_DETECTED`/`MANUAL_REVIEW` for one click) before the SMS is sent? This adds one state and one endpoint.

---

## 9. Affected areas

| Path | Why |
|---|---|
| `app/domain/banks.py` (new) | Bank profiles, numeric codes, short codes, VE amount/phone formatting, command rendering. |
| `app/domain/bank_replies.py` (new) | Bank reply success/failure parsing. |
| `app/domain/ports.py` (new) | `BinanceBrowser` protocol + `PaymentInstruction` DTO. |
| `app/domain/schemas.py` | New/extended contracts (payment instruction, risk assessment fields, richer webhook response). |
| `app/domain/enums.py` | Possibly a bank-reply outcome enum; **do not touch the mojibake alias table**. Avoid adding `PENDING_PAYMENT` (map Binance status → `PENDING` instead). |
| `app/infrastructure/clients/binance_browser.py` (new) | Playwright adapter (lazy import) + HTML extraction parser. |
| `app/infrastructure/clients/sms_gateway.py` | Replace `build_payment_sms` usage; add national-number/cedula helpers or move them to `domain/banks.py` (preferred). Short-code dispatch stays in the service. |
| `app/services/buy_flow.py` | Extraction, banking caps, bank command dispatch, no premature `COMPLETED`, resume path, confirmation handler. |
| `app/services/sell_flow.py` | Delegate to the shared risk policy (avoid divergence). |
| `app/services/risk_policy.py` (new) | Single risk implementation + parameterized ledger carrier. |
| `app/core/config.py`, `.env.example` | New settings + validators. |
| `app/api/v1/webhooks.py` | Richer SMS webhook outcome, `get_buy_flow` dependency seam. |
| `app/api/v1/orders.py` (new, S7) | Operator trigger; possibly status read. |
| `app/main.py` | Buy worker lifespan wiring, browser client lifecycle. |
| `app/infrastructure/models.py` + new Alembic revision | `counterparty_id_number`, `sms_message_id`, `sms_sent_at`, `bank_short_code`, `bank_confirmed_at`. |
| `tests/conftest.py`, `tests/test_banks.py`, `tests/test_bank_replies.py`, `tests/test_buy_flow.py`, `tests/test_buy_webhook.py`, `tests/test_browser_parser.py`, `tests/fixtures/**` | New suite + fixtures. |
| `openspec/specs/` | Currently empty (`.gitkeep` only) — `sdd-spec` will create the first domain spec. |

---

## 10. Risks

- **Irreversible money action.** The SMS is a real bank debit with no API to cancel. Every failure path must fail closed to `MANUAL_REVIEW`; no automatic retry of a command whose fate is unknown.
- **Refactor blast radius in S2.** Touching `sell_flow.py` risks the only working flow. Mitigation: characterization tests before refactor, one delegate-and-delete commit.
- **Live pre-existing hazard (not just a gap).** `_dispatch_cachicamo` (`webhooks.py:271-281`) matches `PENDING`/`PAYMENT_DETECTED` orders by exact amount **without filtering `order_type`**, then drives `SellFlow.handle_incoming_payment`, which can release crypto (`sell_flow.py:501`). A buy order sitting in `PENDING` with a matching amount could therefore be consumed by a sell path. S2/S4 should add the direction guard; this deserves explicit mention in the proposal as a safety fix, even though it is adjacent to Flow B.
- **`OrderType` semantics are inverted in the docstring** (`enums.py:9-17`) relative to `BuyFlow`'s behaviour; any new code must follow the spec (BUY = we pay VES), and the docstring should be corrected in the slice that enforces the direction.
- **Two live engines in tests.** `client` vs `session` fixtures bind different databases (`conftest.py:80-130`). Webhook tests must override `get_session` or they will assert against the wrong data.
- **SETTINGS CONSTRUCTION IS BRITTLE.** `get_settings()` is `lru_cache`d and `Settings()` is constructed at import (`config.py:284-290`); adding required fields breaks every test and the app unless `.env.example`, `conftest.py:29-31`, and `test_settings` are updated in the same commit.
- **Player dependency on mojibake-adjacent code.** `enums.py` must not be reformatted; new bank data lives in a new module.
- **Global `bank_reference` uniqueness** across banks/orders can reject a legitimate reference; must map to `DuplicateBankReferenceError` → manual review, never a crash.
- **No migration for enum members** is fine (no CHECK), but any *column* addition needs a real Alembic revision — and `create_all()` dev paths will mask a missing migration unless a migration test exists.
- **Review budget**: ~1,870–2,490 authored lines total; each slice must stay under 480 to respect the 800-line cap with margin.

## 11. Ready for proposal

**Yes.** Scope, gaps, seams, slice boundaries and verification are concrete enough to write `proposal.md` immediately. The five open questions in §8 do not block proposal/spec work: recommended defaults are stated for each (conservative parser + manual review, digits-only cedula, order-intrinsic rate check, "notified" completion, manual gate default off), and `sdd-design`/`sdd-tasks` can proceed against them with an explicit assumption list.

Suggested domain for `sdd-spec`: `buy-flow` (delta spec), with `sms-payment` as an alternative if you prefer one spec per channel.

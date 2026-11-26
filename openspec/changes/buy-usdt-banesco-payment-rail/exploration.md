# Exploration: BUY payment rail via Banesco Online + Telegram Clave Dinámica

- **Change**: `buy-usdt-banesco-payment-rail`
- **Phase**: explore (SDD)
- **Store**: `openspec` (filesystem); Engram mirror at `sdd/buy-usdt-banesco-payment-rail/explore`
- **Project**: `liquid-ve` (repository `C:\Cursos\liquidVE`)
- **Mode**: `auto`, delivery strategy `auto-chain` (stacked-to-main), review budget **800 changed lines**
- **Strict TDD**: ACTIVE. Runner: `.\.venv\Scripts\python.exe -m pytest -q`. Lint: `.\.venv\Scripts\python.exe -m ruff check app tests`.
- **Baseline**: 263 passed, 2 skipped; ruff clean. `openspec/config.yaml` is authoritative.
- **Supersedes**: the SMS rail shipped by `openspec/changes/archive/2026-11-23-buy-usdt-sms-payment-flow/` (read-only; never modified).
- **Date**: 2026-11-25

---

## ⚠️ Credential Handling Rule (non-negotiable)

The operator supplied real Banesco online-banking credentials and security answers
in chat. **No credential value may be written into any artifact, code, test,
fixture, log, commit, or Engram observation.** Credentials are referenced only by
their environment-variable **names** (for example `BANESCO_ONLINE_USER`,
`BANESCO_ONLINE_PASSWORD`, `BANESCO_SECURITY_ANSWER_<N>`). This rule applies to:

- this exploration, the proposal, specs, design, tasks, verify report, archive;
- source code, docstrings, comments, error messages, audit payloads, logs;
- pytest fixtures, golden HTML snapshots, and test factories;
- `.env.example` (placeholders only) and every document in `openspec/`;
- Engram observations and session summaries.

The real values live only in the operator's local `.env` (gitignored). The
repository is shared and auditable; a leaked bank credential enables fraudulent
payments and account lockout.

---

## Exploration: BUY payment rail replacement

### Current State

Flow B today is a complete but **SMS-based** BUY path:

1. `BUY_WORKER_ENABLED` worker or `POST /api/v1/orders/buy/{order_number}/pay`
   detects a `BUY` order in external `PENDING_PAYMENT` and persists it as
   `PENDING` (`app/main.py:_buy_worker_tick`, `app/api/v1/orders.py`).
2. `BuyFlow.execute()` evaluates risk, then `BuyFlow._extract()` scrapes the
   Binance order page through the `BinanceBrowser` port
   (`app/infrastructure/clients/binance_browser.py`) into a frozen
   `PaymentInstruction` (`app/domain/ports.py`).
3. `BuyFlow._dispatch()` renders one Pago Móvil SMS command
   (`app/domain/banks.py:render_bank_command`) and sends it through
   `SmsGatewayClient.send_sms()` via `app/services/sms_dispatch.py`, after
   committing `SENDING_SMS` + the `VES_SMS` ledger reservation + the
   `idempotency_key`.
4. The bank reply arrives as an SMS, is deduplicated in `webhook_events`,
   parsed by `app/domain/bank_replies.py`, correlated by
   `BuyFlow._match_candidates()`, and committed as one `OUTGOING`
   `BankTransaction` plus `p2p_orders.bank_reference` / `bank_confirmed_at`.
5. `BuyFlow._close_order()` calls `notify_paid()` (click
   “Transferido, notificar al vendedor” + post the reference in chat) and only
   then transitions to `COMPLETED`.

What exists that the new rail can reuse:

- The `BinanceBrowser` port and Playwright adapter pattern: lazy
  `import playwright`, pure HTML parser separated from the driver, session-state
  validation, browser-free CI, `LIQUIDVE_BROWSER_TESTS` opt-in smoke
  (`tests/test_browser_adapter.py`).
- `RiskPolicy` with per-order caps, `VES` / `VES_SMS` ledger channels,
  read-only `assess_buy()`, atomic `reserve_buy()` at the durable boundary
  (`app/services/risk_policy.py`).
- Idempotency anchors: `order_number` UNIQUE, `bank_reference` UNIQUE,
  `idempotency_key` UNIQUE, `webhook_events(provider, external_event_id)`.
- Append-only `AuditLog`, typed `LiquidVEException` hierarchy, masked logging
  (`app/core/logging.py`), `SecretStr` settings and fail-fast validation.
- Existing Binance order-detail scrape and `notify_paid()`; the C2C API client
  for detection and status polling (`BinanceC2CClient.get_order_detail`).
- Existing fixtures convention: `tests/fixtures/binance/*.html`,
  `tests/fixtures/sms/*.txt`; `FakeBrowser`, `FakeSms`, `FakeBinance`,
  `FakeCachicamo` in `tests/conftest.py`.

What is missing or wrong for the target flow:

- No Banesco Online automation at all (no login, no security questions, no ad
  modal, no Pago Móvil form, no Clave Dinámica, no receipt capture).
- No Telegram client or dependency (`requirements.txt` has **no** Telegram
  library; `httpx` is present).
- `PaymentInstruction` / `banks.national_digits` / `cedula_digits` produce a
  single phone string and a single cedula string; the Banesco form needs **two
  controls each** (prefix select + digits; letter select + digits).
- The lock/unlock tail stops at `COMPLETED`; there is no
  `AWAITING_RELEASE`, no release watcher, and no appeal.
- `SENDING_SMS` is semantically wrong for a browser payment rail.
- The SMS confirmation path (`sms_dispatch.py`, `bank_replies.py`,
  `/webhooks/sms`, `SmsGatewayClient.send_sms`) has no role once Banesco Online
  captures the receipt directly.
- The Binance API DTO does **not** expose the counterparty document letter; the
  Banesco ID select needs it.

### Affected Areas

| Area | Path | Why affected |
|---|---|---|
| Order lifecycle | `app/domain/enums.py` | Rename `SENDING_SMS`; add `AWAITING_RELEASE`, `APPEALED`; transition table; new `AuditEvent`/`AuditActor` members. |
| Provider port/DTOs | `app/domain/ports.py` | Extend/replace payment DTOs; add Banesco rail, Telegram notifier, Binance evidence/appeal ports. |
| Payment mapping | `app/domain/banks.py` | Reuse bank registry/`resolve_bank`; add Banesco prefix/ID-type mapping; `cedula_digits` prefix set mismatch (`P` vs `R`). |
| SMS reply domain | `app/domain/bank_replies.py` | Obsolete for BUY (`REMOVED`/dormant). |
| SMS dispatch | `app/services/sms_dispatch.py` | Obsolete for BUY; classification vocabulary is SMS-specific. |
| BUY orchestration | `app/services/buy_flow.py` | Replace dispatch/confirm/close with payment rail → receipt → evidence upload → awaiting release → appeal. |
| Risk/ledger | `app/services/risk_policy.py` | Keep caps; decide `VES_SMS` → `VES_PAYOUT` rename and widen `DailyVolumeLedger.currency` (`String(8)` cannot hold `VES_PAYOUT`). |
| ORM | `app/infrastructure/models.py` | New nullable columns (`payment_rail`, receipt, release, appeal); new `operator_challenges` table. |
| Binance browser | `app/infrastructure/clients/binance_browser.py` | Keep extraction/close; add receipt upload and appeal actions. |
| Binance API | `app/infrastructure/clients/binance_client.py` | Release watcher uses `get_order_detail`; document status mapping; no document letter. |
| SMS gateway | `app/infrastructure/clients/sms_gateway.py` | No BUY caller after the switch; keep adapter behind a rail switch. |
| Webhooks | `app/api/v1/webhooks.py` | `/webhooks/sms` becomes obsolete; `/webhooks/cachicamo` unchanged; optional `/webhooks/telegram`. |
| Operator API | `app/api/v1/orders.py` | `SENDING_PAYMENT` no-op; new outcomes; safe resume guard; appeal/reconcile actions. |
| Health | `app/api/v1/health.py` | Replace `sms_gateway` probe with `banesco`/`telegram` (or keep gated). |
| Lifespan/workers | `app/main.py` | Replace SMS poller with payment/release workers + Telegram polling. |
| Config | `app/core/config.py` | New Banesco/Telegram/receipt/release settings; fail-fast validation. |
| Logging | `app/core/logging.py` | Mask security answers, `clave`, `R-` cedulas; never log receipt text/bytes. |
| Specs | `openspec/specs/buy-flow/spec.md`, `bank-sms/spec.md` | Delta specs: MODIFY/REMOVE SMS requirements; ADD `banesco-payment` / `telegram-handoff` / `release-appeal`. |
| Tests/fixtures | `tests/test_buy_*.py`, `tests/test_orders_api.py`, `tests/test_enums.py`, `tests/test_migrations.py`, new `tests/fixtures/banesco/*.html` | Rename refs, new rail/fakes, fixture-based parser/adapter tests. |
| Docs | `README.md`, `.env.example` | Status names, rail description, new setting names. |
| Archived change | `openspec/changes/archive/2026-11-23-buy-usdt-sms-payment-flow/` | **Read-only audit trail; never edit.** |

### Approaches

#### A. Payment rail

| Approach | Pros | Cons | Effort |
|---|---|---|---|
| **A1. Banesco Online Playwright adapter + Telegram handoff** (target) | Matches the real operator flow; no Android work; receipt captured directly; reusable page-object pattern | Bank ToS/account-lock/captcha risk; DOM drift; PII in screenshots | High |
| A2. Keep SMS rail and add Banesco later | Already shipped; no new risk | Rejected: the SMS channel is limited and the target flow is web; duplicate rails to maintain | Low |
| A3. Android app automation for Banesco | No web DOM | Explicitly out of scope in this change; device farming risk | High |
| A4. Banesco public API | Clean | No public API exists for natural-person Pago Móvil | N/A |

#### B. Telegram handoff

| Approach | Pros | Cons | Effort |
|---|---|---|---|
| **B1. Raw Bot API over `httpx`** (`sendMessage` + long-poll `getUpdates`) | No new dependency; reuses `BaseAsyncHTTPClient`/`respx`; testable; simple | We own offset/reply correlation, retries, and update parsing | Medium |
| B2. `python-telegram-bot` | Batteries included; conversation handlers | New runtime dependency; framework/event-loop integration; harder to fake at the port | Medium |
| B3. Telegram webhook route | No long-poll loop | Needs public TLS ingress + secret token; worker/webhook process coupling | Medium |

#### C. Receipt storage

| Approach | Pros | Cons | Effort |
|---|---|---|---|
| **C1. File under `data/receipts/{order}/` + DB path + SHA-256** | Gitignored (`data/*`); no DB bloat; uploadable via `set_input_files`; hash dedupe | File lifecycle/retention to manage | Low |
| C2. Store bytes in DB | Single backup | Bloat, PII in backups, row locks | Medium |
| C3. Object storage | Durable | New infra/secrets; overkill for one operator | High |

#### D. Status model

| Approach | Pros | Cons | Effort |
|---|---|---|---|
| **D1. Rename `SENDING_SMS` → `SENDING_PAYMENT` + add `AWAITING_RELEASE`/`APPEALED`** | Truthful state machine; no production data; columns are VARCHAR (no DDL) | ~60 non-archive references + a data migration for existing dev rows | Medium |
| D2. Keep `SENDING_SMS`, add new states | No rename churn | The state name lies forever; operators/future multi-bank confusion | Low |
| D3. New separate enum/status table | Clean | Breaks the established single state machine | High |

### Recommendation

Adopt **A1 + B1 + C1 + D1**:

- One **Banesco Online Playwright adapter** behind a domain port, split into a
  pure parser/page-object layer and a lazily-imported driver, mirroring
  `binance_browser.py`.
- One **Telegram Bot API adapter over `httpx`** (no new dependency), long-polling
  in the existing worker process, with a persisted `operator_challenges` row that
  survives restarts for audit and duplicate prevention.
- **Rename** `SENDING_SMS` to `SENDING_PAYMENT` (no production data exists) and
  add `AWAITING_RELEASE` and `APPEALED`. Include a data-only migration for any
  local/dev rows.
- Replace the BUY tail with: `SENDING_PAYMENT` → receipt captured + evidence
  uploaded + Binance “paid” notified → `AWAITING_RELEASE` → `COMPLETED` or
  (timeout) `APPEALED`; every unknown/ambiguous/captcha/locked state fails closed
  to `MANUAL_REVIEW`.
- Keep `bank-sms`/`bank_replies`/`sms_dispatch` code as **dormant** behind a
  `BUY_PAYMENT_RAIL=sms|banesco` switch for one rollout window, then remove the
  SMS BUY path in a dedicated decommission slice (keeps rollback cheap and
  reviewable).

### Risks (summary)

- Bank ToS / personal-account lockout; captcha/anti-bot; credential compromise.
- DOM drift in login/questions/modal/form/confirmation/receipt; silent selector
  breakage must always raise a typed error.
- Wrong document letter (V/E/J/G/R) if the Binance page does not expose it.
- Double payment if a paid order is re-dispatched (`MANUAL_REVIEW → PENDING`).
- Receipt screenshot contains counterparty PII; must never reach logs/git/Engram.
- Appeal is consequential and must be at-most-once (durable attempt identity +
  status check + kill switch).
- Restart mid-payment is unrecoverable by design; must fail closed, never
  auto-resume.
- Telegram key leakage (logs, echo, retention) and reply mis-correlation.

### Ready for Proposal

**Yes**, with the open decisions listed at the end. The gap analysis below is
concrete enough for `sdd-propose` / `sdd-spec` / `sdd-design` to proceed without
re-exploring.

---

## Gap Analysis

### 1. Reuse map

#### Survive unchanged

| Module | Why |
|---|---|
| `app/services/sell_flow.py` | Flow A is independent; no SMS dispatch, no Banesco. |
| `app/infrastructure/clients/cachicamo_client.py` | Flow A only. |
| `app/infrastructure/clients/base.py` | HTTP retry/backoff/typed-error primitives reused by Telegram and any new HTTP adapter. |
| `app/core/security.py` | HMAC/bearer helpers unchanged; add no bank logic. |
| `app/infrastructure/database.py` | Session/worker primitives unchanged. |
| `app/domain/schemas.py` (most) | `RiskAssessment`, `OrderRead`, error envelopes, Cachicamo payloads unchanged; SMS schemas become dormant/removed. |
| `app/api/v1/health.py` | Structure unchanged; only probe names change. |
| `tests/fixtures/binance/*.html` | Reused by the existing parser tests; Banesco fixtures are additive. |

#### Reused with modification

| Module | Modification |
|---|---|
| `app/domain/ports.py` | Keep `PaymentInstruction` as the provider-neutral scrape; add `BanescoPaymentTarget` (pure mapping), `PaymentReceipt`, `PaymentRail` protocol, `ClaveChallenge`/`TelegramNotifier` protocol, and Binance `upload_evidence`/`file_appeal` methods (or a separate `BinanceOrderActions` protocol). |
| `app/domain/banks.py` | Keep `BankSmsProfile`/registry/`resolve_bank`/`format_ves_amount`/`national_digits`/`cedula_digits`; add Banesco prefix/ID-type constants and a destination-bank option map. Fix the `[VEJGP]` vs `V/E/J/G/R` mismatch. `render_bank_command`/short codes become SMS-only. |
| `app/services/buy_flow.py` | Replace `_dispatch` SMS body with rail invocation; replace inbound confirmation with receipt persistence; replace `_close_order` with upload → notify → `AWAITING_RELEASE`; add release/appeal handlers; delete SMS-only methods in the decommission slice. |
| `app/services/risk_policy.py` | Keep `assess_buy`/`reserve_buy`/`release`; rename the outgoing channel (see #2/#8) and widen the ledger key column if `VES_PAYOUT` is chosen. |
| `app/infrastructure/models.py` | Add nullable payment-rail columns + `operator_challenges`; keep SMS columns for historical rows. |
| `app/infrastructure/clients/binance_browser.py` | Keep parser/`extract_payment_instruction`/`notify_paid`; add receipt upload and appeal page objects; both behind the same lazy-import pattern. |
| `app/infrastructure/clients/binance_client.py` | Use `get_order_detail` for release polling; document status mapping; no structural change unless a status field is missing. |
| `app/api/v1/orders.py` | `SENDING_PAYMENT` no-op; new response outcomes (`awaiting_release`, `appealed`, `manual_review`); guard resume so a paid order is never re-dispatched; add appeal/reconcile operator action. |
| `app/main.py` | Replace `_run_sms_poller` with `_run_payment_challenge_poller`/`_run_release_watcher` or keep the worker tick and add a release tick; wire Telegram. |
| `app/core/config.py` | Add settings; validate Banesco/Telegram/receipt/release groups at startup. |
| `app/core/logging.py` | Extend sensitive keys/patterns (`security_answer`, `answer`, `clave`, `clave_dinamica`, `R-` cedula). |
| `app/core/exceptions.py` | Add typed codes: `banesco_login_failed`, `banesco_layout_unexpected`, `banesco_captcha_detected`, `banesco_account_locked`, `banesco_maintenance`, `banesco_session_expired`, `banesco_payment_rejected`, `banesco_payment_outcome_unknown`, `clave_dinamica_timeout`, `clave_dinamica_rejected`, `receipt_amount_mismatch`, `receipt_unavailable`, `evidence_upload_failed`, `appeal_already_filed`, `release_timeout`. |
| `tests/conftest.py` | Update `FakeBrowser`; add `FakePaymentRail`, `FakeTelegram`, `FakeReceipt`, Banesco fixture loaders; keep the browser-free default. |
| `README.md` / `.env.example` | Update status names and rail description; add setting names with placeholders. |

#### Become obsolete (dormant first, removed in the decommission slice)

| Module / symbol | Verdict | Notes |
|---|---|---|
| `app/services/sms_dispatch.py` (`DispatchFate`, `DispatchResult`, `dispatch_bank_command`, classifiers) | **Obsolete** | No BUY caller. Do not reuse its fate vocabulary; the Banesco rail needs `PaymentFate` (`CAPTURED`, `REJECTED`, `UNKNOWN`, `ABORTED`). |
| `app/domain/bank_replies.py` (`BankReplyOutcome`, `parse_bank_reply`) | **Obsolete** | No inbound SMS. Keep dormant only if the SMS rail remains switchable. |
| `POST /api/v1/webhooks/sms` (`sms_webhook`) | **Obsolete** | No Android SMS confirmation. Keep `/webhooks/cachicamo`. Optionally return `410` behind the rail switch before removal. |
| `SmsGatewayClient.send_sms` / `fetch_inbox` | **No BUY caller** | Keep the adapter behind the switch; remove from lifespan when the rail is `banesco`. |
| `SmsInboundWebhookPayload`, `SmsWebhookResponse`, `SmsConfirmation` | **Obsolete** | Replaced by rail/receipt/challenge DTOs. |
| `BuyFlow.handle_incoming_sms`, `confirm_bank_payment`, `_claim_sms_event`, `_finish_sms_event`, `_process_bank_reply`, `_match_candidates`, `_reject_webhook`, `_confirm_success`, `_park_duplicate_reference`, `_profile_for_sender`, `_short_code_overrides` | **Delete in decommission** | Replaced by receipt capture + challenge + release/appeal. |
| `sweep_timeouts` (SMS confirmation window) | **Replaced** | New sweeps: challenge timeout, stale payment, release timeout. |
| `LEDGER_VES_SMS` / `DAILY_SMS_LIMIT_VES` / `SMS_CONFIRMATION_TIMEOUT_SECONDS` | **Rename** | Semantics become “outgoing payout cap”; see #2 and #8. |
| `_run_sms_poller` in `app/main.py` | **Obsolete** | Telegram poller + release watcher replace it. |
| `health.py` `sms_gateway` probe | **Replace** | Probe `banesco`/`telegram` when enabled. |

**Rollout switch**: `BUY_PAYMENT_RAIL: Literal["sms", "banesco"] = "banesco"`.
With `sms`, the old path stays reachable for rollback; with `banesco`, the SMS
path is unreachable. After one stable release, a dedicated decommission PR
removes the SMS BUY code. The Android side is not touched (no Android
engineering in this change).

---

### 2. Status model: rename `SENDING_SMS` → `SENDING_PAYMENT` and add the release tail

#### Verdict

**Rename.** `OrderStatus` is `Enum(..., native_enum=False)` stored as
`VARCHAR(32)` with **no CHECK constraint** (confirmed: `models.py:111-115`,
`alembic/versions/20260101_0001_initial_schema.py:7-8,52,145-146`). `SENDING_SMS`
is 11 chars, `SENDING_PAYMENT` is 15, so the column width is sufficient. There is
no production data, so the rename is cheap and correct. Keeping `SENDING_SMS`
would leave a lie in the state machine and confuse the release/appeal tail.

#### Every non-archive reference

| File | Approx. refs | What changes |
|---|---:|---|
| `app/domain/enums.py` | 3 | Member definition + source edge + transition-table key. |
| `app/api/v1/orders.py` | 2 | Docstring + `if order.status is OrderStatus.SENDING_SMS` no-op. |
| `app/services/buy_flow.py` | 19 | Docstrings, `_advance_to_sending`, `_dispatch` guard, `_record_unknown`, `sweep_timeouts`, `resume`, `_match_candidates`. |
| `tests/test_buy_flow.py` | 12 | Status assertions and seed helpers. |
| `tests/test_buy_close.py` | 4 | Seed kwargs + audit assertions. |
| `tests/test_buy_webhook.py` | 8 | Seed kwargs + post-confirmation assertions. |
| `tests/test_buy_worker.py` | 4 | Seeds for the timeout sweep. |
| `tests/test_enums.py` | 3 | Legal/illegal transition assertions. |
| `tests/test_orders_api.py` | 4 | No-op/terminal assertions. |
| `README.md` | 1 | Status list. |
| `openspec/specs/buy-flow/spec.md` | several | R-BUY-002/006/010 text; update via delta `MODIFIED`. |
| `openspec/changes/archive/2026-11-23-buy-usdt-sms-payment-flow/**` | many | **Never modify**; historical name stays. |
| `openspec/changes/buy-usdt-banesco-payment-rail/**` | new | Use the new names. |

Total non-archive code/test refs: **≈59–60** across 9 Python files plus README/specs.

#### Migration consequence

- No DDL is required for the rename itself (VARCHAR, no CHECK), but **existing
  rows must be migrated** or the ORM raises `LookupError` when loading
  `p2p_orders.status`, `audit_logs.from_status`, or `audit_logs.to_status` with
  the old literal.
- Add a data-only Alembic revision (e.g. `20261125_0003_status_rename_payment_rail`):
  ```sql
  UPDATE p2p_orders SET status = 'SENDING_PAYMENT' WHERE status = 'SENDING_SMS';
  UPDATE audit_logs SET from_status = 'SENDING_PAYMENT' WHERE from_status = 'SENDING_SMS';
  UPDATE audit_logs SET to_status = 'SENDING_PAYMENT' WHERE to_status = 'SENDING_SMS';
  ```
  Downgrade reverses the three statements. This works on SQLite and PostgreSQL
  because the columns are strings.
- `AuditLog.payload` JSON strings that mention `SENDING_SMS` are historical and
  should **not** be rewritten (append-only audit semantics).
- Add a `tests/test_migrations.py` case: seed a `SENDING_SMS` row, upgrade, assert
  `SENDING_PAYMENT`; downgrade, assert `SENDING_SMS`.
- API clients that parse `status` will see `SENDING_PAYMENT`; there are no known
  external consumers. Document the breaking change.

#### New lifecycle tail

New members: `SENDING_PAYMENT`, `AWAITING_RELEASE`, `APPEALED`
(replace `SENDING_SMS`). Exact edges:

```
PENDING              -> PAYMENT_DETECTED, FAILED, MANUAL_REVIEW
PAYMENT_DETECTED     -> VERIFYING_CACHICAMO, MANUAL_REVIEW, FAILED
VERIFYING_CACHICAMO  -> SENDING_PAYMENT, COMPLETED, MANUAL_REVIEW, FAILED   (COMPLETED remains for Flow A)
SENDING_PAYMENT      -> AWAITING_RELEASE, MANUAL_REVIEW, FAILED
AWAITING_RELEASE     -> COMPLETED, APPEALED, MANUAL_REVIEW, FAILED
APPEALED             -> COMPLETED, FAILED, MANUAL_REVIEW
COMPLETED            -> (terminal)
FAILED               -> (terminal)
MANUAL_REVIEW        -> PENDING, FAILED
```

- `SENDING_PAYMENT → COMPLETED` is **removed**: completion requires the
  counterparty release. The receipt is payment evidence, not completion.
- `AWAITING_RELEASE` requires `bank_reference IS NOT NULL` and
  `bank_confirmed_at IS NOT NULL` (reuse the existing financial anchor).
- `APPEALED` is entered only after a durable appeal-attempt identity is committed
  (see #7); `APPEALED → COMPLETED` when Binance resolves in our favor.
- `MANUAL_REVIEW → PENDING` must be guarded: only **pre-payment** reasons
  (`awaiting_operator_approval`, `payment_data_unavailable`, risk blocks) may be
  resumed. A payment-related park (`clave_dinamica_timeout`,
  `payment_outcome_unknown`, `receipt_amount_mismatch`, `release_timeout` when
  manual) must **not** re-dispatch. Enforce in `BuyFlow._dispatch`/`resume` and
  in `orders.py`.
- New `AuditEvent` members (VARCHAR, no migration): `PAYMENT_INITIATED`,
  `PAYMENT_CAPTURED`, `EVIDENCE_UPLOADED`, `TELEGRAM_CHALLENGE_SENT`,
  `TELEGRAM_CHALLENGE_ANSWERED`, `RELEASE_DETECTED`, `APPEAL_FILED`,
  `APPEAL_SKIPPED`.
- New `AuditActor` members: `BANK_AUTOMATION`, `TELEGRAM` (optional but useful).

#### Ledger channel rename

`VES_SMS` semantics are now “outgoing payout cap”, not SMS. Options:

- Rename to `VES_PAYOUT` (10 chars). **`DailyVolumeLedger.currency` is
  `String(8)`** (`models.py:337`), so the column must be widened to `String(16)`
  in the payment-rail migration (batch mode on SQLite). A data migration updates
  `daily_volume_ledgers.currency='VES_PAYOUT' WHERE currency='VES_SMS'`.
- Or use a shorter key (`VES_PAY`, 7 chars) and avoid the column change. This
  keeps the migration smaller but the name is less clear.

Recommendation: widen to `String(16)` and use `VES_PAYOUT`; rename
`DAILY_SMS_LIMIT_VES` → `DAILY_PAYOUT_LIMIT_VES` (accept the old name as a
deprecated alias for one release), and `SMS_CONFIRMATION_TIMEOUT_SECONDS` →
`PAYMENT_CONFIRMATION_TIMEOUT_SECONDS`.

---

### 3. Payment data mapping: two-control selects

#### Current gap

`PaymentInstruction` (`app/domain/ports.py:23-68`) carries:

- `phone: str` — one string (`+584141234567` or `04141234567`).
- `id_number: str` — one string (`V-12345678` or `12345678`).
- `bank_numeric_code: str`, `amount_ves: Decimal`, `raw_fields`.

`banks.national_digits()` returns 11 national digits; `banks.cedula_digits()`
strips the `V/E/J/G/P` prefix and returns 6–9 digits. The Banesco form needs:

- **Teléfono del beneficiario**: `select` prefix (`0416`, `0426`, `0414`,
  `0424`, `0412`, `0422`) + remaining **7** digits.
- **Documento de Identificación**: `select` letter (`V`, `E`, `J`, `G`, `R`) +
  number digits.
- **Banco Destino**: `select` (option labels, not 4-digit codes).
- **Monto**: VES amount (comma decimal; possibly thousands separator).
- **Concepto**: free text (no existing field).

#### Proposed extension

Keep `PaymentInstruction` **provider-neutral and unchanged** (it is the raw
scrape). Add a pure mapping layer in a new module, e.g.
`app/domain/banesco.py`:

```python
@dataclass(frozen=True, slots=True)
class BanescoPaymentTarget:
    phone_prefix: str        # one of BANESCO_PHONE_PREFIXES
    phone_rest: str          # exactly 7 digits
    id_type: str             # one of BANESCO_ID_TYPES = ("V","E","J","G","R")
    id_digits: str           # 6-9 digits
    destination_bank_code: str
    destination_bank_option: str  # exact <option> label/value from the live page
    amount_ves: Decimal
    concept: str

def build_banesco_target(
    instruction: PaymentInstruction,
    *,
    bank_options: Mapping[str, str],
    concept: str,
) -> BanescoPaymentTarget | None:
    """Return None (fail closed) when any component is missing or inconsistent."""
```

Derivation rules:

| Banesco control | Source | Rule | Failure |
|---|---|---|---|
| Phone prefix | `instruction.phone` | `national_digits(phone)[:4]` must be in `BANESCO_PHONE_PREFIXES` | `payment_data_unavailable` / `unsupported_phone_prefix` |
| Phone rest | same | `national_digits(phone)[4:]`, exactly 7 digits | same |
| ID letter | `instruction.id_number` | uppercase first char; must be in `BANESCO_ID_TYPES`; **never default** | `payment_data_unavailable` / `id_type_missing` |
| ID digits | same | `cedula_digits()` (6–9 digits) | `payment_data_unavailable` |
| Destination bank | `instruction.bank_numeric_code` | `resolve_bank()` + `bank_options[numeric_code]` | `unsupported_destination_bank` |
| Amount | `instruction.amount_ves` | `format_ves_amount()`; use the page’s accepted separator variant | `payment_data_unavailable` |
| Concept | settings | fixed/configurable `BANESCO_PAYMENT_CONCEPT` (default `Pago`) | n/a |

**Prefix set mismatch to fix**: `_CEDULA_PREFIX_RE = ^[VEJGP]` and
`schemas._ID_PATTERN = ^([VEJGP])` accept `P` but **not `R`**; the Banesco select
offers `R` but not `P`. Extend the prefix set to `V/E/J/G/P/R` for normalization
and logging, and let the Banesco mapper accept only `V/E/J/G/R` (reject `P` with
`id_type_unsupported`). Update `logging._CEDULA_PATTERN` too, so `R-` documents
are masked in free text.

#### Does the Binance payload expose the letter?

**No, not reliably.**

- `BinanceC2COrder` (`app/infrastructure/clients/binance_client.py:82-129`)
  exposes `order_number`, `trade_type`, `asset`, `fiat`, `price`, `amount`,
  `total_price`, `status`, `counterparty_nickname`, `created_at`. There is **no
  document field**.
- `P2POrder.raw_payload` stores `detail.model_dump(mode="json")`, i.e. the
  normalized DTO, so the letter is not persisted from the API path either.
- The only source is the Binance order-page scrape (`PaymentInstruction.id_number`).
  The committed fixture `tests/fixtures/binance/order_view_banesco.html` shows
  `Cédula` = `12345678` **without a letter**, so the letter is not guaranteed.
  Real pages may render `V-12345678` or a separate field; this must be verified
  against recorded real DOM before claiming support.

**Risk and rule**: paying with the wrong document type can cause a bank
rejection; if the destination bank matches on digits only, it could misdirect a
payment. Therefore **fail closed** when the letter is absent:
`MANUAL_REVIEW` with `payment_data_unavailable` and reason
`id_type_missing`. Do **not** default to `V`.

Escape hatch (only if the operator explicitly accepts it): a
`BANESCO_DEFAULT_ID_TYPE` setting, audited, with a failure-closed default of
empty. The proposal should treat the default as a decision, not an assumption.

**Destination-bank interpretation**: “Banesco only first” most likely means
*Banesco Online is the payer rail*; the `Banco Destino` select still lists the
counterparty’s bank. Keep the two supported destination profiles (`0102`, `0134`)
and fail closed for any other code. If the intent is “destination bank must be
Banesco only”, add a hard gate `destination_bank_code == "0134"`. The proposal
must confirm which reading is authoritative.

---

### 4. Receipt model

After the final `Pagar` + Clave Dinámica, Banesco shows a receipt. The rail
returns:

```python
@dataclass(frozen=True, slots=True)
class PaymentReceipt:
    order_number: str
    bank_code: BankCode                 # BANESCO
    bank_reference: str                 # operation/reference number, verbatim
    amount_ves: Decimal                 # parsed, must equal the expected amount
    paid_at: datetime                   # bank timestamp, UTC; adapter clock fallback
    screenshot_path: str                # relative to RECEIPT_STORAGE_DIR
    screenshot_sha256: str              # hex digest
    raw_text: str                       # receipt text, never logged verbatim
    raw_html: str | None = None         # optional, never committed
```

Persistence:

- Screenshot to `data/receipts/{order_number}/{sha256}.png` (gitignored via
  `data/*`; create the directory with restrictive permissions). Store **path +
  SHA-256** in `p2p_orders.receipt_path` / `receipt_sha256`; do **not** store
  bytes in the DB.
- `bank_reference` reuses the existing UNIQUE `p2p_orders.bank_reference` and the
  `BankTransaction(direction=OUTGOING)` row; `bank_confirmed_at` is set when the
  receipt is captured. `raw_payload` stores `{"source": "banesco_online",
  "receipt_sha256": ...}` **without** raw text or PII.
- Idempotency: compute the hash before writing; if a receipt already exists for
  the order with a different hash, park (`receipt_conflict`) rather than
  overwrite. A repeated capture with the same hash is a no-op.
- Upload to Binance: pass the **file path** to `set_input_files` (or a temp file
  if the driver requires bytes). Never log the path’s contents or the receipt
  text.
- Retention: `RECEIPT_RETENTION_DAYS` (default 30); a cleanup task deletes old
  files. The path is not PII, but the image is.
- API responses and audit payloads expose only `receipt_sha256` and a masked
  reference (`mask_secret(reference, visible=4)`), never the image or raw text.

---

### 5. Telegram handoff

#### Library

`requirements.txt` has **no** Telegram library. `httpx==0.28.1` is present.

| Option | Verdict |
|---|---|
| **Raw Bot API over `httpx`** | **Recommended.** No new dependency; reuses `BaseAsyncHTTPClient`; `respx` tests; full control over update parsing/offset. |
| `python-telegram-bot` | Rejected for this change: new dependency, framework lifecycle, harder to fake at the port. |
| Webhook route | Viable alternative if a public TLS ingress already exists; use `X-Telegram-Bot-Api-Secret-Token` and dedupe `update_id`. |

Implementation shape: `app/infrastructure/clients/telegram_bot.py` exposing
`send_message`, `send_challenge`, `get_updates(offset, timeout)`, and
`answer_callback`. A domain `TelegramNotifier` protocol lives in
`app/domain/ports.py` so tests fake it.

#### Wait/timeout semantics

1. The Banesco adapter reaches the Clave Dinámica form and calls
   `request_clave(order_number, context) -> str` (async).
2. The flow creates a durable `operator_challenges` row **before** sending the
   Telegram message:
   - `order_id`, `channel="telegram"`, `chat_id`, `telegram_message_id`,
     `nonce_hash` (SHA-256 of a one-time nonce), `status=PENDING`,
     `expires_at`, `attempt_count=0`, `created_at`.
3. The notifier sends a message that **replies-to** a known bot message or asks
   the operator to reply to it:
   ```
   liquidVE | Orden <order_number> | Clave Dinámica
   Responde a este mensaje con los 6 dígitos.
   Nonce: <nonce>
   ```
   Do not include the amount, phone, cedula, or any receipt data.
4. The adapter waits on an `asyncio.Future`/queue resolved by the Telegram
   poller, with `asyncio.wait_for(..., TELEGRAM_CLAVE_TIMEOUT_SECONDS)`.
5. Correlation: `chat_id` must equal `TELEGRAM_OPERATOR_CHAT_ID`; the reply must
   be a reply to `telegram_message_id` (preferred) or carry the nonce; the body
   must match `^\d{6}$`. Store the challenge `telegram_message_id` so reply
   correlation is unambiguous.
6. On success: mark `ANSWERED`, resolve the future, submit the key **once**.
   Never store the key; never echo it; never log it.
7. On timeout: mark `EXPIRED`; abort the browser page; `SENDING_PAYMENT →
   MANUAL_REVIEW` with `clave_dinamica_timeout`; notify the operator.
8. On malformed reply (not 6 digits): reply once with “send exactly 6 digits”;
   do not consume; count as a clarification, not an attempt.
9. On ambiguous reply (no reply-to and more than one pending challenge): ask the
   operator to reply to the specific message; never guess. Prefer serializing
   payments (`BANESCO_MAX_CONCURRENT_PAYMENTS=1`) so ambiguity is impossible.
10. On wrong key (Banesco rejects): increment `attempt_count`; if
    `attempt_count > BANESCO_CLAVE_RETRY_LIMIT` (default **0**, fail closed),
    `MANUAL_REVIEW` with `clave_dinamica_rejected`. Never retry indefinitely;
    account lockout is a real cost.

#### Persistence across restart

The `operator_challenges` row is the durable record. On startup:

- every `PENDING` challenge whose `expires_at` has passed → `EXPIRED`;
- every order in `SENDING_PAYMENT` with a challenge row but no live browser
  session → `MANUAL_REVIEW` with `payment_interrupted`.

The browser page and the `asyncio.Future` cannot survive a restart. **By design,
a restart aborts the payment; it is never auto-resumed.** The operator confirms
the bank state manually. This is the same fail-closed philosophy as the SMS
“unknown fate” rule.

#### Telegram security

- `TELEGRAM_BOT_TOKEN` as `SecretStr`; never logged; never in artifacts.
- Only `TELEGRAM_OPERATOR_CHAT_ID` is accepted; all other chats are ignored and
  optionally logged as a rejected update.
- Ignore edits/channel posts/messages without a `from` user (or verify the
  `from.id` if configured).
- Use `protect_content` where available; do not store the key.
- The bot token is a full-control credential; rotate on suspicion.

---

### 6. Login mechanics (Banesco Online)

Target URL: `https://banesconline.com/mantis/Website/Login.aspx`.

#### ASP.NET WebForms

- The login, questions, and payment pages are WebForms postbacks with hidden
  `__VIEWSTATE`, `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION`.
- **Use Playwright interaction, not raw HTTP.** Build the flow with `fill`,
  `select_option`, and `click`; the browser carries the hidden fields. Manually
  POSTing would require re-reading and re-submitting the hidden fields per step
  and is brittle.
- After every postback: `wait_for_load_state("domcontentloaded")` plus an
  explicit wait for the next known element. Avoid relying on `networkidle`;
  ASP.NET pages may keep connections open. Use `expect_navigation`/`wait_for_url`
  where deterministic.
- Generated ASP.NET IDs are unstable. Prefer accessible names, labels, roles,
  and relative text selectors; pin them in page objects and fixture contracts.

#### Step-by-step contract

1. `goto(Login.aspx)`; wait for `Usuario`.
2. Fill `Usuario` → click `Aceptar`.
3. **Security questions**: exactly **2 of 4** displayed, randomized. The adapter
   must read the displayed question text, normalize accents/case, map it to a
   known `SecurityQuestion` key, and fetch the answer from `SecretStr` settings.
   Never assume order or position. If fewer/more than 2, a question is repeated,
   or a question is unknown → `banesco_layout_unexpected` (fail closed).
4. Fill each answer → submit.
5. Fill `Clave` (the online-banking password) → submit. Never log it.
6. **Advertisement modal**: detect a dialog/overlay and dismiss it via the X
   (`aria-label`/text `×`/`Cerrar`). If the modal is present but cannot be
   closed, or it blocks the menu → `banesco_ad_modal_blocked` (fail closed).
7. Menu **Pago Móvil → Enviar pago**; wait for the form.
8. Fill the form:
   - `Teléfono del beneficiario`: select prefix + 7 digits.
   - `Documento de Identificación`: select letter + digits.
   - `Banco Destino`: select exact option.
   - `Monto`: VES amount.
   - `Concepto`: configured neutral text.
9. Click **Pagar** → wait for the confirmation screen → click **Pagar** again.
10. **Clave Dinámica** form appears → hand off to Telegram (#5).
11. Enter the key once → receipt appears → capture (#4).

#### Fail-closed failure modes

| Condition | Detection | Typed error / reason |
|---|---|---|
| Captcha / “no soy un robot” / unexpected OTP | Known captcha element/text | `banesco_captcha_detected` → `MANUAL_REVIEW` |
| Unexpected layout / missing field | Selector timeout or missing label | `banesco_layout_unexpected` → `MANUAL_REVIEW` |
| Locked account / too many attempts | Bank error banner/text | `banesco_account_locked` → `MANUAL_REVIEW` + alert; **no retry** |
| Maintenance / service unavailable | Error page/text | `banesco_maintenance` → `MANUAL_REVIEW` |
| Session expired / redirect to login | URL/DOM change | `banesco_session_expired` → `MANUAL_REVIEW` if after the final `Pagar`; safe re-login only before it |
| Payment rejected (funds/limit) | Error banner | `banesco_payment_rejected` → `MANUAL_REVIEW` |
| Unknown outcome after `Pagar` | No confirmation/receipt | `banesco_payment_outcome_unknown` → `MANUAL_REVIEW`; **never re-click** |

**No automatic retry of the login or the payment.** The only allowed retry is a
single, explicit Clave Dinámica re-prompt controlled by
`BANESCO_CLAVE_RETRY_LIMIT` (default 0). The rail is invoked at most once per
order via the durable `payment_attempt_id`.

---

### 7. Release watcher + appeal

#### Sequence after the receipt

1. Capture the receipt (#4), commit `bank_reference` + `bank_confirmed_at` +
   the `OUTGOING` `BankTransaction`.
2. Upload the receipt image to the Binance order chat and confirm payment
   (“Transferido, notificar al vendedor”). If the upload or confirmation fails,
   **do not** transition to `AWAITING_RELEASE`; park in `MANUAL_REVIEW` with
   `evidence_upload_failed` (no appeal without evidence).
3. On success: `SENDING_PAYMENT → AWAITING_RELEASE`, set
   `release_deadline_at = now + RELEASE_TIMEOUT_SECONDS`.

#### Watcher

- A worker tick (reuse `BUY_WORKER_ENABLED` or a dedicated
  `RELEASE_WATCHER_ENABLED`) selects `AWAITING_RELEASE` orders and calls
  `BinanceC2CClient.get_order_detail(order_number)` every
  `RELEASE_POLL_INTERVAL_SECONDS`.
- Map external status conservatively:
  - `COMPLETED` → release detected → `AWAITING_RELEASE → COMPLETED`.
  - `APPEALING` → already appealed (e.g. the counterparty filed first) → record
    and stay `APPEALED` (do **not** file a second appeal).
  - `CANCELLED` / `EXPIRED` → fail closed to `MANUAL_REVIEW` with
    `external_order_cancelled` (after payment this is a serious anomaly).
  - `PENDING_PAYMENT` / `PAID` → still waiting.
  - Unknown status → `MANUAL_REVIEW` with `external_status_unknown`.
- The exact status vocabulary must be verified against real payloads; the
  watcher must never guess.

#### Timeout policy

- `RELEASE_TIMEOUT_SECONDS` (default e.g. 900, or the Binance payment window +
  grace). When `now > release_deadline_at`:
  - `RELEASE_APPEAL_MODE=automatic` (operator decision): `AWAITING_RELEASE →
    APPEALED` + file the appeal.
  - `RELEASE_APPEAL_MODE=manual_review`: `AWAITING_RELEASE → MANUAL_REVIEW` with
    `release_timeout`; an operator endpoint files the appeal.
- Before filing, re-check the Binance status; if the counterparty released,
  transition to `COMPLETED` and skip the appeal (`APPEAL_SKIPPED` audit).

#### Appeal mechanics

The Binance order page exposes an appeal action (button/localized label). The
adapter:

1. Opens the order page.
2. Clicks the appeal control.
3. Selects the reason that matches “paid but crypto not received” (exact label
   to be recorded from the live UI; fail closed on unknown).
4. Uploads the receipt image(s) and optionally a short text.
5. Submits and waits for the confirmation banner / `APPEALING` status.

**At-most-once guarantee**:

- Add `p2p_orders.appeal_attempt_id` (UNIQUE, nullable) and `appeal_filed_at`,
  or a dedicated `external_actions(order_id, action, idempotency_key UNIQUE,
  status, attempted_at, completed_at, payload)` table.
- Commit `appeal_attempt_id = f"{order_number}:appeal"` and
  `status=APPEALED` **before** clicking submit (the same “persist the intent,
  then act” rule as B2).
- If the click/submit fails or the outcome is unknown, leave `APPEALED` with
  `appeal_status="unknown"` and require an operator to verify. **Never auto-retry
  an appeal.**
- If Binance already shows `APPEALING`, record and do not click.
- Appeal is consequential (it can freeze funds and affect account reputation):
  gate it with `RELEASE_APPEAL_MODE` and a kill switch. Auto-appeal is the
  operator’s explicit decision; document it and keep the manual mode available.

---

### 8. Credentials / secrets

#### New settings (names only — no values)

Banesco Online:

- `BANESCO_ONLINE_ENABLED: bool = False`
- `BANESCO_ONLINE_LOGIN_URL: str = "https://banesconline.com/mantis/Website/Login.aspx"`
- `BANESCO_ONLINE_USER: SecretStr`
- `BANESCO_ONLINE_PASSWORD: SecretStr`
- `BANESCO_SECURITY_ANSWER_HOBBIE: SecretStr`
- `BANESCO_SECURITY_ANSWER_CHILDHOOD_HERO: SecretStr`
- `BANESCO_SECURITY_ANSWER_FAVOURITE_DESSERT: SecretStr`
- `BANESCO_SECURITY_ANSWER_COUNTRY_TO_VISIT: SecretStr`
- `BANESCO_BROWSER_HEADLESS: bool = True`
- `BANESCO_BROWSER_TIMEOUT_MS: int = 30000`
- `BANESCO_MAX_CONCURRENT_PAYMENTS: int = 1`
- `BANESCO_CLAVE_RETRY_LIMIT: int = 0`
- `BANESCO_PAYMENT_CONCEPT: str = "Pago"`
- `BANESCO_DEFAULT_ID_TYPE: str = ""` (empty = fail closed; do not default to `V`)

Telegram:

- `TELEGRAM_ENABLED: bool = False`
- `TELEGRAM_BOT_TOKEN: SecretStr`
- `TELEGRAM_OPERATOR_CHAT_ID: int`
- `TELEGRAM_API_BASE_URL: str = "https://api.telegram.org"`
- `TELEGRAM_CLAVE_TIMEOUT_SECONDS: int = 300`
- `TELEGRAM_POLL_TIMEOUT_SECONDS: int = 30`
- `TELEGRAM_MAX_CLARIFICATIONS: int = 2`

Receipt / release / appeal:

- `RECEIPT_STORAGE_DIR: str = "./data/receipts"`
- `RECEIPT_RETENTION_DAYS: int = 30`
- `RELEASE_WATCHER_ENABLED: bool = False`
- `RELEASE_POLL_INTERVAL_SECONDS: int = 30`
- `RELEASE_TIMEOUT_SECONDS: int = 900`
- `RELEASE_APPEAL_MODE: Literal["automatic", "manual_review"] = "automatic"`

Rail switch:

- `BUY_PAYMENT_RAIL: Literal["sms", "banesco"] = "banesco"`

Renames (backward-compatible aliases for one release):

- `DAILY_SMS_LIMIT_VES` → `DAILY_PAYOUT_LIMIT_VES`
- `SMS_CONFIRMATION_TIMEOUT_SECONDS` → `PAYMENT_CONFIRMATION_TIMEOUT_SECONDS`
- ledger channel `VES_SMS` → `VES_PAYOUT`

#### Startup validation

Extend `Settings._validate_consistency`:

- If `banesco_online_enabled` or `buy_payment_rail == "banesco"`:
  - require non-empty `BANESCO_ONLINE_USER`, `BANESCO_ONLINE_PASSWORD`, and all
    four security answers;
  - require `BANESCO_ONLINE_LOGIN_URL` to be an absolute `https` URL (use
    `_ensure_secure_base_url` against the host);
  - require `BANESCO_BROWSER_TIMEOUT_MS > 0`,
    `BANESCO_MAX_CONCURRENT_PAYMENTS >= 1`.
- If `telegram_enabled` or `banesco_online_enabled`:
  - require non-empty `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OPERATOR_CHAT_ID > 0`;
  - require `TELEGRAM_CLAVE_TIMEOUT_SECONDS > 0`.
- If `release_watcher_enabled`:
  - require `RELEASE_POLL_INTERVAL_SECONDS > 0` and `RELEASE_TIMEOUT_SECONDS > 0`.
- If `release_appeal_mode == "automatic"`: require the watcher to be enabled and
  the Binance browser to be enabled (the appeal is a browser action).
- Add booleans/timeouts to `base_settings_summary()`; **never** add secrets.
- Add a boot guard that refuses `buy_payment_rail == "banesco"` without
  `telegram_enabled` (the Clave Dinámica step cannot complete unattended).

#### Why values can never live in artifacts

- A bank credential in git/Engram/CI logs enables unauthorized payments and
  account takeover.
- The repository is shared and auditable; the archive is permanent.
- Tests must use fake values that are clearly not real (e.g. `test-banesco-user`),
  and fixtures must be sanitized.
- Logs already mask `secret`/`password`/`token` keys and cedula/phone patterns;
  extend the mask list and add a regression test that a known canary value never
  appears in a rendered log/audit payload.
- `.env` stays gitignored; `.env.example` contains only placeholders.

---

### 9. Slicing (each unit < 800 changed lines)

| Slice | Start | Finish | Verification | Rollback | Est. lines |
|---|---|---|---|---|---|
| **S1 — Domain/status/config groundwork (FIRST AUTONOMOUS SLICE)** | Current enums/config/DTOs | `SENDING_PAYMENT` + `AWAITING_RELEASE` + `APPEALED`; data migration; `BanescoPaymentTarget` pure mapper; new setting **names** with defaults; typed exceptions; updated tests | `pytest` green; `ruff`; migration upgrade/downgrade; no behavior change to Flow A | Revert slice; data migration downgrade restores `SENDING_SMS` | ~650 |
| S2 — Pure Banesco page contracts | No Banesco parser | `app/domain/banesco.py` constants/validation + page-object text parsers (questions, modal, form labels, confirmation, clave, receipt) | Fixture-based parser tests (`tests/fixtures/banesco/*.html`); no browser import | Revert; no runtime wiring | ~500 |
| S3 — Banesco login + questions + ad modal adapter | Page contracts | Playwright adapter `__aenter__`/login/questions/modal; lazy import; typed fail-closed errors | Fake page/context tests; subprocess asserts `playwright` not imported; opt-in live smoke skipped | Revert; rail still `sms` | ~550 |
| S4 — Banesco payment form + confirmation + clave handoff | Login works | Fill form, first/second `Pagar`, detect Clave Dinámica form, call the notifier port | Fake page tests for each control and failure mode; fake notifier | Revert; no real payment path | ~600 |
| S5 — Receipt capture + storage | Clave handoff | Screenshot + text + reference + amount verification; `data/receipts` storage; hash; DB columns | Fake receipt page; storage tests; PII/log assertions | Revert; no receipt writes | ~450 |
| S6 — Telegram adapter + challenge persistence | Notifier port | `TelegramBotClient` (httpx), long-poll loop, `operator_challenges` table, correlation/timeout/ambiguity | `respx` tests; fake notifier; `freezegun` expiry; restart/expiry test | Revert; rail disabled | ~500 |
| S7 — Buy flow rewiring + Binance evidence upload | SMS dispatch still active | `BUY_PAYMENT_RAIL` switch; durable `payment_attempt_id`; rail invocation; receipt persistence; upload + notify; `AWAITING_RELEASE` | Fake rail/notifier/browser; state/audit assertions; resume guard | Set `BUY_PAYMENT_RAIL=sms`; no new path | ~700 |
| S8 — Release watcher + appeal | `AWAITING_RELEASE` exists | Watcher tick; status mapping; timeout; at-most-once appeal; `APPEALED` transitions; operator manual mode | `FakeBinance` status sequence; `freezegun`; at-most-once/concurrency tests | Disable watcher; orders stay `AWAITING_RELEASE` | ~650 |
| S9 — Decommission the SMS BUY path | Rail switch stable | Remove `/webhooks/sms`, `sms_dispatch`, inbound confirmation, SMS poller; keep `cachicamo`; update health/docs | Full suite; no dead imports; health probes; grep no SMS BUY refs | Revert PR; rail switch still available | ~450 (deletion-heavy; may split S9a/S9b) |
| S10 — Operator/observability polish | Core flow works | `orders.py` outcomes/guards; appeal/reconcile endpoint; audit dashboard queries; README/`.env.example` | API tests; PII-free responses; docs | Revert; core flow unaffected | ~300 |

**Review-budget notes**:

- The rename slice touches ~60 refs; if it exceeds 800 changed lines, split
  “rename + migration” (S1a) from “Banesco DTO/settings” (S1b).
- The decommission slice is deletion-heavy; keep it separate from behavior
  changes so a reviewer can read it as a pure removal.
- Each slice starts with RED tests (strict TDD), then GREEN, then refactor.
- `auto-chain` stacked-to-main: S1 → S2 → …; each PR targets the previous slice
  until clean diffs.

---

### 10. Test strategy: browser automation without a browser

#### Page-object seams

Mirror the proven `binance_browser.py` split:

1. **Pure layer** (`app/domain/banesco.py` + page-text parsers): question
   normalization/mapping, modal detection, form label extraction, confirmation
   detection, clave-form detection, receipt parsing. Standard library only; no
   Playwright import.
2. **Page objects** (`app/infrastructure/clients/banesco_pages.py`): thin classes
   over a `PageLike` protocol (`goto`, `fill`, `click`, `select_option`,
   `get_by_role`, `get_by_label`, `locator`, `content`, `screenshot`,
   `wait_for_selector`, `url`). They expose semantic actions
   (`login(user, password)`, `answer_questions(mapping)`, `dismiss_ad()`,
   `fill_payee(target)`, `click_pay()`, `submit_clave(key)`, `capture_receipt()`).
3. **Driver** (`app/infrastructure/clients/banesco_browser.py`): lazily imports
   `playwright.async_api` inside `__aenter__`; creates the context; delegates to
   page objects. Importing the module never imports Playwright (assert in-process
   and in a subprocess, like `tests/test_browser_adapter.py`).

Tests inject a `FakePage`/`FakeContext` that serves recorded HTML and records
actions. This covers selector contracts, postback waits, modal dismissal, form
values, double-`Pagar` ordering, and receipt capture without Chromium.

#### Recorded DOM fixtures

Add `tests/fixtures/banesco/` with sanitized snapshots:

- `login.html`
- `security_questions_a.html`, `security_questions_b.html` (different question
  order/subset; exactly 2 of 4)
- `ad_modal.html`
- `payment_form.html` (all select options, including the 6 phone prefixes and
  the 5 ID letters; bank options)
- `confirmation.html`
- `clave_form.html`
- `receipt.html`
- `error_captcha.html`, `error_locked.html`, `error_maintenance.html`,
  `error_session_expired.html`

Rules:

- **Sanitize every fixture**: fake names, phones, cedulas, references, and
  amounts; no real credentials or real counterparty data.
- Add a fixture-hygiene test that scans fixtures for known canary credentials
  and real-looking sensitive values, and fails if found.
- Pin selectors to accessible names/roles/labels; avoid generated ASP.NET IDs.
- The fixtures are the contract; a live DOM change requires a fixture update and
  a test change (reviewable DOM drift).

#### Fake Telegram

- `FakeTelegram` implementing the `TelegramNotifier` port: record sent
  challenges, script a reply/timeout/wrong-chat/malformed/ambiguous response.
- `respx` tests for `TelegramBotClient`: `sendMessage` payload (chat id, text,
  reply markup, `protect_content`), `getUpdates` offset handling, 429/5xx
  behavior, token never logged, update dedupe by `update_id`.
- `freezegun` for `expires_at` and timeout.
- Persistence test: create a `PENDING` challenge, simulate restart/expiry,
  assert it is `EXPIRED` and the order is parked with `payment_interrupted` or
  `clave_dinamica_timeout`.

#### Release/appeal fakes

- `FakeBinance` scripts a status sequence per order:
  `PAID → PAID → COMPLETED`, `PAID → APPEALING`, unknown, cancelled.
- `FakeOrderActions` (new Binance browser port) records `upload_evidence` and
  `file_appeal` calls; asserts at-most-once and the ordering
  upload → notify → awaiting release → appeal.
- `freezegun` advances past `release_deadline_at`; assert exactly one appeal
  attempt, durable `appeal_attempt_id`, and no second appeal on a retry.

#### Live-only verification (opt-in, never CI)

Marked `@pytest.mark.integration` and gated on an environment variable, following
`LIQUIDVE_BROWSER_TESTS`:

- `LIQUIDVE_BANESCO_TESTS=1`: login + security questions + ad modal + navigate
  to the Pago Móvil form **without submitting a payment** (dry run). Requires a
  real `.env`; never run in CI.
- `LIQUIDVE_BANESCO_PAYMENT_TESTS=1`: a full low-value real payment in a
  controlled window, manually supervised; never CI.
- Live checks must assert no credential is printed and must not commit
  screenshots/HTML.
- What can **only** be verified live: actual captcha/anti-bot behavior, the real
  ad modal, the exact bank option labels, the exact `Pagar`/confirmation DOM,
  the Clave Dinámica form, the receipt DOM, and the Binance appeal UI labels.

#### Default-run invariants

- No network, no Chromium, no Playwright import, no real `.env`.
- `pytest -q` remains fast (baseline ~15s) and deterministic.
- `ruff check app tests` clean.
- Every new module has Google-style docstrings and `from __future__ import
  annotations`, matching the repo convention.

---

### 11. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Bank ToS / personal-account lockout** | Account frozen; funds inaccessible; automation stops | Operator accepted the risk and documented it; low volume; human Clave Dinámica; fail closed; no login/payment retries; kill switch (`BUY_PAYMENT_RAIL`, `BANESCO_ONLINE_ENABLED`). |
| **DOM drift** | Silent wrong field/amount or hard failure | Page objects + recorded fixtures + fail-closed typed errors + opt-in live smoke; never guess a selector. |
| **Captcha / anti-bot** | Login blocked | Detect and park (`banesco_captcha_detected`); do not bypass; operator completes manually. |
| **Credential compromise** | Fraudulent payments; account takeover | Secrets only in `.env`/`SecretStr`; never in artifacts/logs/tests/Engram; rotate; least-privilege; document the leak rule. |
| **Wrong document letter** | Bank rejection or misdirected payment | Fail closed if the letter is absent (`id_type_missing`); never default to `V`; optional audited override setting. |
| **Double payment** | Irreversible loss | Durable `payment_attempt_id`; `SENDING_PAYMENT → COMPLETED` removed; guard `MANUAL_REVIEW → PENDING` to pre-payment reasons only; no auto-resume. |
| **Restart mid-payment** | Unknown bank state | Never auto-resume; `MANUAL_REVIEW/payment_interrupted`; challenge rows expired on startup. |
| **Receipt PII in screenshots** | Privacy/legal leak | Gitignored `data/receipts`, restricted permissions, retention, never log/Engram/API; SHA-256 only in DB/audit. |
| **Appeal abuse / duplicate appeal** | Funds frozen; account reputation | At-most-once durable attempt identity; status re-check; manual mode; kill switch; audit. |
| **Appeal while release happens** | Unnecessary appeal | Re-check Binance status immediately before filing; `APPEAL_SKIPPED` audit. |
| **Telegram key leakage** | Account access | Never store/echo/log the key; `protect_content`; operator-only chat; delete/avoid retention where possible. |
| **Telegram reply mis-correlation** | Wrong key submitted to the wrong payment | Serialize payments (1 concurrent), reply-to correlation, nonce, explicit clarification, never guess. |
| **Receipt amount mismatch** | Wrong payment recorded | Parse and compare to the expected `Decimal`; park `receipt_amount_mismatch`; never mark paid. |
| **Evidence upload fails** | Cannot appeal/prove payment | Park `evidence_upload_failed`; no `AWAITING_RELEASE`/appeal without evidence. |
| **Binance status vocabulary drift** | Wrong release/appeal decision | Conservative mapping; unknown → `MANUAL_REVIEW`; verify against real payloads. |
| **Log PII leakage** | Compliance/privacy | Extend masking (`security_answer`, `clave`, `R-` cedula); regression test with canaries; never log receipt text/bytes. |
| **Scope creep to multi-bank** | Review budget/late delivery | Banesco rail only; keep `bank-sms` dormant; destination-bank support remains the existing two profiles. |

---

## Open decisions for the proposal phase

1. **Destination bank interpretation**: “Banesco only first” = payer rail only
   (recommended: keep `0102`/`0134` destination profiles) or destination bank
   must be `0134` only?
2. **ID letter availability**: confirm against a real recorded Binance DOM
   whether the document letter is present. If not, is the operator willing to
   supply it via an operator endpoint, or accept a parked order?
3. **Telegram transport**: long-poll (recommended, no public ingress) vs webhook
   (needs TLS + secret token).
4. **Appeal default**: `RELEASE_APPEAL_MODE=automatic` (operator decision) vs
   `manual_review` for the first release. Recommend automatic per the operator,
   with a kill switch.
5. **Ledger rename**: widen `DailyVolumeLedger.currency` to `String(16)` and use
   `VES_PAYOUT`, or keep `String(8)` and use `VES_PAY`.
6. **SMS rail retention**: keep the `BUY_PAYMENT_RAIL=sms` switch for one release
   (recommended) or remove the SMS BUY path in this change?
7. **Receipt retention**: `RECEIPT_RETENTION_DAYS` default 30 days; confirm.
8. **Concept text**: exact `Concepto` value accepted by Banesco (neutral
   `Pago` vs an order-derived value; do not include PII).

---

## Key Learnings

1. `SENDING_SMS` is stored as a plain `VARCHAR(32)` with no CHECK constraint, so the rename needs a data migration only, not a DDL change.
2. The Banesco form needs two controls per field, but `PaymentInstruction` currently exposes only one phone string and one cedula string.
3. The Binance C2C DTO does not expose the counterparty document letter, and the committed order fixture omits it, so the letter must fail closed rather than default to `V`.
4. `DailyVolumeLedger.currency` is `String(8)`, which cannot hold `VES_PAYOUT`; the ledger key column must be widened or the channel name shortened.
5. `requirements.txt` has no Telegram library, so the raw Bot API over the existing `httpx` dependency is the lowest-risk handoff adapter.

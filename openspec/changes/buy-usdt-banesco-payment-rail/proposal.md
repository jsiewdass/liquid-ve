# Proposal: BUY payment rail via Banesco Online + Telegram Clave Dinámica

- **Change**: `buy-usdt-banesco-payment-rail`
- **Phase**: propose
- **Inputs**: `exploration.md` (gap analysis), archived SMS change (read-only), `openspec/config.yaml`, current `buy-flow`/`bank-sms` specs
- **Delivery**: `auto-chain` stacked-to-main, 800-line review budget, strict TDD (`.\.venv\Scripts\python.exe -m pytest -q`)
- **Baseline**: 263 passed, 2 skipped; ruff clean
- **Supersedes**: the SMS rail archived at `openspec/changes/archive/2026-11-23-buy-usdt-sms-payment-flow/` (never modified)

## Intent

The SMS rail cannot satisfy Binance's evidence requirement. It sends a bank command
over an Android gateway, then waits for a human-readable bank SMS; it produces no
cryptographic or visual proof of the transfer, cannot attach a receipt to the order
chat, and stops at a premature close instead of tracking the counterparty release.
As a result, a BUY payment cannot be defended in a dispute and the release/appeal
tail does not exist.

Replacing the rail with Banesco Online automation (Playwright) plus a Telegram
Clave Dinámica handoff gives the engine a verifiable payment artifact: a captured
receipt (image, reference, amount, timestamp) uploaded to the Binance order chat,
a durable `AWAITING_RELEASE` watcher, and an at-most-once appeal when release
times out. This unlocks the supply side: automated USDT acquisition with
auditable proof rather than an unverifiable SMS assertion.

## Scope

### In Scope
- Banesco Online login: `Usuario` → `Aceptar` → 2-of-4 randomized security questions (text-detected, answered from `.env`) → `Clave` → ad-modal dismissal.
- `Pago Móvil → Enviar pago` with two-control fields (phone prefix + rest; document letter + digits), destination bank, amount, concept.
- Double `Pagar` confirmation, pause at Clave Dinámica, Telegram handoff, single key submission.
- Receipt capture and persistence (screenshot + SHA-256 + reference + amount + timestamp).
- Evidence upload to Binance, payment notification, `AWAITING_RELEASE` watcher, automatic appeal with kill switch.
- Status model rename/additions, `VES_PAY` ledger channel, migrations, operator outcomes, health probes, logging masks, tests/fixtures, docs.
- Eventual decommission of the SMS BUY path (last slice).

### Out of Scope
- Multi-bank payer support (only Banesco Online is automated).
- Order creation/detection changes beyond the payment tail; Flow A (sell) behavior.
- Android-side work; the SMS gateway remains for Flow A compatibility but has no BUY caller.
- Crypto settlement APIs, external market feeds, operator UI, multi-operator routing.
- Destination banks beyond the existing supported profiles (`0102`, `0134`) in this change.
- Any modification of archived change folders.

## Capabilities

### New Capabilities
- `banesco-payment`: Banesco Online page contracts, login/question/modal handling, two-control payment mapping, Clave Dinámica handoff, receipt capture, fail-closed error taxonomy.
- `telegram-handoff`: raw Bot API client over `httpx`, durable operator challenges, correlation (chat + order + nonce), timeout, restart semantics, key handling.
- `release-appeal`: `AWAITING_RELEASE` watcher, Binance status mapping, timeout policy, at-most-once appeal with durable attempt identity and kill switch.

### Modified Capabilities
- `buy-flow`: R-BUY-002/003/004/006/007/008/009/010 change to the rail/receipt/release model; `SENDING_SMS` references become `SENDING_PAYMENT`; `SENDING_PAYMENT → COMPLETED` removed; resume guard tightened; R-BUY-011 (direction guard) preserved.
- `bank-sms`: registry/normalization/`resolve_bank` retained as pure mapping helpers; command rendering and bank-reply parsing are removed when the decommission slice lands (delta `REMOVED`), not before.

## Decisions

| # | Decision | Rationale | Risk if wrong |
|---|---|---|---|
| **D1** | Destination bank: map the order's 4-digit code to an exact, reviewed `Banco Destino` option via a versioned label/value map captured in fixtures. Accept only `0102`/`0134`. Zero matches, multiple matches, or a changed option label → `MANUAL_REVIEW` (`unsupported_destination_bank` / `destination_bank_ambiguous`); never fuzzy-match or guess. "Banesco only" means Banesco is the payer rail, not the destination. | Bank dropdowns list names, not codes; guessing can pay the wrong bank. | A wrong mapping could misdirect real money; a stale map blocks payments (safe). |
| **D2** | Document letter: extend the Playwright order-page parser to capture `V/E/J/G/R` when the page exposes it (prefixed value or separate field). When absent, open a Telegram operator-override challenge; validate the reply against `V/E/J/G/R`. Timeout/refusal → `MANUAL_REVIEW` (`id_type_missing`). Never default to `V`; `P` is normalized by the domain but rejected by the Banesco mapper (`id_type_unsupported`). | The C2C DTO omits the letter and the committed fixture omits it. A wrong letter can reject or misdirect a payment. | Blocked orders and operator load (safe); a silent `V` default could lose money. |
| **D3** | Telegram transport: raw Bot API over the existing `httpx` with long polling. No new dependency. Correlation: configured operator `chat_id` only + reply-to challenge message + order number in text + one-time nonce hashed at rest; body must be exactly 6 digits; at most one pending challenge (`BANESCO_MAX_CONCURRENT_PAYMENTS=1`). Timeout default 300 s. Restart expires pending challenges and parks the order `MANUAL_REVIEW`/`payment_interrupted`; never auto-resume a payment. | `httpx` is already present and testable via `respx`; a framework adds dependency and lifecycle risk. | A mis-correlated key could authorize the wrong payment; serialization + reply-to + nonce make ambiguity impossible rather than guessed. |
| **D4** | Appeal is **automatic** in steady state, gated by `RELEASE_APPEAL_MODE=auto\|alert\|off`. `auto` files at most once after a pre-click status re-check; `alert` notifies the operator and parks; `off` disables appeal entirely. Persist a UNIQUE durable `appeal_attempt_id` and `APPEALED` **before** the submit click; an unknown outcome is never auto-retried. | The operator explicitly chose automatic; appeals are the only recovery when release times out. | **Consequential risk**: an unnecessary or duplicate appeal can freeze funds and damage account reputation. Mitigated by re-check, at-most-once identity, audit, and `alert`/`off` as emergency modes. |
| **D5** | Ledger channel: rename `VES_SMS` → `VES_PAY` (7 chars, fits existing `String(8)`; no widening). Keep double-cap semantics: a dispatched BUY reserves `VES` **and** `VES_PAY`. Migrate existing dev rows `daily_volume_ledgers.currency` to `VES_PAY`; if a `VES_PAY` row already exists, aggregate totals then delete the stale row. Downgrade restores `VES_SMS`. | Avoids an unnecessary column migration and keeps the existing locking/limit accounting. | A split ledger could under- or over-count the daily cap; the migration aggregates atomically and tests cover rollover. |
| **D6** | Status model: rename `SENDING_SMS` → `SENDING_PAYMENT`; add `AWAITING_RELEASE` and `APPEALED`. Edges: `PENDING→{PAYMENT_DETECTED,FAILED,MANUAL_REVIEW}`; `PAYMENT_DETECTED→{VERIFYING_CACHICAMO,MANUAL_REVIEW,FAILED}`; `VERIFYING_CACHICAMO→{SENDING_PAYMENT,COMPLETED,MANUAL_REVIEW,FAILED}`; `SENDING_PAYMENT→{AWAITING_RELEASE,MANUAL_REVIEW,FAILED}`; `AWAITING_RELEASE→{COMPLETED,APPEALED,MANUAL_REVIEW,FAILED}`; `APPEALED→{COMPLETED,FAILED,MANUAL_REVIEW}`; `COMPLETED`/`FAILED` terminal; `MANUAL_REVIEW→{PENDING,FAILED}`, where `PENDING` is allowed **only for pre-payment reasons** (approval, data unavailable, risk blocks), never for `clave_dinamica_timeout`, `payment_outcome_unknown`, `receipt_amount_mismatch`, or `release_timeout`. Data-only migration updates `p2p_orders.status`, `audit_logs.from_status`, `audit_logs.to_status`; historical audit payload strings are not rewritten. | Completion must mean release, not "receipt captured"; the direct `SENDING_PAYMENT → COMPLETED` edge would recreate the SMS lie. Columns are `VARCHAR` with no CHECK, so only data changes are needed. | Re-enabling a direct completion edge or a payment-park resume could produce a double payment (high impact); guarded by tests and service checks. |
| **D7** | SMS rail: **explicit decommission**, no steady-state `BUY_PAYMENT_RAIL=sms` fallback. The new rail lands first; old modules become uncalled, then are deleted in the final slice (S9). Recovery is from git history and the archived OpenSpec change. A runtime fallback that can pay without proof is a hazard, not a rollback. | A fallback would silently reintroduce an unverifiable payment path. | If the new rail is unavailable, BUY halts (safe) until a redeploy of a prior build is deliberately chosen; no silent wrong-rail payment. |
| **D8** | Receipts: store under gitignored `data/receipts/{order_number}/{sha256}.png`; persist path + SHA-256 + reference + amount + timestamp; never store raw image bytes in the DB, never log/audit/Engram raw receipt text, HTML, or PII. API/audit expose only the hash and a masked reference. Retention default 30 days. | File storage avoids DB bloat and keeps PII out of backups; the hash is the dedupe anchor. | Unlogged evidence would weaken disputes; leaked receipt text would expose counterparty PII. |
| **D9** | Bank ToS/account risk is **accepted and documented**. Mitigations: dedicated account recommendation, minimal operating balance, credential rotation, low volume, no login/payment retries, captcha/lockout → park, kill switches. | The operator owns the account and accepts automation risk; the bank offers no public Pago Móvil API. | Account lockout or closure is an operational loss the operator accepted; no technical control removes it. |

## Approach

Mirror the proven `BinanceBrowser` split: a pure domain layer for mapping/parsing, thin page objects over a `PageLike` protocol, and a driver that lazily imports Playwright. The service replaces SMS dispatch with a `PaymentRail` port invocation, persists the attempt identity before any browser action, captures the receipt, uploads evidence, then enters `AWAITING_RELEASE`. Every unknown outcome fails closed to `MANUAL_REVIEW`; no path retries a payment, resumes a payment across restarts, or appeals without a durable attempt identity. Money remains `Decimal` throughout; all new external access stays behind adapters under `app/infrastructure/clients`.

## Affected Areas

| Area | Impact | Reference |
|---|---|---|
| `app/domain/enums.py` | Modified | `SENDING_SMS` at `:26`; transitions `:52-73`; `AuditActor` `:220-227`; `AuditEvent` `:230-243`. Add statuses/events/actors. |
| `app/domain/ports.py` | Modified | `PaymentInstruction` `:23-68`; `BinanceBrowser` `:71-102`. Add rail/notifier/receipt/evidence/appeal ports. |
| `app/domain/banks.py` | Modified | `resolve_bank` `:172`; `national_digits` `:272`; `cedula_digits` `:301`. Keep pure helpers; add Banesco two-control mapping; fix `P`/`R` prefix sets. |
| `app/domain/bank_replies.py` | Removed (last slice) | No inbound SMS for BUY. |
| `app/services/sms_dispatch.py` | Dormant → removed | No BUY caller. |
| `app/services/buy_flow.py` | Modified | `execute` `:363`; `_dispatch` `:450`; `_extract` `:587`; `_record_sent` `:657`; `sweep_timeouts` `:778`; `resume` `:872`; `handle_incoming_sms` `:930`; `_close_order` `:1332`. Replace SMS tail with rail/receipt/release/appeal. |
| `app/services/risk_policy.py` | Modified | `LEDGER_VES`/`LEDGER_VES_SMS` `:39-41`; double reservation `:400-406`; release `:455`. Rename to `VES_PAY`; retain double cap. |
| `app/infrastructure/models.py` | Modified | Status `:111-115`; bank anchors `:128-139`; `AuditLog` `:240-277`; ledger `:323-355`. Add nullable rail/receipt/release/appeal columns and `operator_challenges`; no status DDL. |
| `app/infrastructure/clients/binance_browser.py` | Modified | Add receipt upload and appeal page actions; keep lazy import. |
| `app/infrastructure/clients/binance_client.py` | Modified | Release watcher via `get_order_detail`; document conservative status mapping. |
| `app/infrastructure/clients/banesco_*` (new) | New | Page contracts, driver, receipt capture; lazy Playwright import. |
| `app/infrastructure/clients/telegram_bot.py` (new) | New | `httpx` Bot API client, long polling, `sendMessage`/`getUpdates`. |
| `app/core/config.py` | Modified | Buy settings `:95-115`; validators `:220-250`; summary `:269-306`. Add Banesco/Telegram/receipt/release settings and fail-fast gates; never add secrets to the summary. |
| `app/core/logging.py` | Modified | Mask security answers, `clave`, `R-` documents; never log receipt text/bytes. |
| `app/core/exceptions.py` | Modified | Typed Banesco/Telegram/receipt/release/appeal errors. |
| `app/api/v1/orders.py` | Modified | Pay endpoint `:141-198`; new outcomes/guards/appeal action. |
| `app/api/v1/webhooks.py` | Modified | `/webhooks/sms` becomes `410`/removed in S9; Cachicamo untouched. |
| `app/api/v1/health.py` | Modified | Probes `:115`, `:130`; add Banesco/Telegram when enabled. |
| `app/main.py` | Modified | SMS poller `:42-54`, task `:183`; add Telegram poller and release watcher. |
| `app/infrastructure/models.py` + Alembic | Modified | Status/ledger data migrations; new columns/table. |
| `tests/` | Modified | New `test_banesco_*`, `test_telegram_*`, `test_release_*`, `test_challenges`; fixtures `tests/fixtures/banesco/*.html`; update SMS-era tests. |
| `openspec/specs/*` | Modified/Added | Deltas for `buy-flow`, `bank-sms`; new capability specs. |
| `README.md`, `.env.example` | Modified | Statuses, rail description, setting names with placeholders only. |

## Slicing Plan (`auto-chain`, ordered, each under 800 changed lines)

| # | Slice | Files | Start boundary | Finish boundary | Verification | Rollback | Est. lines |
|---|---|---|---|---|---|---|---|
| **S1** | **Status/ledger/config groundwork + pure Banesco mapper — FIRST AUTONOMOUS SLICE** | `enums.py`, `config.py`, `risk_policy.py`, `domain/banesco.py`, Alembic, tests | Current enums/config; no new statuses | `SENDING_PAYMENT`/`AWAITING_RELEASE`/`APPEALED`; `VES_PAY`; data migrations; `BanescoPaymentTarget` mapper; setting names/defaults; typed errors | `pytest`; ruff; migration up/down; transition tests; Flow A unchanged | Revert; downgrade restores `SENDING_SMS`/`VES_SMS` | ~650 (split S1a/S1b if >800) |
| **S2** | Pure Banesco page contracts | `domain/banesco.py`, fixtures, tests | S1 merged; no Banesco parser | Question normalization, modal/form/confirmation/clave/receipt parsers; no browser import | Fixture tests; import guard | Delete parsers; no runtime wiring | ~500 |
| **S3** | Login + questions + ad modal adapter | `banesco_pages.py`, `banesco_browser.py`, tests | Page contracts | `__aenter__`, login/questions/modal, fail-closed errors, lazy import | Fake page tests; subprocess import assertion; live smoke skipped | Revert; rail unused | ~550 |
| **S4** | Payment form + double `Pagar` + Clave handoff | Adapters, notifier port, tests | Login works | Form fill (two-control fields, bank, amount, concept), confirmation, clave-form detection, notifier call | Fake page per control/failure; fake notifier | Revert; no real payment path | ~600 |
| **S5** | Receipt capture + storage | Adapter, models/migration, tests | Clave handoff | Screenshot + text parse + reference/amount verification; `data/receipts`; hash; DB metadata | Fake receipt page; storage/hash/PII-log tests | Revert; no receipt writes | ~450 |
| **S6** | Telegram adapter + durable challenges | `telegram_bot.py`, `operator_challenges`, config, tests | Notifier port | httpx Bot API, long polling, challenge rows, correlation/timeout/ambiguity, restart expiry | `respx`; fake notifier; freezegun; restart test | Revert; rail disabled | ~500 |
| **S7** | Buy-flow rewiring + evidence upload | `buy_flow.py`, `ports.py`, `binance_browser.py`, tests | SMS dispatch still present but unreachable for BUY | Rail invocation, durable attempt, receipt persistence, upload + notify, `AWAITING_RELEASE`, resume guard | Fake rail/browser/notifier; state/audit assertions; no duplicate dispatch | Revert; old code still present (not re-enabled at runtime) | ~700 |
| **S8** | Release watcher + automatic appeal | `main.py`, `binance_client.py`, `buy_flow.py`, models, tests | `AWAITING_RELEASE` exists | Watcher, status mapping, timeout, at-most-once appeal, `APPEALED`, kill switches, pre-click re-check | `FakeBinance` sequences; freezegun; concurrency/at-most-once tests | Disable watcher/kill switch; orders stay `AWAITING_RELEASE` | ~650 |
| **S9** | Decommission SMS BUY path | `sms_dispatch.py`, `bank_replies.py`, `webhooks.py`, `main.py`, `buy_flow.py`, health, tests | New rail stable | Remove SMS webhook/dispatch/inbound confirmation/poller; keep Cachicamo; docs/health updated | Full suite; no dead imports; grep no SMS BUY refs | Revert PR; no runtime SMS re-enable | ~450 deletion-heavy (split S9a/S9b if needed) |
| **S10** | Operator/observability polish | `orders.py`, `health.py`, docs, tests | Core flow works | Response outcomes, appeal/reconcile endpoint, masked observability, README/`.env.example` | API tests; PII-free responses; docs | Revert; core flow unaffected | ~300 |

Each slice starts with RED tests, then GREEN, then refactor. Each PR targets the previous slice until diffs are clean. `sdd-tasks` must re-validate line counts.

## Risk Register

> **Security rule (non-negotiable): no credential value may ever appear in any artifact, code, test, fixture, comment, log, commit, or Engram observation.** Real Banesco credentials and security answers were disclosed in chat; artifacts reference them only by `.env` variable names (for example `BANESCO_ONLINE_USER`, `BANESCO_ONLINE_PASSWORD`, `BANESCO_SECURITY_ANSWER_<N>`, `TELEGRAM_BOT_TOKEN`). A canary test enforces that known secret values never render in logs/audit payloads, and fixture hygiene rejects real-looking PII.

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Bank ToS violation / account lockout | Medium | High | D9 accepted risk; dedicated account, minimal balance, low volume, no retries, captcha/lockout parks, kill switches. |
| DOM drift (login/questions/modal/form/receipt/appeal) | High over time | High | Page objects + recorded fixtures + typed fail-closed errors + opt-in live smoke; never guess a selector. |
| Captcha / anti-bot challenge | Medium | Medium | Detect and park `banesco_captcha_detected`; never bypass; operator completes manually. |
| Credential compromise | Low | Critical | `SecretStr` + `.env` only; no artifact/log/test/Engram values; rotation; canary regression test; security rule above. |
| Missing document letter | High | High | D2: parser capture, Telegram override, fail closed; never default `V`. |
| Double payment | Medium | Critical | Durable `payment_attempt_id`; no `SENDING_PAYMENT → COMPLETED`; payment-parks cannot resume; serialized payments. |
| Restart mid-payment | Medium | High | Startup expires challenges, parks `payment_interrupted`; never auto-resume; operator verifies bank state. |
| Receipt PII leakage | Medium | High | Gitignored restricted `data/receipts`, retention, hash-only DB/audit/API, never log text/bytes. |
| Consequential appeal | Medium | High | D4: at-most-once `appeal_attempt_id`, pre-click re-check, audit, `RELEASE_APPEAL_MODE=alert\|off`. |
| Telegram key/mis-correlation | Medium | Critical | Operator-only chat, reply-to + order + nonce, one pending challenge, 6-digit validation, never store/echo/log key. |
| Limit/idempotency regression from ledger rename | Medium | High | Double-cap `VES`+`VES_PAY` tests, atomic migration, UTC rollover, UNIQUE anchors unchanged. |
| Evidence upload failure | Medium | Medium | Park `evidence_upload_failed`; no `AWAITING_RELEASE`/appeal without evidence. |
| Binance status vocabulary drift | Medium | High | Conservative mapping; unknown → `MANUAL_REVIEW`; verify against real payloads. |

**Money, limits, idempotency impact**: every BUY keeps `order_number`, `bank_reference`, and `idempotency_key` UNIQUE anchors; the attempt identity is committed before the browser acts; the daily cap is preserved via `VES` + `VES_PAY`; the reservation is released only on a confirmed permanent failure. The only new consequential external actions are the bank payment and the appeal — both fail closed and at-most-once.

## Rollback Plan

- **Stop new payments**: set `BUY_AUTO_PAY_ENABLED=false` (parks at approval) and `BANESCO_RAIL_ENABLED=false` (no new browser sessions). Do not re-enable the SMS rail.
- **Mid-flight safety**: before the final `Pagar`, abort and park; after the final `Pagar` with unknown outcome, park `MANUAL_REVIEW`/`payment_outcome_unknown` and never re-click — the operator verifies the bank state. Never auto-resume across restarts.
- **Appeals**: set `RELEASE_APPEAL_MODE=off`; a filed appeal is not auto-withdrawn (manual operator action). `alert` is the emergency middle mode.
- **Credentials**: rotate all `BANESCO_ONLINE_*` and `TELEGRAM_BOT_TOKEN` values, invalidate the bank session, and review audit entries.
- **Schema/status rollback**: migration downgrade reverses the `SENDING_SMS` and `VES_SMS` data renames and drops new nullable columns. Downgrade is safe only before rows exist in the new tail; if `AWAITING_RELEASE`/`APPEALED` rows exist, map them to `MANUAL_REVIEW` first — never back to `SENDING_SMS`, and never auto-resume. Historical audit payload strings are not rewritten.
- **SMS path**: stays recoverable from git history and the archived OpenSpec change; emergency recovery is a deliberate redeploy of a prior build, not a runtime switch.

## Dependencies

- `playwright==1.49.1` + Chromium in the runtime image (already declared; CI never launches it).
- Existing `httpx` for Telegram (no new runtime dependency).
- Operator-supplied Telegram bot token/chat id and Banesco credentials, only in the local gitignored `.env`.
- Recorded, sanitized live DOM snapshots and Binance session state for opt-in smoke tests.
- Real Binance status/release payloads to confirm mapping before enabling the watcher.

## Success Criteria

- [ ] `.\.venv\Scripts\python.exe -m pytest -q` is green and `.\.venv\Scripts\python.exe -m ruff check app tests` is clean; no test requires a browser, network, real credentials, or a real Telegram bot.
- [ ] `alembic upgrade head`/`downgrade` pass on fresh SQLite; a seeded `SENDING_SMS` row becomes `SENDING_PAYMENT`, and `VES_SMS` becomes `VES_PAY`.
- [ ] Login handles exactly 2-of-4 randomized questions by text; unknown/repeated/wrong-count questions fail closed without a login.
- [ ] Payment mapping produces the exact phone prefix/rest, document letter/digits, bank option, and amount; a missing letter or unmatched bank blocks with `MANUAL_REVIEW` and no payment.
- [ ] A Clave Dinámica challenge is persisted before Telegram send; timeout parks `clave_dinamica_timeout`; restart expires it and never auto-resumes.
- [ ] Receipt capture persists hash/reference/amount/timestamp; mismatch parks; a canary test proves no credential, key, or receipt PII appears in logs/audit.
- [ ] Evidence upload succeeds before `AWAITING_RELEASE`; a failure parks and no appeal is filed without evidence.
- [ ] The watcher maps statuses conservatively; an automatic appeal fires at most once, skips when release is detected first, and is disabled by `RELEASE_APPEAL_MODE=off`.
- [ ] A payment-related `MANUAL_REVIEW` order cannot be resumed into a second payment; every kill switch stops new payments.
- [ ] No credential value exists in any artifact, code, test, fixture, comment, log, or Engram observation.

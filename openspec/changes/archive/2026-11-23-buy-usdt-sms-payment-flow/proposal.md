# Proposal: Flow B — Buy USDT via Pago Móvil SMS

- **Change**: `buy-usdt-sms-payment-flow`
- **Phase**: propose
- **Input**: `exploration.md` (526 lines), `PROJECT_SPEC.md:27-35`, `openspec/config.yaml`
- **Delivery**: `auto-chain`, 800-line review budget, strict TDD (`.\.venv\Scripts\python.exe -m pytest -q`)

## Intent

Flow A (sell) is the only path wired to production: it is reachable, tested end-to-end and protected by the order state machine. Flow B (`BuyFlow`) exists on paper but is unreachable (`main.py:217-218` registers no buy route, `main.py:96-98` starts only the SMS poller) and semantically incomplete — it sends a human-readable sentence to the counterparty phone, never extracts bank/phone/cedula data, and marks `COMPLETED` before any bank reply arrives (`buy_flow.py:469-477`).

Completing Flow B unlocks the supply side of the engine: automated USDT acquisition via real Pago Móvil SMS. Without it `liquidVE` can only sell inventory it already holds, so the business is capped by manual top-ups. Flow B is the increment that turns the product from a one-sided seller into a two-sided P2P engine.

## Scope

### In Scope
- Detection of open `PENDING_PAYMENT` BUY orders (worker + operator trigger).
- Payment-data extraction (bank 4-digit code, phone, cedula, exact Bs) via a `BinanceBrowser` port with a Playwright adapter.
- Pre-payment circuit breakers: idempotency, per-order cap, daily SMS cap, implied-rate margin, supported-bank gate.
- Real bank SMS command dispatch through the Android gateway (Tailscale), per-bank profile, short-code fallback.
- Inbound bank-reply parsing, correlation to the dispatch, bank-reference extraction.
- Binance close (notify seller + chat reference) and `COMPLETED` only after confirmation.
- Security correction of a pre-existing cross-flow hazard (see below).

### Out of Scope
- Crypto settlement / `release_order` / waiting for the seller's release (see D5).
- Flow A (sell) behaviour changes beyond a shared-risk refactor and the direction guard.
- External Bs/USDT market feed (implied rate is order-intrinsic, D4).
- Telegram approval UI, mojibake cleanup in `enums.py:179,186`, multi-account/multi-bank routing.
- New enum members (Binance `PENDING_PAYMENT` maps to local `PENDING`, D7).

## Capabilities

### New Capabilities
- `buy-flow`: BUY-side orchestration — detection, risk gate, dispatch, bank-reply confirmation, Binance close, and its state transitions.
- `bank-sms`: pure admin data and parsing — `BankSmsProfile`, numeric/short codes, VE amount & phone formatting, command rendering, per-bank reply patterns.

### Modified Capabilities
- None. `openspec/specs/` contains only `.gitkeep`; `bank-sms` and `buy-flow` are the first domain specs.

## Decisions & Assumptions

| # | Decision | Chosen default | Rationale | Risk if wrong |
|---|---|---|---|---|
| D1 | Auto-pay vs operator approval | New flag `BUY_AUTO_PAY_ENABLED`, default **false**. With false: after risk passes, order parks in `MANUAL_REVIEW` (`reason=awaiting_operator_approval`) and no SMS is sent; an internal-key endpoint resumes it (`MANUAL_REVIEW → PENDING → … → SENDING_SMS`, legal per `enums.py:52-73`). | The spec's `AUTO_RELEASE_ENABLED` is not implemented anywhere (`grep` finds none), so there is no precedent — and unattended irreversible bank debits must be opt-in. Symmetry: one explicit flag per direction. Fail-safe default = money cannot move until a human enables it. | If operators expect full auto like Flow A, throughput is gated by clicks until the flag is flipped; flipping it is a one-line env change + restart, so the cost is recoverable. |
| D2 | Bank-reply wording per bank | Data-driven `success_pattern` / `failure_patterns` per `BankSmsProfile`; ship BDV (`0102`) and Banesco (`0134`); any unparsed reply → `MANUAL_REVIEW`. | Real bank wording is unverified and varies; regex as editable data (not code) lets ops fix wording without a release. Unknown reply must never be guessed for a money confirmation. | Over-narrow patterns create manual work (safe); over-broad patterns could mis-parse a reference (caught by sender + amount + exactly-one-candidate checks in S5). |
| D3 | Cedula prefix in the command | Bare digits only (`12345678`), no `V-`/`E-`/`J-`, matching `Pagar 0102 04141234567 12345678 1500,00` (`PROJECT_SPEC.md:33`). Profile carries an optional `cedula_format` so a bank needing a prefix is a data-only change. | The spec example is the only evidence available; `validate_venezuelan_id` (`schemas.py:51-71`) already returns `V-…` and digits are derivable. | If BDV/Banesco require the prefix, the command is rejected — recoverable (bank does not debit) but requires a profile edit + manual review; no silent money loss. |
| D4 | Implied-rate reference | Order-intrinsic: `implied = amount_fiat / amount_asset` compared to the order's own `price` within `RATE_TOLERANCE_PCT` (default 1.0%), no external feed. | Both values are already persisted (`models.py:118-120`); this is an extraction-integrity check that catches the dominant risk — a mangled Playwright scrape (wrong decimal separator) — without a new provider or race. | A genuinely mispriced order would pass; that risk is bounded by `MAX_BUY_AMOUNT_VES` and is accepted for this increment. Market-relative checks are a separate change. |
| D5 | Meaning of "close" | Close = Playwright clicks *"Transferido, notificar al vendedor"* + posts the reference in the order chat. `COMPLETED` means "seller notified". `release_order` is never called. | The C2C client's release/mark-paid are merchant-only stubs (`binance_client.py:8-13,246-281`); crypto settlement is outside this capability. | If `COMPLETED` is read elsewhere as "crypto received", reporting is optimistic; mitigated by storing the reference and leaving settlement to a future change. |
| D6 | Daily SMS cap carrier | Reuse `daily_volume_ledgers` with namespaced `currency="VES_SMS"` via `_load_or_create_ledger(currency=...)`; no migration. | Keeps every money-risk slice migration-free while reusing existing locking (`buy_flow.py:221-273`). | Overloads `currency` semantics; reporting must know the pseudo-currency. A follow-up `channel` column is documented if a third channel appears. |
| D7 | `PENDING_PAYMENT` representation | Do not add an enum member; map Binance's `PENDING_PAYMENT` to local `PENDING`. | `OrderStatus` is `native_enum=False` (`models.py:111-115`) so a member is cheap, but it buys nothing: the local status already means "registered, no money moved". Fewer states = fewer transition edges to prove. | If a future UI must distinguish "awaiting payment" from "registered", a member can be added without a column migration. |

## Approach

Pure domain modules first (`app/domain/banks.py`, `bank_replies.py`, `ports.py`) so formatting and parsing are testable without I/O; a `BinanceBrowser` `Protocol` under `app/domain/ports.py` with the Playwright implementation in `app/infrastructure/clients/binance_browser.py` (lazy `import playwright`, `pytest.importorskip` in tests) keeps CI browser-free. Services carry the state machine and all side effects. Every unknown outcome fails closed to `MANUAL_REVIEW`; no path auto-retries a bank command. `SENDING_SMS` stays durable and committed before the gateway call (`buy_flow.py:441-444` is the pattern to preserve).

## Affected Areas

| Area | Impact | Reference |
|---|---|---|
| `app/domain/banks.py` | New | Profiles, numeric/short codes, `format_ves_amount` (`1500,00`), `render_bank_command`, `resolve_bank` |
| `app/domain/bank_replies.py` | New | Per-bank success/failure patterns, reference extraction |
| `app/domain/ports.py` | New | `BinanceBrowser` protocol, `PaymentInstruction` DTO |
| `app/domain/schemas.py` | Modified | Payment instruction, richer webhook outcome, risk fields (`:149-165`) |
| `app/services/buy_flow.py` | Modified | Extraction, caps, command dispatch, no premature `COMPLETED` (`:445-477`), resume (`:116-132,374`), confirmation (`:481-522`) |
| `app/services/sell_flow.py` | Modified | Delegate to shared risk policy (`:295-337`) |
| `app/services/risk_policy.py` | New | Single risk implementation + parameterized ledger carrier |
| `app/infrastructure/clients/binance_browser.py` | New | Playwright adapter + HTML extraction parser |
| `app/infrastructure/clients/sms_gateway.py` | Modified | Replace `build_payment_sms` (`:206-235`); short-code destination in service (`:96-102`) |
| `app/api/v1/webhooks.py` | Modified | Direction guard (`:271-281`), `get_buy_flow` seam (`:197-204`), richer SMS outcome (`:155-220`) |
| `app/api/v1/orders.py` | New | Operator approval/trigger endpoint |
| `app/main.py` | Modified | Buy worker lifespan (`:36-69,96-98,217-218`) |
| `app/infrastructure/models.py` + Alembic | Modified | `counterparty_id_number`, `sms_message_id`, `sms_sent_at`, `bank_short_code`, `bank_confirmed_at` |
| `app/core/config.py`, `.env.example` | Modified | New settings + validators (`:89-93,143-193,284-285`; `.env.example:73,91-95`) |
| `tests/conftest.py` + new suites/fixtures | Modified | `app_client`, fakes, HTML/SMS fixtures, new env defaults (`:16-31,49-130`) |

## Pre-existing Hazard (Security-Relevant Correction)

`_dispatch_cachicamo` (`app/api/v1/webhooks.py:271-281`) selects `PENDING`/`PAYMENT_DETECTED` orders by **exact fiat amount only**, with no `order_type` filter, then drives `SellFlow.handle_incoming_payment`, which can call `release_order` (`sell_flow.py:501`). A BUY order awaiting payment can therefore be consumed by the SELL path — an inbound Cachicamo payment intended for a buy could release crypto against the wrong direction. This is a live defect, independent of whether Flow B ships.

**Fix (part of S2, independently cherry-pickable):** add `P2POrder.order_type == OrderType.SELL` to the selector, deny on mismatch, and correct the inverted `OrderType` docstring (`enums.py:9-17`) so BUY consistently means "we pay VES".

**Dedicated verification:** a regression test seeds one identical-amount BUY and one SELL order in `PENDING`, posts a Cachicamo webhook, and asserts only the SELL order advances and `release_order` is never awaited for the BUY. Plus a test asserting zero candidates when only a BUY order exists (returns the no-match message, HTTP 202).

## Slicing Plan (`auto-chain`, ordered)

Constraints: S1→S4, S2→S4, S3→S4, S5→S6, (S1,S2,S3,S5,S6)→S7. Every slice is independently mergeable, under the 800-line budget (forecast ≤480 authored lines each), and reverts as a unit.

| # | Slice (first autonomous) | Files | Start boundary | Finish boundary | Verification | Rollback boundary | Est. lines |
|---|---|---|---|---|---|---|---|
| **S1** | **Bank SMS domain + formatting (pure)** — **FIRST AUTONOMOUS SLICE** | `app/domain/banks.py` (new), `tests/test_banks.py` | No profile registry; wrong message format; `sms_gateway.py:206-235` is the only builder | `BankSmsProfile`, `BANK_SMS_PROFILES` (BDV `0102`/`2661`,`2662`; Banesco `0134`/`2846`), `resolve_bank`, `format_ves_amount`, `national_digits`, `cedula_digits`, `render_bank_command`; no I/O | Pure unit tests: exact rendered strings, `1500,00`, phone/cedula normalization, unsupported bank → `None`; no fakes, no network | Delete the two files; no callers exist yet → zero runtime impact | 180–260 |
| **S2** | Risk policy extraction + buy/SMS caps + **security guard** | `app/services/risk_policy.py` (new), `buy_flow.py`, `sell_flow.py`, `webhooks.py`, `enums.py` (docstring only), `config.py`, `.env.example`, `conftest.py`, tests | Duplicated `_assess_risk` (`buy_flow.py:275-314`, `sell_flow.py:295-337`); no buy/SMS caps; no rate check; no direction guard | Shared policy used by both flows; ledger parameterized by `currency`; `max_buy_amount_ves`, `daily_sms_limit_ves`, `max_implied_rate_ves_per_usdt`/`RATE_TOLERANCE_PCT`, `rate_tolerance_pct`; buy-specific keys in `RiskAssessment.limits`; ledger `release_*`; `order_type == SELL` guard in `_dispatch_cachicamo` | Characterization tests pin current `SellFlow` before refactor; per-limit boundary tests with freezegun at UTC rollover; security regression test above; settings-consistency tests | Revert PR → both flows return to the duplicated policy; guard removal is a plain code revert (no schema, no data) | 320–420 |
| **S3** | Browser port + Playwright adapter + extraction parser | `app/domain/ports.py` (new), `app/infrastructure/clients/binance_browser.py` (new), `tests/test_browser_parser.py`, `tests/fixtures/binance/*.html` | No Playwright code, no `PaymentInstruction` | `PaymentInstruction` DTO, `BinanceBrowser` protocol, lazy-import `PlaywrightBinanceBrowser`, pure `parse_payment_instructions(html)`; adapter test skipped without Chromium | Parser tests against committed BDV/Banesco HTML fixtures; port-conformance test via fake; `pytest.importorskip("playwright")`; CI never launches a browser | Delete new modules + fixtures; nothing imports them until S4 | 380–480 |
| **S4** | Buy dispatch (`PENDING → SENDING_SMS`) with bank command + migration | `app/services/buy_flow.py`, `app/infrastructure/models.py`, new Alembic revision, `tests/test_buy_flow.py` | `BuyFlow.execute` sends a human sentence and completes immediately | Extraction via S3 port; `bank_code` persisted; unsupported bank → `MANUAL_REVIEW`; risk via S2; bank command via S1; short-code fallback; `BUY_AUTO_PAY_ENABLED` gate; **no `COMPLETED`**; idempotent `resume()`; failure keeps `SENDING_SMS` + `EXTERNAL_ERROR`; ledger released on permanent failure; nullable columns added | Fake browser + fake SMS + in-memory SQLite; respx for real `SmsGatewayClient` (path, bearer, `Idempotency-Key`, 500 → typed error, no retry); `DuplicateOrderError` vs resume; migration up/down | `alembic downgrade` drops nullable columns (no data dependency); revert service → previous premature-`COMPLETED` behaviour returns, acceptable only because S5/S6 are not merged yet | 350–450 |
| **S5** | Inbound bank-reply confirmation + reference extraction | `app/domain/bank_replies.py` (new), `app/services/buy_flow.py`, `app/api/v1/webhooks.py`, `tests/test_bank_replies.py`, `tests/test_buy_webhook.py`, `tests/fixtures/sms/*.txt`, `conftest.py` | Substring matching only (`buy_flow.py:481-522`); no parser, no `BankTransaction` | `confirm_bank_payment`; `BankTransaction(direction=OUTGOING)`, `p2p_orders.bank_reference`; exactly-one-candidate matcher (status + sender short code + amount + `sms_confirmation_timeout_seconds`); webhook distinguishes `matched/confirmed/ambiguous/no_match`; `DuplicateBankReferenceError` → `MANUAL_REVIEW` | Parser tests from SMS fixtures; matcher tests 0/1/many, wrong sender, expired window; ASGI webhook tests with `get_session` overridden and fake `app.state`; replay → 202 `duplicate=True` | Revert webhook + parser; SENDING_SMS orders simply stop being confirmed and remain parked (no money state corrupted) | 300–380 |
| **S6** | Binance close (notify + chat reference) | `app/domain/ports.py`, `app/infrastructure/clients/binance_browser.py`, `app/services/buy_flow.py`, `tests/test_buy_close.py` | Nothing calls Binance after confirmation | `notify_paid(order_number, reference)`; `SENDING_SMS → COMPLETED` only after success; failure → `MANUAL_REVIEW` + `failure_reason`; masked reference in `STATUS_CHANGED` audit | Fake-port call-order/argument assertions; success/failure state tests; explicit test that a Binance failure never yields `COMPLETED` | Revert the close step; confirmed orders remain in `SENDING_SMS` awaiting manual close (safe, visible) | 160–240 |
| **S7** | Buy-order detection worker + operator endpoint | `app/main.py`, `app/api/v1/orders.py` (new), `app/core/config.py`, `.env.example`, tests | `BuyFlow` unreachable; only the SMS poller runs | `buy_worker_enabled`, `buy_worker_poll_interval_seconds`; worker filters `tradeType=BUY`, skips known `order_number`s, enforces `order_type == BUY`; `POST /api/v1/orders/buy/{order_number}/pay` behind `require_internal_api_key`; cancellation-safe lifespan; timeout sweep for stale `SENDING_SMS` | respx for `get_order_history`; worker tick with fake clock; endpoint auth tests; shutdown-cancellation test | Set `BUY_WORKER_ENABLED=false` and remove the router registration → Flow B goes dormant, all data preserved | 180–260 |

**Total forecast**: ~1,870–2,490 authored lines across 7 slices; no slice exceeds ~480, so `auto-chain` fits the 800-line budget with margin. `sdd-tasks` must re-validate.

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Irreversible money movement (real bank SMS debit, no cancel API) | Medium | High | Fail closed on every unknown: no auto-retry of a command whose fate is unknown; `BUY_AUTO_PAY_ENABLED=false` default; per-order and daily caps before dispatch; durable `SENDING_SMS` before the gateway call. |
| Timeout semantics (no bank reply) | Medium | High | Never auto-release the ledger reservation and never resend on timeout; park in `MANUAL_REVIEW` after `sms_confirmation_timeout_seconds`; S7 sweep only flags, it does not act. |
| Settings are import-time constructed and `lru_cache`d (`config.py:284-285`) | High if mismanaged | High | Any new required setting lands simultaneously with `.env.example:73,91-95`, `conftest.py:16-31`, `test_settings` and validator updates (`config.py:143-193`); prefer defaults for non-critical values so omission does not crash imports. |
| `sell_flow.py` refactor (S2) | Medium | High | Characterization tests before the refactor; land as "add shared policy + delegate from both + delete copies" in two commits; per-flow regression test pins unchanged behaviour. |
| Playwright unavailable in CI | High | Medium | Domain port + HTML-fixture parser; adapter tests `importorskip` and are marked `integration`; default `-q` run never touches a browser. |
| Global UNIQUE `bank_reference` collision (`models.py:127,169`) | Low | Medium | Catch `IntegrityError` and convert to `DuplicateBankReferenceError` → `MANUAL_REVIEW`; never surface as HTTP 500; matcher requires exactly one candidate. |
| Cross-flow direction bug (existing) | High until fixed | High | S2 direction guard + dedicated regression test (see above); deny by default when `order_type` is not SELL. |
| Two test engines (`conftest.py:80-130`) | Medium | Medium | `app_client` + `override_session` fixtures introduced in S5; webhook tests must never rely on the default `client` session. |
| Two flows sharing the VES ledger (`buy_flow.py:221-273`) | Medium | Medium | S2 parameterizes the ledger carrier and keeps the existing `"VES"` row for shared spend; SMS cap uses `VES_SMS`; add release path. |

## Rollback Plan

- **Disable Flow B entirely**: set `BUY_WORKER_ENABLED=false` and `BUY_AUTO_PAY_ENABLED=false`, then remove the `orders` router registration (`main.py:217-218`). No new worker starts, no SMS is dispatched, no Binance close occurs. In-flight `SENDING_SMS`/`MANUAL_REVIEW` rows are preserved for manual handling — nothing is deleted.
- **Reverse schema delta**: all added columns are nullable (`counterparty_id_number`, `sms_message_id`, `sms_sent_at`, `bank_short_code`, `bank_confirmed_at`), so `alembic downgrade -1` drops them without data loss for the core flow; the UNIQUE anchors (`order_number`, `bank_reference`) are untouched.
- **No enum rollback needed**: no members are added (D7). The `VES_SMS` ledger row is inert if the feature is off; it can be deleted by `ledger_date` if reporting must be cleaned.
- **Per-slice revert**: each slice is a standalone PR with the rollback boundary listed in the table; S1/S3 are additive modules, S2 has a characterization-test safety net, S4's migration is nullable-only.

## Dependencies

- `playwright==1.49.1` (declared, unused) + Chromium in the runtime image for extraction/close; CI does not need it.
- Android SMS gateway reachable over Tailscale (`sms_gateway_base_url`, `sms_gateway_token`) for any real send.
- Real BDV/Banesco success and failure SMS samples to finalize D2 patterns (conservative defaults + `MANUAL_REVIEW` unblock the build).
- Binance session storage state for the browser adapter (manual smoke only).

## Success Criteria

- [ ] All tests pass offline: `.\.venv\Scripts\python.exe -m pytest -q` green with no network, no browser binaries, no real credentials.
- [ ] A BUY order in `PENDING_PAYMENT` is detected and persisted once; re-running detection creates no duplicate (`order_number` UNIQUE) and `resume()` is idempotent.
- [ ] With `BUY_AUTO_PAY_ENABLED=false`, no SMS is dispatched and the order parks in `MANUAL_REVIEW` with `awaiting_operator_approval`.
- [ ] With the flag enabled, the dispatched command matches `Pagar <0102|0134> <phone_national> <cedula_digits> <amount_ve>` exactly (e.g. `Pagar 0102 04141234567 12345678 1500,00`) and the gateway receives `Idempotency-Key = "{order_number}:payout"` with no retry on failure.
- [ ] Unsupported bank, over-cap amount, over daily SMS cap, or implied rate outside tolerance blocks dispatch and yields `MANUAL_REVIEW` with a machine-readable reason.
- [ ] A valid bank success SMS produces exactly one `BankTransaction(direction=OUTGOING)`, stores the reference, and transitions `SENDING_SMS → COMPLETED` **only after** a successful `notify_paid`; a failure reply or ambiguous/no match leaves state unchanged or moves to `MANUAL_REVIEW`, never `COMPLETED`.
- [ ] A replayed SMS webhook returns HTTP 202 `duplicate=True`; a duplicate `bank_reference` maps to `MANUAL_REVIEW`, never HTTP 500.
- [ ] Regression test proves a BUY order can never be consumed by `_dispatch_cachicamo` and `release_order` is never awaited for it.
- [ ] `alembic upgrade head` and `downgrade -1` both succeed on a fresh SQLite database.

# Tasks: Flow B — Buy USDT via Pago Móvil SMS

- **Change**: `buy-usdt-sms-payment-flow`
- **Phase**: tasks
- **Inputs**: `proposal.md` (S1–S7, D1–D7), `specs/buy-flow/spec.md` (R-BUY-001..011), `specs/bank-sms/spec.md` (R-BANK-001..007), `design.md` (B1–B7, A1–A12, §15), `openspec/config.yaml` (`rules.tasks`, `rules.apply`).
- **Store**: openspec (artifact) + Engram `sdd/buy-usdt-sms-payment-flow/tasks`.
- **Delivery**: `auto-chain`, `stacked-to-main`, 800-line review budget, strict TDD.
- **Runner**: `.\.venv\Scripts\python.exe -m pytest -q`.
- **Status**: planning complete. This phase writes no application code, tests, or configuration.

## How to execute these tasks

1. **RED first, always.** Each task names its RED node IDs. Run the **Verify** command before writing production code and confirm the failure stated in the RED bullet (`ImportError`, `AttributeError`, `fixture ... not found`, or the named assertion). Then implement until that same command is green. Characterisation tests (marked as such) are the only deliberate exception: they are written against current behaviour as a safety net, and the task's RED is the new-API import/assertion that fails until the refactor exists.
2. **One session per task.** Do not start the next task until its dependency task is green.
3. **No external dependencies in the default run.** No network, no Playwright import, no Chromium, no real credentials. `respx` only. The single live-browser smoke test is opt-in (`LIQUIDVE_BROWSER_TESTS=1`, `-m integration`) and is never part of `-q`.
4. **Money is `Decimal`.** Any function accepting money rejects `float` explicitly; no adapter returns a `float`.
5. **Typed errors only.** Reuse/extend `app/core/exceptions.py`; no bare `Exception`/`ValueError` for domain refusals (the documented `RiskPolicy` channel guard is the one `ValueError`).
6. **Style rules (`rules.apply`).** `from __future__ import annotations`; module and Google-style docstrings; persist idempotency anchors and audit entries **before** non-idempotent provider calls.
7. **Do not invent open questions.** See "Out of scope — do not invent" below.
8. **PR mechanics.** Every phase below is one stacked PR to `main`, in numeric order. A task never edits a file owned by a later phase except where the dependency table says so. Each PR is independently mergeable and independently revertible at its stated rollback boundary.

## Slice decision — re-validated against the 800-line budget

The design forecasts are re-cut so that **every PR stays well under 800 authored lines** and each PR is independently mergeable/revertible. Splits adopt the design's own plans; no new split is invented.

| Unit | PR | Slices | Authored lines | Decision |
|---|---|---|---|---|
| S1 | PR 1 | Bank SMS domain + formatting | 180–260 | **Keep whole.** Pure module, zero callers, single concern. |
| S2a | PR 2 | RiskPolicy extraction + flow delegation | 150–200 | **Split S2.** The extraction is a reversible refactor with a characterisation net; it must not be entangled with the live security fix. |
| S2b | PR 3 | Buy/SMS caps + settings + direction guard | 170–230 | **Split S2.** The guard is the live security fix and stays a cherry-pickable commit pair inside this PR. |
| S3a | PR 4 | Ports + pure HTML parser + fixtures | 230–300 | **Split S3.** Removes the Playwright import risk from the parser and unblocks S4 without the browser binary. |
| S3b | PR 5 | Playwright adapter + browser settings | 150–190 | **Split S3.** Adapter is optional for S4 and only needed for S6/live smoke. |
| S4a | PR 6 | Models + Alembic revision | 100–140 | **Split S4.** Inert nullable columns; isolates schema risk and the downgrade proof. |
| S4b | PR 7 | Dispatch, resume, classification | 250–330 | **Split S4.** The behavioural half; depends on S1, S2a/S2b, S3a. |
| S5 | PR 8 | Bank-reply confirmation + webhook contract | 300–400 | **Keep whole.** One coherent parser+matcher+anchor change; splitting would break the replay invariant across PRs. |
| S6 | PR 9 | Binance close | 160–240 | **Keep whole.** Small, and S5 deliberately stops before `COMPLETED`. |
| S7 | PR 10 | Worker + operator endpoint + lifecycle | 180–280 | **Keep whole.** Last slice; all hard prerequisites are merged. |

**Hard ordering preserved:** S1→S4, S2→S4, S3→S4, S5→S6, (S1,S2,S3,S5,S6)→S7. Concretely: PR 1→PR 7; PR 2 and PR 3→PR 7; PR 4→PR 7; PR 8→PR 9; all→PR 10. PR 5 (S3b) has no code dependency from S4b; it is stacked before PR 6/7 to keep the chain linear.

**Security guard independence:** within PR 3 the guard is commits A (test seams) + B (guard + `tests/test_direction_guard.py`). Cherry-picking A+B onto `main` is a complete, settings-free security fix; commit C–D (settings) and commit E (buy breakers) are not required.

### S1 is the first autonomous slice (PR 1)

| Boundary | Definition |
|---|---|
| **Start** | `main` at the scaffold commit `fe7e93a`; no `app/domain/banks.py`; no `BankSmsFormatError`/`BankSmsParseError`; the only message builder is `SmsGatewayClient.build_payment_sms` (`sms_gateway.py:206-235`). |
| **Finish** | `app/domain/banks.py` ships `BankSmsProfile`, `BANK_SMS_PROFILES` (BDV `0102` → `2661`,`2662`; Banesco `0134` → `2846`), `SHORT_CODE_INDEX`, `resolve_bank`, `profile_for_sender`, `apply_short_code_overrides`, `format_ves_amount`, `parse_ves_amount`, `national_digits`, `cedula_digits`, `render_bank_command`, plus the two typed errors. `tests/test_banks.py` green. No config, schema, service or webhook change. |
| **Verification** | `.\.venv\Scripts\python.exe -m pytest -q tests/test_banks.py` then `.\.venv\Scripts\python.exe -m pytest -q`. |
| **Rollback** | Revert PR 1: delete `app/domain/banks.py`, `tests/test_banks.py`, and the two exception classes. No caller exists, so runtime impact is zero; no data or schema touched. |
| **Dependencies** | None. Task 0.1 is its first action. |

## Out of scope — do not invent

1. **No operator path out of `SENDING_SMS`.** There is no `POST /orders/{order_number}/review`, no automatic `SENDING_SMS → MANUAL_REVIEW` transition and no resend. `resume()` MUST not re-send while `SENDING_SMS` (A2); the pay endpoint MUST no-op on `SENDING_SMS` (R-BUY-002); the S7 sweep only flags `sms_confirmation_timeout`. Operators resolve these rows manually — that runbook gap is design-owned.
2. **Bank-reply wording is provisional.** The shipped BDV/Banesco patterns are unverified. Do not "improve" them from imagination, do not add patterns, and do not turn an unparsed reply into a confirmation. Unparseable/ambiguous replies go to `MANUAL_REVIEW` with `bank_reply_unparseable` / `bank_reply_ambiguous`.

Other design open questions (reply-carried amount, `VES_SMS` reporting, real order-detail URL) stay design-owned and MUST NOT expand this change.

## Requirement → task map

| Requirement | Tasks |
|---|---|
| R-BUY-001 detection | 7.3, 10.2 |
| R-BUY-002 idempotency/resume | 7.6, 10.4 |
| R-BUY-003 extraction | 4.1, 4.2, 7.3, 7.4 |
| R-BUY-004 circuit breakers | 0.2, 3.2, 7.4, 7.5 |
| R-BUY-005 operator gate | 7.7, 10.4 |
| R-BUY-006 durable dispatch | 7.2, 7.3, 7.5, 7.8 |
| R-BUY-007 correlation | 8.2, 8.3, 8.5 |
| R-BUY-008 outgoing transaction | 8.4 |
| R-BUY-009 close/COMPLETED | 9.1, 9.2, 9.3 |
| R-BUY-010 timeout | 10.3 |
| R-BUY-011 direction guard | 3.1 |
| R-BANK-001 profile | 1.4 |
| R-BANK-002 registry | 1.4 |
| R-BANK-003 resolve | 1.5 |
| R-BANK-004 amount format | 1.1 |
| R-BANK-005 phone/cedula | 1.2 |
| R-BANK-006 command rendering | 1.6 |
| R-BANK-007 reply parsing | 8.1 |

## Phase 0 — Prerequisites (cross-cutting; lands at the head of the named PR)

Phase 0 is not a separate PR. Each item is executed in the PR named in its heading, before the first task that consumes it.

### 0.1 Baseline, runner and repository invariants — PR 1, first action

- [x] Done (PR 1 / S1): baseline 33 tests green at `fe7e93a`.

- **What**: confirm the scaffold suite and toolchain are green before any change; record the baseline commit and test count.
- **Files**: none.
- **RED**: gate task, not TDD. No production code.
- **Impl**: run the baseline; confirm `pytest.ini` has `asyncio_mode = auto`, `testpaths = tests`, `addopts = -q`; confirm `.venv\Scripts\python.exe` exists; record `git rev-parse HEAD`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q` (must be green before PR 1 starts) and `git status --porcelain` (only `openspec/` and `.atl/` untracked).
- **Deps**: none.

### 0.2 Settings co-change order — PR 3 (S2b), commits C–D

- [x] Done (PR 3 / S2b)

- **What**: add every S2 setting with the mandated order **conftest first**, then RED, then GREEN. `Settings()` is built at import time (`config.py:284-290`) and `extra="ignore"` makes early `test_settings` kwargs a no-op, so this order is the only safe one.
- **Files**: `tests/conftest.py`; `tests/test_config.py` (new); `app/core/config.py`; `.env.example`.
- **RED**: `tests/test_config.py::test_buy_settings_defaults`, `::test_buy_caps_are_required` (uses `monkeypatch.delenv` for both caps), `::test_daily_sms_limit_must_cover_max_buy`, `::test_buy_caps_reject_float_and_zero`, `::test_short_code_overrides_parse_from_env`, `::test_settings_builds_from_conftest_env_alone` — fail with `AttributeError` for the missing fields (or the consistency assertion does not raise yet).
- **Ordering (do not reorder)**:
  1. `tests/conftest.py`: `os.environ.setdefault("MAX_BUY_AMOUNT_VES", "5000.00")`, `os.environ.setdefault("DAILY_SMS_LIMIT_VES", "20000.00")`; add `max_buy_amount_ves=Decimal("5000.00")`, `daily_sms_limit_ves=Decimal("20000.00")` to `test_settings` (no-op until the fields exist).
  2. Write `tests/test_config.py`; observe RED.
  3. GREEN: `config.py` fields `buy_auto_pay_enabled=False`, `max_buy_amount_ves` (required `Decimal`), `daily_sms_limit_ves` (required `Decimal`), `rate_tolerance_pct=Decimal("1.0")`, `sms_confirmation_timeout_seconds=300`, `bank_short_codes_bdv: Annotated[list[str], NoDecode] = []`, `bank_short_codes_banesco` (same); add the two Decimals to `_validate_money_amounts`; add a `rate_tolerance_pct` validator (`0 < v ≤ 100`, quantize `0.01`); add `daily_sms_limit_ves >= max_buy_amount_ves` to `_validate_consistency`; add both short-code fields to `_parse_list_env`; extend `base_settings_summary()` with `buy_auto_pay_enabled`, `max_buy_amount_ves`, `daily_sms_limit_ves`, `rate_tolerance_pct`.
  4. `.env.example`: every S2 row from design §7 with `REQUIRED` on the two caps and `false`/safe defaults on switches.
- **Impl detail**: keep `internal_api_key` in `test_settings` identical to the conftest env value (`security.py:246,263` reads the cached `get_settings()`); otherwise every auth test flips to 401.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_config.py` then `.\.venv\Scripts\python.exe -m pytest -q`.
- **Deps**: 0.1.

### 0.3 ASGI + DB test seams — PR 3 (S2b), commit A

- [x] Done (PR 3 / S2b)

- **What**: introduce the one-engine integration harness so webhook/endpoint tests bind requests and assertions to the same in-memory DB.
- **Files**: `tests/conftest.py`; `tests/test_test_seams.py` (new).
- **RED**: `tests/test_test_seams.py::test_app_client_shares_the_override_engine`, `::test_app_client_does_not_touch_module_global_engine`, `::test_fake_binance_records_and_returns_orders`, `::test_seed_order_is_deterministic` — fail with `fixture 'app_client' not found` / `fixture 'fake_binance' not found`.
- **Impl**: `_override_session(engine)` (`async_sessionmaker(expire_on_commit=False, autoflush=False)` + commit-on-success/rollback-on-error, mirroring `get_session`); `app_client` (`create_app(test_settings)` **without** lifespan, doubles on `app.state.{binance,cachicamo,sms,browser}`, `application.dependency_overrides[get_session] = _override_session(engine)`, `httpx.ASGITransport`); `fake_binance` (records `get_order_detail`/`get_order_history`/`mark_order_as_paid`/`release_order`, exposes `release_calls`); `seed_order(**kwargs)`; `signed_cachicamo_headers(body)` helper reusing `CachicamoWebhookVerifier` inputs. Never mutate the module-global engine.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_test_seams.py`.
- **Deps**: 0.1.

### 0.4 Provider doubles — fake browser (PR 4), fake SMS + browserless (PR 7)

- **What**: give the suite port doubles so no test imports Playwright or opens a socket.
- **Files**: `tests/conftest.py`; `tests/test_test_seams.py`.
- **RED**: `tests/test_test_seams.py::test_fake_browser_records_ordered_events`, `::test_fake_sms_records_idempotency_key`, `::test_fake_browserless_is_none` — fail with fixture-not-found.
- **Impl**: `fake_browser` conforming to `BinanceBrowser` with scriptable `instruction`/`extract_error`/`notify_error` and ordered `("extract", n)` / `("notify", n, ref)` events (landed in PR 4 with task 4.3); `fake_sms` recording `(phone_number, message, idempotency_key)` and scriptable `errors=[...]`, plus an `on_send` hook (landed in PR 7 with 7.2); `fake_browserless` = `None` (PR 7 with 7.4).
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_test_seams.py`.
- **Deps**: 0.3.

### 0.5 respx seams — SMS (PR 7), Binance URL-regex (PR 10)

- **What**: standardise the offline provider contracts and the Binance pre-signed-query matcher.
- **Files**: `tests/test_test_seams.py`; `tests/test_buy_flow.py` (SMS contract in 7.8); `tests/test_buy_worker.py` (Binance in 10.1).
- **RED**: `tests/test_test_seams.py::test_binance_history_route_matches_presigned_query` — fails with `helper not found` / no `trade_type` parameter. (The SMS respx contract is the RED of 7.8, not this task.)
- **Impl**: document and add the route helper `binance_history_route()` using `respx.post(url__regex=r".*/sapi/v1/c2c/orderMatch/listUserOrderHistory\?.*")`; SMS uses `respx.post("http://100.64.0.10:8080/messages")`. Assert on `request.url.params["tradeType"]`, never on `params__contains` (the adapter sends a pre-signed query string).
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_test_seams.py::test_binance_history_route_matches_presigned_query`.
- **Deps**: 0.3.

## Phase 1 — S1: Bank SMS domain and formatting (PR 1, first autonomous slice)

Boundary: see "S1 is the first autonomous slice" above. All tasks are pure: no DB, no fixtures beyond strings, no I/O, no settings.

### 1.1 Amount formatting and typed format errors

- [x] Done (PR 1 / S1).

- **What**: `format_ves_amount` renders exactly `1500,00` / `1500,50` / `25000,00` (comma decimal, two decimals, no grouping, no symbol, half-up) and refuses `float`/non-positive input.
- **Files**: `app/domain/banks.py` (new), `app/core/exceptions.py`, `tests/test_banks.py` (new).
- **RED**: `tests/test_banks.py::test_format_ves_amount_matches_venezuelan_format`, `::test_format_ves_amount_rejects_float`, `::test_format_ves_amount_rejects_non_positive` — run the Verify command now; fails with `ModuleNotFoundError: app.domain.banks`.
- **Impl**: add `BankSmsFormatError` (`code="bank_sms_format_error"`, 422) and `BankSmsParseError` (`code="bank_sms_parse_error"`, 422) as `LiquidVEException` subclasses; `format_ves_amount` checks `isinstance(value, Decimal)` → finite → `> 0` → `quantize(Decimal("0.01"), ROUND_HALF_UP)` → `f"{q:.2f}".replace(".", ",")`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banks.py::test_format_ves_amount_matches_venezuelan_format tests/test_banks.py::test_format_ves_amount_rejects_float tests/test_banks.py::test_format_ves_amount_rejects_non_positive`.
- **Deps**: 0.1.

### 1.2 Phone and cedula normalization

- [x] Done (PR 1 / S1).

- **What**: `national_digits("+584141234567") == "04141234567"` (idempotent on national input); `cedula_digits("V-12345678") == "12345678"` and applies `cedula_format` when supplied; invalid input raises.
- **Files**: `app/domain/banks.py`, `tests/test_banks.py`.
- **RED**: `tests/test_banks.py::test_e164_phone_becomes_national_digits`, `::test_national_phone_is_idempotent`, `::test_cedula_digits_and_optional_prefix`, `::test_cedula_rejects_non_normalizable_input` — fail with `ImportError` for the symbols.
- **Impl**: reuse `validate_venezuelan_phone` (`schemas.py:18-48`), strip `+58`, prepend `0`, require 11 digits; strip `V/E/J/G/P`/dashes/spaces, require 6–9 digits, then `cedula_format.format(cedula=digits)`; wrap failures in `BankSmsFormatError`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banks.py::test_e164_phone_becomes_national_digits tests/test_banks.py::test_national_phone_is_idempotent tests/test_banks.py::test_cedula_digits_and_optional_prefix tests/test_banks.py::test_cedula_rejects_non_normalizable_input`.
- **Deps**: 1.1.

### 1.3 Reply/scrape amount parsing

- [x] Done (PR 1 / S1).

- **What**: `parse_ves_amount("1.500,00") == Decimal("1500.00")`; unmatched text raises `BankSmsParseError`, never guesses.
- **Files**: `app/domain/banks.py`, `tests/test_banks.py`.
- **RED**: `tests/test_banks.py::test_parse_ves_amount_inverts_formatting`, `::test_parse_ves_amount_rejects_unmatched_text` — fail with `ImportError`.
- **Impl**: strip grouping dots, replace `,` with `.`, `Decimal(...)`, require finite `> 0`, quantize `0.01`; raise `BankSmsParseError` on anything else.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banks.py::test_parse_ves_amount_inverts_formatting tests/test_banks.py::test_parse_ves_amount_rejects_unmatched_text`.
- **Deps**: 1.1.

### 1.4 Immutable profiles and the shipped registry

- [x] Done (PR 1 / S1).

- **What**: `BankSmsProfile` (bank, display name, 4-digit `numeric_code`, ordered `short_codes`, `command_template`, ≥1 success pattern, failure patterns, optional `cedula_format`, aliases); `BANK_SMS_PROFILES` keyed by numeric code and `SHORT_CODE_INDEX` keyed by short code, both `MappingProxyType`.
- **Files**: `app/domain/banks.py`, `tests/test_banks.py`.
- **RED**: `tests/test_banks.py::test_profile_carries_routing_data`, `::test_shipped_registry_primary_short_codes` (`0102`→`2661`, `0134`→`2846`), `::test_registry_is_immutable` — fail with `ImportError`.
- **Impl**: BDV `0102` (`2661`,`2662`) and Banesco `0134` (`2846`), template `Pagar {numeric_code} {phone_national} {cedula} {amount_ve}`, `cedula_format=None` for both.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banks.py::test_profile_carries_routing_data tests/test_banks.py::test_shipped_registry_primary_short_codes tests/test_banks.py::test_registry_is_immutable`.
- **Deps**: 1.1.

### 1.5 Bank resolution and profile lookup

- [x] Done (PR 1 / S1).

- **What**: `resolve_bank` accepts `" 0102 "`, a member name, or an alias; returns `None` for `"0105"`, empty, ambiguous; never `OTHER`/nearest. `profile_for_sender("2661")` returns BDV; `apply_short_code_overrides` returns a new profile with ordered overrides.
- **Files**: `app/domain/banks.py`, `tests/test_banks.py`.
- **RED**: `tests/test_banks.py::test_code_and_alias_both_resolve`, `::test_unsupported_bank_returns_none`, `::test_ambiguous_alias_returns_none`, `::test_profile_for_sender_uses_short_code_index`, `::test_short_code_overrides_replace_primary_first` — fail with `ImportError`.
- **Impl**: trim → `/^\d{4}$/` hit registry, miss → `None` (no alias fallback, so `0105` stays unsupported); otherwise NFKD/alnum-lower, longest alias wins, more than one distinct `BankCode` → `None`; reuse `BankCode.from_aliases` but treat `OTHER` as "no profile".
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banks.py::test_code_and_alias_both_resolve tests/test_banks.py::test_unsupported_bank_returns_none tests/test_banks.py::test_ambiguous_alias_returns_none tests/test_banks.py::test_profile_for_sender_uses_short_code_index tests/test_banks.py::test_short_code_overrides_replace_primary_first`.
- **Deps**: 1.4.

### 1.6 Exact command rendering

- [x] Done (PR 1 / S1).

- **What**: `render_bank_command(BDV, phone="04141234567", cedula="12345678", amount=Decimal("1500")) == "Pagar 0102 04141234567 12345678 1500,00"`; a missing/invalid component raises and returns no partial command.
- **Files**: `app/domain/banks.py`, `tests/test_banks.py`.
- **RED**: `tests/test_banks.py::test_command_matches_product_example`, `::test_banesco_command_matches_example`, `::test_missing_cedula_raises_and_renders_nothing`, `::test_rendered_command_has_no_stray_whitespace` — fail with `ImportError`.
- **Impl**: validate numeric code (4 digits), phone (11 digits, leading `0`), cedula (non-empty), amount (`Decimal > 0`); render; assert `" ".join(out.split()) == out`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banks.py::test_command_matches_product_example tests/test_banks.py::test_banesco_command_matches_example tests/test_banks.py::test_missing_cedula_raises_and_renders_nothing tests/test_banks.py::test_rendered_command_has_no_stray_whitespace`.
- **Deps**: 1.2, 1.4.

### 1.7 S1 gate and rollback proof

- [x] Done (PR 1 / S1): full suite green; only `banks.py`, `exceptions.py`, `test_banks.py` touched.

- **What**: prove S1 is complete, caller-free and revertible.
- **Files**: none.
- **RED**: gate task.
- **Impl**: grep that no module imports `app.domain.banks` outside the tests; confirm no schema/config change in the PR diff.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q` and `git diff --stat main...HEAD` (only `app/domain/banks.py`, `app/core/exceptions.py`, `tests/test_banks.py`).
- **Deps**: 1.1–1.6.

## Phase 2 — S2a: RiskPolicy extraction and flow delegation (PR 2)

Boundary: start with duplicated `_assess_risk`/`_load_or_create_ledger` in both flows and no `VES_SMS` channel; finish with one policy used by both flows, byte-identical SELL behaviour pinned, and no new settings or guards. Rollback: revert PR 2; both flows return to their duplicated copies; no schema/data change.

### 2.1 RiskPolicy skeleton and shared assessment

- [x] Done (PR 2 / S2a).

- **What**: create `RiskPolicy` with `assess_shared(amount_ves, amount_asset)` returning the same `RiskAssessment` (limits keys and reason strings) as today's `_assess_risk`.
- **Files**: `app/services/risk_policy.py` (new), `tests/test_risk_policy.py` (new).
- **RED**: `tests/test_risk_policy.py::test_assess_shared_allows_and_increments_ves_ledger`, `::test_assess_shared_block_reason_matches_legacy_sell_flow` — fail with `ModuleNotFoundError: app.services.risk_policy`.
- **Impl**: `LEDGER_VES`, `CHANNELS`; `load_or_create_ledger(currency=...)` with the existing PostgreSQL `FOR UPDATE` / SQLite `ON CONFLICT` branches; `check_channel_cap` (read-only projection); `assess_shared` does check+reserve exactly like the current code; no new settings.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_risk_policy.py::test_assess_shared_allows_and_increments_ves_ledger tests/test_risk_policy.py::test_assess_shared_block_reason_matches_legacy_sell_flow`.
- **Deps**: 0.1.

### 2.2 Parameterised ledger, reserve and release

- [x] Done (PR 2 / S2a).

- **What**: `VES` and `VES_SMS` are independent rows; `reserve` refuses when projected exceeds the limit; `release` clamps at zero, decrements the count, and rejects unknown channels.
- **Files**: `app/services/risk_policy.py`, `tests/test_risk_policy.py`.
- **RED**: `tests/test_risk_policy.py::test_ves_and_ves_sms_rows_are_independent`, `::test_reserve_refuses_when_projected_over_limit`, `::test_release_clamps_at_zero_and_decrements_count`, `::test_release_rejects_unknown_channel` — fail with `AttributeError` for the missing methods.
- **Impl**: `LEDGER_VES_SMS`; `reserve(currency, amount_ves)` (`projected = (total + amount).quantize(0.01)`, increment count); `release(...)` (`max(0, total - amount)`, `max(0, count - 1)`); raise `ValueError` outside `CHANNELS`; keep the row lock per `(date, currency)`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_risk_policy.py::test_ves_and_ves_sms_rows_are_independent tests/test_risk_policy.py::test_reserve_refuses_when_projected_over_limit tests/test_risk_policy.py::test_release_clamps_at_zero_and_decrements_count tests/test_risk_policy.py::test_release_rejects_unknown_channel`.
- **Deps**: 2.1.

### 2.3 SellFlow delegates to RiskPolicy (characterisation net)

- [x] Done (PR 2 / S2a).

- **What**: delete `SellFlow._load_or_create_ledger`/`_assess_risk`; call `RiskPolicy.assess_shared`. SELL prose reasons and ledger math stay byte-identical.
- **Files**: `app/services/sell_flow.py`, `tests/test_risk_policy.py`.
- **RED**: `tests/test_risk_policy.py::test_sell_flow_delegates_to_risk_policy` — monkeypatches `RiskPolicy.assess_shared`, runs `SellFlow.execute`, and asserts the policy was called; fails because the flow still uses its local copy. The extra `test_sell_flow_risk_reason_and_ledger_unchanged` is a characterisation net written before the refactor (expected green before and after) — it is not this task's RED.
- **Impl**: remove the duplicated helpers and their `sqlite_insert`/`select` imports if now unused; keep the exact `"Transaction amount exceeds the per-transaction limit"` / `"Projected daily volume exceeds the daily limit"` strings.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_risk_policy.py` then `.\.venv\Scripts\python.exe -m pytest -q`.
- **Deps**: 2.2.

### 2.4 BuyFlow delegates to RiskPolicy

- [x] Done (PR 2 / S2a).

- **What**: delete `BuyFlow`'s duplicated ledger/risk helpers; use the same policy. Dispatch behaviour is otherwise untouched (S4b rewrites it).
- **Files**: `app/services/buy_flow.py`, `tests/test_risk_policy.py`.
- **RED**: `tests/test_risk_policy.py::test_buy_flow_delegates_to_risk_policy` — same monkeypatch pattern with `BuyFlow.execute`; fails while the local copy is used.
- **Impl**: replace `_assess_risk`/`_load_or_create_ledger` calls; keep the current order of checks and messages.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_risk_policy.py::test_buy_flow_delegates_to_risk_policy` then `.\.venv\Scripts\python.exe -m pytest -q`.
- **Deps**: 2.2.

### 2.5 S2a gate

- [x] Done (PR 2 / S2a): full suite green; only `risk_policy.py`, `sell_flow.py`, `buy_flow.py`, `test_risk_policy.py` touched.

- **What**: prove no behavioural drift and no new settings/guards in this PR.
- **Files**: none.
- **RED**: gate task.
- **Impl**: confirm `git diff` touches only `risk_policy.py`, `sell_flow.py`, `buy_flow.py`, `test_risk_policy.py`; confirm the characterisation tests pass unchanged.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q` and `git diff --stat main...HEAD`.
- **Deps**: 2.1–2.4.

## Phase 3 — S2b: Buy caps, settings and the security direction guard (PR 3)

Boundary: start with no buy/SMS caps, no `order_type` filter and the inverted `OrderType` docstring; finish with the buy breaker policy, the S2 settings, and the live SELL-only guard. **Commit order inside PR 3 is mandatory**: 0.3 (test seams) → 3.1 (guard + tests) → 0.2 (settings) → 3.2 (breakers) → 3.3 (gate). Commits for 0.3 + 3.1 are the cherry-pickable security fix. Rollback: revert PR 3; flows fall back to the shared-VES-only policy and the guard disappears; no schema/data change.

### 3.1 Security direction guard — `_dispatch_cachicamo`

- [x] Done (PR 3 / S2b)

- **What**: make the sell-side inbound path SELL-only by construction, add defense-in-depth assertions, and correct the inverted `OrderType` docstring (`BUY` = we pay VES and receive crypto).
- **Files**: `app/api/v1/webhooks.py`, `app/services/sell_flow.py`, `app/services/buy_flow.py`, `app/domain/enums.py`, `tests/test_direction_guard.py` (new).
- **RED**: `tests/test_direction_guard.py::test_equal_amounts_do_not_cross_directions` and `::test_only_buy_candidate_yields_no_match`, plus `::test_sell_flow_refuses_buy_order_before_bank_transaction`, `::test_buy_flow_refuses_sell_order` — fail because the current selector matches the BUY order (it advances / `release_calls` contains it) and/or the refusal does not exist.
  - `test_equal_amounts_do_not_cross_directions`: seed one BUY and one SELL in `PENDING` with identical `amount_fiat`; install `fake_binance`; POST a correctly signed Cachicamo webhook through `app_client`; assert the SELL advanced, the BUY is still `PENDING` with no `PAYMENT_VERIFIED`, `fake_binance.release_calls == [sell_only]`, and `buy_order_number not in fake_binance.release_calls`.
  - `test_only_buy_candidate_yields_no_match`: BUY-only DB → HTTP 202 with the no-match message, order unchanged, `fake_binance.release_calls == []`.
- **Impl**: add `P2POrder.order_type == OrderType.SELL` to the selector (`webhooks.py:271-281`); after load, `if order.order_type is not OrderType.SELL: audit WEBHOOK_REJECTED(reason="direction_mismatch"); return "Payment stored; no matching order is waiting for it"`; `SellFlow.handle_incoming_payment` raises `InvalidStateTransitionError` for a non-SELL order **before** any `BankTransaction`; `SellFlow.execute` refuses `detail.trade_type is not SELL`; `BuyFlow.execute` refuses non-BUY; fix the `enums.py:9-17` docstring. `release_order` is a coroutine — assert "never awaited" structurally via the empty call list.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_direction_guard.py` then `.\.venv\Scripts\python.exe -m pytest -q`.
- **Deps**: 0.3.

### 3.2 Buy breaker policy in RiskPolicy

- [x] Done (PR 3 / S2b)

- **What**: implement the ordered buy breakers (per-order cap → daily SMS cap → rate tolerance → supported bank) with machine-readable reasons and reservation only on the allow path. Wiring into dispatch happens in 7.4/7.5; there is no live BUY path in this PR, so this is policy + tests only.
- **Files**: `app/services/risk_policy.py`, `tests/test_risk_policy.py`.
- **RED**: `tests/test_risk_policy.py::test_buy_breakers_block_in_order` (table-driven: `amount_over_cap`, `daily_sms_cap_exceeded`, `rate_out_of_tolerance`, `unsupported_destination_bank`), `::test_buy_allow_path_reserves_ves_sms_only`, `::test_buy_limits_snapshot_has_buy_keys` — fail with `AttributeError` for `assess_buy`/missing keys.
- **Impl**: `assess_buy(*, amount_ves: Decimal, amount_asset: Decimal, price: Decimal, bank_value: str | None)`; per-order checks `amount_ves <= max_buy_amount_ves` and `amount_asset <= max_usdt_per_order`; daily check `reserve("VES_SMS", ...)` only after all checks pass; implied rate `abs(amount_ves/amount_asset - price)/price <= rate_tolerance_pct/100`; bank via `banks.resolve_bank`; add buy keys to the `RiskAssessment.limits` snapshot. Reason codes exactly as named above.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_risk_policy.py::test_buy_breakers_block_in_order tests/test_risk_policy.py::test_buy_allow_path_reserves_ves_sms_only tests/test_risk_policy.py::test_buy_limits_snapshot_has_buy_keys`.
- **Deps**: 0.2, 2.2.

### 3.3 S2b gate and cherry-pick proof

- [x] Done (PR 3 / S2b)

- **What**: prove the guard is independent of the settings/breakers and that the PR is revertible.
- **Files**: none.
- **RED**: gate task.
- **Impl**: `git show --stat` the guard commits (A+B) and confirm they touch only `webhooks.py`, `sell_flow.py`, `buy_flow.py`, `enums.py`, `tests/conftest.py`, `tests/test_direction_guard.py`; confirm no settings symbol is referenced.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_direction_guard.py tests/test_config.py` then `.\.venv\Scripts\python.exe -m pytest -q`.
- **Deps**: 0.2, 3.1, 3.2.

## Phase 4 — S3a: Browser port + pure HTML parser (PR 4)

Boundary: start with no Playwright code and no `PaymentInstruction`; finish with the port, the stdlib-only parser and committed HTML fixtures. No Playwright import anywhere in the default run. Rollback: delete `ports.py`, `binance_browser.py` part 1, fixtures and tests; nothing imports them until PR 7.

### 4.1 `BinanceBrowser` port and BDV parser

- [x] Done (PR 4 / S3a)

- **What**: `PaymentInstruction` (frozen, `amount_ves: Decimal`) and the `BinanceBrowser` protocol; `parse_payment_instructions(html, order_number=...)` extracts numeric bank code, phone, cedula and exact amount from the BDV fixture.
- **Files**: `app/domain/ports.py` (new), `app/infrastructure/clients/binance_browser.py` (new), `tests/test_browser_parser.py` (new), `tests/fixtures/binance/order_view_bdv.html` (new).
- **RED**: `tests/test_browser_parser.py::test_parse_bdv_order_fixture`, `::test_parsed_amount_is_decimal` — fail with `ModuleNotFoundError` / `FileNotFoundError` before the fixture exists.
- **Impl**: parser part 1 only, stdlib `re`/`html`; `_VES_AMOUNT_RE` as in design §4; `_extract_label_fields`; return `None` on incomplete data; no `playwright` import at module level.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_browser_parser.py::test_parse_bdv_order_fixture tests/test_browser_parser.py::test_parsed_amount_is_decimal`.
- **Deps**: 0.1.

### 4.2 Banesco fixture, failure modes and lazy-import audit

- [x] Done (PR 4 / S3a)

- **What**: the parser handles both shipped banks, rejects mangled amounts and missing fields, and importing the module never imports Playwright.
- **Files**: `binance_browser.py`, `tests/test_browser_parser.py`, `tests/fixtures/binance/order_view_banesco.html` (new).
- **RED**: `tests/test_browser_parser.py::test_parse_banesco_order_fixture`, `::test_parse_missing_cedula_returns_none`, `::test_parse_mangled_amount_returns_none`, `::test_parser_import_does_not_import_playwright` — fail with `FileNotFoundError` / `AssertionError` (`playwright` in `sys.modules` only if someone adds a top-level import; the initial failure is the missing fixture/behaviour).
- **Impl**: add the Banesco fixture and the negative paths; keep `None` (never a guessed instruction) on any missing field.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_browser_parser.py`.
- **Deps**: 4.1.

### 4.3 Fake browser and port conformance

- [x] Done (PR 4 / S3a)

- **What**: `fake_browser` conforms structurally to `BinanceBrowser` and records ordered events for later call-order assertions.
- **Files**: `tests/conftest.py`, `tests/test_test_seams.py`, `tests/test_browser_parser.py`.
- **RED**: `tests/test_test_seams.py::test_fake_browser_conforms_to_binance_browser_port`, `::test_fake_browser_records_ordered_events` — fail with fixture-not-found (`fake_browser` lands here, consumed by 0.4).
- **Impl**: implement `extract_payment_instruction`/`notify_paid`, scriptable `instruction`/`extract_error`/`notify_error`, ordered events; protocol check via a structural conformance assertion (no `isinstance` requirement on a `Protocol`).
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_test_seams.py tests/test_browser_parser.py`.
- **Deps**: 4.1.

### 4.4 S3a gate and import audit

- [x] Done (PR 4 / S3a)

- **What**: prove the default run stays browser-free.
- **Files**: none.
- **RED**: gate task.
- **Impl**: run the import audit in a subprocess.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q` and `.\.venv\Scripts\python.exe -c "import sys, app.infrastructure.clients.binance_browser as m; assert 'playwright' not in sys.modules; assert hasattr(m, 'parse_payment_instructions')"`.
- **Deps**: 4.1–4.3.

## Phase 5 — S3b: Playwright adapter and browser settings (PR 5)

Boundary: start with the parser and port, no browser adapter; finish with the lazy-import adapter, its settings, and an opt-in-only live smoke. S4b does not depend on this PR. Rollback: revert PR 5; delete the adapter and settings; parser and port remain.

### 5.1 Browser settings

- [x] Done (PR 5 / S3b)

- **What**: add `binance_browser_enabled=false`, `binance_browser_headless=true`, `binance_browser_timeout_ms=20000`, `binance_session_state_path="./data/binance_state.json"`, `binance_p2p_order_url_template="https://p2p.binance.com/en/order/{order_number}"`.
- **Files**: `app/core/config.py`, `.env.example`, `tests/test_config.py`.
- **RED**: `tests/test_config.py::test_browser_settings_defaults`, `::test_browser_switches_default_off` — fail with `AttributeError`.
- **Impl**: fields + `.env.example` rows (safe defaults, so no `conftest` env required); follow the 0.2 co-change rule if any field were required (none is).
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_config.py::test_browser_settings_defaults tests/test_config.py::test_browser_switches_default_off` then `.\.venv\Scripts\python.exe -m pytest -q`.
- **Deps**: 0.2.

### 5.2 Lazy-import adapter and typed browser errors

- [x] Done (PR 5 / S3b)

- **What**: `PlaywrightBinanceBrowser` imports `playwright.async_api` only inside `__aenter__`/first call; missing/expired state → `ExternalServiceError(service="binance_browser", code="binance_session_expired")`; extraction → navigation → `page.content()` → parser; `None` → `ExternalServiceResponseError(code="payment_data_unavailable")`.
- **Files**: `binance_browser.py`, `tests/test_browser_adapter.py` (new).
- **RED**: `tests/test_browser_adapter.py::test_adapter_module_import_does_not_require_playwright`, `::test_missing_state_error_is_typed` (unit-level path) — fail with `AttributeError` for the adapter class; the live DOM test is gated and skipped by default.
- **Impl**: adapter part 2, lazy import, `new_context(storage_state=..., headless=...)`, typed errors, timeout wiring; validate the state-file existence in a **pure helper before** the lazy import so `binance_session_expired` is testable in the default run without importing Playwright; do **not** implement `notify_paid` here (S6/9.3).
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_browser_adapter.py`.
- **Deps**: 4.1, 5.1.

### 5.3 S3b gate and opt-in smoke registration

- [x] Done (PR 5 / S3b)

- **What**: keep the live smoke out of `-q` and prove the default suite is green.
- **Files**: `tests/test_browser_adapter.py` (the `integration` marker is already registered by `conftest.pytest_configure`, so no `pytest.ini` edit).
- **RED**: gate task.
- **Impl**: register the smoke with `@pytest.mark.integration` **and** `@pytest.mark.skipif(not os.getenv("LIQUIDVE_BROWSER_TESTS"), ...)`; document `LIQUIDVE_BROWSER_TESTS=1 .\.venv\Scripts\python.exe -m pytest -m integration tests/test_browser_adapter.py` as manual-only.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q` (the smoke must be skipped) and `.\.venv\Scripts\python.exe -m pytest -q -rs tests/test_browser_adapter.py` (shows the skip reason).
- **Deps**: 5.2.

## Phase 6 — S4a: Schema delta and Alembic revision (PR 6)

Boundary: start with no telemetry columns and no revision `20261123_0002`; finish with five nullable columns and a proven reversible revision. Inert: no query reads them yet. Rollback: `alembic downgrade -1` drops only nullable columns.

### 6.1 Five nullable columns and the additive revision

- [x] Done (PR 6 / S4a)

- **What**: add `counterparty_id_number`, `sms_message_id`, `sms_sent_at`, `bank_short_code`, `bank_confirmed_at` to `P2POrder`; create revision `20261123_0002_buy_payment_telemetry.py` with `down_revision="20260101_0001"`.
- **Files**: `app/infrastructure/models.py`, `alembic/versions/20261123_0002_buy_payment_telemetry.py` (new), `tests/test_migrations.py` (new).
- **RED**: `tests/test_migrations.py::test_upgrade_head_adds_nullable_telemetry_columns` — fails because the revision does not exist. Keep this test **sync** (`def test_...`): Alembic's `command.upgrade` calls `asyncio.run`, which cannot run inside a pytest-asyncio test.
- **Impl**: plain `op.add_column` on upgrade; `with op.batch_alter_table("p2p_orders")` dropping in reverse order on downgrade; no new index.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_migrations.py::test_upgrade_head_adds_nullable_telemetry_columns`.
- **Deps**: 0.1.

### 6.2 Downgrade round-trip preserves rows

- [x] Done (PR 6 / S4a)

- **What**: `upgrade head` → insert one `p2p_orders` row → `downgrade -1` → columns gone, row count unchanged → `upgrade head` again.
- **Files**: `tests/test_migrations.py`.
- **RED**: `tests/test_migrations.py::test_downgrade_minus_one_drops_columns_and_keeps_rows` — fail because the revision does not exist yet (in the same RED cycle as 6.1 if written together; otherwise fails on the missing `batch` downgrade).
- **Impl**: sync test with a temp `sqlite+aiosqlite` file, `monkeypatch.setenv("DATABASE_URL", ...)`, `get_settings.cache_clear()` before and after; Alembic `Config` + `command`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_migrations.py`.
- **Deps**: 6.1.

### 6.3 Alembic CLI gate on a fresh SQLite DB

- [x] Done (PR 6 / S4a)

- **What**: prove the operator-facing commands work outside pytest.
- **Files**: none.
- **RED**: gate task.
- **Impl / Verify** (PowerShell, repo root):
  ```powershell
  New-Item -ItemType Directory -Force data | Out-Null
  $env:DATABASE_URL = "sqlite+aiosqlite:///./data/migration_check.db"
  Remove-Item -LiteralPath .\data\migration_check.db -ErrorAction SilentlyContinue
  .\.venv\Scripts\python.exe -m alembic upgrade head
  .\.venv\Scripts\python.exe -m alembic downgrade -1
  .\.venv\Scripts\python.exe -m alembic upgrade head
  Remove-Item -LiteralPath .\data\migration_check.db
  Remove-Item Env:DATABASE_URL
  ```
- **Deps**: 6.1, 6.2.

## Phase 7 — S4b: Buy dispatch, resume and outcome classification (PR 7)

Boundary: start with `BuyFlow.execute` sending a human sentence and reaching `COMPLETED` immediately; finish with extraction via the port, B1–B5, short-code fallback, fail-closed classification, idempotent `resume()` and no `COMPLETED`. S5/S6 are not merged yet, so a confirmed order has no path to `COMPLETED` — that is the intended intermediate state. Rollback: revert PR 7; the old (premature-`COMPLETED`) behaviour returns while S5/S6 stay unmerged.

### 7.1 Gateway outcome classification

- [x] Done (PR 7 / S4b).

- **What**: pure `classify_gateway_exception` / result → `DELIVERED` | `NOT_DELIVERED` | `PERMANENT` | `UNKNOWN` exactly as design §2.
- **Files**: `app/services/sms_dispatch.py` (new), `tests/test_sms_dispatch.py` (new).
- **RED**: `tests/test_sms_dispatch.py::test_classifies_delivered`, `::test_classifies_not_delivered_4xx`, `::test_classifies_unauthorized_as_permanent`, `::test_classifies_conflict_as_unknown`, `::test_classifies_retryable_and_malformed_as_unknown` — fail with `ModuleNotFoundError`.
- **Impl**: `DispatchResult`; no I/O; `ConflictError` → `UNKNOWN`; `UnauthorizedError` → `PERMANENT`; `ExternalServiceResponseError` with `details["status"]` in 400–499 → `NOT_DELIVERED`, otherwise (no status: malformed 2xx payload) → `UNKNOWN`; retryable/rate-limited/transport/timeout → `UNKNOWN`; a returned `SmsSendResult` → `DELIVERED`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_sms_dispatch.py`.
- **Deps**: 0.1.

### 7.2 Dispatch with one classified fallback and a stable key

- [x] Done (PR 7 / S4b).

- **What**: send to the primary short code with `Idempotency-Key="{order_number}:payout"`; on exactly one `NOT_DELIVERED` fallback to `short_codes[1]` with the same key/body; a second not-delivered is permanent; `PERMANENT` never falls back.
- **Files**: `sms_dispatch.py`, `tests/test_buy_flow.py` (new), `tests/test_test_seams.py` (`fake_sms` lands here).
- **RED**: `tests/test_buy_flow.py::test_primary_not_delivered_falls_back_once_with_same_key`, `::test_second_not_delivered_is_permanent`, `::test_unauthorized_does_not_fall_back`, `::test_conflict_is_unknown_without_fallback` — fail because `fake_sms`/dispatch do not exist.
- **Impl**: `dispatch_bank_command(sms, *, short_codes, message, order_number)`; exactly one HTTP attempt per try; no gateway destination phone (the short code is the destination); record `bank_short_code` used.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_primary_not_delivered_falls_back_once_with_same_key tests/test_buy_flow.py::test_second_not_delivered_is_permanent tests/test_buy_flow.py::test_unauthorized_does_not_fall_back tests/test_buy_flow.py::test_conflict_is_unknown_without_fallback`.
- **Deps**: 0.4, 7.1.

### 7.3 B1/B2/B4 durability, extraction and the attempt identity

- [x] Done (PR 7 / S4b).

- **What**: commit B1 (`PENDING` + `ORDER_CREATED`), extract through the port, commit B2 (`SENDING_SMS` + `VES_SMS` reservation + `bank_code`/`bank_short_code`/`counterparty_id_number` + `idempotency_key`) **before** the gateway call, and commit B4 (`SMS_SENT` + `sms_message_id` + `sms_sent_at`, status still `SENDING_SMS`) after a delivered result.
- **Files**: `app/services/buy_flow.py`, `tests/test_buy_flow.py`, `tests/conftest.py`.
- **RED**: `tests/test_buy_flow.py::test_state_is_durable_before_the_gateway_call`, `::test_idempotency_key_is_order_number_payout`, `::test_extraction_persists_bank_phone_and_cedula`, `::test_delivered_send_commits_sms_telemetry_and_stays_sending_sms`, `::test_duplicate_order_number_raises_duplicate_order_error` — the durability test fails because the independent session opened by `fake_sms.on_send` sees a `SENDING_SMS` row with a **NULL** `idempotency_key`/`bank_code`/`counterparty_id_number` (today the reservation exists but B2's attempt identity does not); the telemetry test fails because `sms_message_id`/`sms_sent_at` are never written; the other tests fail for the same missing writes.
- **Impl**: adopt the `buy_flow.py:441-444` commit-before-call pattern; set the five telemetry columns as applicable; the `SMS_SENT` payload carries the short code and a masked phone (never the command text); a duplicate insert → `DuplicateOrderError`; extend `BuyFlow.__init__` with `browser: BinanceBrowser | None`; expose the internal `_dispatch(order)` used by `execute` (and later by `resume`/the operator endpoint) so an existing `PENDING` order can continue without a second insert; no `COMPLETED`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_state_is_durable_before_the_gateway_call tests/test_buy_flow.py::test_idempotency_key_is_order_number_payout tests/test_buy_flow.py::test_extraction_persists_bank_phone_and_cedula tests/test_buy_flow.py::test_delivered_send_commits_sms_telemetry_and_stays_sending_sms tests/test_buy_flow.py::test_duplicate_order_number_raises_duplicate_order_error`.
- **Deps**: 4.1, 7.2.

### 7.4 Extraction and breaker failures park in `MANUAL_REVIEW`

- [x] Done (PR 7 / S4b).

- **What**: missing cedula/bank/no browser → `MANUAL_REVIEW` + `payment_data_unavailable` and no gateway call; unsupported bank → `unsupported_destination_bank`; over cap → `amount_over_cap`; rate outside tolerance → `rate_out_of_tolerance`; daily cap → `daily_sms_cap_exceeded`.
- **Files**: `app/services/buy_flow.py`, `tests/test_buy_flow.py`, `tests/conftest.py` (`fake_browserless`).
- **RED**: `tests/test_buy_flow.py::test_incomplete_extraction_parks_payment_data_unavailable`, `::test_no_browser_parks_payment_data_unavailable`, `::test_unsupported_bank_parks_unsupported_destination_bank`, `::test_over_cap_parks_amount_over_cap`, `::test_rate_outside_tolerance_parks_rate_out_of_tolerance`, `::test_daily_sms_cap_parks_daily_sms_cap_exceeded` — fail because the pipeline does not consult `assess_buy`/the port yet.
- **Impl**: wire `RiskPolicy.assess_buy` after extraction, in spec order; reserve only on the allow path; every block appends `RISK_BLOCKED` with the reason and the limits snapshot; `EXTERNAL_ERROR` audit for a port failure (PII masked).
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py -k "parks"`.
- **Deps**: 3.2, 7.3, 0.4.

### 7.5 Unknown fate vs permanent failure (B3/B5)

- [x] Done (PR 7 / S4b).

- **What**: unknown outcome → exactly one request, stay `SENDING_SMS`, `EXTERNAL_ERROR` only; permanent → release `VES_SMS` and `SENDING_SMS → MANUAL_REVIEW` in one transaction with `sms_dispatch_failed`.
- **Files**: `app/services/buy_flow.py`, `tests/test_buy_flow.py`.
- **RED**: `tests/test_buy_flow.py::test_unknown_outcome_makes_exactly_one_request_and_stays_sending_sms`, `::test_permanent_failure_releases_reservation_and_parks_manual_review`, `::test_timeout_never_releases_the_reservation` — fail until B3/B5 exist.
- **Impl**: B3 audit-only commit; B5 single transaction (release + transition + `EXTERNAL_ERROR`), guarded by the legal `SENDING_SMS → MANUAL_REVIEW` edge so it cannot run twice; the release emits `STATUS_CHANGED` naming the amount; never release on timeout.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_unknown_outcome_makes_exactly_one_request_and_stays_sending_sms tests/test_buy_flow.py::test_permanent_failure_releases_reservation_and_parks_manual_review tests/test_buy_flow.py::test_timeout_never_releases_the_reservation`.
- **Deps**: 7.3.

### 7.6 `resume()` idempotence and no premature `COMPLETED`

- [x] Done (PR 7 / S4b).

- **What**: delete the `COMPLETED` transition at `buy_flow.py:469-477`; `resume(order_number)` never re-sends/re-reserves while `SENDING_SMS`, refuses terminal orders with a typed error, and emits one `STATUS_CHANGED` per resume.
- **Files**: `app/services/buy_flow.py`, `tests/test_buy_flow.py`.
- **RED**: `tests/test_buy_flow.py::test_dispatch_leaves_order_in_sending_sms`, `::test_resume_while_sending_sms_sends_nothing_and_reserves_nothing`, `::test_resume_while_sending_sms_does_not_reextract`, `::test_resume_on_completed_order_raises_typed_error`, `::test_resume_on_manual_review_without_approval_is_refused` — fail because `resume` does not exist and/or `COMPLETED` is still reached.
- **Impl**: A2 semantics; reuse the persisted instruction/attempt identity instead of re-extracting; terminal/`MANUAL_REVIEW` (without approval) → `DuplicateOrderError` (`code="duplicate_order"`) plus one `IDEMPOTENCY_HIT` rejection audit and no `SMS_SENT`; keep the same `Idempotency-Key`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_dispatch_leaves_order_in_sending_sms tests/test_buy_flow.py::test_resume_while_sending_sms_sends_nothing_and_reserves_nothing tests/test_buy_flow.py::test_resume_while_sending_sms_does_not_reextract tests/test_buy_flow.py::test_resume_on_completed_order_raises_typed_error tests/test_buy_flow.py::test_resume_on_manual_review_without_approval_is_refused`.
- **Deps**: 7.3.

### 7.7 Operator-approval gate (`BUY_AUTO_PAY_ENABLED=false`)

- [x] Done (PR 7 / S4b).

- **What**: a risk-approved order parks in `MANUAL_REVIEW` with `awaiting_operator_approval` and reserves nothing / calls nothing (B2a). The resume endpoint that consumes this state is 10.4.
- **Files**: `app/services/buy_flow.py`, `tests/test_buy_flow.py`.
- **RED**: `tests/test_buy_flow.py::test_gate_off_parks_manual_review_awaiting_operator_approval`, `::test_gate_off_reserves_nothing_and_calls_no_gateway` — fail because the gate is not consulted.
- **Impl**: after all breakers pass, `if not settings.buy_auto_pay_enabled and not operator_approved`: `MANUAL_REVIEW` + `manual_review_reason="awaiting_operator_approval"` + `RISK_BLOCKED`/`STATUS_CHANGED`; no `VES_SMS` reservation, no `idempotency_key`, no `SENDING_SMS`. `operator_approved` is the human override supplied only by the authenticated endpoint (10.4), never by the worker.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_gate_off_parks_manual_review_awaiting_operator_approval tests/test_buy_flow.py::test_gate_off_reserves_nothing_and_calls_no_gateway`.
- **Deps**: 3.2, 7.3.

### 7.8 `sms_gateway` cleanup and the no-retry contract

- [x] Done (PR 7 / S4b).

- **What**: delete `SmsGatewayClient.build_payment_sms` (last caller removed) and prove the real adapter sends once with the right headers.
- **Files**: `app/infrastructure/clients/sms_gateway.py`, `tests/test_buy_flow.py`, `tests/test_test_seams.py`.
- **RED**: `tests/test_buy_flow.py::test_build_payment_sms_is_removed` (asserts `not hasattr(SmsGatewayClient, "build_payment_sms")`) — fails today because the builder still exists. `::test_send_sms_contract_headers_and_no_retry` (`Idempotency-Key == "{n}:payout"`, `Authorization == "Bearer test-sms-gateway-token"`, 500 → `RetryableExternalServiceError` with `len(route.calls) == 1`, malformed 200 → `ExternalServiceResponseError`) is the contract guard proving the surviving adapter does not regress.
- **Impl**: remove the builder and its now-unused imports; keep `send_sms` signature; respx route `http://100.64.0.10:8080/messages`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_build_payment_sms_is_removed tests/test_buy_flow.py::test_send_sms_contract_headers_and_no_retry`.
- **Deps**: 0.5, 7.2.

### 7.9 S4b gate

- [x] Done (PR 7 / S4b).

- **What**: prove the intermediate state is safe and the suite is green.
- **Files**: none.
- **RED**: gate task.
- **Impl**: grep that no BUY path transitions to `COMPLETED`; confirm `build_payment_sms` has no callers.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q` and `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py tests/test_sms_dispatch.py`.
- **Deps**: 7.1–7.8.

## Phase 8 — S5: Bank-reply confirmation and webhook contract (PR 8)

Boundary: start with substring matching only and no `BankTransaction(OUTGOING)`; finish with the four-valued parser, the replay-aware anchor, the exactly-one matcher and B6. **This PR stops after B6**: it does not call `notify_paid` and does not transition to `COMPLETED` (S6 owns B7). A successful reply returns `outcome="confirmed"` with the order still `SENDING_SMS` and `bank_confirmed_at` set. Rollback: revert PR 8; `SENDING_SMS` orders stop being confirmed and stay parked; no money state is corrupted.

### 8.1 Four-valued bank-reply parser

- [x] Done (PR 8 / S5)

- **What**: `parse_bank_reply(body, profile)` → `SUCCESS` (one reference) | `FAILURE` (typed reason, no reference) | `UNPARSEABLE` | `AMBIGUOUS` (success+failure or multiple references).
- **Files**: `app/domain/bank_replies.py` (new), `tests/test_bank_replies.py` (new), `tests/fixtures/sms/*.txt` (new).
- **RED**: `tests/test_bank_replies.py::test_success_yields_one_reference`, `::test_failure_yields_typed_reason`, `::test_success_and_failure_is_ambiguous`, `::test_multiple_references_is_ambiguous`, `::test_unknown_wording_is_unparseable`, `::test_edited_failure_pattern_applies_without_code_change`, parametrised over BDV/Banesco fixtures — fail with `ModuleNotFoundError` / `FileNotFoundError`.
- **Impl**: `BankReplyOutcome` + `BankReplyParse` live in this module (not `enums.py`); exactly one reference match required; no invented reference.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_bank_replies.py`.
- **Deps**: 1.4.

### 8.2 `processed`-aware replay anchor (B0′)

- [x] Done (PR 8 / S5)

- **What**: the event row is committed before processing; a **processed** replay returns 202 `duplicate=True` and writes nothing; an **unprocessed** row is re-processed at-least-once.
- **Files**: `app/api/v1/webhooks.py`, `tests/test_buy_webhook.py` (new).
- **RED**: `tests/test_buy_webhook.py::test_processed_replay_returns_duplicate_true_and_writes_nothing`, `::test_unprocessed_row_is_reprocessed_once` — fail because the current `_store_event` treats any existing row as a duplicate.
- **Impl**: on `IntegrityError`, rollback, reload the row; `processed` → short-circuit; unprocessed → continue and mark processed at the end (+ `processing_error` on failure). Commit B0′ before any money-adjacent side effect.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_webhook.py::test_processed_replay_returns_duplicate_true_and_writes_nothing tests/test_buy_webhook.py::test_unprocessed_row_is_reprocessed_once`.
- **Deps**: 4.1, 7.9.

### 8.3 Exactly-one-candidate matcher

- [x] Done (PR 8 / S5)

- **What**: `confirm_bank_payment(message_id, sender, body, received_at)` resolves the profile, parses, and correlates only `BUY` + `SENDING_SMS` + same bank + matching amount + `coalesce(sms_sent_at, updated_at)` inside `sms_confirmation_timeout_seconds`; 0 → no match, 2+ → ambiguous/alert, foreign sender → no match.
- **Files**: `app/services/buy_flow.py`, `tests/test_buy_webhook.py`.
- **RED**: `tests/test_buy_webhook.py::test_single_success_candidate_confirms`, `::test_failure_reply_moves_manual_review_and_retains_reservation`, `::test_unparseable_reply_moves_manual_review`, `::test_ambiguous_parse_moves_manual_review`, `::test_zero_candidates_no_match`, `::test_two_candidates_ambiguous_and_no_state_change`, `::test_foreign_sender_no_match`, `::test_expired_window_no_match`, `::test_amount_mismatch_no_match` — fail because `confirm_bank_payment` does not exist.
- **Impl**: limit 2 to prove ambiguity; exact `Decimal` equality; when the reply carries an amount use `parse_ves_amount`, otherwise use the candidate baseline; profile lookup applies `apply_short_code_overrides` from settings; `FAILURE` → `MANUAL_REVIEW(bank_rejected_payment)` with the reservation retained; `UNPARSEABLE`/`AMBIGUOUS` parse → `MANUAL_REVIEW(bank_reply_unparseable|bank_reply_ambiguous)`; ambiguity across candidates raises an operator alert (log + `WEBHOOK_REJECTED(ambiguous)`), never a state change.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_webhook.py`.
- **Deps**: 8.1, 8.2.

### 8.4 B6 — one `OUTGOING` transaction, reference uniqueness

- [x] Done (PR 8 / S5)

- **What**: a confirmed reply commits `BankTransaction(direction=OUTGOING)` + `p2p_orders.bank_reference` + `bank_confirmed_at` + `SMS_RECEIVED`/`PAYMENT_VERIFIED` in one transaction; a UNIQUE collision → `MANUAL_REVIEW(duplicate_bank_reference)` and HTTP 202, never 500.
- **Files**: `app/services/buy_flow.py`, `tests/test_buy_webhook.py`.
- **RED**: `tests/test_buy_webhook.py::test_confirmation_writes_one_outgoing_transaction_with_reference`, `::test_duplicate_reference_moves_manual_review_and_returns_202`, `::test_failure_reply_writes_no_bank_transaction` — fail until B6 exists.
- **Impl**: `verification_status=NULL` on purpose; raw payload carries the masked sender; `IntegrityError` → rollback only B6's transaction (B0′ already committed) → reload → transition → 202 `matched`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_webhook.py::test_confirmation_writes_one_outgoing_transaction_with_reference tests/test_buy_webhook.py::test_duplicate_reference_moves_manual_review_and_returns_202 tests/test_buy_webhook.py::test_failure_reply_writes_no_bank_transaction`.
- **Deps**: 8.3, 6.1.

### 8.5 Enriched response and the thin poller adapter

- [x] Done (PR 8 / S5)

- **What**: `SmsWebhookResponse` carries `outcome`/`order_number`/`reason`; `handle_incoming_sms` becomes a thin adapter over `confirm_bank_payment`, so the poller gets the same dedupe and vocabulary.
- **Files**: `app/domain/schemas.py`, `app/api/v1/webhooks.py`, `app/services/buy_flow.py`, `tests/test_buy_webhook.py`.
- **RED**: `tests/test_buy_webhook.py::test_sms_webhook_response_carries_outcome_and_order_number`, `::test_poller_path_uses_same_confirmation_vocabulary` — fail because the response model/adapter do not exist.
- **Impl**: additive `SmsConfirmation`/`SmsWebhookResponse` (subclass of `WebhookAcceptedResponse`); all eight rows of design §9's outcome table except the `COMPLETED` row; audit `WEBHOOK_RECEIVED` per accepted delivery, `SMS_RECEIVED` on confirmation, `WEBHOOK_REJECTED` on ambiguous/no-match; **no notify, no `COMPLETED`**.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_webhook.py`.
- **Deps**: 8.3, 8.4.

### 8.6 S5 gate

- [x] Done (PR 8 / S5)

- **What**: prove the intermediate "confirmed, close pending" state is visible and safe.
- **Files**: none.
- **RED**: gate task.
- **Impl**: confirm a confirmed order is `SENDING_SMS` with `bank_confirmed_at` set and exactly one `OUTGOING` row; confirm no `notify_paid` call exists in `buy_flow.py`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q` and `rg -n "notify_paid" app/services/buy_flow.py` (must be empty).
- **Deps**: 8.1–8.5.

## Phase 9 — S6: Binance close and the `COMPLETED` boundary (PR 9)

Boundary: start with confirmed orders stuck in `SENDING_SMS`; finish with `notify_paid` through the port, `COMPLETED` only after success, and `binance_close_failed` on failure/unknown. Rollback: revert PR 9; confirmed orders remain `SENDING_SMS` awaiting manual close (safe, visible).

### 9.1 Successful close reaches `COMPLETED`

- [x] Done (PR 9 / S6)

- **What**: after B6, call `browser.notify_paid(order_number, reference)`; only on success transition `SENDING_SMS → COMPLETED` with a masked-reference `STATUS_CHANGED`.
- **Files**: `app/services/buy_flow.py`, `tests/test_buy_close.py` (new).
- **RED**: `tests/test_buy_close.py::test_success_closes_order_with_masked_reference`, `::test_close_is_attempted_only_after_confirmation` — fail because no close call exists.
- **Impl**: pass the reference; mask it with `mask_secret(reference, visible=4)` from `security.py` in audit payloads; no PII/command text in logs.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_close.py::test_success_closes_order_with_masked_reference tests/test_buy_close.py::test_close_is_attempted_only_after_confirmation`.
- **Deps**: 4.1, 8.4, 8.5.

### 9.2 Failure or unknown close never completes

- [x] Done (PR 9 / S6)

- **What**: a failing/uncertain `notify_paid` → `MANUAL_REVIEW` + `binance_close_failed`; no `COMPLETED` entry exists; no automatic re-invocation.
- **Files**: `app/services/buy_flow.py`, `tests/test_buy_close.py`.
- **RED**: `tests/test_buy_close.py::test_failed_close_never_completes`, `::test_unknown_close_outcome_is_not_retried`, `::test_confirmed_order_with_failed_close_is_queryable` — fail until the failure branch exists.
- **Impl**: guard the transition with `bank_confirmed_at IS NOT NULL` and `status == SENDING_SMS`; leave `bank_confirmed_at` set for operator recovery; one attempt only.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_close.py::test_failed_close_never_completes tests/test_buy_close.py::test_unknown_close_outcome_is_not_retried tests/test_buy_close.py::test_confirmed_order_with_failed_close_is_queryable`.
- **Deps**: 9.1.

### 9.3 Close runs at most once; real adapter `notify_paid`

- [x] Done (PR 9 / S6)

- **What**: a second webhook for a confirmed/terminal order cannot re-close; implement the adapter's DOM click + chat reference, raising on uncertainty.
- **Files**: `app/services/buy_flow.py`, `app/infrastructure/clients/binance_browser.py`, `tests/test_buy_close.py`, `tests/test_browser_adapter.py`.
- **RED**: `tests/test_buy_close.py::test_adapter_exposes_notify_paid` (fails with `AttributeError`: 5.2 deliberately left `notify_paid` unimplemented), and the gated `tests/test_browser_adapter.py::test_notify_paid_raises_on_uncertain_dom` (skipped by default; opt-in only). `tests/test_buy_close.py::test_close_called_at_most_once_for_a_confirmed_order` is the call-once guard proving the replay path cannot add a second notify.
- **Impl**: keep the four independent blocks in the confirmation path (candidate filter `bank_reference IS NULL` + `bank_confirmed_at IS NULL`, `message_id` dedupe, `status == SENDING_SMS`) and invoke `notify_paid` only from the B6 success branch, so it cannot be reached twice; adapter clicks “Transferido, notificar al vendedor”, posts the reference, confirms the resulting DOM, raises otherwise.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_close.py` and `.\.venv\Scripts\python.exe -m pytest -q tests/test_browser_adapter.py`.
- **Deps**: 9.1, 5.2.

### 9.4 S6 gate

- [x] Done (PR 9 / S6)

- **What**: prove `COMPLETED` is reachable only through `notify_paid` success.
- **Files**: none.
- **RED**: gate task.
- **Impl**: audit every `OrderStatus.COMPLETED` assignment in `buy_flow.py`; there must be exactly one, inside the success branch.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q` and `rg -n "OrderStatus.COMPLETED" app/services/buy_flow.py`.
- **Deps**: 9.1–9.3.

## Phase 10 — S7: Buy worker, operator endpoint and lifecycle (PR 10)

Boundary: start with `BuyFlow` unreachable and only the SMS poller running; finish with detection, the timeout sweep, the authenticated operator endpoint, optional browser lifecycle and cancellation-safe shutdown. Rollback: set `BUY_WORKER_ENABLED=false`, `BUY_AUTO_PAY_ENABLED=false`, `BINANCE_BROWSER_ENABLED=false` and remove the `orders` router registration; all data preserved.

### 10.1 `get_order_history` filters via respx URL regex

- [x] Done (PR 10 / S7)

- **What**: optional `trade_type`/`order_status` kwargs on the Binance history call; `tradeType=BUY` and the external `PENDING_PAYMENT` filter are observable on the pre-signed query.
- **Files**: `app/infrastructure/clients/binance_client.py`, `tests/test_buy_worker.py` (new), `tests/test_test_seams.py`.
- **RED**: `tests/test_buy_worker.py::test_history_sends_trade_type_and_status`, `::test_history_401_raises_unauthorized`, `::test_history_500_is_retried_because_read_is_idempotent` — fail with `TypeError` for the unknown kwargs / helper missing.
- **Impl**: add kwargs to the signed params; keep `idempotent=True`; respx matcher `url__regex=r".*/sapi/v1/c2c/orderMatch/listUserOrderHistory\?.*"` and assert `request.url.params["tradeType"] == "BUY"` (never `params__contains`).
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_worker.py::test_history_sends_trade_type_and_status tests/test_buy_worker.py::test_history_401_raises_unauthorized tests/test_buy_worker.py::test_history_500_is_retried_because_read_is_idempotent`.
- **Deps**: 0.5.

### 10.2 Worker tick: detect BUY/PENDING_PAYMENT once

- [x] Done (PR 10 / S7)

- **What**: `_buy_worker_tick(app)` fetches history, ignores non-BUY/non-`PENDING_PAYMENT`, skips known `order_number`s, persists B1 once (`ORDER_CREATED`), and runs the pipeline; with the gate off the order parks `awaiting_operator_approval`.
- **Files**: `app/main.py`, `app/core/config.py`, `.env.example`, `tests/test_buy_worker.py`, `tests/test_config.py`.
- **RED**: `tests/test_buy_worker.py::test_tick_persists_pending_payment_buy_order_once`, `::test_tick_ignores_sell_orders`, `::test_tick_skips_known_order_numbers`, `::test_tick_with_gate_off_parks_manual_review`, `::test_tick_never_sleeps`, plus `tests/test_config.py::test_worker_settings_default_off` — fail with `AttributeError` (`_buy_worker_tick` missing) / missing settings.
- **Impl**: settings `buy_worker_enabled=false`, `buy_worker_poll_interval_seconds=30` (follow the 0.2 ordering if any becomes required); tick calls `get_order_history(trade_type=BUY, order_status="PENDING_PAYMENT")` once; `order_type == BUY` enforced by `BuyFlow.execute` (3.1); no `asyncio.sleep` in the test path.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_worker.py tests/test_config.py::test_worker_settings_default_off`.
- **Deps**: 3.1, 7.7, 10.1.

### 10.3 Timeout sweep flags, never acts

- [x] Done (PR 10 / S7)

- **What**: one flag per stale `SENDING_SMS` window (`sms_confirmation_timeout`); status unchanged, reservation retained, no resend.
- **Files**: `app/main.py` (or `app/services/buy_flow.py` helper), `tests/test_buy_worker.py`.
- **RED**: `tests/test_buy_worker.py::test_sweep_flags_stale_sending_sms_without_resend_or_release`, `::test_sweep_does_not_duplicate_flag`, `::test_sweep_ignores_orders_inside_the_window` — fail with `AttributeError` for the sweep.
- **Impl**: query `coalesce(sms_sent_at, updated_at) + timeout < now`; append `EXTERNAL_ERROR` with `reason=sms_confirmation_timeout`; check for an existing flag in the same window before writing; freezegun for `T + timeout + 1s`.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_worker.py -k sweep`.
- **Deps**: 7.5.

### 10.4 Operator endpoint and resume (R-BUY-005)

- [x] Done (PR 10 / S7)

- **What**: `POST /api/v1/orders/buy/{order_number}/pay` behind `require_internal_api_key`: `MANUAL_REVIEW + awaiting_operator_approval` → `MANUAL_REVIEW → PENDING` then the pipeline; `PENDING`/`PAYMENT_DETECTED`/`VERIFYING_CACHICAMO` → pipeline; `SENDING_SMS` → 200 no-op; `COMPLETED`/`FAILED` → 409 `terminal_status`; second submission is a no-op.
- **Files**: `app/api/v1/orders.py` (new), `app/domain/schemas.py`, `app/main.py` (router registration), `tests/test_orders_api.py` (new).
- **RED**: `tests/test_orders_api.py::test_missing_api_key_is_401`, `::test_unauthenticated_attempt_emits_rejection_audit`, `::test_unknown_order_is_404`, `::test_sell_order_is_409`, `::test_gate_off_park_resumes_and_dispatches` (enable `buy_auto_pay_enabled=True` for the resume call), `::test_second_resume_is_noop`, `::test_sending_sms_is_noop_200`, `::test_completed_is_409_terminal_status`, `::test_response_contains_no_amount_phone_or_cedula` — fail with 404/`ImportError` for the missing router.
- **Impl**: `BuyPayRequest(destination_phone: str | None, reason: str | None ≤280)` (body optional); `BuyPayResponse` exactly as design §9; normalize the phone, never echo it; `AuditActor.OPERATOR`; on `UnauthorizedError` write an `AuditLog(event=WEBHOOK_REJECTED, order_id=None, actor=SYSTEM)` before re-raising (R-BUY-005 audit); for the parked branch perform `MANUAL_REVIEW → PENDING` then call `_dispatch(order, operator_approved=True)` (the human is the approval, so the auto-pay gate does not re-park it); `resume()` refusal of unapproved `MANUAL_REVIEW` is unchanged; 202 when state changed, 200 for no-op; errors through the existing envelope.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_orders_api.py`.
- **Deps**: 7.6, 7.7, 8.5.

### 10.5 Lifespan: optional browser, worker loop, cancellation

- [x] Done (PR 10 / S7)

- **What**: construct the browser only when `binance_browser_enabled`; a missing browser surfaces as a typed `binance_browser` error, never a crash; start/cancel the buy worker safely; register the `orders` router.
- **Files**: `app/main.py`, `tests/test_buy_worker.py`, `tests/test_orders_api.py`, `tests/test_health.py` (no regression).
- **RED**: `tests/test_buy_worker.py::test_lifespan_starts_and_cancels_buy_worker`, `::test_missing_browser_surfaces_typed_error_not_crash`, `::test_worker_disabled_starts_nothing` — fail because the lifespan does not manage the worker/browser.
- **Impl**: A12 lifecycle; `suppress(asyncio.CancelledError)` on shutdown; router mounted next to `webhooks` (`main.py:217-218`).
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_worker.py tests/test_orders_api.py tests/test_health.py`.
- **Deps**: 10.2, 10.4, 5.2.

### 10.6 S7 gate and rollout order

- [x] Done (PR 10 / S7)

- **What**: prove the whole feature is reachable only behind its flags and document the enablement sequence.
- **Files**: none.
- **RED**: gate task.
- **Impl / Verify**: `.\.venv\Scripts\python.exe -m pytest -q`; confirm defaults `BUY_AUTO_PAY_ENABLED=false`, `BUY_WORKER_ENABLED=false`, `BINANCE_BROWSER_ENABLED=false`; enablement order: deploy all-false → `BUY_WORKER_ENABLED=true` (orders park) → operator approvals with a small cap → `BUY_AUTO_PAY_ENABLED=true` → `BINANCE_BROWSER_ENABLED=true` once the session state file exists.
- **Deps**: 10.1–10.5.

## Phase 11 — Release gate

### 11.1 Full offline suite, lint and import audit

- [x] Done (Phase 11)

- **What**: prove the success criteria for a fully offline run.
- **Files**: none.
- **RED**: gate task.
- **Impl**: run the whole suite, the linter, and the browser-import audit.
- **Verify**: `.\.venv\Scripts\python.exe -m pytest -q`, `.\.venv\Scripts\python.exe -m ruff check app tests`, `.\.venv\Scripts\python.exe -c "import sys, app.main; assert 'playwright' not in sys.modules"`.
- **Deps**: all phases.

### 11.2 Spec traceability for `sdd-verify`

- [x] Done (Phase 11)

- **What**: map every requirement/scenario to its test node IDs; list any scenario with no direct test before verify runs.
- **Files**: none (input to `verify-report.md`).
- **RED**: gate task.
- **Impl**: walk the requirement→task table above; expand it to scenario→test using `rg -n "R-BUY|R-BANK" tests/`.
- **Verify**: `rg -n "R-BUY-0|R-BANK-0" tests/` and confirm 18 requirement IDs (11 BUY + 7 BANK) are covered.
- **Deps**: 11.1.

### 11.3 Opt-in live smoke protocol (never in `-q`)

- [x] Done (Phase 11)

- **What**: record the only manual/browser step and keep it outside CI.
- **Files**: none.
- **RED**: gate task.
- **Impl**: `LIQUIDVE_BROWSER_TESTS=1 .\.venv\Scripts\python.exe -m pytest -m integration tests/test_browser_adapter.py` with a real `BINANCE_SESSION_STATE_PATH`; assert extraction on a fixture order and a no-op close on a test order. Never run against a live money order.
- **Verify**: the command above (manual, opt-in) plus `.\.venv\Scripts\python.exe -m pytest -q` (must not collect it).
- **Deps**: 5.3, 9.3, 11.1.

## Review Workload Forecast

Estimated changed lines: 1,870-2,570
400-line budget risk: Medium
Chained PRs recommended: Yes
Decision needed before apply: No
Suggested work units: 10
Chain strategy: stacked-to-main
Delivery strategy: auto-chain

**Justification.** The change is a cross-cutting money path (domain formatting, risk policy, browser port, schema, webhook confirmation, worker, operator API) and cannot fit one review. Ten stacked PRs each forecast at 100–400 authored lines: every unit stays far below the mandated 800-line review budget, and only S5 sits at the 400-line default guard's edge (300–400) because parser + matcher + replay anchor must land together for the idempotency contract to hold. The security direction guard is isolated as a settings-free commit pair (PR 3, commits A+B), so it can be cherry-picked and reverted independently. No further split is justified: splitting S5 would place the replay anchor and the confirmation in different PRs, and splitting S6 would create a state no slice can verify. `auto-chain` + `stacked-to-main` is already decided, so no decision is needed before apply; apply starts at PR 1 (S1).

| PR | Slices | Authored lines | Dependency |
|---|---|---|---|
| PR 1 | S1 — bank SMS domain | 180–260 | none (first autonomous slice) |
| PR 2 | S2a — RiskPolicy + delegation | 150–200 | PR 1 (chain base; no code import) |
| PR 3 | S2b — settings + buy caps + direction guard | 170–230 | PR 2; guard commits A+B independently cherry-pickable |
| PR 4 | S3a — port + pure HTML parser | 230–300 | PR 1; chain order after PR 3 |
| PR 5 | S3b — Playwright adapter + settings | 150–190 | PR 4; not a code dependency of S4b |
| PR 6 | S4a — models + migration | 100–140 | PR 3; chain order after PR 5 |
| PR 7 | S4b — dispatch/resume/classification | 250–330 | PR 1, PR 3, PR 4 |
| PR 8 | S5 — confirmation + webhook contract | 300–400 | PR 7; B6 only, no close |
| PR 9 | S6 — Binance close | 160–240 | PR 4, PR 8 |
| PR 10 | S7 — worker + operator endpoint + lifecycle | 180–280 | PR 1, PR 3, PR 7, PR 8, PR 9 |

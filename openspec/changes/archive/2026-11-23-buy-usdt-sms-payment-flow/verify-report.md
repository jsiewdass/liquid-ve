```yaml
schema: gentle-ai.verify-result/v1
evidence_revision: sha256:552f7751620e0120db756af96975dd90e3368b3ea608d15950feb8bab501bc25
verdict: pass_with_warnings
blockers: 0
critical_findings: 0
requirements: 18/18
scenarios: 44/44
test_command: .\.venv\Scripts\python.exe -m pytest -q
test_exit_code: 0
test_output_hash: sha256:c0786037ea739452ae4d40138c719e87b530fb428f760f61c3b201e2f593202e
build_command: .\.venv\Scripts\python.exe -m ruff check app tests
build_exit_code: 0
build_output_hash: sha256:82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18
```

> **Re-verification Round 1 supersedes this baseline. Current verdict: PASS WITH WARNINGS.** The findings below are the historic baseline and are retained verbatim. The C1/C2 money-direction defects are RESOLVED; the current status of every item is in `## Re-verification Round 1` at the end of this file.

# Verification Report

**Change**: `buy-usdt-sms-payment-flow`
**Version**: N/A (delta specs, no version field)
**Mode**: Strict TDD (active per `openspec/config.yaml` `strict_tdd: true`)
**HEAD**: `fe7e93a` — all ten PRs exist only in the working tree (`git status`: 15 modified, 21 untracked). Nothing was committed, pushed or fixed by this verification.
**Authoritative counts**: 18 requirements (11 R-BUY + 7 R-BANK), 44 scenarios (29 buy-flow + 15 bank-sms).
**Verdict**: **FAIL** — the runtime suite is green and 17 of 18 requirements are substantially implemented, but two blocking CRITICAL defects contradict explicit requirement text and move money in a wrong direction: (C1) the external order status is never re-validated before dispatch, and (C2) the shared `VES` daily budget is charged and committed for BUY orders that never dispatch.

---

## Completeness

| Metric | Value |
|---|---|
| Tasks total (`### n.n` headers) | 58 |
| Tasks with delivered work | 58 |
| Tasks checked `- [x]` in `tasks.md` | 56 |
| Tasks incomplete / unchecked | 0 |
| Tasks with no checkbox at all | 2 (`0.4`, `0.5` — deliverables exist) |
| Requirement IDs covered by a passing test | 18 / 18 |
| Scenarios covered by a passing test | 44 / 44 |

No unchecked task blocks full verification. `0.4` (provider doubles) and `0.5` (respx seams) are marked done only in the apply-progress narrative; their artifacts exist (`FakeBrowser`/`FakeSms`/`fake_browserless`, `binance_history_route` seam test). Recorded as WARNING W7.

---

## Build & Tests Execution

All commands were run by this verification in `C:\Cursos\liquidVE` on 2026-11-23. Nothing was taken on trust.

| # | Command | Exit | Result |
|---|---|---|---|
| 1 | `.\.venv\Scripts\python.exe -m pytest -q` (declared test command) | 0 | collected 260; **258 passed, 2 skipped** (summary observed on the companion run; `pytest.ini` adds a second `-q`, so the literal command is `-qq` and omits the summary line). Output hash `2ea824e8…56e770` (324 bytes). |
| 2 | `.\.venv\Scripts\python.exe -m pytest` (`-q` from `addopts`) | 0 | **258 passed, 2 skipped in 15.47s** |
| 3 | `.\.venv\Scripts\python.exe -m pytest tests/test_buy_flow.py tests/test_buy_webhook.py tests/test_buy_close.py tests/test_buy_worker.py tests/test_orders_api.py tests/test_direction_guard.py tests/test_banks.py tests/test_bank_replies.py tests/test_migrations.py` | 0 | **136 passed in 9.03s** |
| 4 | `.\.venv\Scripts\python.exe -m ruff check app tests` (declared lint) | 0 | `All checks passed!` Output hash `82b3e6a6…6b4f18`. |
| 5 | `.\.venv\Scripts\python.exe -c "…configure_logging…; print('BOOT OK')"` | 0 | `BOOT OK` |
| 6 | `.\.venv\Scripts\python.exe -c "import sys, app.main; assert 'playwright' not in sys.modules"` | 0 | `browser-free OK` |
| 7 | `.\.venv\Scripts\python.exe -m pytest -rs tests/test_browser_adapter.py` | 0 | 20 passed, 2 skipped (`LIQUIDVE_BROWSER_TESTS` opt-in smokes) |
| 8 | `.\.venv\Scripts\python.exe -m mypy app` (advisory) | 1 | **31 errors in 7 files** (baseline recorded as 30 with no config; 1 new). Not a gate. |
| 9 | `alembic upgrade head` → `downgrade -1` → `upgrade head` on a temp SQLite file | 0/0/0 | all three green; `20261123_0002` adds and drops the five nullable columns |
| 10 | Independent probe `probe_ledger.py` (this verification) | 0 | gate-off: `MANUAL_REVIEW/awaiting_operator_approval`, 0 SMS, **ledger `VES=1500.00` count=1**, `VES_SMS=0.00`; no-browser: `payment_data_unavailable`, 0 SMS, **`VES=1500.00` count=1** |
| 11 | Independent probe `probe_cancelled.py` (this verification) | 0 | a BUY detail with external status **`CANCELLED`** is persisted and **dispatched**: `sms_calls=1`, `Pagar 0102 04141234567 12345678 1500,00` → `2661` |
| 12 | `.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing` | 0 | 258 passed, 2 skipped; total app coverage 80% |

Output capture note: PowerShell `Out-File`/`cmd` redirection of `pytest -q` loses the final summary line because `addopts = -q` plus the explicit `-q` yields `-qq`; the count line above was captured from the `-q`-less run. The envelope `test_output_hash` is the SHA-256 of the literal declared command's exact (324-byte) output.

---

## Spec Compliance Matrix (18 requirements, 44 scenarios)

Status legend: ✅ COMPLIANT · ⚠️ PARTIAL (tests pass, clause under-covered/violated) · ❌ NON-COMPLIANT.

| Req | Implementation (file:line) | Direct covering tests (file:line node) | Result |
|---|---|---|---|
| **R-BUY-001** detection & status mapping | `app/main.py:93-128` (BUY + `PENDING_PAYMENT` history filter, skip known), `app/services/buy_flow.py:379-414` (BUY check, persist `PENDING`, `ORDER_CREATED`), `buy_flow.py:385-397` | `tests/test_buy_worker.py:163 test_tick_persists_pending_payment_buy_order_once` (repeat tick keeps `ORDER_CREATED`=1), `:206 test_tick_ignores_sell_orders`, `:226 test_tick_skips_known_order_numbers`, `:111 test_history_sends_trade_type_and_status` | ❌ **NON-COMPLIANT** — C1. The externally-authoritative `detail.status` is never checked at `buy_flow.py:378-384`; a `CANCELLED` detail dispatches (probe #11). No test exercises a non-`PENDING_PAYMENT` detail. |
| **R-BUY-002** idempotent processing & resume | `buy_flow.py:167-183`, `:399-406`, `:820-876` | `tests/test_buy_flow.py:437 test_duplicate_order_number_raises_duplicate_order_error`, `:739 test_resume_while_sending_sms_sends_nothing_and_reserves_nothing`, `:774 …does_not_reextract`, `:799 test_resume_on_completed_order_raises_typed_error`, `:824 test_resume_on_manual_review_without_approval_is_refused` | ✅ COMPLIANT |
| **R-BUY-003** payment-data extraction | `buy_flow.py:540-608` (`_extract`), `:455-456`, `:517-521` | `tests/test_buy_flow.py:392 test_extraction_persists_bank_phone_and_cedula`, `:462 test_incomplete_extraction_parks_payment_data_unavailable`, `:485 test_no_browser_parks_payment_data_unavailable`, `:774` (no re-extract while `SENDING_SMS`) | ⚠️ PARTIAL — W6: nothing is persisted at the gate-off park, so operator approval re-scrapes instead of reusing a persisted instruction. |
| **R-BUY-004** pre-dispatch breakers | `app/services/risk_policy.py:226-309` (ordered `assess_buy`), `:361-418` (`reserve`/`release`), `buy_flow.py:458-483`, `:504-514` | `tests/test_risk_policy.py:453 test_buy_breakers_block_in_order`, `:493 test_buy_allow_path_reserves_ves_sms_only`, `:521 test_buy_limits_snapshot_has_buy_keys`, `:257 test_daily_cap_resets_at_utc_rollover`, `:242 test_reserve_refuses_when_projected_over_limit`, `:289 test_release_clamps_at_zero_and_decrements_count`, `:306 test_release_rejects_unknown_channel`; `tests/test_buy_flow.py:530/:556/:579/:509` (each block reason) | ⚠️ PARTIAL — W1: the gate-off park performs a reservation before releasing it, contrary to “released only on a confirmed permanent dispatch failure” and design B2a. |
| **R-BUY-005** operator-approval gate | `buy_flow.py:475-483` (park), `app/api/v1/orders.py:50-85` (auth+audit), `:206-232` (approval→`PENDING`→pipeline) | `tests/test_buy_flow.py:851 test_gate_off_parks_manual_review_awaiting_operator_approval`, `:872 test_gate_off_reserves_nothing_and_calls_no_gateway`; `tests/test_orders_api.py:52 test_missing_api_key_is_401`, `:59 test_unauthenticated_attempt_emits_rejection_audit`, `:101 test_gate_off_park_resumes_and_dispatches`, `:142 test_second_resume_is_noop`, `:171 test_sending_sms_is_noop_200`, `:191 test_completed_is_409_terminal_status` | ❌ **NON-COMPLIANT** — C2: “MUST reserve no budget” is violated on the shared `VES` channel (probe #10). The VES_SMS assertion in test 872 is real but only covers the outgoing channel. |
| **R-BUY-006** durable dispatch | `buy_flow.py:516-538` (B2 commit at 524 before call at 526), `:610-724` (B3/B4/B5), `app/services/sms_dispatch.py:131-226`, `app/infrastructure/clients/sms_gateway.py:94-139` (`idempotent=False`) | `tests/test_buy_flow.py:319 test_state_is_durable_before_the_gateway_call` (independent session at send time sees `SENDING_SMS` + key + bank + cedula), `:367 test_idempotency_key_is_order_number_payout`, `:181/:205/:229/:246/:263/:283` (fallback/classification), `:607`, `:634`, `:899 test_build_payment_sms_is_removed`, `:905 test_send_sms_contract_headers_and_no_retry`; `tests/test_sms_dispatch.py:39-71` (5 classification tests) | ✅ COMPLIANT |
| **R-BUY-007** reply correlation & confirmation | `buy_flow.py:899-946` (entry), `:947-1009` (B0′), `:1031-1107` (profile/parse/exactly-one), `:1109-1152` (matcher), `:1154-1185` (rejections) | `tests/test_buy_webhook.py:226 test_single_success_candidate_confirms`, `:423 test_two_candidates_ambiguous_and_no_state_change`, `:401 test_zero_candidates_no_match`, `:462 test_foreign_sender_no_match`, `:490 test_expired_window_no_match`, `:520 test_amount_mismatch_no_match`, `:349 test_unparseable_reply_moves_manual_review`, `:375 test_ambiguous_parse_moves_manual_review`, `:150 test_processed_replay_returns_duplicate_true_and_writes_nothing`, `:183 test_unprocessed_row_is_reprocessed_once`, `:610/:637/:672/:700`; `tests/test_bank_replies.py:44-139` | ⚠️ PARTIAL — W5 (amount condition dropped when the reply carries no amount); S2 (alert is a log line only). |
| **R-BUY-008** outgoing transaction & reference uniqueness | `buy_flow.py:1216-1264` (B6), `:1338-1376` (collision → review), `app/infrastructure/models.py:128`, `:180` | `tests/test_buy_webhook.py:265 test_confirmation_writes_one_outgoing_transaction_with_reference`, `:547 test_duplicate_reference_moves_manual_review_and_returns_202` (asserts HTTP 202 + `MANUAL_REVIEW` + zero rows), `:587 test_failure_reply_writes_no_bank_transaction`; `tests/test_idempotency.py:52 test_duplicate_bank_reference_raises` | ✅ COMPLIANT |
| **R-BUY-009** close & `COMPLETED` boundary | `buy_flow.py:1266-1278` (post-B6 gate), `:1280-1336` (`notify_paid` → `COMPLETED`; only `OrderStatus.COMPLETED` transition in the module at 1323-1334) | `tests/test_buy_close.py:216 test_success_closes_order_with_masked_reference`, `:267 test_close_is_attempted_only_after_confirmation`, `:311`, `:353 test_failed_close_never_completes`, `:420 test_unknown_close_outcome_is_not_retried`, `:467 test_confirmed_order_with_failed_close_is_queryable`, `:507 test_missing_browser_leaves_confirmed_order_pending`, `:547 test_close_called_at_most_once_for_a_confirmed_order`, `:604 test_adapter_exposes_notify_paid` | ⚠️ PARTIAL — W3: confirmed-but-unclosed orders are stranded (`confirmed` reported, no operator re-attempt). |
| **R-BUY-010** fail-closed timeout | `buy_flow.py:726-774` (`sweep_timeouts`), `app/main.py:128` (one sweep per tick) | `tests/test_buy_worker.py:279 test_sweep_flags_stale_sending_sms_without_resend_or_release`, `:325 test_sweep_does_not_duplicate_flag`, `:353 test_sweep_ignores_orders_inside_the_window`; `tests/test_buy_flow.py:660 test_timeout_never_releases_the_reservation` | ⚠️ PARTIAL — W4 (per-order-ever dedupe, not per window; concurrent ticks untested). |
| **R-BUY-011** direction guard | `app/api/v1/webhooks.py:304-342` (`order_type == SELL` predicate at 328), `:267-301` (defence-in-depth), `app/services/sell_flow.py:325-332`, `buy_flow.py:379-384` | `tests/test_direction_guard.py:103 test_equal_amounts_do_not_cross_directions` (asserts `release_calls == [sell]`, BUY untouched, no `PAYMENT_VERIFIED`), `:149 test_only_buy_candidate_yields_no_match`, `:190 test_direction_mismatch_is_audited_after_load`, `:225 test_sell_flow_refuses_buy_order_before_bank_transaction`, `:262 test_sell_flow_execute_refuses_buy_detail`, `:289 test_buy_flow_refuses_sell_order` | ✅ COMPLIANT |
| **R-BANK-001** declarative profile | `app/domain/banks.py:57-127` | `tests/test_banks.py:97 test_profile_carries_routing_data`, `:115 test_registry_is_immutable` | ✅ COMPLIANT |
| **R-BANK-002** bank registry | `banks.py:86-123` | `tests/test_banks.py:108 test_shipped_registry_primary_short_codes`, `:115` | ✅ COMPLIANT |
| **R-BANK-003** resolve semantics | `banks.py:172-208` | `tests/test_banks.py:126 test_code_and_alias_both_resolve`, `:139 test_unsupported_bank_returns_none`, `:144 test_ambiguous_alias_returns_none`, `:149 test_profile_for_sender_uses_short_code_index`, `:156 test_short_code_overrides_replace_primary_first` | ✅ COMPLIANT |
| **R-BANK-004** amount formatting | `banks.py:247-269` | `tests/test_banks.py:40 test_format_ves_amount_matches_venezuelan_format`, `:45 test_format_ves_amount_rejects_float`, `:51 test_format_ves_amount_rejects_non_positive` | ✅ COMPLIANT |
| **R-BANK-005** phone/cedula normalization | `banks.py:272-327` | `tests/test_banks.py:59 test_e164_phone_becomes_national_digits`, `:64 test_national_phone_is_idempotent`, `:70 test_cedula_digits_and_optional_prefix`, `:77 test_cedula_rejects_non_normalizable_input` | ✅ COMPLIANT |
| **R-BANK-006** exact command rendering | `banks.py:357-397` | `tests/test_banks.py:167 test_command_matches_product_example`, `:178 test_banesco_command_matches_example`, `:190 test_missing_cedula_raises_and_renders_nothing`, `:208 test_rendered_command_has_no_stray_whitespace` | ✅ COMPLIANT |
| **R-BANK-007** reply parsing contract | `app/domain/bank_replies.py:80-137` | `tests/test_bank_replies.py:44 test_success_yields_one_reference`, `:56 test_failure_yields_typed_reason`, `:66 test_success_and_failure_is_ambiguous`, `:76 test_multiple_references_is_ambiguous`, `:87 test_unknown_wording_is_unparseable`, `:100 test_edited_failure_pattern_applies_without_code_change`, `:114`, `:125`, `:139` | ✅ COMPLIANT |

**Compliance summary**: 15/18 requirements compliant (2 non-compliant, 5 partial counting overlaps; 9 requirements carry at least one partial). All 44 scenarios have at least one direct covering test; no scenario is `UNTESTED`. The one untested *sub-clause* is R-BUY-001's external-status enforcement at the detail/dispatch step (finding C1).

---

## Invariant Proofs

### I1 — Money is `Decimal` only (PROVEN)
- Every money field/parameter is `Decimal`: `app/domain/ports.py:49` + `__post_init__` rejects non-`Decimal` (`ports.py:60-63`), `app/infrastructure/clients/binance_client.py:89-91` (`price`/`amount`/`total_price`), `models.py` `Numeric` columns.
- Float inputs are explicitly refused: `app/domain/banks.py:262-263`, `app/services/risk_policy.py:55-60` (`_ensure_decimal`), called for all three money arguments at `risk_policy.py:255-257`.
- A full `float` grep across `app/` finds only timeout/latency settings (`config.py:76,82-86`, `schemas.py:173`), the two explicit rejections, and prose. No float arithmetic on amounts. `implied = amount_ves / amount_asset` is `Decimal/Decimal` (`risk_policy.py:330-331`).

### I2 — `SENDING_SMS` is durably committed before the gateway call (PROVEN)
`buy_flow.py:516-524`: assignment of `bank_code`/`bank_short_code`/`counterparty_phone`/`counterparty_id_number`/`idempotency_key`, then `_advance_to_sending(...)`, then `await self._session.commit()`; the gateway call begins only at `:526`. Proven at runtime by `tests/test_buy_flow.py:319 test_state_is_durable_before_the_gateway_call`, which opens an **independent session** inside the fake gateway's `on_send` hook and reads `status == SENDING_SMS`, `idempotency_key == "ORD-DURABLE:payout"`, `bank_code`, `bank_short_code`, `counterparty_id_number`.

### I3 — No path reaches `COMPLETED` without a confirmed bank reply **and** a successful `notify_paid` (PROVEN)
`app/services/buy_flow.py` has exactly one `OrderStatus.COMPLETED` transition (`:1323-1334`), inside `_close_order`, reached only from `_confirm_success` after the B6 commit (`:1243-1264`) and only after `await browser.notify_paid(...)` (`:1302`) returns without raising; any exception transitions to `MANUAL_REVIEW/binance_close_failed` (`:1303-1322`). Pinned by `tests/test_buy_close.py:216` (success), `:353` (failure never completes), `:420` (unknown not retried), `:547` (at most once).

### I4 — A money command with unknown fate is never auto-retried (PROVEN)
- `sms_dispatch.py:169-226`: only `DispatchFate.NOT_DELIVERED` earns **one** fallback; `UNKNOWN`, `PERMANENT` and `DELIVERED` return immediately. `UNKNOWN` includes 409 conflict, retryable/rate-limited/transport/timeout and unparseable 2xx (`:91-115`).
- `buy_flow.py:695-724` (`_record_unknown`) writes an `EXTERNAL_ERROR` audit only; status stays `SENDING_SMS`.
- Transport-level: `sms_gateway.py:117-122` passes `idempotent=False`, and `base.py:231/284` gate all retries on `idempotent`. Proven by `tests/test_buy_flow.py:246/:607/:905`.

### I5 — Direction guard (PROVEN)
- SQL predicate: `webhooks.py:325-336` selects `order_type == OrderType.SELL` only; a BUY row is invisible to the sell path.
- Defence in depth: `webhooks.py:340-342` + `_reject_non_sell_order` (`:267-301`), `sell_flow.py:327-332` raises before any `BankTransaction` insert (insert begins `:333`), `buy_flow.py:379-384` refuses non-BUY at execute.
- `release_order` exists only at `sell_flow.py:403`, after the SELL checks.
- **Exact regression tests**: `tests/test_direction_guard.py:103::test_equal_amounts_do_not_cross_directions`, `:149::test_only_buy_candidate_yields_no_match`, `:225::test_sell_flow_refuses_buy_order_before_bank_transaction`, plus `:190`, `:262`, `:289`. The “never awaited” assertion is structural (`fake_binance.release_calls == []`), which is the correct shape for a coroutine.

### Idempotency contracts (PROVEN)
| Contract | Evidence |
|---|---|
| `order_number` UNIQUE | `models.py:105`; `tests/test_idempotency.py:42`; duplicate insert → `DuplicateOrderError` at `buy_flow.py:399-406`, test `tests/test_buy_flow.py:437` |
| `Idempotency-Key = "{order_number}:payout"` stable across attempts | `sms_dispatch.py:198` used for primary and fallback (`:200-219`); persisted at `buy_flow.py:521`; tests `tests/test_buy_flow.py:181` (both calls share the key), `:367`, `:905` |
| `bank_reference` UNIQUE collision → `MANUAL_REVIEW` + HTTP 202, never 500 | `buy_flow.py:1216-1242` (IntegrityError → rollback → `_park_duplicate_reference` `:1338-1376`), `models.py:128`/`:180`; test `tests/test_buy_webhook.py:547` asserts 202 + `MANUAL_REVIEW` + `bank_reference is None` + 0 transactions |
| Webhook replay → 202 `duplicate=True`, no writes | `buy_flow.py:925-931` + `:947-1009` (processed-aware anchor); test `tests/test_buy_webhook.py:150` asserts the `AuditLog` count is unchanged |

---

## The pre-existing boot blocker fix (VERIFIED)

- `app/core/logging.py:186-191`: `logger.remove()` → `logger.configure(patcher=_patch_record)`; both `logger.add(...)` calls (`:194-215`) no longer pass `patcher`.
- The patcher still applies: `tests/test_logging.py:38 test_configure_logging_applies_global_patcher` (parametrized `log_json` True/False) calls `configure_logging`, then logs a line containing `V-12345678` + `+584141234567` and asserts `V-12345678` is absent and `V-****` is present, and that `request_id`/`app_env` extras are injected. A reintroduced `patcher=` kwarg raises `TypeError` inside `configure_logging` and fails this test, so the regression *is* caught.
- Real boot: `BOOT OK`.

---

## Rollout safety (VERIFIED, with one residual)

| Switch | Default | Evidence |
|---|---|---|
| `buy_auto_pay_enabled` | `False` | `config.py:96`; `tests/test_config.py:18`, `:178` |
| `binance_browser_enabled` | `False` | `config.py:105`; `tests/test_config.py:119`, `:178` |
| `buy_worker_enabled` | `False` | `config.py:114`; `tests/test_config.py:172`, `:178` |

Dormancy when all three are off: no browser is constructed (`main.py:179-181`), no worker task is created (`:184-185`), so there is no detection and no timeout sweep; the SMS poller is independently off by default (`config.py:78`). `.env.example:102,121,137` ships `false`. Residual: the `orders` router is *always* mounted (`main.py:309`), so an authenticated operator can dispatch a pre-existing `PENDING`/`awaiting_operator_approval` row even with the gate off — intended as the human override (design §9), but it means “dormant” relies on there being no BUY rows in the database.

---

## Findings

### CRITICAL

**C1 — The pipeline never re-validates the external order status; a cancelled/expired BUY order still gets paid.**
- Where: `app/main.py:93-96` (history is filtered on `PENDING_PAYMENT`), `:106-119`; `app/services/buy_flow.py:378-414` (the only guard is `detail.trade_type is not OrderType.BUY` at `:379`); `app/api/v1/orders.py:175-232` (operator path dispatches from local state with no upstream re-check).
- Evidence: independent probe `probe_cancelled.py` registered a `BinanceC2COrder(status="CANCELLED")` and ran `BuyFlow.execute`: result `persisted status=SENDING_SMS detail_status=CANCELLED sms_calls=1`, `command_sent='Pagar 0102 04141234567 12345678 1500,00' to='2661'`. The persisted `raw_payload` still records `CANCELLED`, so local state and provider state disagree.
- Requirement: R-BUY-001 (“select only orders … whose external status is `PENDING_PAYMENT`, and map that status to local `PENDING`”). A non-`PENDING_PAYMENT` detail is mapped to `PENDING` and dispatched.
- Impact: irreversible bank debit to the counterparty of an order that is no longer awaiting payment (money-loss path). No test covers a non-`PENDING_PAYMENT` detail, so the suite cannot catch it.
- Suggested remedy (not applied): reject a detail whose `status != "PENDING_PAYMENT"` in `execute` (typed `InvalidStateTransitionError`), and re-check the provider status in the operator path before dispatch.

**C2 — The shared `VES` daily ledger is charged and committed for BUY orders that never dispatch, and is never released.**
- Where: `app/services/buy_flow.py:415-419` (`assess_shared` on every BUY, before extraction), `app/services/risk_policy.py:215-217` (increments `LEDGER_VES`), `buy_flow.py:303-350` (`_park` commits the session), `:516-524` (B2 commit); only `LEDGER_VES_SMS` is ever released (`:336`, `:674-675`) — `LEDGER_VES` has no release call in `buy_flow.py`.
- Evidence: independent probe `probe_ledger.py`:
  - gate-off, complete data → `MANUAL_REVIEW/awaiting_operator_approval`, `sms_calls=0`, `ledger VES=1500.00 count=1`, `VES_SMS=0.00`;
  - auto-pay on, no browser → `MANUAL_REVIEW/payment_data_unavailable`, `sms_calls=0`, `ledger VES=1500.00 count=1`.
  The same leak persists after a permanent SMS dispatch failure (`_record_permanent` releases only `VES_SMS`).
- Requirement: R-BUY-005 (“… MUST reserve no budget …”) and the design’s ledger-truthfulness invariant (“the ledger a truthful cap”); the shared `VES` cap is consumed by SELL too (`sell_flow.py:275-291`), so a parked BUY silently reduces sell capacity for the UTC day.
- Impact: budget exhaustion / wrongful blocks; the ledger no longer reflects money that may actually move. Direction is fail-closed (it over-charges), so no money is lost, but the accounting is wrong and it is unrecoverable without manual SQL.
- Suggested remedy (not applied): move the gate/breaker evaluation before the shared reservation, release the shared channel when an order parks without dispatch, or reserve the shared channel only at B2.

### WARNING

**W1 — The operator gate is evaluated after the `VES_SMS` reservation, so a gate-off park reserves and then releases (contrary to design B2a and R-BUY-004’s “released only on a confirmed permanent dispatch failure”).**
- `buy_flow.py:459-474` (`assess_buy` reserves on the allow path) runs before `:475-483` (gate check → `_park(..., release_amount=instruction.amount_ves)`); `risk_policy.py:302` is the reservation. It commits nothing between the two calls, so the final VES_SMS value is `0.00` (test 872 passes), but the ledger row is created and the release is not a dispatch failure, is not audited with the amount, and would persist if the process died between the two operations in a future refactor.
- Evidence: `tests/test_buy_flow.py:872` asserts `VES_SMS == 0.00`, not “no reservation occurred”; probe #10 shows the `VES_SMS` row exists with `0.00`.

**W2 — The operator endpoint has no row lock and no status precondition before `_dispatch`; concurrent submissions can double-dispatch.**
- `orders.py:175-232` reads the order without `with_for_update`, then dispatches; `buy_flow.py:433-538` has no `if order.status not in {PENDING, PAYMENT_DETECTED, VERIFYING_CACHICAMO}` guard before B2. Two requests that both read `MANUAL_REVIEW(awaiting_operator_approval)` each transition to `PENDING` in their own snapshot, each reserve `VES_SMS`, each set the same `idempotency_key`, and each call the gateway. The only downstream defence is the gateway honouring `Idempotency-Key`.
- Evidence: `tests/test_orders_api.py:142 test_second_resume_is_noop` is sequential only; there is no concurrency test. `_advance_to_sending` silently does nothing for a status outside the three, which is the mechanism by which a stale second request still reaches the send.

**W3 — A confirmed order whose close cannot run is stranded, and the webhook still reports `confirmed`.**
- `buy_flow.py:1266-1278` returns `SmsConfirmation(outcome="confirmed")` when `self._browser is None`, with the order left in `SENDING_SMS` + `bank_confirmed_at`; `:1301-1322` parks a failed close in `MANUAL_REVIEW/binance_close_failed`. `orders.py:193-195` makes `SENDING_SMS` a 200 no-op and `:206-209` makes any other `MANUAL_REVIEW` reason a 200 no-op — there is no re-attempt path, although design §9 maps `confirmed → COMPLETED` and R-BUY-009 says “only an operator action may re-attempt it”.
- Evidence: `tests/test_buy_close.py:507 test_missing_browser_leaves_confirmed_order_pending` asserts `outcome == "confirmed"` **and** `status is SENDING_SMS`; `tests/test_buy_webhook.py:226` does the same through the HTTP webhook (`app_client` sets `state.browser = None`, `conftest.py:473`). A provider that treats `confirmed` as terminal will never retry, and a replay returns `duplicate=True`.

**W4 — Timeout-sweep dedupe is “once per order ever”, not “once per window”.**
- `buy_flow.py:750-761` scans **all** `EXTERNAL_ERROR` audits for the order for `reason == sms_confirmation_timeout`, with no window bound. R-BUY-010 requires “One … flag per window”. An operator who follows the design’s runbook (`SENDING_SMS → MANUAL_REVIEW → PENDING` → re-dispatch) produces a new `sms_sent_at`, but the second silent window is never flagged. Two overlapping ticks in separate processes can also insert two flags because the check is a read-then-write with no lock/unique constraint.
- Evidence: `tests/test_buy_worker.py:325` runs two sequential ticks in one process only; no test re-dispatches an already-flagged order.

**W5 — Amount matching is skipped when the reply carries no amount.**
- `buy_flow.py:1144-1145` adds the `amount_fiat == amount` condition only when `parsed.amount is not None`. The shipped provisional patterns make the amount optional (`banks.py:46-49`), so a normal BDV/Banesco reply confirms on bank + window + exactly-one only. R-BUY-007 says the correlation is against orders “whose amount matches”. The design documents this compromise (§6), and the exactly-one/window constraints keep it safe, but the literal clause is only partially enforced.
- Evidence: `tests/test_buy_webhook.py:520 test_amount_mismatch_no_match` uses `bank_reply_amount_mismatch_bdv.txt` (an amount is present), so the no-amount accept path is untested as a discrimination check.

**W6 — R-BUY-003’s “persisted instruction is reused on resume” is not honored on the approval path.**
- At the gate-off park, `buy_flow.py:475-483` runs **before** the persistence block at `:516-521`, so an `awaiting_operator_approval` order has no persisted bank/phone/cedula. The endpoint then calls `_dispatch`, which re-scrapes (`:452-456`). That is safe (risk is re-evaluated) but is a re-extraction, not reuse; no test covers the re-extraction count for the approval path (only `tests/test_buy_flow.py:774` covers the `SENDING_SMS` no-re-extract case).

**W7 — Strict-TDD evidence is incomplete.**
- The Engram apply-progress artifact has a per-task “TDD Cycle Evidence” table for **PR 10 only** (6 rows: 10.1–10.6). PRs 1–9 are narrative summaries without per-task RED/GREEN/TRIANGULATE rows. The strict-TDD module expects a row per task; the protocol is only partially evidenced. Additionally `tasks.md` has 58 task headers but 56 checkboxes — `0.4` and `0.5` have no checkbox (their work is present).
- Evidence: `sdd/buy-usdt-sms-payment-flow/apply-progress` (Engram #879); `openspec/changes/buy-usdt-sms-payment-flow/tasks.md` (checkbox scan).

**W8 — Changed-file coverage below the 80% informational bar for three touched files.**
- Measured with `pytest --cov=app`: `app/api/v1/orders.py` 58%, `app/api/v1/webhooks.py` 66%, `app/main.py` 74%; `app/services/sell_flow.py` 64% (pre-existing, refactored). New cores are strong: `banks.py` 92%, `bank_replies.py` 97%, `ports.py` 88%, `risk_policy.py` 90%, `sms_dispatch.py` 98%, `buy_flow.py` 88%, `binance_browser.py` 92%.
- Caveat: the reported missing ranges for `orders.py` (`178-236`) and `webhooks.py` (`337-368`) are the endpoint/webhook dispatch bodies, which demonstrably execute in the passing tests; treat the attribution as a coverage/async artefact, not proof of untested paths. Informational only per the strict module.

### SUGGESTION

**S1 — The unprocessed-replay branch loses its `WEBHOOK_RECEIVED` audit.** `_claim_sms_event` adds the event and the audit, then a unique violation rolls back **both** (`buy_flow.py:966-1009`); the replay is then re-processed with no `WEBHOOK_RECEIVED` row. R-BUY-007 says “`WEBHOOK_RECEIVED` per accepted delivery”. `tests/test_buy_webhook.py:183` asserts the reprocessing works but not the audit.

**S2 — “Operator alert” for ≥2 candidates is only a `logger.error` + audit.** `buy_flow.py:1062-1076`; there is no notification channel. Operationally acceptable if log alerting exists; otherwise the alert is easy to miss.

**S3 — The operator response has no “unknown” outcome.** `orders.py:227-236` maps any non-`MANUAL_REVIEW` result to `"dispatched"`, including an `UNKNOWN` gateway fate (`sms_message_id is None`, status `SENDING_SMS`). The vocabulary also contains `"resumed"`/`"awaiting_approval"` that the endpoint never emits (`schemas.py:246-252`).

**S4 — mypy grew by one error.** 31 vs the recorded 30-error baseline; the new one is `app/services/buy_flow.py:144-145` (`_clients` yields `tuple[Client | None, ...]`). Advisory only (no mypy config; not a gate).

**S5 — Encoding/format drift.** Mojibake in new test section comments (e.g. `tests/test_buy_flow.py` headers `# 7.4 \uFFFD?"`) and the pre-existing `app/core/logging.py:146` docstring (`cÃƒÂ©dulas`). `ruff format` was not run (would rewrite ~19 files) — known pre-existing condition.

**S6 — The timeout sweep is coupled to `buy_worker_enabled`.** `main.py:128` calls the sweep inside `_buy_worker_tick`, and the task only starts when `:184-185` is true. Disabling the worker (the documented rollback) also stops flagging in-flight `SENDING_SMS` rows. A separate sweep switch (or running it under the poller) would keep observability during rollback.

**S7 — Window upper bound uses the provider timestamp.** `buy_flow.py:1141-1142` requires `coalesce(sms_sent_at, updated_at) <= received_at`; a gateway clock slightly behind the app clock yields `no_match` and strands the order until the sweep flags it. Consider a small clock-skew allowance or using `now()` as the upper bound.

---

## TDD Compliance

| Check | Result | Details |
|---|---|---|
| TDD Evidence reported | ⚠️ | Table exists for PR 10 only (6 task rows); PRs 1–9 narrative only (W7) |
| All tasks have tests | ✅ | Every named task test node exists; 58/58 tasks have delivered tests |
| RED confirmed (tests exist) | ✅ | All named node IDs from `tasks.md` resolve to real test functions (spot-checked across all suites) |
| GREEN confirmed (tests pass) | ✅ | 258/258 executed tests pass on this run |
| Triangulation adequate | ⚠️ | Strong for most behaviours (multiple distinct expectations); sequential-only for concurrent endpoint/sweep cases (W2/W4) |
| Safety net for modified files | ✅ | `tests/test_risk_policy.py:132 test_sell_flow_risk_reason_and_ledger_unchanged` pins SELL behaviour before/after the refactor |

**TDD Compliance**: 4/6 checks fully pass, 2 partial.

### Test Layer Distribution

| Layer | Tests | Files | Tools |
|---|---|---|---|
| Unit (pure, no I/O) | 114 | 8 | pytest |
| Unit (in-memory DB) | 89 | 7 | pytest-asyncio + SQLite `StaticPool` + freezegun |
| Integration (ASGI) | 35 | 4 | httpx `ASGITransport`, `respx` |
| Browser adapter (mocked Playwright) | 22 (2 opt-in skipped) | 1 | unittest.mock doubles |
| E2E (real browser) | 0 (2 skipped) | 0 | Playwright (opt-in only) |
| **Total** | **260 collected / 258 passed / 2 skipped** | **20** | |

### Changed File Coverage

| File | Line % | Rating |
|---|---|---|
| `app/api/v1/orders.py` | 58% | ⚠️ Low (see W8 caveat) |
| `app/api/v1/webhooks.py` | 66% | ⚠️ Low (see W8 caveat) |
| `app/main.py` | 74% | ⚠️ Low |
| `app/core/config.py` | 85% | ✅ |
| `app/domain/bank_replies.py` | 97% | ✅ |
| `app/domain/banks.py` | 92% | ✅ |
| `app/domain/ports.py` | 88% | ✅ |
| `app/infrastructure/clients/binance_browser.py` | 92% | ✅ |
| `app/services/buy_flow.py` | 88% | ✅ |
| `app/services/risk_policy.py` | 90% | ✅ |
| `app/services/sell_flow.py` | 64% | ⚠️ pre-existing |
| `app/services/sms_dispatch.py` | 98% | ✅ |

**Average of the listed changed files**: ~82%. Coverage is informational, not a gate (`coverage_threshold: 0`).

### Assertion Quality

| File | Line | Assertion | Issue | Severity |
|---|---|---|---|---|
| — | — | — | None of the banned patterns (`assert True`, tautologies, ghost loops over possibly-empty collections, standalone type-only assertions) | — |

**Assertion quality**: ✅ All assertions call production code and assert values/behaviour. The `is None` / `is not None` checks are consistently paired with value assertions in the same test. The only soft spot is triangulation breadth for concurrency (W2/W4), not assertion truthfulness.

### Quality Metrics

**Linter**: ✅ No errors (`ruff check app tests`, exit 0)
**Type Checker**: ❌ 31 errors (advisory; pre-existing baseline 30, no mypy config)
**Formatter**: ➖ Not run by design (`ruff format` would rewrite ~19 files)

---

## Design Coherence

| Decision | Followed? | Notes |
|---|---|---|
| A1 durable `SENDING_SMS` before the call | ✅ | B2 commit at `buy_flow.py:524` before `:526`; proven by test 319 |
| A2 `resume()` never re-sends while `SENDING_SMS` | ✅ | `buy_flow.py:846-855`; tests 739/774 |
| A3 gate-off parks in `MANUAL_REVIEW/awaiting_operator_approval`, reserves nothing | ⚠️ | VES_SMS nets to zero, but the shared `VES` channel is charged (C2) and VES_SMS is transiently reserved then released (W1) |
| A4 `cedula_format` data-only | ✅ | `banks.py:57-108`, `:301-327`; test 70 |
| A5 four-valued reply outcome | ✅ | `bank_replies.py:32-137`; 9 parser tests |
| A6 release only on confirmed permanent dispatch failure | ⚠️ | Also released on the gate-off park (`buy_flow.py:481`) and on render failure (`:512`); the latter is defensible, the former is not a dispatch failure |
| A7 processed-aware replay anchor | ✅ | `buy_flow.py:947-1029`; tests 150/183 |
| A8 pure stdlib parser, lazy Playwright | ✅ | import audit + `tests/test_browser_parser.py:117` |
| A9 machine-readable buy reasons, SELL prose unchanged | ✅ | `risk_policy.py:267-300`; `tests/test_risk_policy.py:132` |
| A10 SQL predicate + defensive assertion | ✅ | `webhooks.py:328`, `:267-301`; `tests/test_direction_guard.py` |
| A11 `add_column` up / batch down | ✅ | migration file; tests 109/120; independent CLI gate |
| A12 browser optional, typed errors | ✅ | `main.py:179-181`, `buy_flow.py:559-567`, `binance_browser.py:343-385`; tests 485/254 |
| B0′ event committed before processing | ✅ | `buy_flow.py:996-999` |
| B1 `PENDING` + `ORDER_CREATED` | ✅ | `buy_flow.py:398-414` |
| B2 state + reservation + key before call | ✅ | `:516-524` |
| B2a `MANUAL_REVIEW`, no reservation, no key | ❌ | Reservation happens first (W1); shared channel kept (C2) |
| B3 audit-only for unknown fate | ✅ | `:695-724` |
| B4 telemetry after delivery | ✅ | `:610-645`; observability `updated_at` fallback preserved by `coalesce` |
| B5 release + transition atomically | ✅ | `:647-693`; test 634 |
| B6 one OUTGOING + reference + confirmed_at | ✅ | `:1216-1264`; tests 265/547 |
| B7 `COMPLETED` only after `notify_paid` | ✅ | `:1280-1336`; tests 216/353 |
| §7 settings defaults/required caps | ✅ | `config.py:96-115`, `.env.example:102-139`, `tests/test_config.py` |
| §9 API contract; enriched webhook | ⚠️ | Endpoint PII-free and auth-gated ✅; `confirmed`-without-close row is outside the design table (W3); `resumed`/`awaiting_approval` never emitted (S3) |
| §9 “two or more candidates → operator alert” | ⚠️ | `logger.error` + audit only (S2) |

---

## Anything Unverified or Unverifiable Offline

1. **Real BDV/Banesco reply wording** (design D2 / open question). The success/failure regexes are explicitly provisional (`banks.py:42-54`); deep-link `bank_reply_*.txt` fixtures are synthetic. Real wording drift will route to `MANUAL_REVIEW/bank_reply_unparseable` — safe, but unverified.
2. **Gateway `Idempotency-Key` semantics.** The header is sent (`sms_gateway.py:90-92`) but whether the Android gateway actually de-duplicates a repeated key is not testable offline. This is the last line of defence for the concurrent double-dispatch in W2.
3. **Playwright DOM selectors, the real P2P order-view HTML, and `binance_p2p_order_url_template`.** Covered only by committed synthetic fixtures and mocked adapters; the two live smokes are skipped (`tests/test_browser_adapter.py:589,621`; `LIQUIDVE_BROWSER_TESTS` not set). The real URL path is an acknowledged open question.
4. **PostgreSQL behaviour.** All tests run on SQLite. The `SELECT … FOR UPDATE` ledger path (`risk_policy.py:106-125`) and any row-lock semantics under concurrency (W2) are unexercised.
5. **Concurrent operator submissions and overlapping sweeps.** No test; findings W2/W4 are static/probe reasoning, not runtime reproductions.
6. **The two blocking defects themselves** (C1/C2) are *not* covered by the committed suite; they were proven only by the independent probes in this report. A regression test for each is missing.
7. **Coverage attribution** for `orders.py`/`webhooks.py` is internally inconsistent with the passing endpoint/webhook tests (W8 caveat); not resolvable offline without a coverage-tool investigation.
8. **`bank_transactions.raw_payload` / `webhook_events.payload`** intentionally store the raw reply body (design §6); the audit rows themselves are masked (verified), but raw PII at rest in those two tables was not re-verified against a data-retention policy.

---

## Verdict

**FAIL** — all declared commands are green (258 passed, 2 skipped; ruff clean; boot OK; browser-free; migrations reversible), and 15/18 requirements are fully compliant with direct tests for all 44 scenarios. It nevertheless fails on two independent, reproducible defects that the suite does not cover: a BUY order whose authoritative external status is no longer `PENDING_PAYMENT` is still persisted and dispatched with a real Pago Móvil command (C1, money-loss path), and the shared `VES` daily budget is permanently consumed by BUY orders that never dispatch, including the default gate-off park (C2, violates R-BUY-005’s “MUST reserve no budget”). Both are fixable in `buy_flow.py` without schema or contract changes; after fixes, add a regression test for each, then re-run this phase.

---

# Re-verification Round 1

**When**: 2026-11-23 (after Correction Round 1 that addressed C1, C2, W1 and W2)
**HEAD**: `fe7e93a`; all work remains uncommitted in the working tree (same 15 modified / 21 untracked shape as the baseline).
**Scope**: independently re-run the full gate, reproduce both original probes from scratch with a throwaway script (deleted afterwards), and adversarially probe the correction's regression surface. Nothing from the apply agent's claims was taken on trust.
**Current verdict**: **PASS WITH WARNINGS** — 0 CRITICAL, 0 blockers. The two money-direction defects (C1, C2) are closed and independently reproduced as fixed; W1 and W2 are closed; no WARNING is a demonstrated money-safety or data-integrity defect.

## Commands re-executed this round

| # | Command | Exit | Result | Hash (exact output) |
|---|---|---|---|---|
| 1 | `.\.venv\Scripts\python.exe -m pytest -q` (declared) | 0 | **263 passed, 2 skipped** in 14.72 s (`-qq` suppresses the summary line; count from the `-q`-less companion run) | `sha256:c0786037…f593202e` (324 bytes) |
| 2 | `.\.venv\Scripts\python.exe -m ruff check app tests` | 0 | `All checks passed!` | `sha256:82b3e6a6…156b4f18` (19 bytes) |
| 3 | `…configure_logging(get_settings()); print('BOOT OK')` | 0 | `BOOT OK` | — |
| 4 | Independent probe `probe_reverify_round1.py` (this verification; 8 scenarios, 40 checks) | 0 | **ALL PROBE CHECKS PASSED** (deleted afterwards) | — |

Suite grew 258 → 263 (+5 net). `git status` is unchanged in shape; `tasks.md` still has 58 numeric task headers, 56 checked, 0 unchecked, and 2 with no checkbox (`0.4`, `0.5`).

## Correction findings — status

| ID | Status | Independent evidence |
|---|---|---|
| **C1** | ✅ **RESOLVED** | `BuyFlow.execute` rejects any non-`PENDING_PAYMENT` detail before persistence: `app/services/buy_flow.py:396-401` (`InvalidStateTransitionError(code="external_status_not_pending_payment")`). The operator endpoint locks and re-validates the provider: `app/api/v1/orders.py:178-182` (`with_for_update()`), `:207-213` (re-fetch + same typed code). New tests `tests/test_buy_flow.py:507 test_cancelled_detail_is_rejected_before_persistence_or_dispatch`, `tests/test_orders_api.py:120 test_cancelled_provider_status_blocks_operator_dispatch`. Probe **S1**: typed code `external_status_not_pending_payment`, 0 SMS calls, no `P2POrder` row, 0 ledger rows. Worker safety: a raise is caught per-order (`app/main.py:118-127`, `except LiquidVEException`) and history is already `PENDING_PAYMENT`-filtered (`main.py:93-96,109-110`), so no poison-pill tick. |
| **C2** | ✅ **RESOLVED** | `RiskPolicy.assess_buy` is check-only (`app/services/risk_policy.py:293-377`, read-only projection via `check_channel_cap`/`channel_total`). A new `RiskPolicy.reserve_buy` charges **both** channels atomically (`risk_policy.py:379-407`; releases `VES` if `VES_SMS` refuses). Called once at B2 (`buy_flow.py:562-571`), committed with `SENDING_SMS` before the gateway call. `_record_permanent` releases both (`buy_flow.py:718-745`). Gate-off and `payment_data_unavailable` parks happen before any reservation. Probe **S2** (gate-off: 0 rows, VES 0.00, VES_SMS 0.00), **S3** (success: VES 1500.00 + VES_SMS 1500.00, 2 rows), **S4** (permanent failure: both 0.00, both `transaction_count=0`). New/updated tests `tests/test_risk_policy.py:498,532`, `tests/test_buy_flow.py:464,716,1001`. |
| **W1** | ✅ **RESOLVED** | The operator gate (`buy_flow.py:524-531`) is evaluated strictly before `reserve_buy` (`:563`); gate-off parks pass no `release_channels`, so no ledger row is created at all. Probe **S2** confirms `ledger_rows == 0` for both channels (R-BUY-005 “MUST reserve no budget”). |
| **W2** | ✅ **RESOLVED** | `_dispatch` returns immediately for an order already in `SENDING_SMS` and raises `InvalidStateTransitionError(code="invalid_dispatch_status")` for any non pre-dispatch status (`buy_flow.py:474-486`); the endpoint loads under `with_for_update()` and re-checks status before dispatch (`orders.py:178-182,198-200,239-242`). Probe **S5**: a stale second `_dispatch` on a `SENDING_SMS` order produced 0 additional gateway calls and 0 additional `VES`/`VES_SMS` reservation. Test `tests/test_buy_flow.py:938`. **Caveat (carried, not new):** true PostgreSQL concurrency is still offline-unexercised; SQLite treats `FOR UPDATE` as a no-op (see “Unverified offline” #4/#5). |

## Independent probe results (written from scratch, outside `tests/`)

| Probe | Scenario | Observed | Result |
|---|---|---|---|
| S1 | CANCELLED detail via `BuyFlow.execute` | `code=external_status_not_pending_payment`; `sms.calls=[]`; `order_row_count=0`; `ledger_rows=0` | ✅ |
| S2 | Gate-off, complete payment data | `MANUAL_REVIEW/awaiting_operator_approval`; `sms.calls=[]`; `VES=0.00`, `VES_SMS=0.00`; `ledger_rows=0` | ✅ |
| S3 | Successful dispatch | `SENDING_SMS`; 1 SMS call; `VES=1500.00`; `VES_SMS=1500.00`; `ledger_rows=2` | ✅ |
| S4 | Permanent dispatch failure | `MANUAL_REVIEW/sms_dispatch_failed`; `VES=0.00`, `VES_SMS=0.00`; both `transaction_count=0` | ✅ |
| S5 | W2 stale second dispatch on `SENDING_SMS` | stays `SENDING_SMS`; 0 extra gateway calls; 0 extra reservation on both channels | ✅ |
| S6 | Durability (independent session inside `on_send`) | independent session sees `SENDING_SMS` **and** `VES=1500.00` **and** `VES_SMS=1500.00` at send time | ✅ |
| S7 | Operator approval happy path (real ASGI endpoint, `with_for_update`) | HTTP **202**; response `SENDING_SMS`/`dispatched`; 1 SMS call; both channels reserved 1500.00; exactly one “Operator approved the payout” audit | ✅ |
| S8 | Error mapping for `InvalidStateTransitionError` | `http_status == 409`; a real endpoint raising the class returns **409** (not 500) with the canonical envelope | ✅ |

## Regression checks introduced by the correction

- **Durability invariant (reservation + `SENDING_SMS` committed before the gateway call) — PRESERVED.** `_dispatch` flushes the double reservation (`reserve_buy`), then `_advance_to_sending`, then commits at `buy_flow.py:562-571`; the gateway call starts only at `:573`. Probe **S6** proves an **independent session** observes `status == SENDING_SMS` **and** both channel totals (1500.00 each) during the fake send. This is a stronger result than the baseline (which only checked status/key), and confirms reserve-at-B2 did not move the commit after the call.
- **Legitimate approval path not blocked — PROVEN.** Probe **S7** drives the real `POST /api/v1/orders/buy/{n}/pay` on a `MANUAL_REVIEW + awaiting_operator_approval` row with `with_for_update()` enabled: `MANUAL_REVIEW → PENDING → _dispatch` reaches `SENDING_SMS`, sends exactly one SMS and reserves both channels. The new status precondition (`invalid_dispatch_status`) only fires for genuinely non pre-dispatch statuses.
- **No unhandled 500 for `invalid_dispatch_status`.** `InvalidStateTransitionError` → `ConflictError` → `LiquidVEException` with `http_status=409` (`app/core/exceptions.py:126-137,84-95`); the global `LiquidVEException` handler renders the typed envelope (`app/main.py:207-210`). Probe **S8** exercised the same class through a real endpoint (409, not 500). No API path calls `_dispatch` with an invalid status: the endpoint transitions/refuses first (`orders.py:222-242`).
- **SELL path unaffected.** `app/services/sell_flow.py:276` still uses the reserving `assess_shared`; `tests/test_risk_policy.py:132 test_sell_flow_risk_reason_and_ledger_unchanged` passes. The reserve-at-B2 change is BUY-only.

## Still-open findings (unchanged unless noted)

| ID | State | One-line evidence |
|---|---|---|
| **W3** | STILL OPEN (unchanged) | `buy_flow.py:1318-1323` returns `confirmed` with `browser is None` while leaving the order in `SENDING_SMS`; no operator re-attempt path. |
| **W4** | STILL OPEN (unchanged) | Timeout-sweep dedupe scans **all** `EXTERNAL_ERROR` audits with no window bound (`buy_flow.py:800-813`); concurrent ticks still untested. |
| **W5** | STILL OPEN (unchanged) | Amount condition applied only when `parsed.amount is not None` (`buy_flow.py:1196-1197`); mitigated by exactly-one + window, not demonstrated as a defect. |
| **W6** | STILL OPEN (unchanged) | Gate-off park (`buy_flow.py:524-531`) precedes instruction persistence (`:564-569`); the endpoint re-scrapes on approval. |
| **W7** | STILL OPEN (partially improved) | Apply-progress now carries a per-item TDD table for Correction Round 1 (8 rows), but PRs 1–9 remain narrative; `tasks.md` is still 58 task headers / 56 checked / 2 without checkbox (`0.4`, `0.5`). |
| **W8** | STILL OPEN (informational) | Refreshed coverage: `orders.py` 53%, `webhooks.py` 66%, `main.py` 74%, `sell_flow.py` 64% (pre-existing); new cores strong — `buy_flow.py` 87%, `risk_policy.py` 91%, `sms_dispatch.py` 98%; TOTAL 80%. Same async-coverage caveat as the baseline. |
| **S1** | STILL OPEN (unchanged) | `_claim_sms_event` rollback discards both the event row and its `WEBHOOK_RECEIVED` audit (`buy_flow.py:1048-1061`). |
| **S2** | STILL OPEN (unchanged) | ≥2-candidate “operator alert” is still `logger.error` + audit only (`buy_flow.py:1114-1119`). |
| **S3** | STILL OPEN (unchanged) | Endpoint maps any non-`MANUAL_REVIEW` result to `"dispatched"` (`orders.py:253-256`); unused vocabulary values remain. |
| **S4** | UNCHANGED | `mypy app` → **31 errors** (identical to baseline; advisory only, no mypy config). |
| **S5** | STILL OPEN (partially improved) | `app/core/logging.py:146` docstring mojibake remains (`cǟ…dulas`); the test-comment drift cited in the baseline was not reproducible on re-inspection; `ruff format` still not run. |
| **S6** | STILL OPEN (unchanged) | Sweep still runs inside `_buy_worker_tick` (`main.py:128`), which only starts when `buy_worker_enabled` (`main.py:184-185`). |
| **S7** | STILL OPEN (unchanged) | Window upper bound still uses the provider timestamp (`buy_flow.py:1193-1194`, `last_dispatch <= received_at`). |

## New observations (non-blocking, introduced/noticed this round)

- **N1 (SUGGESTION) — row lock held across external I/O.** The operator endpoint keeps the `SELECT … FOR UPDATE` lock while awaiting `binance.get_order_detail` (`orders.py:178-213`). Correctness is unaffected, but concurrent approvals for the same order can serialise for the duration of the network call. Consider fetching the detail before locking, or a shorter lock scope.
- **N2 (SUGGESTION) — fail-closed but silent on a reserve race.** If `reserve_buy` raises `RiskLimitExceededError` because a concurrent transaction consumed the cap between the read-only checks and B2, the exception propagates with a net-zero ledger (fail-closed) but the order is left in a pre-dispatch status with only a worker `logger.warning` (`main.py:122-127`) — no audit / `MANUAL_REVIEW` transition. Narrow concurrency window; no money moves.
- **N3 (SUGGESTION) — redundant read-only projection.** `execute` calls `check_shared` (`buy_flow.py:433-436`) and `_dispatch` calls `check_shared` again plus `assess_buy` (`:495-513`). Harmless duplicate reads; could be collapsed.

None of N1–N3 is a money-safety or data-integrity defect, and none blocks.

## Re-verification verdict

**PASS WITH WARNINGS.** All declared gates are green on a fresh run: **263 passed, 2 skipped**, ruff clean, `BOOT OK`. Both baseline CRITICALs are closed and independently reproduced as fixed: a `CANCELLED` detail now raises `external_status_not_pending_payment` with zero SMS and zero persistence (C1), and BUY orders that never dispatch no longer touch the ledger — the gate-off park leaves **no ledger rows at all** for both `VES` and `VES_SMS` (C2). Both WARNINGs are closed (W1 gate-before-reserve, W2 stale-dispatch no-op + endpoint lock/recheck). The durability invariant is preserved and now provably covers the double reservation (S6); the approval happy path still dispatches (S7); `invalid_dispatch_status` maps to a typed 409, never a 500 (S8); SELL is untouched.

The remaining findings are the carried W3–W8 and S1–S7 plus three new suggestions (N1–N3); none is a demonstrated money-safety or data-integrity defect. The change is admissible to proceed toward archive, with W3–W8/S1–S7 tracked as follow-ups.

**Findings count — Re-verification Round 1**: CRITICAL **0** (2 resolved: C1, C2) · WARNING **0 new** (2 resolved: W1, W2; 6 carried: W3–W8) · SUGGESTION **3 new** (N1–N3) + 7 carried (S1–S7).

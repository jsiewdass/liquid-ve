# Tasks: BUY payment rail via Banesco Online + Telegram Clave DinÃ¡mica

- **Change**: `buy-usdt-banesco-payment-rail`
- **Store**: `openspec` (`openspec/changes/buy-usdt-banesco-payment-rail/`)
- **Delivery**: `auto-chain`, `stacked-to-main`, **800 authored changed lines per PR**
- **TDD**: strict. Every task names its RED test and the exact command that shows it failing before implementation.
- **Runner**: `.\.venv\Scripts\python.exe -m pytest -q`
- **Lint**: `.\.venv\Scripts\python.exe -m ruff check app tests`
- **Baseline**: 263 passed, 2 skipped; ruff clean
- **Security rule**: credentials and security answers are referenced only by environment-variable name. No value, fixture, example, log line, audit payload, DB row, artifact, or Engram observation contains a credential.

## Scope Guard

### In scope
- Banesco Online login, 2-of-4 security questions, clave, ad modal, Pago MÃ³vil form, double `Pagar`, Clave DinÃ¡mica handoff, receipt capture.
- Telegram operator handoff over the raw Bot API, durable operator challenges, correlation, timeout, restart semantics.
- Receipt storage, SHA-256 evidence, Binance upload + payment notification, `AWAITING_RELEASE`, release watcher, at-most-once automatic appeal.
- Status model rename/additions, `VES_PAY` ledger channel, data migrations, typed errors, settings, test seams, docs, SMS BUY decommission.

### Out of scope â€” apply must not invent it
- Multi-bank payer support; only Banesco Online is automated.
- Order creation/detection changes beyond the payment tail; Flow A (SELL/Cachicamo) behavior stays unchanged.
- Android-side work; no new Android code, no SMS BUY fallback.
- Crypto settlement APIs, external market feeds, operator UI, multi-operator routing.
- Destination banks beyond the existing supported profiles `0102` and `0134`.
- Modification of any archived change folder.
- Dropping historical SMS telemetry columns from `p2p_orders`; they are historical data, not live code.

### Default-run invariant
No task in the default `-q` run may require a browser binary, Chromium, a network socket, a real `.env`, real credentials, or a real Telegram bot. Playwright is imported lazily only inside the adapter; tests use page-object fakes, recorded DOM fixtures, `respx`, and `freezegun`.

## Ordering Constraints

1. Phase 0 settings/test seams â†’ S1a status/ledger/data migration â†’ S1b pure Banesco domain/errors.
2. S2 pure page contracts and recorded fixtures before any page object.
3. S3 login/questions/modal adapter before S4 payment form/clave.
4. S4 payment form/double `Pagar`/clave handoff before S5 receipt capture.
5. S5 receipt storage and nullable payment/receipt schema before S7a durable attempt wiring.
6. S6 Telegram adapter and durable challenges before S7a invokes a real notifier shape.
7. S7a buy-flow tail/durable attempt/resume guard before S7b evidence upload and `AWAITING_RELEASE`.
8. S7b evidence upload before S8 release watcher and appeal.
9. S8 watcher/appeal before S10 operator/observability polish.
10. **S9 SMS decommission is the final PR.** S10 polish is deliberately re-sequenced immediately before it so no post-decommission edit touches the removed SMS surface.

## Slice Budget Re-validation (800-line ceiling)

| Slice | Re-validated authored lines | PR | Decision |
|---|---:|---|---|
| Phase 0 prerequisites | 320 (280â€“360) | PR 0 | Separate prerequisite PR; no production behavior |
| S1a status/ledger/data migrations | 430 (380â€“470) | PR 1 | Adopt design split S1a |
| S1b Banesco domain/settings/errors/secrets | 360 (320â€“420) | PR 2 | Adopt design split S1b |
| S2 page contracts/fixtures | 520 (450â€“600) | PR 3 | Single PR, under 800 |
| S3 login/questions/modal adapter | 560 (480â€“650) | PR 4 | Single PR, under 800 |
| S4 payment form/double Pagar/clave | 620 (540â€“700) | PR 5 | Single PR; split S4a/S4b only if authored diff exceeds 760 |
| S5 receipt capture/storage/schema | 470 (400â€“540) | PR 6 | Single PR, under 800 |
| S6 Telegram/durable challenges | 540 (460â€“620) | PR 7 | Single PR, under 800 |
| S7a buy-flow tail/durable attempt | 420 (360â€“500) | PR 8 | Adopt design split S7a |
| S7b evidence upload/AWAITING_RELEASE | 320 (260â€“380) | PR 9 | Adopt design split S7b |
| S8 release watcher/automatic appeal | 680 (560â€“760) | PR 10 | Single PR; split S8a/S8b only if authored diff exceeds 760 |
| S10 operator/observability polish | 300 (250â€“360) | PR 11 | Single PR, under 800 |
| S9 SMS decommission (final) | 480 (400â€“560) | PR 12 | Single final PR; split S9a/S9b only if authored diff exceeds 760 |

Re-validation notes:
- No slice exceeds 800 authored changed lines after the settings, test-seam, and strict-TDD additions. The design's S1 and S7 splits are adopted because their combined settings/tests/evidence work crosses the safe review threshold.
- The design's single schema revision `20261125_0004_banesco_rail_schema.py` is split by owning slice into `0004` receipt/payment/release-deadline columns, `0005` `operator_challenges`, and `0006` appeal columns. Each revision is nullable-only and independently downgrade-safe.
- Every PR is under 800 lines, independently mergeable on its base, and independently revertible at its stack position.

## S1 â€” First Autonomous Slice Boundaries

- **S1 covers PR 1 (S1a) and PR 2 (S1b); PR 0 supplies the settings and test-seam prerequisites.**
- **Start**: after PR 0 is merged; current enums/config are unchanged; no new runtime behavior; Flow A is the baseline.
- **Finish**: `SENDING_PAYMENT`, `AWAITING_RELEASE`, `APPEALED`; `VES_PAY`; the `0003` status and ledger data migration with a downgrade; `BanescoPaymentTarget` pure mapper; typed `PaymentRailError` tree; secrets-hygiene canary. Settings names/defaults come from PR 0 and are exercised here. No Banesco browser, Telegram client, or release watcher exists yet.
- **Verification**: focused S1 tests, full suite at 263+ passed and 2 skipped or better, `ruff check app tests`, `alembic upgrade head` and `downgrade -1` on a fresh SQLite DB, Flow A transition graph unchanged.
- **Rollback**: revert PR 2 then PR 1; run `alembic downgrade -1` once to reverse `0003`; `SENDING_SMS` and `VES_SMS` are restored. PR 0 settings may remain because they are inert without an enabled rail. No kill switch is needed because S1 starts no external session.
- **Autonomy**: no browser, network, credential, Telegram bot, or real `.env` is required.

---

## Phase 0 â€” Prerequisites and Shared Seams (PR 0)

- [x] **0.1 Settings co-change set in mandatory order** â€” Done (PR 0)
  - **What**: Add every new rail/Telegram/receipt/release setting with safe defaults, then update `.env.example` placeholders, `tests/conftest.py` environment defaults and the `test_settings` fixture, settings tests, and finally validators. Gate validators on enablement flags only. The two BUY caps already exist: `MAX_BUY_AMOUNT_VES` and the daily BUY cap currently named `DAILY_SMS_LIMIT_VES`; no new cap is introduced.
  - **Files**: `app/core/config.py`, `.env.example`, `tests/conftest.py`, `tests/test_config.py`.
  - **RED test**: `tests/test_config.py::test_new_rail_settings_are_defaulted_and_summary_is_secret_free`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_config.py::test_new_rail_settings_are_defaulted_and_summary_is_secret_free`
  - **Implementation**: Execute in this exact order so import-time `Settings()` never breaks: `.env.example` names/placeholders â†’ `tests/conftest.py` `setdefault` values and fixture values â†’ `tests/test_config.py` assertions â†’ `app/core/config.py` fields and `_validate_consistency`. Add only non-secret values to `base_settings_summary()`.
  - **Verify**: same node, then `.\.venv\Scripts\python.exe -m pytest -q tests/test_config.py`.
  - **Deps**: none.
  - **Offline**: yes; no `.env`, network, or credentials.

- [x] **0.2 Shared test-seam contract and skeletons** â€” Done (PR 0)
  - **What**: Establish the offline seams used by every later slice: page-object `FakePage`/`FakeBanescoContext`, recorded Banesco DOM fixture catalog, fake confirmation provider (`FakeOperatorNotifier`), fake Telegram transport (`FakeTelegramBotClient`), `respx` Bot API helpers, and `freezegun` clock helpers. Materialize the actual DOM files in S2; fix their names and sanitization contract here.
  - **Files**: `tests/conftest.py`, `tests/test_test_seams.py`, `tests/fixtures/banesco/` catalog notes.
  - **RED test**: `tests/test_test_seams.py::test_shared_seams_are_offline_browser_free_and_network_free`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_test_seams.py::test_shared_seams_are_offline_browser_free_and_network_free`
  - **Implementation**: Add the fake classes and helper fixtures, assert importing them never imports `playwright`, assert `respx` intercepts are used instead of sockets, and assert time is frozen where expiry is asserted. Keep the existing `FakeBrowser.notify_paid` compatibility until S7a replaces its signature.
  - **Verify**: same node, then `.\.venv\Scripts\python.exe -m pytest -q tests/test_test_seams.py`.
  - **Deps**: 0.1.
  - **Offline**: yes.

- [x] **0.3 Deprecated setting aliases and import-time guard** â€” Done (PR 0)
  - **What**: Accept `DAILY_SMS_LIMIT_VES` as a one-release alias for `DAILY_PAY_LIMIT_VES` and `SMS_CONFIRMATION_TIMEOUT_SECONDS` as a one-release alias for `PAYMENT_CONFIRMATION_TIMEOUT_SECONDS`; prove `Settings()` imports with no rail enabled and no secret present.
  - **Files**: `app/core/config.py`, `tests/test_config.py`.
  - **RED test**: `tests/test_config.py::test_deprecated_aliases_work_and_import_time_settings_never_break`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_config.py::test_deprecated_aliases_work_and_import_time_settings_never_break`
  - **Implementation**: Add the alias resolution and a minimal import-time settings probe. Do not make any new field required.
  - **Verify**: same node, then `.\.venv\Scripts\python.exe -m pytest -q tests/test_config.py`.
  - **Deps**: 0.1.
  - **Offline**: yes.

**PR 0 gate**: focused Phase 0 nodes, full suite, ruff clean; `.env.example` contains placeholders only.

## Phase 1 â€” S1a: Status, Ledger, and Data Migrations (PR 1)

- [x] **1.1 Enum rename, new statuses, transition graph** â€” Done (PR 1 / S1a)
  - **What**: Rename `SENDING_SMS` â†’ `SENDING_PAYMENT`; add `AWAITING_RELEASE` and `APPEALED`; remove `SENDING_PAYMENT â†’ COMPLETED`; implement every R-BUY-012 edge exactly. Keep Flow A edges unchanged.
  - **Files**: `app/domain/enums.py`, `tests/test_enums.py`, enum consumers touched by the rename.
  - **RED test**: `tests/test_enums.py::test_payment_tail_transitions_and_direct_completion_rejected`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_enums.py::test_payment_tail_transitions_and_direct_completion_rejected`
  - **Implementation**: Update `OrderStatus`, `ALLOWED_TRANSITIONS`, and the legal-tail assertions. Add the `AuditEvent`/`AuditActor` members needed by later slices (`PAYMENT_INITIATED`, `PAYMENT_CAPTURED`, `PAYMENT_VERIFIED`, `EVIDENCE_UPLOADED`, `RELEASE_DETECTED`, `APPEAL_FILED`, `APPEAL_SKIPPED`, `TELEGRAM_CHALLENGE_REJECTED`, `OPERATOR`).
  - **Verify**: same node, then `.\.venv\Scripts\python.exe -m pytest -q tests/test_enums.py`.
  - **Deps**: 0.3.
  - **Offline**: yes.

- [x] **1.2 `VES_SMS` â†’ `VES_PAY` code rename and double cap** â€” Done (PR 1 / S1a)
  - **What**: Rename the ledger channel constant to `LEDGER_VES_PAY = VES_PAY`; keep `CHANNELS = {VES, VES_PAY}`; preserve the all-or-nothing `reserve_buy` that reserves `VES` then `VES_PAY` and releases `VES` if the second refuses. `_channel_limit(VES_PAY)` uses `DAILY_PAY_LIMIT_VES`.
  - **Files**: `app/services/risk_policy.py`, `tests/test_risk_policy.py`.
  - **RED test**: `tests/test_risk_policy.py::test_ves_pay_channel_preserves_double_cap_and_release_semantics`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_risk_policy.py::test_ves_pay_channel_preserves_double_cap_and_release_semantics`
  - **Implementation**: Rename constants and docstrings; update the double-cap reservation, channel limit, and release tests. Do not widen the `String(8)` column.
  - **Verify**: same node, then `.\.venv\Scripts\python.exe -m pytest -q tests/test_risk_policy.py`.
  - **Deps**: 1.1.
  - **Offline**: yes.

- [x] **1.3 Status data migration `SENDING_SMS â†’ SENDING_PAYMENT`** â€” Done (PR 1 / S1a)
  - **What**: Create the data-only Alembic revision `20261125_0003_status_ledger_rename.py` with `down_revision = 20261123_0002`; upgrade `p2p_orders.status`, `audit_logs.from_status`, and `audit_logs.to_status`. The downgrade first collapses `AWAITING_RELEASE`/`APPEALED` to `MANUAL_REVIEW`, then reverses the rename; it never maps a paid order back to `SENDING_SMS` and never rewrites historical audit payload JSON strings.
  - **Files**: `alembic/versions/20261125_0003_status_ledger_rename.py`, `tests/test_migrations.py`.
  - **RED test**: `tests/test_migrations.py::test_status_rename_upgrade_and_new_tail_downgrade`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_migrations.py::test_status_rename_upgrade_and_new_tail_downgrade`
  - **Implementation**: Parameterized SQL via `op.get_bind()`; seed `SENDING_SMS` and `AWAITING_RELEASE`/`APPEALED` rows, upgrade, downgrade, and assert the exact mappings. Works on SQLite and PostgreSQL because all columns are `VARCHAR(32)` with no CHECK constraint.
  - **Verify**: same node.
  - **Deps**: 1.1.
  - **Offline**: yes.

- [x] **1.4 Ledger data migration `VES_SMS â†’ VES_PAY`** â€” Done (PR 1 / S1a)
  - **What**: In the same `0003` revision, migrate `daily_volume_ledgers.currency`: for a date with both `VES_SMS` and `VES_PAY`, add `total_volume` and `transaction_count` into `VES_PAY`, delete the stale `VES_SMS` row, then rename every remaining `VES_SMS` row to `VES_PAY`. Downgrade reverses with the same aggregation guard. Preserve `UNIQUE(ledger_date, currency)`.
  - **Files**: `alembic/versions/20261125_0003_status_ledger_rename.py`, `tests/test_migrations.py`.
  - **RED test**: `tests/test_migrations.py::test_ledger_rename_aggregates_collisions_and_rolls_back`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_migrations.py::test_ledger_rename_aggregates_collisions_and_rolls_back`
  - **Implementation**: Cover a same-date collision, a rename-only date, and a UTC date rollover. Use `op.get_bind()` and parameterized SQL; keep each migration atomic.
  - **Verify**: same node.
  - **Deps**: 1.2, 1.3.
  - **Offline**: yes.

- [x] **1.5 Fresh-SQLite upgrade/downgrade gate** â€” Done (PR 1 / S1a)
  - **What**: Prove the full chain on a throwaway SQLite file: `alembic upgrade head`, seed/assert the renames, `alembic downgrade -1`, assert `SENDING_SMS`/`VES_SMS` restoration and new-tail collapse.
  - **Files**: `tests/test_migrations.py`.
  - **RED test**: `tests/test_migrations.py::test_upgrade_head_and_downgrade_minus_one_on_fresh_sqlite`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_migrations.py::test_upgrade_head_and_downgrade_minus_one_on_fresh_sqlite`
  - **Implementation**: Use Alembic's programmatic `command.upgrade(config, head)` and `command.downgrade(config, -1)` against a `tmp_path` SQLite URL. Optionally repeat with the CLI on a temp DB: `$env:DATABASE_URL=sqlite+aiosqlite:///<tmp>/mig.db; .\.venv\Scripts\python.exe -m alembic upgrade head; .\.venv\Scripts\python.exe -m alembic downgrade -1`.
  - **Verify**: same node, then `.\.venv\Scripts\python.exe -m pytest -q tests/test_migrations.py`.
  - **Deps**: 1.3, 1.4.
  - **Offline**: yes.

- [x] **1.6 Flow A transition graph unchanged** â€” Done (PR 1 / S1a)
  - **What**: Add a regression test that the SELL/Cachicamo edges (`PENDING`, `PAYMENT_DETECTED`, `VERIFYING_CACHICAMO`, `COMPLETED`, `FAILED`, `MANUAL_REVIEW`) are identical to the pre-change graph.
  - **Files**: `tests/test_enums.py`, `tests/test_buy_webhook.py`.
  - **RED test**: `tests/test_enums.py::test_flow_a_transition_graph_is_unchanged`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_enums.py::test_flow_a_transition_graph_is_unchanged`
  - **Implementation**: Assert the exact Flow A edge set and run the existing Cachicamo webhook tests unchanged.
  - **Verify**: same node plus `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_webhook.py`.
  - **Deps**: 1.1.
  - **Offline**: yes.

**PR 1 gate**: focused PR 1 nodes, full suite, ruff clean, fresh-SQLite upgrade/downgrade green. **S1a is independently revertible**; no runtime behavior starts.

---

## Phase 2 â€” S1b: Pure Banesco Domain, Typed Errors, Secrets Hygiene (PR 2)

- [x] **2.1 Question normalization and exactly-2-of-4 resolution** â€” Done (PR 2 / S1b)
  - **What**: Pure `normalize_question` (NFKD, strip combining marks, casefold, collapse whitespace, strip trailing `?`, `:`, `.`) and `resolve_question_keys` against the pinned alias table for `HOBBIE`, `CHILDHOOD_HERO`, `FAVOURITE_DESSERT`, `COUNTRY_TO_VISIT`. Fewer than two, more than two, repeated keys, unknown labels, or missing configured answers fail closed with `BanescoLayoutUnexpected`.
  - **Files**: `app/domain/banesco.py`, `tests/test_banesco_domain.py`.
  - **RED test**: `tests/test_banesco_domain.py::test_question_normalization_and_exactly_two_of_four_validation`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_domain.py::test_question_normalization_and_exactly_two_of_four_validation`
  - **Implementation**: Add the pure constants/alias table and validator. Never include question answer values or question text in code/artifacts; the alias table is keyed by normalized fake fixture text.
  - **Verify**: same node.
  - **Deps**: 1.6.
  - **Offline**: yes.

- [x] **2.2 Phone/document two-control projection and `R`/`P` normalization** â€” Done (PR 2 / S1b)
  - **What**: `project_phone(phone) -> (prefix, rest)` with a four-digit prefix and exactly seven remainder digits; `project_document(document) -> (letter, digits)` accepting `V/E/J/G/R`; normalize `P` at the domain layer but reject it at the Banesco mapper with `id_type_unsupported`. Extend `_CEDULA_PREFIX_RE` and `_ID_PATTERN` to include `R`.
  - **Files**: `app/domain/banesco.py`, `app/domain/banks.py`, `app/domain/schemas.py`, `tests/test_banesco_domain.py`, `tests/test_banks.py`.
  - **RED test**: `tests/test_banesco_domain.py::test_phone_and_document_two_control_projection_with_r_and_p`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_domain.py::test_phone_and_document_two_control_projection_with_r_and_p`
  - **Implementation**: Add the projections and prefix-set fixes. `P` normalizes but never projects as an accepted Banesco letter; never default to `V`.
  - **Verify**: same node plus `.\.venv\Scripts\python.exe -m pytest -q tests/test_banks.py`.
  - **Deps**: 2.1.
  - **Offline**: yes.

- [x] **2.3 Destination-bank option map and 0/1/many matching** â€” Done (PR 2 / S1b)
  - **What**: Versioned `BANESCO_DESTINATION_BANK_OPTIONS` for `0102` and `0134` with exact reviewed label/value; match the live option set after whitespace/accent normalization. Zero matches â†’ `unsupported_destination_bank`; multiple â†’ `destination_bank_ambiguous`; changed live label â†’ `unsupported_destination_bank`. Never fuzzy-match or select by index.
  - **Files**: `app/domain/banesco.py`, `tests/fixtures/banesco/bank_options.json`, `tests/test_banesco_domain.py`.
  - **RED test**: `tests/test_banesco_domain.py::test_destination_bank_options_match_zero_one_or_many`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_domain.py::test_destination_bank_options_match_zero_one_or_many`
  - **Implementation**: Add the frozen option DTO, mapping, and a test asserting the domain map equals the sanitized fixture snapshot.
  - **Verify**: same node.
  - **Deps**: 2.2.
  - **Offline**: yes.

- [x] **2.4 Amount rendering and DTO invariants** â€” Done (PR 2 / S1b)
  - **What**: `BanescoPaymentTarget` is frozen/slots, amount is positive finite `Decimal`, `format_ves_amount` renders `1500,50`, and every invalid target fails closed with `payment_data_unavailable`.
  - **Files**: `app/domain/banesco.py`, `tests/test_banesco_domain.py`.
  - **RED test**: `tests/test_banesco_domain.py::test_amount_rendering_dto_invariants_and_decimal_only`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_domain.py::test_amount_rendering_dto_invariants_and_decimal_only`
  - **Implementation**: Add the DTO and rendering helpers; reject floats, zero, negative, and non-finite amounts.
  - **Verify**: same node.
  - **Deps**: 2.3.
  - **Offline**: yes.

- [x] **2.5 Typed `PaymentRailError` taxonomy** â€” Done (PR 2 / S1b)
  - **What**: Add the `PaymentRailError` subtree and every code from design Â§20: `banesco_login_failed`, `banesco_layout_unexpected`, `banesco_captcha_detected`, `banesco_account_locked`, `banesco_maintenance`, `banesco_session_expired`, `banesco_ad_modal_blocked`, `banesco_payment_rejected`, `banesco_payment_outcome_unknown`, `unsupported_phone_prefix`, `id_type_missing`, `id_type_unsupported`, `payment_data_unavailable`, `unsupported_destination_bank`, `destination_bank_ambiguous`, `clave_dinamica_timeout`, `clave_dinamica_rejected`, `receipt_amount_mismatch`, `receipt_conflict`, `receipt_unavailable`, `evidence_upload_failed`, `binance_close_failed`, `appeal_already_filed`, `release_timeout`.
  - **Files**: `app/core/exceptions.py`, `tests/test_banesco_domain.py`.
  - **RED test**: `tests/test_banesco_domain.py::test_payment_rail_error_codes_and_fail_closed_defaults`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_domain.py::test_payment_rail_error_codes_and_fail_closed_defaults`
  - **Implementation**: Derive from `LiquidVEException`; names-only configuration errors; no credential, URL token, or HTML in details.
  - **Verify**: same node.
  - **Deps**: 2.1.
  - **Offline**: yes.

- [x] **2.6 Secrets-hygiene canary suite** â€” Done (PR 2 / S1b)
  - **What**: Add canary assertions that a synthetic credential/key/PII canary never appears in rendered logs, audit payloads, DB text columns, repository/OpenSpec artifacts, or the exact text payload used for Engram saves. The automated test covers logs, audit, DB text columns, and repository/OpenSpec artifacts; the Engram check scans the redacted observation body and is repeated in the final gate.
  - **Files**: `tests/test_secrets_hygiene.py`, `tests/test_logging.py`, `app/core/logging.py`.
  - **RED test**: `tests/test_secrets_hygiene.py::test_no_canary_reaches_logs_audit_db_text_columns_artifacts_or_engram_payload`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_secrets_hygiene.py::test_no_canary_reaches_logs_audit_db_text_columns_artifacts_or_engram_payload`
  - **Implementation**: Use synthetic canary strings only; scan `openspec/changes/**`, the repository tree, rendered log records, audit payload dicts, and seeded DB text columns. Add a redaction-payload assertion for the Engram observation body. Do not save canary values to Engram.\n  - **Verify**: same node. If the Engram store is available, search it for the synthetic canary key and assert zero matches; otherwise the redacted-payload assertion is the automated gate and the final operator check repeats the search.\n  - **Engram check**: this task must leave the live store free of the canary and must never save a credential or canary value.
  - **Deps**: 0.1, 2.5.
  - **Offline**: yes.

- [x] **2.7 Logging masks for new sensitive keys** â€” Done (PR 2 / S1b)
  - **What**: Extend masking for `security_answer`, `answer`, `clave`, `clave_dinamica`, and `R-` documents; ensure receipt text/bytes never reach the logger.
  - **Files**: `app/core/logging.py`, `tests/test_logging.py`.
  - **RED test**: `tests/test_logging.py::test_new_sensitive_keys_and_receipt_text_are_masked`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_logging.py::test_new_sensitive_keys_and_receipt_text_are_masked`
  - **Implementation**: Add key normalization/masking and free-text masking for the new patterns; never log receipt text or image bytes.
  - **Verify**: same node plus `.\.venv\Scripts\python.exe -m pytest -q tests/test_logging.py`.
  - **Deps**: 2.6.
  - **Offline**: yes.

- [x] **2.8 Pure-domain import purity** â€” Done (PR 2 / S1b)
  - **What**: Prove `app/domain/banesco.py` imports without Playwright, without I/O, and without a settings object.
  - **Files**: `app/domain/banesco.py`, `tests/test_banesco_domain.py`.
  - **RED test**: `tests/test_banesco_domain.py::test_domain_module_is_pure_and_import_safe`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_domain.py::test_domain_module_is_pure_and_import_safe`
  - **Implementation**: Keep all pure constants/mappers in this module; remove any accidental I/O.
  - **Verify**: same node.
  - **Deps**: 2.7.
  - **Offline**: yes.

**PR 2 gate**: S1 finish boundary. Focused PR 2 nodes, full suite, ruff clean, secrets canary green. **S1 is complete and autonomous.**

---

## Phase 3 â€” S2: Pure Page Contracts and Recorded Fixtures (PR 3)

- [x] **3.1 Recorded fixture catalog** — Done (PR 3 / S2)
  - **What**: Add sanitized fixtures `login.html`, `security_questions_a.html`, `security_questions_b.html`, `ad_modal.html`, `payment_form.html`, `confirmation.html`, `clave_form.html`, `receipt.html`, `error_captcha.html`, `error_locked.html`, `error_maintenance.html`, `error_session_expired.html`, `bank_options.json`. Fixtures contain fake names/phones/cedulas/references/amounts only.
  - **Files**: `tests/fixtures/banesco/*`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_recorded_fixture_catalog_is_complete_and_sanitized`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_recorded_fixture_catalog_is_complete_and_sanitized`
  - **Implementation**: Create the fixtures; no credential, real PII, real bank account, or real reference.
  - **Verify**: same node.
  - **Deps**: 2.8.
  - **Offline**: yes.

- [x] **3.2 Page-shape parsers over recorded HTML** — Done (PR 3 / S2)
  - **What**: Pure parsers for login entry, 2-of-4 question labels, payment-form option sets, confirmation, clave form, receipt reference/amount/timestamp, and error pages. No Playwright, no I/O.
  - **Files**: `app/domain/banesco.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_page_shape_parsers_over_recorded_html`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_page_shape_parsers_over_recorded_html`
  - **Implementation**: Parse sanitized fixtures into the pure DTOs; a missing/extra critical element is a typed failure, never a best-effort result.
  - **Verify**: same node.
  - **Deps**: 3.1.
  - **Offline**: yes.

- [x] **3.3 Two distinct 2-of-4 question fixtures** — Done (PR 3 / S2)
  - **What**: `security_questions_a.html` and `_b.html` show different subsets/order/positions; tests assert exactly two recognized keys in each and no order assumption.
  - **Files**: `tests/fixtures/banesco/security_questions_a.html`, `_b.html`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_two_of_four_question_fixtures_cover_distinct_subsets_and_orders`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_two_of_four_question_fixtures_cover_distinct_subsets_and_orders`
  - **Implementation**: Adjust fixtures only; the parser must resolve by normalized label text.
  - **Verify**: same node.
  - **Deps**: 3.1.
  - **Offline**: yes.

- [x] **3.4 Fixture hygiene canary** — Done (PR 3 / S2)
  - **What**: Scan `tests/fixtures/banesco/**` for known canary secrets and real-looking PII; fail if found.
  - **Files**: `tests/test_banesco_pages.py`, `tests/fixtures/banesco/*`.
  - **RED test**: `tests/test_banesco_pages.py::test_fixtures_contain_no_canary_or_real_looking_pii`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_fixtures_contain_no_canary_or_real_looking_pii`
  - **Implementation**: Add the scan and sanitize any fixture that trips it.
  - **Verify**: same node.
  - **Deps**: 3.2.
  - **Offline**: yes.

- [x] **3.5 Page-contract import purity** — Done (PR 3 / S2)
  - **What**: Prove the page-contract/parser layer imports without Playwright and without touching the network.
  - **Files**: `app/domain/banesco.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_page_contracts_import_without_playwright`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_page_contracts_import_without_playwright`
  - **Implementation**: Keep parsers in the pure domain module; no adapter import here.
  - **Verify**: same node.
  - **Deps**: 3.2.
  - **Offline**: yes.

**PR 3 gate**: focused S2 nodes, full suite, ruff clean.

---

## Phase 4 â€” S3: Login, Questions, Ad-Modal Adapter (PR 4)

- [x] **4.1 `PageLike`, `BasePage`, and selector tables** - Done (PR 4 / S3)
  - **What**: Add the narrow `PageLike` protocol and `BasePage` with `_expect`, `_await_screen`, and `assert_shape`; selector tables live one per screen, never inline in action methods.
  - **Files**: `app/infrastructure/clients/banesco_pages.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_page_like_fake_records_goto_fill_click_and_select`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_page_like_fake_records_goto_fill_click_and_select`
  - **Implementation**: Implement the protocol shape and fake-compatible page base. No Playwright import in this module.
  - **Verify**: same node.
  - **Deps**: 3.5.
  - **Offline**: fake page only.

- [x] **4.2 `LoginPage`, `SecurityQuestionsPage`, `PasswordPage`** - Done (PR 4 / S3)
  - **What**: Login entry, read labels, pair labelâ†’input by DOM adjacency, answer from settings, submit clave once. Ambiguous pairing, unknown/repeated/wrong-count questions, or missing answers fail closed.
  - **Files**: `app/infrastructure/clients/banesco_pages.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_login_questions_and_clave_page_objects_fail_closed`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_login_questions_and_clave_page_objects_fail_closed`
  - **Implementation**: Use accessible names/roles/labels; audit recognized keys only, never answers.
  - **Verify**: same node.
  - **Deps**: 4.1.
  - **Offline**: fake page only.

- [x] **4.3 `AdModalPage` and `MenuPage`** - Done (PR 4 / S3)
  - **What**: Detect/dismiss the ad modal once; absent is a no-op; undismissable or menu-unreachable fails with `banesco_ad_modal_blocked`. Navigate `Pago MÃ³vil â†’ Enviar pago`; a different product fails closed.
  - **Files**: `app/infrastructure/clients/banesco_pages.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_ad_modal_present_absent_and_undismissable`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_ad_modal_present_absent_and_undismissable`
  - **Implementation**: Add modal/menu page objects with explicit waits and typed failures.
  - **Verify**: same node.
  - **Deps**: 4.2.
  - **Offline**: fake page only.

- [x] **4.4 Typed login error pages** - Done (PR 4 / S3)
  - **What**: Captcha, locked account, maintenance, and session-expiry fixtures map to `banesco_captcha_detected`, `banesco_account_locked`, `banesco_maintenance`, `banesco_session_expired`; no bypass, no retry.
  - **Files**: `app/infrastructure/clients/banesco_pages.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_captcha_locked_maintenance_and_session_expiry_are_typed`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_captcha_locked_maintenance_and_session_expiry_are_typed`
  - **Implementation**: Add error detection and typed raises with safe details (`{stage: ...}` only).
  - **Verify**: same node.
  - **Deps**: 4.2.
  - **Offline**: fake page only.

- [x] **4.5 `BanescoBrowser` lazy import and settings gate** - Done (PR 4 / S3)
  - **What**: `__aenter__` validates required settings before importing Playwright, then lazily imports `playwright.async_api`; `__aexit__`/`aclose` release the context; `pay()` does not exist yet.
  - **Files**: `app/infrastructure/clients/banesco_browser.py`, `tests/test_banesco_browser.py`.
  - **RED test**: `tests/test_banesco_browser.py::test_banesco_browser_lazy_imports_playwright_only_in_aenter`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_browser.py::test_banesco_browser_lazy_imports_playwright_only_in_aenter`
  - **Implementation**: Add the driver skeleton with typed configuration failure naming variables only.
  - **Verify**: same node.
  - **Deps**: 4.3.
  - **Offline**: no browser launch; settings/fake only.

- [x] **4.6 In-process and subprocess import guard** - Done (PR 4 / S3)
  - **What**: Importing `banesco_browser`/`banesco_pages` never imports `playwright`, asserted in-process and in a subprocess mirroring `test_browser_adapter.py`.
  - **Files**: `tests/test_banesco_browser.py`, `app/infrastructure/clients/banesco_browser.py`.
  - **RED test**: `tests/test_banesco_browser.py::test_banesco_adapter_import_is_browser_free_in_subprocess`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_browser.py::test_banesco_adapter_import_is_browser_free_in_subprocess`
  - **Implementation**: Move every Playwright import inside the lazy boundary.
  - **Verify**: same node.
  - **Deps**: 4.5.
  - **Offline**: yes.

**PR 4 gate**: focused S3 nodes, full suite, ruff clean, subprocess import guard green.

---

## Phase 5 â€” S4: Payment Form, Double `Pagar`, Clave Handoff (PR 5)

- [x] **5.1 `PagoMovilFormPage` live options and two-control phone fill** - Done (PR 5a)
  - **What**: Read live phone-prefix options and fill prefix + rest; unoffered prefix/wrong remainder length fails with `unsupported_phone_prefix`/`payment_data_unavailable`; never type the full phone into one control.
  - **Files**: `app/infrastructure/clients/banesco_pages.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_payment_form_reads_live_options_and_fills_two_controls`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_payment_form_reads_live_options_and_fills_two_controls`
  - **Implementation**: Add the form page object and option-set assertions.
  - **Verify**: same node.
  - **Deps**: 4.6.
  - **Offline**: fake page only.

- [x] **5.2 Destination bank selection** - Done (PR 5a)
  - **What**: Select exactly one `Banco Destino` option from the reviewed map; zero/multiple/changed label fails closed; never select by index or fuzzy match.
  - **Files**: `app/infrastructure/clients/banesco_pages.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_destination_bank_selection_zero_one_many_fails_closed`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_destination_bank_selection_zero_one_many_fails_closed`
  - **Implementation**: Read the live option set and match against the domain map.
  - **Verify**: same node.
  - **Deps**: 5.1.
  - **Offline**: fake page only.

- [x] **5.3 Document letter/digits, amount, and concept fill** - Done (PR 5a)
  - **What**: Fill the letter control and bare digits; missing/ambiguous/unsupported letter fails closed (`id_type_missing`/`id_type_unsupported`); amount is `Decimal` rendered in `1500,50`; concept is non-empty and PII-free.
  - **Files**: `app/infrastructure/clients/banesco_pages.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_document_amount_and_concept_fill_only_decimal_ves`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_document_amount_and_concept_fill_only_decimal_ves`
  - **Implementation**: Add the remaining form controls and validations.
  - **Verify**: same node.
  - **Deps**: 5.1.
  - **Offline**: fake page only.

- [x] **5.4 `ConfirmationPage` and double `Pagar`** - Done (PR 5a)
  - **What**: First submit; wait for and observe the confirmation screen; second submit only after observation. Missing/unexpected confirmation fails with `banesco_layout_unexpected` and no second submit.
  - **Files**: `app/infrastructure/clients/banesco_pages.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_double_pagar_requires_observed_confirmation`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_double_pagar_requires_observed_confirmation`
  - **Implementation**: Add `ConfirmationPage.assert_confirmation()` and `submit_second()`; audit each submit without payment data.
  - **Verify**: same node.
  - **Deps**: 5.2, 5.3.
  - **Offline**: fake page only.

- [ ] **5.5 `ClaveDinamicaPage`, `OperatorNotifier` port, fake notifier**
  - **PR 5a note**: only the detection part landed. `ClaveDinamicaPage.assert_present()` waits for `#otp`, pins `maxlength="8"` and exposes no fill/submit surface. Dynamic-key submission, the `OperatorNotifier` port and the fake notifier remain deliberately deferred.
  - **What**: Detect the clave form and submit the accepted key exactly once. Add the `OperatorNotifier` protocol and `FakeOperatorNotifier` scripting correlated reply, timeout, wrong chat, malformed, ambiguous, multiple replies, and wrong key.
  - **Files**: `app/infrastructure/clients/banesco_pages.py`, `app/domain/ports.py`, `tests/conftest.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_clave_form_detected_and_key_submitted_exactly_once`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_clave_form_detected_and_key_submitted_exactly_once`
  - **Implementation**: Add the page object and port; never store/echo/log the key.
  - **Verify**: same node.
  - **Deps**: 5.4.
  - **Offline**: fake page/notifier only.

- [ ] **5.6 `BanescoBrowser.pay()` composition and no retry**
  - **PR 5a note**: the screen composition landed as `BanescoBrowser.fill_and_submit_payment(target)` (login -> menu -> form fill -> first `Pagar` -> observed confirmation -> second `Pagar` -> clave-form detection, returning before any key entry). `pay()`, receipt capture and the explicit no-retry policy remain deferred.
  - **What**: Compose login â†’ questions â†’ clave â†’ modal â†’ menu â†’ form â†’ confirmation â†’ clave handoff in order, with one browser context per attempt. A typed failure aborts the session; no retry, no re-click, no auto-resume.
  - **Files**: `app/infrastructure/clients/banesco_browser.py`, `tests/test_banesco_browser.py`.
  - **RED test**: `tests/test_banesco_browser.py::test_pay_composes_screens_in_order_and_never_retries`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_browser.py::test_pay_composes_screens_in_order_and_never_retries`
  - **Implementation**: Add `pay(attempt, target)` over the fake page/context; receipt capture lands in S5.
  - **Verify**: same node.
  - **Deps**: 5.5.
  - **Offline**: fake page only.

**PR 5 gate**: focused S4 nodes, full suite, ruff clean. Split to S4a/S4b only if authored diff exceeds 760.

---

## Phase 6 â€” S5: Receipt Capture, Storage, and Schema (PR 6)

- [ ] **6.1 `ReceiptStore` digest, dedupe, conflict, amount, retention**
  - **What**: Compute SHA-256 before persistence; same digest is a no-op; different digest on an existing order parks `receipt_conflict`; amount mismatch parks `receipt_amount_mismatch`; retention cleanup removes old files only.
  - **Files**: `app/infrastructure/receipt_store.py`, `tests/test_receipt_store.py`.
  - **RED test**: `tests/test_receipt_store.py::test_receipt_store_hash_dedupe_conflict_amount_and_retention`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_receipt_store.py::test_receipt_store_hash_dedupe_conflict_amount_and_retention`
  - **Implementation**: Add restricted directory creation, digest, dedupe, conflict, mismatch, and retention. Never store raw bytes in the DB.
  - **Verify**: same node.
  - **Deps**: 5.6.
  - **Offline**: `tmp_path` only.

- [ ] **6.2 `ReceiptPage` capture and parse**
  - **What**: Capture the screenshot and parse reference, `Decimal` amount, and bank timestamp; a missing receipt is `receipt_unavailable`/unknown, never success.
  - **Files**: `app/infrastructure/clients/banesco_pages.py`, `app/domain/banesco.py`, `tests/test_banesco_pages.py`.
  - **RED test**: `tests/test_banesco_pages.py::test_receipt_page_captures_reference_amount_timestamp_and_digest`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_banesco_pages.py::test_receipt_page_captures_reference_amount_timestamp_and_digest`
  - **Implementation**: Add `ReceiptPage.capture()` returning the receipt DTO with in-memory raw text only.
  - **Verify**: same node.
  - **Deps**: 6.1.
  - **Offline**: fake page/fixture only.

- [ ] **6.3 Payment/receipt/release-deadline schema revision `0004`**
  - **What**: Add nullable columns `payment_rail`, `payment_attempt_id` UNIQUE, `receipt_path`, `receipt_sha256`, `receipt_amount_ves`, `receipt_paid_at`, `evidence_uploaded_at`, `release_deadline_at` to `p2p_orders`; downgrade drops them with batch mode.
  - **Files**: `app/infrastructure/models.py`, `alembic/versions/20261125_0004_receipt_payment_schema.py`, `tests/test_migrations.py`.
  - **RED test**: `tests/test_migrations.py::test_receipt_and_payment_columns_upgrade_downgrade_fresh_sqlite`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_migrations.py::test_receipt_and_payment_columns_upgrade_downgrade_fresh_sqlite`
  - **Implementation**: Nullable-only; no backfill; unique anchors respected; upgrade head/downgrade `-1` on fresh SQLite.
  - **Verify**: same node.
  - **Deps**: 6.1.
  - **Offline**: yes.

- [ ] **6.4 Receipt persistence: one `OUTGOING` transaction and anchors**
  - **What**: Persist path/hash/reference/amount/timestamp, write exactly one `OUTGOING` `BankTransaction` with a PII-free payload, set `bank_reference`/`bank_confirmed_at`; a colliding reference parks `duplicate_bank_reference`, never a 500.
  - **Files**: `app/infrastructure/receipt_store.py`, `app/infrastructure/models.py`, `tests/test_receipt_store.py`.
  - **RED test**: `tests/test_receipt_store.py::test_receipt_persistence_writes_one_outgoing_and_never_stores_raw_bytes_or_text`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_receipt_store.py::test_receipt_persistence_writes_one_outgoing_and_never_stores_raw_bytes_or_text`
  - **Implementation**: Add the metadata persistence helper; audit `PAYMENT_CAPTURED`/`PAYMENT_VERIFIED` with digest and masked reference only.
  - **Verify**: same node.
  - **Deps**: 6.2, 6.3.
  - **Offline**: in-memory SQLite only.

- [ ] **6.5 Receipt no-PII canary**
  - **What**: Prove raw receipt text, image bytes, and receipt PII never appear in logs, audit payloads, or DB text columns.
  - **Files**: `tests/test_secrets_hygiene.py`, `app/infrastructure/receipt_store.py`, `app/core/logging.py`.
  - **RED test**: `tests/test_secrets_hygiene.py::test_receipt_text_bytes_and_pii_never_reach_logs_audit_or_db`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_secrets_hygiene.py::test_receipt_text_bytes_and_pii_never_reach_logs_audit_or_db`
  - **Implementation**: Render a canary receipt through the store/audit/logging path and assert absence everywhere.
  - **Verify**: same node.
  - **Deps**: 6.4.
  - **Offline**: yes.

- [ ] **6.6 Gitignored restricted receipt directory**
  - **What**: `data/receipts/` is gitignored and created with restrictive permissions; only the relative path is persisted.
  - **Files**: `.gitignore`, `app/infrastructure/receipt_store.py`, `tests/test_receipt_store.py`.
  - **RED test**: `tests/test_receipt_store.py::test_receipt_directory_is_gitignored_and_restrictive`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_receipt_store.py::test_receipt_directory_is_gitignored_and_restrictive`
  - **Implementation**: Add the ignore rule and directory mode; never commit a receipt.
  - **Verify**: same node.
  - **Deps**: 6.1.
  - **Offline**: `tmp_path` only.

**PR 6 gate**: focused S5 nodes, full suite, ruff clean, fresh-SQLite `0004` up/down green.

---

## Phase 7 â€” S6: Telegram Adapter and Durable Challenges (PR 7)

- [ ] **7.1 `TelegramBotClient` raw Bot API**
  - **What**: `send_message` (chat id, text, reply markup, `protect_content`) and `get_updates` (offset, timeout) over existing `httpx`; no new dependency.
  - **Files**: `app/infrastructure/clients/telegram_bot.py`, `tests/test_telegram_bot.py`.
  - **RED test**: `tests/test_telegram_bot.py::test_send_message_and_get_updates_contract_with_respx`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_telegram_bot.py::test_send_message_and_get_updates_contract_with_respx`
  - **Implementation**: Add the adapter; `TELEGRAM_BOT_TOKEN` is a `SecretStr`; never log the token-bearing URL.
  - **Verify**: same node.
  - **Deps**: 6.6.
  - **Offline**: `respx` only.

- [ ] **7.2 Telegram failure, dedupe, and token-log safety**
  - **What**: Handle 429/5xx, dedupe updates by `update_id`, and prove the token never renders in logs.
  - **Files**: `app/infrastructure/clients/telegram_bot.py`, `tests/test_telegram_bot.py`.
  - **RED test**: `tests/test_telegram_bot.py::test_429_5xx_update_dedupe_and_token_never_logged`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_telegram_bot.py::test_429_5xx_update_dedupe_and_token_never_logged`
  - **Implementation**: Add bounded retry/backoff for reads only; never retry a challenge `sendMessage` that may have been delivered.
  - **Verify**: same node.
  - **Deps**: 7.1.
  - **Offline**: `respx` only.

- [ ] **7.3 `operator_challenges` schema revision `0005`**
  - **What**: Add the table with the exact columns from design Â§8.4, `nonce_hash` UNIQUE, index `(order_id, status)`, and nullable timestamps.
  - **Files**: `app/infrastructure/models.py`, `alembic/versions/20261125_0005_operator_challenges.py`, `tests/test_migrations.py`.
  - **RED test**: `tests/test_migrations.py::test_operator_challenges_upgrade_downgrade_fresh_sqlite`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_migrations.py::test_operator_challenges_upgrade_downgrade_fresh_sqlite`
  - **Implementation**: Nullable-only/table add; downgrade drops the table.
  - **Verify**: same node.
  - **Deps**: 7.1.
  - **Offline**: yes.

- [ ] **7.4 Durable challenge state machine and persist-before-send**
  - **What**: Persist `PENDING` with `nonce_hash`/`expires_at` before `sendMessage`; `PENDING â†’ ANSWERED|EXPIRED|CANCELLED` terminal; one `PENDING` per order; a reply after terminal is ignored and audited.
  - **Files**: `app/services/operator_challenges.py`, `tests/test_operator_challenges.py`.
  - **RED test**: `tests/test_operator_challenges.py::test_challenge_persisted_before_send_and_state_machine_is_terminal`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_operator_challenges.py::test_challenge_persisted_before_send_and_state_machine_is_terminal`
  - **Implementation**: Add the service and DB guards; never store the answer/key.
  - **Verify**: same node.
  - **Deps**: 7.3.
  - **Offline**: fake transport only.

- [ ] **7.5 Correlation: wrong chat, malformed, ambiguous, multiple, expired**
  - **What**: Accept only operator chat + reply-to/nonce + order + six-digit or `^[VEJGR]$` body + `PENDING` and unexpired. Wrong chat ignored/audited; malformed replies one clarification then fail closed at `TELEGRAM_MAX_CLARIFICATIONS`; first valid reply wins; later/expired replies ignored.
  - **Files**: `app/services/operator_challenges.py`, `tests/test_operator_challenges.py`.
  - **RED test**: `tests/test_operator_challenges.py::test_correlation_rejects_wrong_chat_malformed_ambiguous_multiple_and_expired`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_operator_challenges.py::test_correlation_rejects_wrong_chat_malformed_ambiguous_multiple_and_expired`
  - **Implementation**: Add correlation and clarification counters; never guess a challenge.
  - **Verify**: same node.
  - **Deps**: 7.4.
  - **Offline**: fake transport only.

- [ ] **7.6 Timeout and restart never resume a payment**
  - **What**: On expiry mark `EXPIRED`, abort the browser page, park `SENDING_PAYMENT â†’ MANUAL_REVIEW` with `clave_dinamica_timeout`/`id_type_missing`, and send an informational notice only. On startup mark every `PENDING` challenge `EXPIRED` and park `SENDING_PAYMENT` with `payment_interrupted`; never auto-resume or re-send a challenge.
  - **Files**: `app/services/operator_challenges.py`, `tests/test_operator_challenges.py`.
  - **RED test**: `tests/test_operator_challenges.py::test_timeout_expires_and_startup_parks_sending_payment_without_resume`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_operator_challenges.py::test_timeout_expires_and_startup_parks_sending_payment_without_resume`
  - **Implementation**: Add `expire_pending` and `reconcile_startup` using `freezegun`; `AWAITING_RELEASE`/`APPEALED` remain safe for read-only polling.
  - **Verify**: same node.
  - **Deps**: 7.5.
  - **Offline**: fake transport + frozen time.

- [ ] **7.7 Fake Telegram transport seam**
  - **What**: Add `FakeTelegramBotClient` at the port and `respx` Bot API helpers; tests correlate by reply-to/nonce without a real bot.
  - **Files**: `tests/conftest.py`, `tests/test_test_seams.py`.
  - **RED test**: `tests/test_test_seams.py::test_fake_telegram_transport_is_offline_and_correlates_by_nonce`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_test_seams.py::test_fake_telegram_transport_is_offline_and_correlates_by_nonce`
  - **Implementation**: Keep the fake and `respx` patterns reusable by S7a/S8.
  - **Verify**: same node.
  - **Deps**: 7.6.
  - **Offline**: yes.

**PR 7 gate**: focused S6 nodes, full suite, ruff clean, fresh-SQLite `0005` up/down green.

---

## Phase 8 â€” S7a: Buy-Flow Tail Rewiring, Durable Attempt, Resume Guard (PR 8)

- [ ] **8.1 Port DTOs and protocol extensions**
  - **What**: Add `PaymentAttempt`, `PaymentReceipt`, `PaymentRail`, `OperatorNotifier`, `ReceiptStore`, and extend `BinanceBrowser` with `upload_evidence`, `notify_paid(..., evidence_path=...)`, `file_appeal`; add `PaymentFate` classification.
  - **Files**: `app/domain/ports.py`, `tests/test_buy_flow.py`.
  - **RED test**: `tests/test_buy_flow.py::test_buy_ports_expose_payment_attempt_target_receipt_and_notifier`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_buy_ports_expose_payment_attempt_target_receipt_and_notifier`
  - **Implementation**: Add frozen DTOs with `Decimal` money; structural protocols only.
  - **Verify**: same node.
  - **Deps**: 7.7.
  - **Offline**: yes.

- [ ] **8.2 Build the Banesco target before B2; `id_type_missing` path**
  - **What**: Build the target from the instruction and live option sets; a missing letter opens an `ID_TYPE` challenge correlated to the order and fails closed to `MANUAL_REVIEW`/`id_type_missing`; never default to `V`. All of this happens before B2/reservation/browser.
  - **Files**: `app/services/buy_flow.py`, `app/domain/banesco.py`, `app/services/operator_challenges.py`, `tests/test_buy_flow.py`.
  - **RED test**: `tests/test_buy_flow.py::test_target_built_before_b2_and_missing_letter_opens_id_type_challenge`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_target_built_before_b2_and_missing_letter_opens_id_type_challenge`
  - **Implementation**: Add the mapping step and ID-type challenge; no reservation and no browser before the target exists.
  - **Verify**: same node.
  - **Deps**: 8.1.
  - **Offline**: fake notifier/browser only.

- [ ] **8.3 Durable B2 attempt and no browser before commit**
  - **What**: Commit `SENDING_PAYMENT`, both reservations, and `payment_attempt_id = {order_number}:payment` UNIQUE before any browser action; a second invocation is refused; a crash leaves a `SENDING_PAYMENT` order that cannot auto-reinvoke the rail.
  - **Files**: `app/services/buy_flow.py`, `tests/test_buy_flow.py`.
  - **RED test**: `tests/test_buy_flow.py::test_b2_commits_sending_payment_attempt_and_reservation_before_rail`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_b2_commits_sending_payment_attempt_and_reservation_before_rail`
  - **Implementation**: Replace the SMS dispatch tail with the rail invocation after the B2 commit; audit `PAYMENT_INITIATED`.
  - **Verify**: same node.
  - **Deps**: 8.2.
  - **Offline**: fake rail/browser only.

- [ ] **8.4 Rail invoked once; unknown outcome never retried**
  - **What**: Invoke `PaymentRail.pay` exactly once; timeout/missing receipt/ambiguous page state parks `MANUAL_REVIEW`/`payment_outcome_unknown`; never retry, never complete.
  - **Files**: `app/services/buy_flow.py`, `tests/test_buy_flow.py`.
  - **RED test**: `tests/test_buy_flow.py::test_rail_invoked_once_and_unknown_parks_without_retry`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_rail_invoked_once_and_unknown_parks_without_retry`
  - **Implementation**: Add fate handling for `CAPTURED`, `REJECTED`, `ABORTED`, `UNKNOWN`; no automatic second call.
  - **Verify**: same node.
  - **Deps**: 8.3.
  - **Offline**: fake rail/browser only.

- [ ] **8.5 Permanent pre-submission failure releases both channels**
  - **What**: Only a classified permanent pre-submission failure releases `VES` and `VES_PAY` and parks `MANUAL_REVIEW`; timeout/unknown/receipt mismatch/release timeout never release.
  - **Files**: `app/services/buy_flow.py`, `tests/test_buy_flow.py`.
  - **RED test**: `tests/test_buy_flow.py::test_permanent_pre_submission_failure_releases_ves_and_ves_pay`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_permanent_pre_submission_failure_releases_ves_and_ves_pay`
  - **Implementation**: Add the classification and release guard; audit released amounts/channels.
  - **Verify**: same node.
  - **Deps**: 8.4.
  - **Offline**: in-memory SQLite only.

- [ ] **8.6 Resume guard for payment-related parks**
  - **What**: `SENDING_PAYMENT`/`AWAITING_RELEASE` resume does not invoke the rail; `MANUAL_REVIEW â†’ PENDING` is allowed only for pre-payment reasons and refused for `clave_dinamica_timeout`, `payment_outcome_unknown`, `receipt_amount_mismatch`, and `release_timeout`.
  - **Files**: `app/services/buy_flow.py`, `app/api/v1/orders.py`, `tests/test_buy_flow.py`, `tests/test_orders_api.py`.
  - **RED test**: `tests/test_buy_flow.py::test_payment_related_manual_review_cannot_resume_and_awaiting_release_is_noop`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_payment_related_manual_review_cannot_resume_and_awaiting_release_is_noop`
  - **Implementation**: Tighten `resume` and `orders.py`; audit refusals with `IDEMPOTENCY_HIT`.
  - **Verify**: same node plus `.\.venv\Scripts\python.exe -m pytest -q tests/test_orders_api.py`.
  - **Deps**: 8.5.
  - **Offline**: in-memory SQLite only.

**PR 8 gate**: focused S7a nodes, full suite, ruff clean. No evidence upload or `AWAITING_RELEASE` yet.

---

## Phase 9 â€” S7b: Binance Evidence Upload and `AWAITING_RELEASE` (PR 9)

- [ ] **9.1 `upload_evidence` and extended `notify_paid`**
  - **What**: Upload the receipt image through the file input, wait for the attachment bubble, click the notify control, post the reference, and wait for the reference bubble. `upload_evidence` is reusable by the appeal path.
  - **Files**: `app/infrastructure/clients/binance_browser.py`, `app/domain/ports.py`, `tests/test_browser_adapter.py`.
  - **RED test**: `tests/test_browser_adapter.py::test_upload_evidence_then_notify_paid_in_order`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_browser_adapter.py::test_upload_evidence_then_notify_paid_in_order`
  - **Implementation**: Add the fake-page interactions; keep the lazy Playwright import and no network.
  - **Verify**: same node.
  - **Deps**: 8.6.
  - **Offline**: fake page only.

- [ ] **9.2 Evidence at-most-once and failure parking**
  - **What**: Invoke upload+confirm at most once per receipt; check `evidence_uploaded_at` and prior `EVIDENCE_UPLOADED`; failure/unknown parks `MANUAL_REVIEW` with `evidence_upload_failed`/`binance_close_failed`; no `AWAITING_RELEASE`, no appeal.
  - **Files**: `app/services/buy_flow.py`, `tests/test_buy_flow.py`.
  - **RED test**: `tests/test_buy_flow.py::test_evidence_uploaded_once_and_failure_parks_without_release_or_appeal`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_evidence_uploaded_once_and_failure_parks_without_release_or_appeal`
  - **Implementation**: Add the evidence guard and failure taxonomy; never auto-retry an unknown outcome.
  - **Verify**: same node.
  - **Deps**: 9.1.
  - **Offline**: fake browser only.

- [ ] **9.3 B5 commits `AWAITING_RELEASE` only after upload + confirm**
  - **What**: On success commit `EVIDENCE_UPLOADED`, `SENDING_PAYMENT â†’ AWAITING_RELEASE`, `release_deadline_at = now + RELEASE_TIMEOUT_SECONDS`, and `evidence_uploaded_at`; never `COMPLETED` at this point.
  - **Files**: `app/services/buy_flow.py`, `tests/test_buy_flow.py`, `tests/test_enums.py`.
  - **RED test**: `tests/test_buy_flow.py::test_b5_commits_awaiting_release_only_after_upload_and_confirm`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_buy_flow.py::test_b5_commits_awaiting_release_only_after_upload_and_confirm`
  - **Implementation**: Add the B5 transition and deadline; assert `SENDING_PAYMENT â†’ COMPLETED` is impossible.
  - **Verify**: same node plus `.\.venv\Scripts\python.exe -m pytest -q tests/test_enums.py`.
  - **Deps**: 9.2.
  - **Offline**: fake browser + in-memory SQLite.

**PR 9 gate**: focused S7b nodes, full suite, ruff clean. Orders can now rest in `AWAITING_RELEASE`.

---

## Phase 10 â€” S8: Release Watcher and Automatic Appeal (PR 10)

- [ ] **10.1 Conservative status mapping**
  - **What**: Map observed release â†’ `COMPLETED`; `APPEALING` â†’ `APPEALED` without filing another appeal; `CANCELLED`/`EXPIRED` â†’ `MANUAL_REVIEW`/`external_order_cancelled`; still paid/pending â†’ continue; unknown â†’ `MANUAL_REVIEW`/`external_status_unknown`.
  - **Files**: `app/services/release_watcher.py`, `app/infrastructure/clients/binance_client.py`, `tests/test_release_watcher.py`.
  - **RED test**: `tests/test_release_watcher.py::test_status_mapping_release_appealing_cancelled_unknown`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_release_watcher.py::test_status_mapping_release_appealing_cancelled_unknown`
  - **Implementation**: Add the pure mapping and `FakeBinance` sequences; never guess an unrecognized status.
  - **Verify**: same node.
  - **Deps**: 9.3.
  - **Offline**: `FakeBinance` only.

- [ ] **10.2 Watcher selection, cadence, read-only polling**
  - **What**: Select `AWAITING_RELEASE` and `APPEALED` orders with non-null `bank_reference`/`bank_confirmed_at`; poll `get_order_detail` at `RELEASE_POLL_INTERVAL_SECONDS`; repeated ticks are no-ops.
  - **Files**: `app/services/release_watcher.py`, `tests/test_release_watcher.py`.
  - **RED test**: `tests/test_release_watcher.py::test_watcher_selects_awaiting_release_and_appealed_read_only`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_release_watcher.py::test_watcher_selects_awaiting_release_and_appealed_read_only`
  - **Implementation**: Add the tick and guarded transitions; no completion without observed release.
  - **Verify**: same node.
  - **Deps**: 10.1.
  - **Offline**: `FakeBinance` only.

- [ ] **10.3 Timeout policy `auto`/`alert`/`off`**
  - **What**: When `now > release_deadline_at`: `auto` runs the appeal path after a pre-click re-check; `alert` parks `MANUAL_REVIEW`/`release_timeout` and notifies; `off` leaves the order flagged with no appeal and no completion.
  - **Files**: `app/services/release_watcher.py`, `tests/test_release_watcher.py`.
  - **RED test**: `tests/test_release_watcher.py::test_release_timeout_honors_auto_alert_off`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_release_watcher.py::test_release_timeout_honors_auto_alert_off`
  - **Implementation**: Add the mode switch using `freezegun`; `RELEASE_APPEAL_MODE=off` is the emergency kill switch.
  - **Verify**: same node.
  - **Deps**: 10.2.
  - **Offline**: `FakeBinance` + frozen time.

- [ ] **10.4 Appeal schema revision `0006`**
  - **What**: Add nullable `appeal_attempt_id` UNIQUE, `appeal_filed_at`, `appeal_status`; downgrade drops them.
  - **Files**: `app/infrastructure/models.py`, `alembic/versions/20261125_0006_appeal_schema.py`, `tests/test_migrations.py`.
  - **RED test**: `tests/test_migrations.py::test_appeal_columns_upgrade_downgrade_fresh_sqlite`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_migrations.py::test_appeal_columns_upgrade_downgrade_fresh_sqlite`
  - **Implementation**: Nullable-only; unique attempt identity; upgrade/downgrade on fresh SQLite.
  - **Verify**: same node.
  - **Deps**: 10.1.
  - **Offline**: yes.

- [ ] **10.5 Pre-click re-check and durable appeal identity**
  - **What**: Re-check `get_order_detail`; released â†’ `COMPLETED` + `APPEAL_SKIPPED`; already `APPEALING` â†’ `APPEALED` with no click; unknown â†’ `MANUAL_REVIEW`. Otherwise commit `appeal_attempt_id = {order_number}:appeal` UNIQUE + `APPEALED` + `appeal_filed_at` + `appeal_status=pending` + `APPEAL_FILED` before the click.
  - **Files**: `app/services/appeal.py`, `app/infrastructure/clients/binance_browser.py`, `app/domain/ports.py`, `tests/test_appeal.py`.
  - **RED test**: `tests/test_appeal.py::test_pre_click_recheck_and_durable_appeal_attempt_before_click`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_appeal.py::test_pre_click_recheck_and_durable_appeal_attempt_before_click`
  - **Implementation**: Add `AppealPage.file(reason, evidence_path)` and the service; the reason label is pinned in a fixture.
  - **Verify**: same node.
  - **Deps**: 10.3, 10.4.
  - **Offline**: fake page + `FakeBinance`.

- [ ] **10.6 Unknown appeal outcome is never retried**
  - **What**: Success sets `appeal_status=filed`; failure/unknown sets `unknown` and stays `APPEALED`; no second click; operator verifies.
  - **Files**: `app/services/appeal.py`, `tests/test_appeal.py`.
  - **RED test**: `tests/test_appeal.py::test_unknown_appeal_outcome_stays_appealed_and_never_clicks_twice`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_appeal.py::test_unknown_appeal_outcome_stays_appealed_and_never_clicks_twice`
  - **Implementation**: Add outcome recording and the duplicate-attempt guard.
  - **Verify**: same node.
  - **Deps**: 10.5.
  - **Offline**: fake page + `FakeBinance`.

- [ ] **10.7 Watcher wiring and restart at-most-once**
  - **What**: Add `_run_release_watcher` in `app/main.py`, gated by `RELEASE_WATCHER_ENABLED`; a restart re-polls but never re-files an appeal. `AWAITING_RELEASE`/`APPEALED` are safe to resume read-only.
  - **Files**: `app/main.py`, `tests/test_release_watcher.py`, `tests/conftest.py`.
  - **RED test**: `tests/test_release_watcher.py::test_watcher_restart_never_refiles_appeal`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_release_watcher.py::test_watcher_restart_never_refiles_appeal`
  - **Implementation**: Add the task and guarded transitions; no payment/appeal re-invocation.
  - **Verify**: same node.
  - **Deps**: 10.6.
  - **Offline**: `FakeBinance` + no background loop in tests.

**PR 10 gate**: focused S8 nodes, full suite, ruff clean, fresh-SQLite `0006` up/down green, kill switches tested. Split to S8a/S8b only if authored diff exceeds 760.

---

## Phase 11 â€” S10: Operator/Observability Polish (PR 11)

- [ ] **11.1 Operator outcomes for `AWAITING_RELEASE` and `APPEALED`**
  - **What**: Add PII-free response outcomes and an appeal/reconcile action; payment-related parks stay non-resumable; expose only status, reason, digest, and masked reference.
  - **Files**: `app/api/v1/orders.py`, `app/domain/schemas.py`, `tests/test_orders_api.py`.
  - **RED test**: `tests/test_orders_api.py::test_operator_views_awaiting_release_and_appealed_without_pii`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_orders_api.py::test_operator_views_awaiting_release_and_appealed_without_pii`
  - **Implementation**: Extend the response model and guarded action; never echo phone/cedula/receipt text.
  - **Verify**: same node.
  - **Deps**: 10.7.
  - **Offline**: ASGI in-process only.

- [ ] **11.2 Health probes for Banesco and Telegram when enabled**
  - **What**: Add readiness probes that report `banesco_online` and `telegram` only when the corresponding feature is enabled; disabled features do not degrade readiness. Database remains the only critical component.
  - **Files**: `app/api/v1/health.py`, `tests/test_health.py`.
  - **RED test**: `tests/test_health.py::test_banesco_and_telegram_probes_are_reported_only_when_enabled`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_health.py::test_banesco_and_telegram_probes_are_reported_only_when_enabled`
  - **Implementation**: Add the conditional probes and keep existing SMS/Cachicamo behavior until S9.
  - **Verify**: same node.
  - **Deps**: 11.1.
  - **Offline**: in-process fakes only.

- [ ] **11.3 Docs and placeholder-only `.env.example` update**
  - **What**: Document the new statuses, rail, kill switches, and setting names with placeholders only; no real value or credential.
  - **Files**: `README.md`, `.env.example`, `tests/test_config.py`.
  - **RED test**: `tests/test_config.py::test_env_example_documents_every_new_setting_by_name_only`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_config.py::test_env_example_documents_every_new_setting_by_name_only`
  - **Implementation**: Add names and neutral descriptions; keep every secret as a placeholder.
  - **Verify**: same node.
  - **Deps**: 11.2.
  - **Offline**: yes.

- [ ] **11.4 Operator runbook and kill-switch verification**
  - **What**: Document `BANESCO_RAIL_ENABLED`, `RELEASE_WATCHER_ENABLED`, `BUY_AUTO_PAY_ENABLED`, and `RELEASE_APPEAL_MODE` behavior; prove docs contain variable names only.
  - **Files**: `README.md`, `tests/test_secrets_hygiene.py`.
  - **RED test**: `tests/test_secrets_hygiene.py::test_operator_runbook_and_docs_contain_variable_names_only`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_secrets_hygiene.py::test_operator_runbook_and_docs_contain_variable_names_only`
  - **Implementation**: Add the runbook and the doc canary. Do not include a real URL, token, user, answer, or chat id.
  - **Verify**: same node.
  - **Deps**: 11.3.
  - **Offline**: yes.

**PR 11 gate**: focused S10 nodes, full suite, ruff clean. No SMS surface removed yet.

---

## Phase 12 â€” S9: Decommission the SMS BUY Path (PR 12 â€” FINAL)

- [ ] **12.1 Decommission guard tests**
  - **What**: Add tests that fail while the SMS BUY surface exists: no `/webhooks/sms` route, no BUY caller of `sms_dispatch`, no `bank_replies` import, no `_run_sms_poller`, no SMS BUY sweep, no `SmsGatewayClient.send_sms`/`fetch_inbox` caller, no `R-BANK-006/007` code. Keep the Cachicamo flow green.
  - **Files**: `tests/test_decommission.py`, `tests/test_buy_webhook.py`.
  - **RED test**: `tests/test_decommission.py::test_sms_buy_surface_removed_and_cachicamo_unchanged`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_decommission.py::test_sms_buy_surface_removed_and_cachicamo_unchanged`
  - **Implementation**: Write the guards first; do not edit production code in this task.
  - **Verify**: expected RED.
  - **Deps**: 11.4.
  - **Offline**: yes.

- [ ] **12.2 Remove the SMS webhook and SMS BUY schemas/methods**
  - **What**: Remove `/webhooks/sms`, `SmsInboundWebhookPayload`, `SmsWebhookResponse`, `confirm_bank_payment`, `handle_incoming_sms`, `_claim_sms_event`, `_finish_sms_event`, `_process_bank_reply`, `_match_candidates`, `_confirm_success`, and the SMS close helpers that have no BUY caller. Keep Cachicamo and its dedupe anchor untouched.
  - **Files**: `app/api/v1/webhooks.py`, `app/domain/schemas.py`, `app/services/buy_flow.py`, `tests/test_buy_webhook.py`.
  - **RED test**: `tests/test_decommission.py::test_sms_webhook_and_sms_schemas_are_gone`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_decommission.py::test_sms_webhook_and_sms_schemas_are_gone`
  - **Implementation**: Delete the BUY SMS symbols and their callers; do not leave stubs.
  - **Verify**: same node plus `.\\.venv\\Scripts\\python.exe -m pytest -q tests/test_buy_webhook.py`.
  - **Deps**: 12.1.
  - **Offline**: yes.

- [ ] **12.3 Delete `sms_dispatch.py`, `bank_replies.py`, and R-BANK-006/007 code**
  - **What**: Delete `app/services/sms_dispatch.py` and `app/domain/bank_replies.py`; remove `render_bank_command`, `parse_bank_reply`, SMS command/reply pattern data, and their tests. Keep pure bank mapping/normalization and `resolve_bank` for the Banesco rail.
  - **Files**: `app/services/sms_dispatch.py` (delete), `app/domain/bank_replies.py` (delete), `app/domain/banks.py`, `tests/test_sms_dispatch.py` (delete), `tests/test_bank_replies.py` (delete), `tests/test_banks.py`.
  - **RED test**: `tests/test_decommission.py::test_sms_dispatch_bank_replies_and_rendering_are_gone`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_decommission.py::test_sms_dispatch_bank_replies_and_rendering_are_gone`
  - **Implementation**: Delete, do not stub; remove dead imports and pattern tables.
  - **Verify**: same node plus `.\\.venv\\Scripts\\python.exe -m pytest -q tests/test_banks.py`.
  - **Deps**: 12.2.
  - **Offline**: yes.

- [ ] **12.4 Remove the SMS poller, sweep, gateway methods, and health probe**
  - **What**: Remove `_run_sms_poller`, the startup task wiring, the SMS timeout sweep, `SmsGatewayClient.send_sms`/`fetch_inbox`, the SMS health probe, and any unused SELL-side SMS client wiring. Keep `health_check` only if a remaining Flow A surface still references the gateway; otherwise remove the dead client wiring.
  - **Files**: `app/main.py`, `app/services/buy_flow.py`, `app/infrastructure/clients/sms_gateway.py`, `app/api/v1/health.py`, `app/services/sell_flow.py`, `tests/test_health.py`.
  - **RED test**: `tests/test_decommission.py::test_sms_poller_sweep_gateway_methods_and_health_probe_are_gone`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_decommission.py::test_sms_poller_sweep_gateway_methods_and_health_probe_are_gone`
  - **Implementation**: Delete the background task and methods; do not reintroduce an SMS BUY fallback. Flow A behavior must remain unchanged.
  - **Verify**: same node plus `.\\.venv\\Scripts\\python.exe -m pytest -q tests/test_health.py tests/test_buy_flow.py`.
  - **Deps**: 12.3.
  - **Offline**: yes.

- [ ] **12.5 Update SMS-era tests, config, and docs**
  - **What**: Update or delete SMS-era tests, remove BUY SMS setting names from `.env.example` and README, and prove no SMS BUY reference remains. Keep historical SMS telemetry columns in the DB; do not add a destructive column-drop migration.
  - **Files**: `tests/`, `.env.example`, `README.md`, `tests/test_decommission.py`.
  - **RED test**: `tests/test_decommission.py::test_no_sms_buy_references_and_cachicamo_sell_flow_green`.
  - **See it fail**: `.\.venv\Scripts\python.exe -m pytest -q tests/test_decommission.py::test_no_sms_buy_references_and_cachicamo_sell_flow_green`
  - **Implementation**: Remove dead references, keep Cachicamo, and update docs with placeholders only.
  - **Verify**: same node plus `.\\.venv\\Scripts\\python.exe -m pytest -q tests/test_buy_webhook.py`.
  - **Deps**: 12.4.
  - **Offline**: yes.

- [ ] **12.6 Final decommission gate**
  - **What**: Prove the SMS BUY path is fully gone and the new rail remains green.
  - **Files**: no production edits; verification only.
  - **RED test**: `tests/test_decommission.py::test_sms_buy_surface_removed_and_cachicamo_unchanged` (must now pass).
  - **See it fail**: it already failed in 12.1; now it must pass.
  - **Implementation**: Run the full gate.
  - **Verify**: `.\.venv\Scripts\python.exe -m pytest -q`, `.\.venv\Scripts\python.exe -m ruff check app tests`, grep for `sms_dispatch`, `bank_replies`, `webhooks/sms`, `_run_sms_poller`, `send_sms`, `fetch_inbox`, `R-BANK-006`, `R-BANK-007`; expect no live BUY references.
  - **Deps**: 12.5.
  - **Offline**: yes.

**PR 12 gate**: this is the last PR. Full suite, ruff, no dead imports, Cachicamo green, no SMS BUY runtime path.

---

## Manual Pre-Production Checklist â€” Live-Only Gates

These cannot be verified offline and must be completed manually before enabling the rail, the watcher, or the appeal in production.

- [ ] Record sanitized live DOM for login, both 2-of-4 question variants, ad modal, Pago MÃ³vil form, confirmation, clave form, receipt, and every error page; update fixtures and rerun the fixture tests.
- [ ] Confirm the exact normalized security-question aliases for all four configured questions; never record answer values.
- [ ] Confirm the exact `Banco Destino` option labels/values for `0102` and `0134`; update `bank_options.json` and the domain map together.
- [ ] Confirm the exact `Pagar`/confirmation DOM and the Clave form; record sanitized fixtures.
- [ ] Confirm the Binance release/appeal status vocabulary and the exact appeal reason label; pin the reason in a fixture before enabling `RELEASE_APPEAL_MODE=auto`.
- [ ] Confirm the bank idle timeout relative to `TELEGRAM_CLAVE_TIMEOUT_SECONDS`; a shorter bank timeout means fail closed.
- [ ] Confirm the amount-input format (comma-decimal vs thousands-grouped) from the live field.
- [ ] Confirm the Telegram operator chat id and bot token exist only in the local gitignored `.env`; never paste them into an artifact, test, fixture, or Engram.
- [ ] Run the opt-in live smoke gates (`LIQUIDVE_BROWSER_TESTS`, `LIQUIDVE_BANESCO_PAYMENT_TESTS`) only with sanitized/fixture orders and a supervised low-value payment; never in CI.

---

## Review Workload Forecast

Estimated changed lines: 5,600â€“6,400
400-line budget risk: High
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
Decision needed before apply: No
Suggested work units: 13

Justification: this change crosses enums, ledger accounting, two data migrations, three nullable schema revisions, a pure domain mapper, four browser page-object layers, a Telegram adapter, a durable challenge state machine, receipt storage, buy-flow rewiring, a release watcher, an at-most-once appeal, and a deletion-heavy decommission. Strict TDD adds a focused RED test for every behavior. The configured review ceiling is 800 authored changed lines per PR; most PRs exceed 400 lines, so `400-line budget risk` is High even though every PR stays under the configured 800-line ceiling. The delivery strategy is `auto-chain` with `stacked-to-main`; no pre-apply decision is needed because the orchestrator starts with PR 0 and proceeds in order. The design's S1 and S7 splits are adopted, and the schema revision is split by owning slice for independent revertibility.

| PR | Slices | Authored lines | Dependency |
|---|---|---:|---|
| PR 0 | Phase 0 prerequisites and shared seams | 320 (280â€“360) | None |
| PR 1 | S1a status/ledger/data migrations | 430 (380â€“470) | PR 0 |
| PR 2 | S1b pure Banesco domain/errors/secrets | 360 (320â€“420) | PR 1 |
| PR 3 | S2 page contracts and fixtures | 520 (450â€“600) | PR 2 |
| PR 4 | S3 login/questions/modal adapter | 560 (480â€“650) | PR 3 |
| PR 5 | S4 payment form/double Pagar/clave | 620 (540â€“700) | PR 4 |
| PR 6 | S5 receipt capture/storage/schema `0004` | 470 (400â€“540) | PR 5 |
| PR 7 | S6 Telegram/durable challenges/schema `0005` | 540 (460â€“620) | PR 6 |
| PR 8 | S7a buy-flow tail/durable attempt | 420 (360â€“500) | PR 7 |
| PR 9 | S7b evidence upload/`AWAITING_RELEASE` | 320 (260â€“380) | PR 8 |
| PR 10 | S8 release watcher/appeal/schema `0006` | 680 (560â€“760) | PR 9 |
| PR 11 | S10 operator/observability polish | 300 (250â€“360) | PR 10 |
| PR 12 | S9 SMS decommission (final) | 480 (400â€“560) | PR 11 |



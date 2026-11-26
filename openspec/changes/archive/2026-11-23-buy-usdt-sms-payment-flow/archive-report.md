# Archive Report: Flow B — Buy USDT via Pago Móvil SMS

- **Change**: `buy-usdt-sms-payment-flow`
- **Archived**: 2026-11-23
- **Archive path**: `openspec/changes/archive/2026-11-23-buy-usdt-sms-payment-flow/`
- **Store**: openspec (filesystem)
- **Final verdict**: **PASS WITH WARNINGS** (0 CRITICAL, 6 carried WARNINGs, 10 SUGGESTIONs)
- **SDD cycle**: complete (propose → spec → design → tasks → apply → verify → archive)

## Final-State Authority

This report describes the change **at close**. Two intermediate snapshots exist and are retained as audit trail:

- `verify-report.md` baseline (`# Verification Report`) recorded **FAIL** with 2 CRITICALs on 2026-11-23. That verdict is history, not the current state.
- `verify-report.md` `# Re-verification Round 1` supersedes it: after Correction Round 1, **C1, C2, W1 and W2 are resolved** and the verdict is **PASS WITH WARNINGS** with 0 CRITICAL and 0 blockers.

No higher-ranked source (review gate, terminal receipt) exists for this candidate: receipt-driven review was not enabled, so there is no `reviewGate` to satisfy. Task completion is validated from the persisted `tasks.md` below.

## What Shipped

Flow B is now a complete, fail-closed BUY path that was previously unreachable and semantically incomplete:

1. **Detect** open BUY orders (worker tick and operator trigger), selecting only `tradeType=BUY` + external `PENDING_PAYMENT`, mapped to local `PENDING` with no new enum member.
2. **Extract** bank numeric code, counterparty phone, cedula and exact VES amount through a `BinanceBrowser` domain port with a Playwright adapter (lazy import, browser-free CI).
3. **Gate** every dispatch behind four ordered circuit breakers: per-order cap, daily SMS cap, implied-rate tolerance, supported-bank resolution.
4. **Park** behind an operator approval gate when `BUY_AUTO_PAY_ENABLED=false` (default), resumable only through an internal-key endpoint.
5. **Dispatch** one exact Pago Móvil command (`Pagar <code> <phone> <cedula> <amount>`) with a durable `SENDING_SMS` commit before the gateway call, a stable `Idempotency-Key`, one classified short-code fallback, and no retry on unknown fate.
6. **Confirm** the bank reply by profile parsing, sender/amount/window correlation, exactly-one-candidate matching, one `OUTGOING` `BankTransaction`, and a unique `bank_reference` anchor.
7. **Close** on Binance via `notify_paid` and reach `COMPLETED` **only** after that notification succeeds.
8. **Secure** a pre-existing cross-flow hazard: the sell-side inbound payment path now selects SELL orders only, so an identical-amount BUY order can never be advanced or released by it.

Additionally, a **pre-existing loguru `patcher` boot blocker** was found and fixed during this change (a follow-up unit, not one of the 58 planned tasks): `logger.remove()` → `logger.configure(patcher=...)`, with a regression test proving the masking patcher still applies and real boot prints `BOOT OK`.

## Final Counts

| Metric | Value |
|---|---|
| Requirements | **18** (11 `R-BUY-001..011` + 7 `R-BANK-001..007`) |
| Scenarios | **44** (29 `buy-flow` + 15 `bank-sms`) |
| Tasks (numeric headers) | 58 |
| Tasks checked `- [x]` | 56 |
| Tasks unchecked `- [ ]` | **0** |
| Tasks with no checkbox | 2 (`0.4` provider doubles, `0.5` respx seams — deliverables exist, recorded as W7) |
| PRs / slices | 10 PRs (S1, S2a/S2b, S3a/S3b, S4a/S4b, S5, S6, S7) — all implemented |
| Tests at archive time | **263 passed, 2 skipped** (the 2 skips are the opt-in Playwright live smokes gated by `LIQUIDVE_BROWSER_TESTS`) |
| Lint | `ruff check app tests` → clean |
| Boot | `configure_logging(get_settings())` → `BOOT OK` |
| Format | `ruff format` deliberately **not** run (would rewrite ~19 pre-existing files) |

## Specs Synced (source of truth)

Both capabilities were NEW (`openspec/specs/` held only `.gitkeep`). They were materialised as full specs, not delta headers, by a deterministic header-only transform; every requirement ID, requirement body, and scenario is byte-identical to the delta.

| Domain | Action | Requirements | Scenarios |
|---|---|---|---|
| `buy-flow` | Created | 11 added (`R-BUY-001..011`) | 29 |
| `bank-sms` | Created | 7 added (`R-BANK-001..007`) | 15 |

Changes applied to each file: `# Delta for {domain}` → `# {domain} Specification`, and `## ADDED Requirements` → `## Requirements`. Verified byte-for-byte: destination equals source with exactly those two substitutions (`body-byte-identical-except-2-header-lines=True` for both files).

Main specs now authoritative at:

- `openspec/specs/buy-flow/spec.md`
- `openspec/specs/bank-sms/spec.md`

## Decisions Recorded

Product/scope decisions from the proposal:

| # | Decision | Chosen default | Rationale |
|---|---|---|---|
| D1 | Auto-pay vs operator approval | `BUY_AUTO_PAY_ENABLED` default **false**; risk-approved orders park in `MANUAL_REVIEW` / `awaiting_operator_approval` and resume through an internal-key endpoint | Unattended irreversible bank debits must be opt-in; fail-safe default means money cannot move until a human enables it |
| D2 | Bank-reply wording per bank | Data-driven `success_pattern` / `failure_patterns` per `BankSmsProfile`; BDV `0102` and Banesco `0134` shipped; unknown reply → `MANUAL_REVIEW` | Real wording varies and is unverified; regex as editable data, never guess a money confirmation |
| D3 | Cedula prefix in the command | Bare digits only; optional `cedula_format` per profile | Matches the only product evidence, `Pagar 0102 04141234567 12345678 1500,00` |
| D4 | Implied-rate reference | Order-intrinsic `amount_fiat / amount_asset` vs order `price` within `RATE_TOLERANCE_PCT` (1.0%), no external feed | Catches mangled scrapes without a new provider |
| D5 | Meaning of "close" | Notify seller + post reference; `release_order` never called | C2C merchant release stubs are out of scope |
| D6 | Daily SMS cap carrier | `daily_volume_ledgers` with namespaced `currency="VES_SMS"`; no migration for the channel | Reuses existing locking; migration-free money-risk slice |
| D7 | `PENDING_PAYMENT` representation | Map Binance `PENDING_PAYMENT` → local `PENDING`; add no enum member | Fewer states, no migration; local status already means "registered, no money moved" |

Deliberate design choices to preserve in future work:

1. **Double-cap semantics**: a *dispatched* BUY consumes both the shared `VES` channel and the `VES_SMS` channel. Both are charged atomically at B2, and both are released together on a confirmed permanent dispatch failure. A BUY that never dispatches (gate-off park, `payment_data_unavailable`) touches neither channel.
2. **Reserve-at-B2**: risk assessment is check-only; the actual reservation happens once, at the B2 transaction that commits `SENDING_SMS` + `idempotency_key` **before** the gateway call. This is what keeps the ledger truthful and closes the C2 leak.
3. **`BUY_AUTO_PAY_ENABLED=false`** by default — operator approval is the safe default.
4. **`BUY_WORKER_ENABLED=false`** by default — no detection, no timeout sweep until enabled.
5. **`BINANCE_BROWSER_ENABLED=false`** by default — a Chromium-less runtime still serves sell traffic and webhooks; extraction failures route to `MANUAL_REVIEW/payment_data_unavailable` instead of crashing.
6. **`COMPLETED` means "seller notified"**, not crypto settlement. The reference is stored; settlement is a future change.

## Schema

- Alembic head: **`20261123_0002`** (down to `20260101_0001`).
- Five **nullable** telemetry columns added to `p2p_orders`: `counterparty_id_number`, `sms_message_id`, `sms_sent_at`, `bank_short_code`, `bank_confirmed_at`.
- Verified on a fresh SQLite DB: `upgrade head` → `downgrade -1` → `upgrade head` all succeed.
- `upgrade` uses `op.add_column`; `downgrade` uses `batch_alter_table` for SQLite compatibility (no-op semantics on PostgreSQL).

**Destructive-delta warning: none.** There are no `REMOVED` requirements, no destructive column drops on the upgrade path, and no enum removals. The only removal is the optional `alembic downgrade -1`, which drops the five newly added nullable columns — by construction no core-flow data depends on them. The UNIQUE idempotency anchors (`order_number`, `bank_reference`) are untouched.

## Runtime Prerequisites (discovered during this change)

- The gitignored local `.env` **must** define `MAX_BUY_AMOUNT_VES` and `DAILY_SMS_LIMIT_VES`. Both are REQUIRED settings with no defaults; `Settings()` is constructed at import time, so omitting either fails the import. `.env.example` documents both.
- `daily_sms_limit_ves` and `rate_tolerance_pct` / `max_implied_rate_ves_per_usdt` gate dispatch; `sms_confirmation_timeout_seconds` bounds the reply window.
- Real sends require the Android SMS gateway over Tailscale (`sms_gateway_base_url`, `sms_gateway_token`).
- Browser extraction/close requires `playwright==1.49.1` + Chromium and a Binance session storage state (manual smoke only; CI is browser-free).
- Local `.env` remains gitignored; never commit it.

## Carried Follow-Up Backlog (non-blocking)

No carried item is a demonstrated money-safety or data-integrity defect.

### WARNING

| ID | Issue | Where |
|---|---|---|
| **W3** | A confirmed order whose close cannot run is stranded, and the webhook still reports `confirmed`; no operator re-attempt path exists | `buy_flow.py` close gate (`browser is None` → `confirmed`, order stays `SENDING_SMS`); `orders.py` no-op responses |
| **W4** | Timeout-sweep dedupe is "once per order ever", not "once per window"; concurrent ticks untested | `buy_flow.py` sweep audit scan (no window bound, read-then-write) |
| **W5** | Amount matching is skipped when the reply carries no amount; mitigated by exactly-one + window, not a demonstrated defect | `buy_flow.py` matcher (`parsed.amount is not None` guard) |
| **W6** | Approval path re-extracts instead of reusing the persisted instruction (gate-off park precedes instruction persistence) | `buy_flow.py` gate vs persistence ordering; `orders.py` → `_dispatch` |
| **W7** | Per-task TDD evidence table exists only for PR 10 (plus Correction Round 1); PRs 1–9 are narrative. `tasks.md` has 58 headers / 56 checkboxes / 2 without checkbox (`0.4`, `0.5`, deliverables present) | apply-progress artifact; `tasks.md` |
| **W8** | Coverage below the informational 80% bar on `orders.py` (53%), `webhooks.py` (66%), `main.py` (74%), `sell_flow.py` (64%, pre-existing). New cores are strong (`buy_flow.py` 87%, `risk_policy.py` 91%, `sms_dispatch.py` 98%, TOTAL 80%); async/coverage attribution caveat applies | `pytest --cov=app` |

### SUGGESTION

- **S1** — Unprocessed-replay branch loses its `WEBHOOK_RECEIVED` audit (event row + audit rolled back together, replay reprocessed without it).
- **S2** — "Operator alert" for ≥2 candidates is only `logger.error` + audit; no notification channel.
- **S3** — Operator response has no "unknown" outcome; any non-`MANUAL_REVIEW` result maps to `"dispatched"`, and `"resumed"`/`"awaiting_approval"` are never emitted.
- **S4** — `mypy app` reports 31 errors vs a 30-error recorded baseline; advisory only (no mypy config, not a gate).
- **S5** — Pre-existing encoding drift (`app/core/logging.py` docstring mojibake); `ruff format` not run.
- **S6** — Timeout sweep is coupled to `BUY_WORKER_ENABLED`; disabling the worker (the documented rollback) also stops flagging in-flight `SENDING_SMS` rows.
- **S7** — Reply-window upper bound uses the provider timestamp; small clock skew can yield `no_match` and strand an order until the sweep flags it.
- **N1** — Operator endpoint holds `SELECT … FOR UPDATE` across external Binance I/O; correctness unaffected, concurrency serialises.
- **N2** — A `reserve_buy` race raises fail-closed with a net-zero ledger but leaves the order pre-dispatch with only a worker warning (no audit / `MANUAL_REVIEW`).
- **N3** — Redundant read-only projection: `check_shared` is called in `execute` and again in `_dispatch`.

### Unverified offline (explicitly recorded)

- Real BDV/Banesco success and failure SMS wording is **unverified**; shipped patterns are conservative defaults and unparseable/ambiguous replies fail closed to `MANUAL_REVIEW`.
- Playwright DOM selectors are **unverified** against live Binance; parser tests use committed HTML fixtures only.
- PostgreSQL row-lock concurrency is **offline-unexercised** (SQLite treats `FOR UPDATE` as a no-op); W2/C1 fixes are proven logically and via sequential/stale-request probes, not under real PG contention.
- `mypy` is advisory and currently red.

## Rollback Instructions

Flow B can be made dormant with **no data deletion**:

1. Set all three flags off:
   - `BUY_AUTO_PAY_ENABLED=false`
   - `BUY_WORKER_ENABLED=false`
   - `BINANCE_BROWSER_ENABLED=false`
2. Remove the `orders` router registration in `app/main.py` (the `include_router(orders.router, ...)` call). With it removed, no operator endpoint can dispatch a pre-existing BUY row; with the flags off, no worker starts, no browser is constructed, no SMS is dispatched and no Binance close occurs.
3. Reverse the schema delta if required: `alembic downgrade -1` (drops the five nullable columns; core-flow data and the UNIQUE anchors are untouched).
4. In-flight `SENDING_SMS` / `MANUAL_REVIEW` rows are preserved for manual handling. Note S6: disabling the worker also disables the timeout sweep, so those rows will not be re-flagged automatically.
5. `VES_SMS` ledger rows are inert when the feature is off and may be removed by `ledger_date` if reporting must be cleaned. No enum rollback is needed (D7).

Per-slice reverts remain available: S1/S3 are additive modules; S2 has a characterization-test net; S4's migration is nullable-only; each PR was designed independently revertible.

## Repository State at Archive

- **No commits, branches, or PRs were created.** HEAD is still `fe7e93a` (`chore: scaffold liquidVE backend (FastAPI + async SQLAlchemy 2.0)`).
- Every code change lives in the **uncommitted working tree** (41 `git status` entries at archive time).
- `origin` is `https://github.com/jsiewdass/liquid-ve.git`.
- The `openspec/` tree itself was untracked; this archive performs no git staging.

## Audit Trail Retained

The archived folder intentionally keeps `verify-report.md` and `tasks.md` (plus `exploration.md`, `proposal.md`, `design.md`, and `specs/`). Per the archive policy, archived changes are never modified after this point.

## Task Completion Gate Result

`tasks.md` was scanned before archive: 56 `- [x]`, **0 `- [ ]`**, 2 task headers without a checkbox (`0.4`, `0.5`) whose deliverables exist and are covered by verify-report. No stale unchecked implementation task remains, so the gate passed without needing an archive-time checkbox reconciliation. W7 remains recorded as evidence-quality debt, not as incomplete work.

## Mechanical Verification

- **Spec materialisation**: destination bytes equal source bytes with exactly two header substitutions; requirement/scenario counts preserved (11/29 and 7/15); verbatim `git diff --no-index` shows only the two intended header hunks per file.
- **Archive move**: pre-move recursive snapshot (7 files) vs archived tree (7 files) → SHA-256 manifest comparison `MANIFEST DIFF COUNT: 0`; verbatim `git diff --no-index <snapshot> <archive>` produced **no content diff** and exited `0`; the source directory no longer exists.

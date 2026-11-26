# Design: Flow B — Buy USDT via Pago Móvil SMS

- **Change**: `buy-usdt-sms-payment-flow`
- **Phase**: design
- **Inputs**: `proposal.md` (D1–D7, S1–S7), `specs/buy-flow/spec.md` (R-BUY-001..011), `specs/bank-sms/spec.md` (R-BANK-001..007), `openspec/config.yaml` (`rules.design`), current source tree.
- **Store**: openspec (artifact) + Engram `sdd/buy-usdt-sms-payment-flow/design`.
- **Scope note**: the orchestrator mandated twelve concrete answers plus four sequence diagrams, so this artifact is intentionally longer than the 800-word default. Prose is table-first and dense; slices S1–S7 are individually budget-checked in §13.

## Technical Approach

Three pure domain modules first — `app/domain/banks.py` (routing data + formatting), `app/domain/bank_replies.py` (reply parsing), `app/domain/ports.py` (`BinanceBrowser` protocol + `PaymentInstruction`) — then one shared risk service (`app/services/risk_policy.py`), then the stateful orchestration inside `BuyFlow`. Every third-party boundary is an adapter under `app/infrastructure/clients` (`binance_browser.py` new; `sms_gateway.py` reused), and every browser/UI concern is reachable only through the port so the default `pytest -q` run never imports Playwright, never opens a socket and never launches Chromium.

The money rule is preserved end-to-end: every bound is a `Decimal`, every function that accepts money rejects `float` explicitly, and no adapter returns a `float`.

The orchestration rule is "persist the intent, then act, then record": the `SENDING_SMS` transition, the ledger reservation and the idempotency key are committed **before** the bank command leaves the process (`buy_flow.py:441-444` is the pattern to preserve and widen); the bank-reply confirmation anchor is committed **before** the Binance notify (`sell_flow.py:472` → `:501` is the existing precedent); and every unknown outcome stays visible and inert instead of being retried.

## Architecture Decisions

| # | Decision | Alternatives considered | Rationale |
|---|---|---|---|
| A1 | **Commit the `SENDING_SMS` intent, the `VES_SMS` reservation and `p2p_orders.idempotency_key` in one transaction before the gateway call.** A crash between commit and call leaves a "phantom" `SENDING_SMS` order that must be resolved by a human. | Send-then-commit; commit per step. | Send-then-commit allows a double bank debit after a crash — unrecoverable. A phantom `SENDING_SMS` costs an operator click and is fully visible (R-BUY-006 scenario "State is durable before the call"). |
| A2 | **`resume()` never re-sends while `SENDING_SMS`.** Recovery from a phantom order is `SENDING_SMS → MANUAL_REVIEW → PENDING → execute()` by an operator, reusing the same `Idempotency-Key`. | Auto-resend on `sms_message_id IS NULL`. | The key is stable per order, so a manual re-dispatch is gateway-deduped if the first request did land. Automatic resend cannot distinguish "never sent" from "sent, response lost". |
| A3 | **`BUY_AUTO_PAY_ENABLED=false` parks in `MANUAL_REVIEW` with `awaiting_operator_approval` and reserves nothing.** | Park at `PAYMENT_DETECTED`. | `MANUAL_REVIEW → PENDING` is already a legal edge (`enums.py:72`); `PAYMENT_DETECTED` has no path back from human approval without inventing one. |
| A4 | **`BankSmsProfile.cedula_format` (`str` or `None`, default `None`) in the profile; bare digits when unset.** | Prefix hardcoded in the template. | D3: a bank needing `V-{cedula}` becomes a data edit (R-BANK-005 scenario 2). |
| A5 | **Reply parsing returns a four-valued `BankReplyOutcome` (`SUCCESS`/`FAILURE`/`UNPARSEABLE`/`AMBIGUOUS`), never a best guess.** Live in `app/domain/bank_replies.py`, not `enums.py`. | Boolean success flag. | R-BANK-007 requires the fourth state (both a success and a failure pattern matched ⇒ ambiguous ⇒ no confirmation). `enums.py` stays untouched except for the D7 docstring fix, avoiding the mojibake table. |
| A6 | **The ledger release fires only on a confirmed permanent *dispatch* failure (R-BUY-006), never on a parsed bank failure reply, ambiguity, no-match or timeout.** | Release on any failure reply (as `exploration.md` §4.4 sketched). | R-BUY-004: "released only on a confirmed permanent dispatch failure and MUST NOT be released on timeout". An over-broad failure regex must not free budget for a debit that may have queued. Bank-rejected orders go to `MANUAL_REVIEW` with the reservation intact and an operator releases after checking the banking app. |
| A7 | **SMS webhook event rows are committed before processing, and the duplicate check is `processed`-aware.** | Keep the current "any existing row ⇒ `duplicate=True`" behaviour (`webhooks.py:187-193`). | The replay anchor must survive a crash (otherwise a provider retry re-runs an irreversible notify), but a crash after the anchor must not lose the confirmation. An unprocessed row is therefore re-processed at-least-once; confirmation is idempotent because of the `bank_reference` UNIQUE index and the `bank_confirmed_at IS NULL` candidate filter. A *processed* replay still returns 202 `duplicate=True` (R-BUY-007 scenario 4). |
| A8 | **HTML extraction parser is module-level pure functions in `binance_browser.py`, using only stdlib `re`/`html`; the Playwright class is a thin caller.** | BeautifulSoup/selectolax; parser as an adapter method; parser in `app/domain`. | No new dependency; the parser is importable with zero Playwright imports, so selector tests run in CI from committed fixtures. A method on the adapter would drag the browser into every test. Domain purity is preserved because the parser is not part of the port contract. |
| A9 | **Buy-side block reasons are machine-readable codes; Sell-side prose reasons stay byte-identical.** | Normalize both flows' wording. | The specs require `amount_over_cap`, `daily_sms_cap_exceeded`, `rate_out_of_tolerance`, `unsupported_destination_bank`, `payment_data_unavailable`, `awaiting_operator_approval`, `sms_confirmation_timeout`, `duplicate_bank_reference`, `binance_close_failed`. Changing `SellFlow` strings would break the S2 characterization test and any operator runbook. |
| A10 | **Direction guard is a SQL predicate plus a defensive assertion in `handle_incoming_payment`.** | Predicate only. | R-BUY-011 must hold even if a future caller bypasses the webhook. Both are in S2 and independently cherry-pickable. |
| A11 | **Alembic `upgrade` uses plain `op.add_column`; `downgrade` uses `with op.batch_alter_table(...)`.** | Plain `drop_column` on both. | SQLite supports `ADD COLUMN` natively but `DROP COLUMN` only from 3.35; batch mode recreates the table and is a no-op on PostgreSQL. `alembic/env.py` already configures `render_as_batch=True`, so this is consistent with the project's Alembic posture. |
| A12 | **The browser is optional at process level (`binance_browser_enabled=false` default) and injected into `BuyFlow`; a missing browser surfaces as a typed `binance_browser` error, not a crash.** | Always construct the browser in the lifespan. | A runtime image without Chromium must still serve sell traffic and webhooks; extraction failures then route to `MANUAL_REVIEW/payment_data_unavailable` instead of taking the process down. |

## 1. Transaction Boundaries (BUY)

`BuyFlow` keeps the caller-owned `AsyncSession` and commits explicitly. Revised boundary map (B-numbers are referenced by the sequence diagrams and the test names):

| # | Commit content | Why here | Crash after commit, before the next step |
|---|---|---|---|
| **B1** | `P2POrder(status=PENDING)` + `ORDER_CREATED` audit. No ledger write. | `order_number` UNIQUE must be the durable idempotency anchor (R-BUY-001), and detection (worker/webhook) is independent of dispatch. | Order exists as `PENDING` with no money moved and no reservation. Next tick/`execute` continues. Re-insert fails on UNIQUE → `DuplicateOrderError`. |
| **B2** | Transition `PENDING → PAYMENT_DETECTED → VERIFYING_CACHICAMO → SENDING_SMS` (+3 `STATUS_CHANGED` audits), `bank_code`, `bank_short_code` (primary), `counterparty_id_number`, `idempotency_key = "{order_number}:payout"`, **`VES_SMS` reservation** (read-modify-write of the ledger row). | R-BUY-006: state, reservation and attempt record durable before the call. `idempotency_key` is UNIQUE, so exactly one dispatch attempt identity can exist per order. | `SENDING_SMS` with a reservation and **no SMS sent**. Deliberate (A1). Recovery: operator verifies the banking app, moves `SENDING_SMS → MANUAL_REVIEW → PENDING`, re-dispatch with the same key. The sweep flags it after the window. |
| **B2a** | `MANUAL_REVIEW` + `manual_review_reason=awaiting_operator_approval` + `RISK_BLOCKED`/`STATUS_CHANGED` audit. No reservation, no key. | R-BUY-005: gate off must reserve nothing. | Parked, inert, zero external calls. |
| **B3** | `EXTERNAL_ERROR` audit only (fate unknown/classified). Status unchanged. | Keeps `SENDING_SMS` as the durable "money may be out" marker. | Same state, no resend on any later resume (A2). |
| **B4** | `SMS_SENT` audit + `sms_message_id` + `sms_sent_at`, status still `SENDING_SMS`. | Post-call telemetry, not a state change; no `COMPLETED` here. | Order stays `SENDING_SMS` with a `NULL` `sms_message_id`; the correlation window falls back to `updated_at`, so a real bank reply is still matchable (§6). |
| **B5** | Ledger release + transition `SENDING_SMS → MANUAL_REVIEW` (+ `manual_review_reason=sms_dispatch_failed`, `EXTERNAL_ERROR` audit), single transaction. | R-BUY-006: permanent failure releases the reservation. | Release and the review transition are atomic: either both happened or neither did, so the release is state-machine-guarded and cannot run twice. |
| **B0′** | `WebhookEvent(provider="sms_gateway", external_event_id=message_id)` committed at the top of the SMS handler. | Replay anchor before any money-adjacent side effect (A7), mirroring `sell_flow`'s `bank_reference` anchor. | Event row exists unprocessed; the provider retry re-processes it (at-least-once) instead of being swallowed. |
| **B6** | `BankTransaction(direction=OUTGOING, bank_reference=<reply ref>)` + `p2p_orders.bank_reference` + `bank_confirmed_at` + `SMS_RECEIVED`/`PAYMENT_VERIFIED` audits, one transaction. | The financial anchor must exist before the non-idempotent Binance notify (same shape as `sell_flow.py:472`). | Order is `SENDING_SMS` **with `bank_confirmed_at` set**: the queryable "confirmed, close pending" state. Operator re-attempts the close; R-BUY-009 forbids automatic re-invocation. |
| **B7** | Transition `SENDING_SMS → COMPLETED` + `STATUS_CHANGED` audit with masked reference. | R-BUY-009: only after a successful `notify_paid`. | `COMPLETED`, terminal. |

Never-committed-alone rule: the `VES_SMS` reservation is **never** committed without the matching `SENDING_SMS` transition (same transaction), and a release is never committed without the matching `SENDING_SMS → MANUAL_REVIEW` transition. Both invariants are what make the ledger a truthful cap.

The pre-existing `COMPLETED` at `buy_flow.py:469-477` is deleted in S4; between S4 and S6 a confirmed order has no path to `COMPLETED`, which is the intended safe intermediate state.

## 2. Idempotent Side Effects

| External effect | Idempotency mechanism | Classified *not-delivered* | Unknown fate |
|---|---|---|---|
| Pago Móvil SMS (real bank debit) | Durable `SENDING_SMS` (B2) + `p2p_orders.idempotency_key` UNIQUE = `"{order_number}:payout"` + A2 (no resume re-send). | **One** fallback to `short_codes[1]`, same `Idempotency-Key`, same body. A second not-delivered result is permanent → B5. | **No retry.** Stay `SENDING_SMS`, commit `EXTERNAL_ERROR` (B3), flag via sweep. |
| Binance `notify_paid` (UI click + chat) | `bank_confirmed_at IS NULL` gate, `bank_reference` UNIQUE anchor, `message_id` dedupe, and `status == SENDING_SMS` guard: each of the four independently blocks a second close. | No fallback exists (one UI action). Failure after a *deterministic* response → `MANUAL_REVIEW` + `binance_close_failed`. | Not re-invoked automatically; operator only (R-BUY-009). |
| Ledger reserve / release | Executed inside the same transaction as the state transition they belong to; release guarded by `SENDING_SMS → MANUAL_REVIEW` being legal exactly once; row lock per `(date, currency)`. | Released once (B5). | **Never released** (R-BUY-010). |
| `webhook_events` row | `UNIQUE(provider, external_event_id)`; `processed`-aware replay (A7). | n/a | Re-processed at-least-once; confirmation itself idempotent. |
| `bank_transactions` OUTGOING | `UNIQUE bank_reference` + "exactly one candidate" matcher + `bank_confirmed_at IS NULL` filter. | n/a | Collision → `DuplicateBankReferenceError` → `MANUAL_REVIEW` (never 500). |
| `P2POrder` creation | `order_number` UNIQUE. | n/a | `DuplicateOrderError` / resume. |
| Binance reads (`get_order_history`, `get_order_detail`) | Naturally idempotent (`idempotent=True` already set in the adapter). | Retried by `BaseAsyncHTTPClient`. | Retried. |

Gateway outcome classification (pure function in `app/services/sms_dispatch.py`, unit-tested without I/O):

| Observed result | Fate | Action |
|---|---|---|
| `SmsSendResult` returned | `DELIVERED` | B4 |
| `ExternalServiceResponseError` (other 4xx: request rejected before enqueue) | `NOT_DELIVERED` | one fallback, else B5 |
| `UnauthorizedError` (401/403) | `PERMANENT` | B5 immediately — another short code cannot fix credentials |
| `ConflictError` (409: a message with this key already exists) | `UNKNOWN` | B3, no fallback |
| `RetryableExternalServiceError`, `ExternalServiceRateLimitedError`, transport/timeout, unparseable 2xx body | `UNKNOWN` | B3, no retry |

`BaseAsyncHTTPClient.request` already refuses retries for `idempotent=False` (`base.py:283-302`), so `send_sms` makes exactly one HTTP request per attempt — the tests assert `len(route.calls) == 1`.

## 3. Ledger Reserve / Release (`VES_SMS`)

Reuse `daily_volume_ledgers` (`models.py:312-350`) with no migration (D6). The channel is a pseudo-currency string in the existing `currency` column (`String(8)`, so `"VES_SMS"` = 7 chars fits).

```python
# app/services/risk_policy.py
LEDGER_VES = "VES"          # shared spend cap, both directions (unchanged semantics)
LEDGER_VES_SMS = "VES_SMS"  # outgoing Pago Móvil channel cap
CHANNELS = frozenset({LEDGER_VES, LEDGER_VES_SMS})

class RiskPolicy:
    def __init__(self, session: AsyncSession, settings: Settings) -> None: ...
    async def load_or_create_ledger(self, *, currency: str) -> DailyVolumeLedger: ...
    async def check_channel_cap(self, *, currency: str, amount_ves: Decimal,
                                limit: Decimal) -> RiskAssessment: ...   # read-only projection
    async def reserve(self, *, currency: str, amount_ves: Decimal) -> Decimal: ...
    async def release(self, *, currency: str, amount_ves: Decimal) -> Decimal: ...
    async def assess_shared(self, *, amount_ves: Decimal, amount_asset: Decimal) -> RiskAssessment:
        ...  # check + reserve in one call: byte-identical behaviour to today's _assess_risk
```

- **Columns**: `(ledger_date DATE, currency VARCHAR(8), total_volume NUMERIC(24,2), transaction_count INT)` with `UNIQUE(ledger_date, currency)`. Two rows per day coexist: `('2026-11-23','VES')` for shared spend and `('2026-11-23','VES_SMS')` for the SMS channel. A reservation on one never touches or blocks the other — the `FOR UPDATE` lock is per row.
- **`_load_or_create_ledger` parameterization**: the single literal `"VES"` at `buy_flow.py:230-272` and `sell_flow.py:250-292` (four occurrences each) becomes the `currency` argument. Both flow copies are deleted; `RiskPolicy` owns the one implementation.
- **Row locking**: PostgreSQL → `SELECT … WHERE ledger_date=:d AND currency=:c ... FOR UPDATE`, creating the row inside the same transaction when absent (the lock is then implicitly held for the pair). SQLite → `INSERT … ON CONFLICT DO NOTHING` followed by `SELECT`, with the guarantee coming from SQLite's single-writer lock plus `PRAGMA busy_timeout=5000` (`database.py:63-68`): the first write in the transaction takes the write lock, so two concurrent reservations serialize. No `FOR UPDATE` is emitted on SQLite. The check-then-write stays inside B2's transaction, so the cap cannot be overrun by two dispatchers.
- **Reserve**: `projected = (total + amount).quantize(0.01)`; refuse when `projected > limit`; otherwise write `projected` and `transaction_count + 1`. Called by BUY only after the per-order cap, rate tolerance and bank support all pass (R-BUY-004: reserve only on the allow path). `assess_shared` for SELL keeps today's check-and-increment shape so the S2 characterization test passes unchanged.
- **Release**: only from B5. `total = max(Decimal("0.00"), total - amount)`, `transaction_count = max(0, count - 1)`. Guarded by the state machine (the `SENDING_SMS → MANUAL_REVIEW` edge is legal once), so double release is impossible; a caller that passes a currency outside `CHANNELS` gets a `ValueError` instead of silently creating a phantom counter row.
- **Never on timeout** (R-BUY-010): the money may already have left. The sweep only flags.

## 4. Ports and Seams

```python
# app/domain/ports.py
@dataclass(frozen=True, slots=True)
class PaymentInstruction:
    order_number: str
    bank_numeric_code: str          # "0102" as scraped, trimmed
    phone: str                      # as scraped, E.164 or national
    id_number: str                  # "V-12345678" or "12345678" as scraped
    amount_ves: Decimal             # never float
    raw_fields: Mapping[str, str] = field(default_factory=dict)

class BinanceBrowser(Protocol):
    async def extract_payment_instruction(self, order_number: str) -> PaymentInstruction: ...
    async def notify_paid(self, order_number: str, reference: str) -> None: ...
```

`bank` is deliberately absent from the DTO: `resolve_bank(numeric_code)` runs in the service, so a scraped-but-unsupported bank is a *business* decision (`unsupported_destination_bank`), not a parser error. Normalization (`national_digits`, `cedula_digits`, `parse_ves_amount`) also happens in the domain layer, so a mangled scrape fails the rate/extraction checks instead of producing a wrong command.

`app/infrastructure/clients/binance_browser.py` has two strictly separated parts:

```python
# part 1 — pure, stdlib only, importable without playwright
_VES_AMOUNT_RE = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$|^\d+,\d{2}$")
def parse_payment_instructions(html: str, *, order_number: str) -> PaymentInstruction | None: ...
def _extract_label_fields(html: str) -> Mapping[str, str]: ...

# part 2 — the adapter
class PlaywrightBinanceBrowser:              # conforms structurally to BinanceBrowser
    def __init__(self, settings: Settings) -> None: ...
    async def __aenter__(self) -> "PlaywrightBinanceBrowser": ...   # lazy `import playwright`
    async def aclose(self) -> None: ...
    async def extract_payment_instruction(self, order_number: str) -> PaymentInstruction: ...
    async def notify_paid(self, order_number: str, reference: str) -> None: ...
```

- **Lazy import**: `playwright.async_api` is imported inside `__aenter__`/first call only. `import app.infrastructure.clients.binance_browser` therefore never requires the wheel or Chromium.
- **Session storage state**: `browser_context = await browser.new_context(storage_state=settings.binance_session_state_path, headless=settings.binance_browser_headless)`; the path is a manual-smoke artifact and is gitignored. Missing/expired state → `ExternalServiceError(service="binance_browser", code="binance_session_expired")`.
- **Extraction** = navigate `settings.binance_p2p_order_url_template.format(order_number=...)` → `html = await page.content()` → `parse_payment_instructions(html, order_number=...)`. `None` → `ExternalServiceResponseError(service="binance_browser", code="payment_data_unavailable")`; navigation/timeout → `ExternalServiceError`.
- **Close** = click the "Transferido, notificar al vendedor" control, post `reference` in the chat box, and *confirm* the resulting DOM state before returning; any uncertainty raises instead of returning silently, so R-BUY-009's "unknown close outcome" is representable.
- **Why the parser must be separate for CI**: `requirements.txt:23` pins `playwright==1.49.1`, so `pytest.importorskip("playwright")` would **pass** in this repo and a naive "skipped when absent" adapter test would try to launch Chromium. Parser tests therefore import part 1 directly (no guard needed), and the one adapter smoke test is gated on `@pytest.mark.integration` **plus** `@pytest.mark.skipif(not os.getenv("LIQUIDVE_BROWSER_TESTS"), ...)`.

## 5. Bank SMS Module (`app/domain/banks.py`)

```python
@dataclass(frozen=True, slots=True)
class BankSmsProfile:
    bank: BankCode
    display_name: str
    numeric_code: str                       # 4 digits, e.g. "0102"
    short_codes: tuple[str, ...]            # ordered, primary first
    command_template: str                   # "Pagar {numeric_code} {phone_national} {cedula} {amount_ve}"
    success_patterns: tuple[str, ...]       # >= 1 (R-BANK-001)
    failure_patterns: tuple[str, ...] = ()  # may be empty
    cedula_format: str | None = None        # None => bare digits
    aliases: tuple[str, ...] = ()

BANK_SMS_PROFILES: Mapping[str, BankSmsProfile]     # MappingProxyType, keyed by numeric_code
SHORT_CODE_INDEX: Mapping[str, BankSmsProfile]      # MappingProxyType, "2661" -> BDV ...

def resolve_bank(value: str) -> BankSmsProfile | None: ...
def profile_for_sender(sender: str) -> BankSmsProfile | None: ...
def apply_short_code_overrides(profile: BankSmsProfile,
                               overrides: Sequence[str]) -> BankSmsProfile: ...
def format_ves_amount(value: Decimal) -> str: ...
def parse_ves_amount(text: str) -> Decimal: ...
def national_digits(phone: str) -> str: ...
def cedula_digits(value: str, *, cedula_format: str | None = None) -> str: ...
def render_bank_command(profile: BankSmsProfile, *, phone: str,
                        cedula: str, amount: Decimal) -> str: ...
```

- **`resolve_bank`** order of evaluation: trim → if `/^\d{4}$/` hit `BANK_SMS_PROFILES` (miss ⇒ `None`, no alias fallback, so `0105` stays unsupported per R-BANK-003) → else normalize (NFKD, alnum-lower) and match aliases/enum names, **longest alias wins**; if the winning length class maps to more than one distinct `BankCode` ⇒ `None` (ambiguous). Never returns `OTHER` and never a nearest bank. `BankCode.from_aliases` (`enums.py:103-126`) is reused for spelling tolerance but its `OTHER` result is treated as "no profile".
- **`national_digits`**: `validate_venezuelan_phone` (`schemas.py:18-48`) → strip the `+58` prefix → prepend `0`. Total length 11, leading `0`. Idempotent on an already-national value (R-BANK-005).
- **`cedula_digits`**: strip `V/E/J/G/P`, dashes and spaces; require 6–9 digits; then apply `cedula_format.format(cedula=digits)` when set.
- **`format_ves_amount`**: `isinstance(value, Decimal)` else `BankSmsFormatError` (floats forbidden) → require finite and `> 0` → `quantize(Decimal("0.01"), ROUND_HALF_UP)` → `f"{q:.2f}".replace(".", ",")`. No grouping, no symbol.
- **`parse_ves_amount`**: inverse of the above for scrapes and replies (`"1.500,00"` → `Decimal("1500.00")`); unmatched text raises `BankSmsParseError`, never guesses.
- **`render_bank_command`**: validates every component (`numeric_code` 4 digits, phone 11 digits starting `0`, cedula non-empty, amount `Decimal > 0`), renders through the template, then asserts `" ".join(out.split()) == out` (no leading/trailing/repeated whitespace). A missing component raises; a partial command is never returned.
- **New typed errors** in `app/core/exceptions.py`: `BankSmsFormatError` (`code="bank_sms_format_error"`, 422) and `BankSmsParseError` (`code="bank_sms_parse_error"`, 422), both `LiquidVEException` subclasses.

Shipped registry (R-BANK-002) and exact rendered output for `phone="04141234567"`, `cedula="12345678"`, `amount=Decimal("1500")`:

| Bank | `numeric_code` | `short_codes` | Rendered command |
|---|---|---|---|
| Banco de Venezuela (`BANCO_DE_VENEZUELA`) | `0102` | `("2661", "2662")` | `Pagar 0102 04141234567 12345678 1500,00` |
| Banesco (`BANESCO`) | `0134` | `("2846",)` | `Pagar 0134 04141234567 12345678 1500,00` |

Both use the same template with `cedula_format=None`. `format_ves_amount(Decimal("1500.5"))` → `1500,50`; `Decimal("25000.00")` → `25000,00`.

The gateway destination is the **short code**, not the counterparty phone: `sms.send_sms(phone_number=short_code, ...)`. `SmsGatewayClient.build_payment_sms` (`sms_gateway.py:206-235`) is deleted in S4 together with its last caller.

## 6. Bank-Reply Correlation (R-BUY-007/008)

```python
async def confirm_bank_payment(self, *, message_id: str, sender: str, body: str,
                               received_at: datetime) -> SmsConfirmation: ...
```

Pseudocode of the single entry point shared by the webhook and the poller:

```
1  anchor   = upsert_webhook_event("sms_gateway", message_id)      # B0' , committed
   if anchor.processed: return SmsConfirmation(duplicate=True, outcome="no_match")
2  profile  = profile_for_sender(sender)          # exact match against every profile's short_codes
   if profile is None:            -> outcome "no_match", reason "unknown_sender", WEBHOOK_REJECTED
3  parsed   = parse_bank_reply(body, profile)     # SUCCESS | FAILURE | UNPARSEABLE | AMBIGUOUS
4  candidates = select(P2POrder)
       .where(order_type == BUY,
              status == SENDING_SMS,
              bank_code == profile.bank,
              amount_fiat == parsed_amount,              # exact Decimal equality
              bank_reference.is_(None),
              bank_confirmed_at.is_(None),
              coalesce(sms_sent_at, updated_at).between(received_at - window, received_at))
       .limit(2)                                     # 2 is enough to prove ambiguity
5  len == 0 -> outcome "no_match", reason "no_candidate", no state change
   len == 2 -> outcome "ambiguous", no state change, WEBHOOK_REJECTED(ambiguous), logger.error(alert)
   len == 1 -> exactly-one rule:
        SUCCESS      -> B6 (BankTransaction OUTGOING + order.bank_reference + bank_confirmed_at)
                        then notify_paid -> B7 COMPLETED      => outcome "confirmed"
        FAILURE      -> MANUAL_REVIEW reason "bank_rejected_payment"; reservation retained => "matched"
        UNPARSEABLE  -> MANUAL_REVIEW reason "bank_reply_unparseable"                => "matched"
        AMBIGUOUS    -> MANUAL_REVIEW reason "bank_reply_ambiguous"                  => "matched"
```

- **Amount source**: `SUCCESS` replies must also expose the paid amount to be comparable. Where the shipped patterns cannot capture it (`BDV`/`Banesco` provisional wording), the matcher uses the candidate's own `amount_fiat` as the comparison baseline and the window+bank+exactly-one constraints carry the discrimination. `parse_ves_amount` is used when the reply *does* carry an amount, and a mismatch yields `no_match` with reason `amount_mismatch`. This keeps the query independent of parser richness while never accepting a reply for a different amount when the amount is visible.
- **`bank_transactions` and `webhook_events` participation**: the reply body is persisted on `webhook_events.payload` (reconciliation source, PII masked in logs only); the confirmed movement is one `BankTransaction(order_id, direction=OUTGOING, bank_code=profile.bank, amount=order.amount_fiat, currency="VES", bank_reference=<reply ref>, verified=True, verification_status=None, raw_payload={"source": "sms_gateway", "message_id": ..., "sender_masked": ...})`. `verification_status` stays `NULL` on purpose: `PaymentVerificationStatus` is Cachicamo vocabulary (`NOT_FOUND`/`AMOUNT_MISMATCH`) and the bank's own reply is the verification for this channel. Failure replies create **no** `bank_transaction` — `bank_reference` is `NOT NULL` there and there is no reference to record; the evidence lives in `audit_logs` + `webhook_events`.
- **UNIQUE `bank_reference` collision** (`models.py:127`, `:169`): the write in B6 sets both `p2p_orders.bank_reference` and the `bank_transactions` row. The surrounding `try` catches `IntegrityError` → `await session.rollback()` (only B6's transaction; B0′ is already committed) → re-load the order → transition `SENDING_SMS → MANUAL_REVIEW` with `manual_review_reason="duplicate_bank_reference"` + `STATUS_CHANGED` audit → webhook returns `outcome="matched", reason="duplicate_bank_reference"`, HTTP 202. Never an HTTP 500, never a second transaction.
- **Replay**: an already-`processed` `message_id` short-circuits at step 1 with HTTP 202 `duplicate=True` and writes nothing.

## 7. Settings Rollout

| Env var | Field | Type | Default | Slice |
|---|---|---|---|---|
| `BUY_AUTO_PAY_ENABLED` | `buy_auto_pay_enabled` | `bool` | `False` | S2 |
| `MAX_BUY_AMOUNT_VES` | `max_buy_amount_ves` | `Decimal` | **required** | S2 |
| `DAILY_SMS_LIMIT_VES` | `daily_sms_limit_ves` | `Decimal` | **required** | S2 |
| `RATE_TOLERANCE_PCT` | `rate_tolerance_pct` | `Decimal` | `Decimal("1.0")` | S2 |
| `SMS_CONFIRMATION_TIMEOUT_SECONDS` | `sms_confirmation_timeout_seconds` | `int` | `300` | S2 |
| `BINANCE_BROWSER_ENABLED` | `binance_browser_enabled` | `bool` | `False` | S3 |
| `BINANCE_BROWSER_HEADLESS` | `binance_browser_headless` | `bool` | `True` | S3 |
| `BINANCE_BROWSER_TIMEOUT_MS` | `binance_browser_timeout_ms` | `int` | `20000` | S3 |
| `BINANCE_SESSION_STATE_PATH` | `binance_session_state_path` | `str` | `"./data/binance_state.json"` | S3 |
| `BINANCE_P2P_ORDER_URL_TEMPLATE` | `binance_p2p_order_url_template` | `str` | `"https://p2p.binance.com/en/order/{order_number}"` | S3 |
| `BANK_SHORT_CODES_BDV` | `bank_short_codes_bdv` | `list[str]` (`NoDecode`) | `[]` (⇒ registry `("2661","2662")`) | S2 |
| `BANK_SHORT_CODES_BANESCO` | `bank_short_codes_banesco` | `list[str]` (`NoDecode`) | `[]` (⇒ registry `("2846",)`) | S2 |
| `BUY_WORKER_ENABLED` | `buy_worker_enabled` | `bool` | `False` | S7 |
| `BUY_WORKER_POLL_INTERVAL_SECONDS` | `buy_worker_poll_interval_seconds` | `int` | `30` | S7 |

The two money caps are **required** (a silently defaulted spend ceiling is worse than a failed boot); everything else defaults so an existing `.env` keeps working. `rate_tolerance_pct` gets its own validator (`0 < v ≤ 100`, quantized to `0.01`); the two caps are added to `_validate_money_amounts` (`config.py:136-165`) and to `_validate_consistency` (`config.py:167-193`) as `daily_sms_limit_ves >= max_buy_amount_ves`.

**Exact co-change set** — `Settings()` is constructed at *import* time (`config.py:284-290`), so these land together in one commit or every test collection fails:

1. `app/core/config.py`: new fields + add the two Decimals to the `_validate_money_amounts` field list + `rate_tolerance_pct` validator + the new consistency rule + `bank_short_codes_*` added to the `_parse_list_env` field list + extend `base_settings_summary()` with the four non-secret buy scalars.
2. `.env.example`: every row above, `REQUIRED` comment on the two caps, `false` defaults for the three switches.
3. `tests/conftest.py:16-31`: `os.environ.setdefault("MAX_BUY_AMOUNT_VES", "5000.00")` and `os.environ.setdefault("DAILY_SMS_LIMIT_VES", "20000.00")` (only the required ones need env entries).
4. `tests/conftest.py::test_settings` (`:49-77`): `max_buy_amount_ves=Decimal("5000.00")`, `daily_sms_limit_ves=Decimal("20000.00")`.
5. `tests/test_config.py` (new): defaults (`buy_auto_pay_enabled is False`, `rate_tolerance_pct == Decimal("1.0")`), the consistency rejection, float/zero rejection on the caps, and a test that `Settings()` builds from the conftest environment alone (`get_settings.cache_clear()` first).

Safe in-slice ordering (strict TDD): (3)+(4) first — unknown init kwargs are dropped because `extra="ignore"` — then (5) as RED, then (1)+(2) as GREEN. Note `require_internal_api_key`/`require_bearer_token` read the cached `get_settings()` (`security.py:246,263`), not `app.state.settings`, so the conftest env block and `test_settings` must keep agreeing on `internal_api_key` or every authenticated endpoint test flips to 401.

## 8. Schema Delta

Five nullable columns on `p2p_orders` — nothing else changes:

| Column | Type | Purpose |
|---|---|---|
| `counterparty_id_number` | `String(32)`, nullable | Normalized cedula (`V-12345678`); masked in logs/audits via `mask_id_number` (`security.py:183-198`). |
| `sms_message_id` | `String(128)`, nullable | Gateway message id for reconciliation. |
| `sms_sent_at` | `DateTime(timezone=True)`, nullable | Correlation window anchor. |
| `bank_short_code` | `String(16)`, nullable | Short code actually used (primary or fallback). |
| `bank_confirmed_at` | `DateTime(timezone=True)`, nullable | Marks "bank reply confirmed"; the `notify_paid` gate and the "confirmed, close pending" query. |

Revision shape:

```python
# alembic/versions/20261123_0002_buy_payment_telemetry.py
revision = "20261123_0002"
down_revision = "20260101_0001"

def upgrade() -> None:
    for name, type_ in (("counterparty_id_number", sa.String(32)),
                        ("sms_message_id", sa.String(128)),
                        ("sms_sent_at", sa.DateTime(timezone=True)),
                        ("bank_short_code", sa.String(16)),
                        ("bank_confirmed_at", sa.DateTime(timezone=True))):
        op.add_column("p2p_orders", sa.Column(name, type_, nullable=True))

def downgrade() -> None:
    with op.batch_alter_table("p2p_orders") as batch:      # SQLite-safe table rebuild
        for name in reversed(COLUMN_NAMES):
            batch.drop_column(name)
```

No new index: the candidate query rides on the existing `ix_p2p_orders_status` and a working set of open `SENDING_SMS` rows, so adding one would buy nothing and widen the migration's blast radius.

**Why `downgrade -1` is safe**: all five columns are nullable with no default, no `NOT NULL`, no FK, no CHECK and no unique constraint, so dropping them cannot violate a row; no query, backfill or data migration depends on them (the same PR's code revert stops reading them, and `sms_message_id`/status remain recoverable from `audit_logs.payload` which already carries `provider_message_id`); batch mode preserves every existing row and every other column on SQLite; and the idempotency anchors (`order_number`, `bank_reference`) are untouched. Verification: `tests/test_migrations.py` runs `command.upgrade(cfg, "head")` then `command.downgrade(cfg, "-1")` against a temp `sqlite+aiosqlite` file after `monkeypatch.setenv("DATABASE_URL", ...)` + `get_settings.cache_clear()`, asserting the columns disappear and the `p2p_orders` row count is unchanged.

## 9. API Surface

**`POST /api/v1/orders/buy/{order_number}/pay`** (new router `app/api/v1/orders.py`, mounted in `create_app` next to `webhooks`, S7):

- **Auth**: `dependencies=[Depends(require_internal_api_key)]` → `X-API-Key` compared in constant time against `internal_api_key`. No bearer alternative (a human/ops action, not a device callback).
- **Request**: `BuyPayRequest(destination_phone: str | None = None, reason: str | None = Field(None, max_length=280))`; body may be omitted entirely. `destination_phone` is normalized with `validate_venezuelan_phone`; it is never echoed back.
- **Behaviour**: load the order (`NotFoundError` 404) → refuse when `order_type is not BUY` (`InvalidStateTransitionError` 409) → dispatch by state: `MANUAL_REVIEW` + `awaiting_operator_approval` → move `MANUAL_REVIEW → PENDING` and run the dispatch pipeline (B2 → gateway → B4/B5) acting as `AuditActor.OPERATOR`; `PENDING`/`PAYMENT_DETECTED`/`VERIFYING_CACHICAMO` → run the pipeline; `SENDING_SMS` → **no-op** returning current state (R-BUY-002/005); `COMPLETED`/`FAILED` → 409 `terminal_status`.
- **Response**: `BuyPayResponse(order_number, status, outcome: Literal["dispatched","resumed","awaiting_approval","noop","manual_review"], manual_review_reason: str | None, bank_short_code: str | None, sms_message_id: str | None)`. No amount, no phone, no cedula, no command text. `202` when the call changed state, `200` for a no-op. Errors use the existing envelope (`LiquidVEException.to_dict`): 401 unauthorized, 404 not found, 409 invalid transition, 422 validation, 503 retryable gateway.

**Enriched SMS webhook contract** (`POST /api/v1/webhooks/sms`, always HTTP 202):

```python
class SmsConfirmation(BaseModel):          # app/domain/schemas.py
    outcome: Literal["matched", "confirmed", "ambiguous", "no_match"]
    order_number: str | None = None
    reason: str | None = None

class SmsWebhookResponse(WebhookAcceptedResponse):   # additive, backward compatible
    outcome: Literal["matched", "confirmed", "ambiguous", "no_match"] = "no_match"
    order_number: str | None = None
    reason: str | None = None
```

| Situation | `outcome` | `duplicate` | `reason` examples | Order state |
|---|---|---|---|---|
| Reply confirmed, notify ok | `confirmed` | `False` | `None` | `COMPLETED` |
| Confirmed, notify failed | `matched` | `False` | `binance_close_failed` | `MANUAL_REVIEW` |
| Bank failure reply | `matched` | `False` | `bank_rejected_payment` | `MANUAL_REVIEW` |
| Unparseable / both patterns | `matched` | `False` | `bank_reply_unparseable`, `bank_reply_ambiguous` | `MANUAL_REVIEW` |
| Duplicate reference | `matched` | `False` | `duplicate_bank_reference` | `MANUAL_REVIEW` |
| ≥2 candidates | `ambiguous` | `False` | `candidate_count` detail in audit | unchanged |
| 0 candidates / foreign sender | `no_match` | `False` | `no_candidate`, `unknown_sender`, `amount_mismatch` | unchanged |
| Already processed `message_id` | `no_match` | `True` | `already_processed` | unchanged |

`handle_incoming_sms(sms: IncomingSms)` becomes a thin adapter over `confirm_bank_payment` so the poller path (`main.py:53-67`) gains the same `webhook_events` dedupe and the same outcome vocabulary.

## 10. Security Direction Guard

`_dispatch_cachicamo` (`webhooks.py:271-281`) becomes:

```python
    result = await session.execute(
        select(P2POrder)
        .where(
            P2POrder.order_type == OrderType.SELL,        # <- added: deny by default
            P2POrder.status.in_([OrderStatus.PENDING, OrderStatus.PAYMENT_DETECTED]),
            P2POrder.amount_fiat == payload.amount_ves,
        )
        .order_by(P2POrder.created_at.asc())
        .limit(1)
    )
```

Defense in depth, same slice (S2): after the load, `if order.order_type is not OrderType.SELL: audit WEBHOOK_REJECTED(reason="direction_mismatch"); return "Payment stored; no matching order is waiting for it"`, and `SellFlow.handle_incoming_payment` raises `InvalidStateTransitionError` for a non-SELL order **before** inserting any `BankTransaction`. `SellFlow.execute` likewise refuses `detail.trade_type is not OrderType.SELL` and `BuyFlow.execute` refuses `is not OrderType.BUY`. `enums.py:9-17`'s inverted docstring is corrected (`BUY` = we pay VES and receive crypto).

**Proving test** (`tests/test_direction_guard.py`, S2, RED first):

1. `test_equal_amounts_do_not_cross_directions`: seed one BUY and one SELL in `PENDING` with identical `amount_fiat`, install `FakeBinance` on `app.state`, POST a correctly signed Cachicamo webhook through `app_client` → assert the SELL order advanced, the BUY order is still `PENDING` with no `PAYMENT_VERIFIED` audit, `fake_binance.release_calls == [sell_order_number]`, and `buy_order_number not in fake_binance.release_calls`.
2. `test_only_buy_candidate_yields_no_match`: BUY-only database → HTTP 202 with the no-match message, order unchanged, `fake_binance.release_calls == []`.

`release_order` is a coroutine, so "never awaited" is asserted structurally (empty call list) rather than by mocking `asyncio` internals.

## 11. Test Seams

| Fixture / double | Definition (S5, with S2 additions) | Solves |
|---|---|---|
| `app_client` | `create_app(test_settings)` **without** the lifespan, `app.state.{binance,cachicamo,sms,browser}` set to doubles, `application.dependency_overrides[get_session] = _override_session(engine)`, `httpx.ASGITransport`. | The `client` fixture (`conftest.py:112-130`) calls `init_engine(test_settings)` (a *file* DB in `tmp_path`) but never overrides `get_session`, while `session`/`engine` (`:80-109`) are an independent in-memory `StaticPool` DB — the two-engine problem. `app_client` binds both the request and the assertions to the same in-memory engine and never touches the module-global engine, so it cannot cross-talk with `client`. |
| `_override_session(engine)` | `async_sessionmaker(engine, expire_on_commit=False, autoflush=False)` + commit-on-success/rollback-on-error, mirroring `get_session`. | Reproduces production transaction semantics inside the handler. |
| `fake_binance` | Records `get_order_detail`/`get_order_history`/`mark_order_as_paid`/`release_order` calls; returns `BinanceC2COrder`. | Direction guard, worker tests. |
| `fake_browser` | Conforms to `BinanceBrowser`; scriptable `instruction` / `extract_error` / `notify_error`; records ordered `("extract", n)` / `("notify", n, ref)` events. | S4/S6 without Playwright; asserts call **order** and arguments. |
| `fake_browserless` (S4) | `None` browser → extraction must degrade to `MANUAL_REVIEW/payment_data_unavailable`. | A12. |
| `fake_sms` | `send_sms` returns `SmsSendResult(message_id=..., status="SENT", sent_at=...)`; records `(phone_number, message, idempotency_key)`; `errors=[...]` scripts primary-fail/secondary-ok and permanent-fail. | S4 fallback, same-key, no-double-send. |
| `seed_order(**kwargs)` | Persists a `P2POrder` in any status with deterministic `amount_fiat`/`bank_code`/`sms_sent_at`. | Matcher tests (0/1/many, wrong sender, expired window). |
| `respx` (SMS) | `respx.post("http://100.64.0.10:8080/messages")`; assert `Idempotency-Key == f"{n}:payout"` and `Authorization == "Bearer test-sms-gateway-token"`; 500 → `RetryableExternalServiceError` with `len(route.calls) == 1`; malformed 200 → `ExternalServiceResponseError`. | Proves the real adapter's no-retry contract. |
| `respx` (Binance, S7) | `respx.post(url__regex=r"...listUserOrderHistory\?.*")` (the adapter sends a **pre-signed query string**, so `params__contains` cannot match); assert `request.url.params["tradeType"] == "BUY"`, 200 envelope → orders, 401 → `UnauthorizedError`, 500 on the idempotent read → retried. | Worker without network. |
| `freeze_time` | `freeze_time("2026-11-23 23:59:30", tz_offset=0)` for the `VES`/`VES_SMS` UTC rollover; `T + sms_confirmation_timeout_seconds + 1s` for the expired window. | R-BUY-004 scenario 3, R-BUY-010. |
| HTML / SMS fixtures | `tests/fixtures/binance/order_view_{bdv,banesco}.html`, `tests/fixtures/sms/bank_reply_{success,failure,ambiguous}_{bdv,banesco}.txt`. | Parser tests with zero I/O. |

Per-slice isolation (hard rule — every suite passes with the network stack down):

- **S1** pure string tests, no fixtures, no DB. **S2** in-memory SQLite + freezegun + `fake_binance` (no respx). **S3** parser tests from fixtures + one conformance test with `fake_browser`; the adapter test is `integration` **and** `skipif(not LIQUIDVE_BROWSER_TESTS)` because the wheel is installed. **S4** fakes + respx for `SmsGatewayClient` only. **S5** `app_client` over `ASGITransport` (no sockets). **S6** fake port. **S7** respx history + a manual worker tick (`await _buy_worker_tick(app)` once, never `asyncio.sleep`) + endpoint auth tests.

## 12. Sequence Diagrams

**(a) Buy happy path across slices**

```mermaid
sequenceDiagram
    autonumber
    participant W as Buy worker (S7)
    participant BF as BuyFlow (S4)
    participant BR as BinanceBrowser (S3)
    participant RP as RiskPolicy (S2)
    participant DB as DB
    participant SMS as SmsGateway (S1/S4)
    participant BK as Bank
    participant WH as SMS webhook (S5)
    participant BN as Binance UI (S6)

    W->>BF: execute(order_number) / or operator POST .../pay
    BF->>BF: B1 commit PENDING + ORDER_CREATED
    BF->>BR: extract_payment_instruction(order_number)
    BR-->>BF: PaymentInstruction(bank 0102, phone, cedula, Decimal)
    BF->>RP: per-order cap -> rate tolerance -> bank support -> VES_SMS cap
    BF->>DB: B2 commit SENDING_SMS + VES_SMS reservation + idempotency_key
    Note over BF,SMS: durable before the money command (A1)
    BF->>SMS: send_sms("Pagar 0102 04141234567 12345678 1500,00", Idempotency-Key=ORD:payout)
    SMS-->>BF: message_id
    BF->>DB: B4 commit SMS_SENT + sms_message_id + sms_sent_at
    SMS->>BK: Pago Movil command
    BK-->>WH: bank reply SMS
    WH->>DB: B0' commit webhook_event (replay anchor)
    WH->>DB: B6 commit BankTransaction(OUTGOING) + bank_reference + bank_confirmed_at
    WH->>BN: notify_paid(order_number, reference)
    BN-->>WH: ok
    WH->>DB: B7 commit COMPLETED
    WH-->>BK: 202 outcome=confirmed
```

**(b) Buy timeout / unknown fate, and the classified fallback**

```mermaid
sequenceDiagram
    autonumber
    participant BF as BuyFlow
    participant SMS as SmsGateway
    participant DB as DB
    participant SW as Sweep (S7)
    participant OP as Operator

    BF->>DB: B2 commit SENDING_SMS + reservation
    alt classified not-delivered (4xx rejected)
        BF->>SMS: send_sms(primary, key=ORD:payout)
        SMS--xBF: ExternalServiceResponseError
        BF->>SMS: send_sms(secondary, SAME key)
        SMS-->>BF: message_id
        BF->>DB: B4 commit SMS_SENT + bank_short_code=secondary
    else unknown fate (5xx, timeout, unparseable body, 409)
        BF->>SMS: send_sms(primary)
        SMS--xBF: RetryableExternalServiceError
        Note over BF,SMS: exactly one request, never retried
        BF->>DB: B3 commit EXTERNAL_ERROR only
    else permanent (401/403, second not-delivered)
        BF->>DB: B5 commit release VES_SMS + MANUAL_REVIEW
    end
    SW->>DB: read SENDING_SMS where coalesce(sms_sent_at, updated_at) + timeout < now
    SW->>DB: commit EXTERNAL_ERROR flag reason=sms_confirmation_timeout
    Note over DB: status stays SENDING_SMS, reservation retained
    OP->>DB: SENDING_SMS -> MANUAL_REVIEW -> PENDING (manual, same idempotency key on re-dispatch)
```

**(c) Bank SMS webhook confirmation path**

```mermaid
sequenceDiagram
    autonumber
    participant GW as Android gateway
    participant WH as POST /webhooks/sms
    participant DB as DB
    participant BR as bank_replies
    participant BF as BuyFlow
    participant BN as Binance UI

    GW->>WH: {message_id, sender=2661, body, received_at} (Bearer)
    WH->>DB: B0' insert webhook_event(provider=sms_gateway, message_id)
    alt row already processed
        WH-->>GW: 202 duplicate=true outcome=no_match
    else new or unprocessed row
        WH->>BR: profile_for_sender("2661") -> BDV
        WH->>BR: parse_bank_reply(body, BDV)
        BR-->>WH: SUCCESS / FAILURE / UNPARSEABLE / AMBIGUOUS
        WH->>BF: confirm_bank_payment(...)
        BF->>DB: candidates = SENDING_SMS + BUY + bank + amount + window (limit 2)
        alt 0 candidates
            BF-->>WH: no_match (no state change)
        else 2+ candidates
            BF-->>WH: ambiguous + operator alert (no state change)
        else exactly 1 candidate
            BF->>DB: B6 commit BankTransaction(OUTGOING) + reference + bank_confirmed_at
            BF->>BN: notify_paid(order_number, reference)
            alt notify ok
                BF->>DB: B7 commit COMPLETED
                BF-->>WH: confirmed
            else notify failed / unknown
                BF->>DB: commit MANUAL_REVIEW reason=binance_close_failed
                BF-->>WH: matched
            end
        end
        WH->>DB: mark webhook_event processed (+ processing_error)
        WH-->>GW: 202 outcome=<matched|confirmed|ambiguous|no_match>
    end
```

**(d) Existing SELL path — where the direction guard sits**

```mermaid
sequenceDiagram
    autonumber
    participant CH as Cachicamo
    participant WH as POST /webhooks/cachicamo
    participant DB as DB
    participant SF as SellFlow
    participant BN as Binance C2C

    CH->>WH: signed payment event
    WH->>DB: insert webhook_event (replay anchor)
    WH->>DB: SELECT ... WHERE order_type = SELL  <-- DIRECTION GUARD (S2)
    Note over WH,DB: BUY rows are invisible to this query by construction
    alt no SELL candidate (e.g. BUY-only DB)
        WH-->>CH: 202 "no matching order is waiting for it"
    else one SELL order
        WH->>SF: handle_incoming_payment(...)
        SF->>SF: assert order_type is SELL  <-- defence in depth
        SF->>DB: commit BankTransaction(INCOMING) + bank_reference (UNIQUE anchor)
        SF->>BN: verify (idempotent)
        SF->>BN: release_order(order_number)  <-- impossible for a BUY order
        SF->>DB: commit COMPLETED
        WH-->>CH: 202 "Payment processed"
    end
```

## 13. File Changes

| File | Action | Slice | Description |
|---|---|---|---|
| `app/domain/banks.py` | Create | S1 | `BankSmsProfile`, `BANK_SMS_PROFILES`, `SHORT_CODE_INDEX`, `resolve_bank`, `profile_for_sender`, `format_ves_amount`, `parse_ves_amount`, `national_digits`, `cedula_digits`, `render_bank_command`, `apply_short_code_overrides`. |
| `app/core/exceptions.py` | Modify | S1 | `BankSmsFormatError`, `BankSmsParseError`. |
| `app/services/risk_policy.py` | Create | S2 | `RiskPolicy`, `LEDGER_VES`/`LEDGER_VES_SMS`, parameterized ledger, reserve/release. |
| `app/services/buy_flow.py` | Modify | S2, S4, S5, S6 | Delete duplicated ledger/risk; new B1–B7 boundaries; extraction via port; command dispatch; `resume`; `confirm_bank_payment`; close. |
| `app/services/sell_flow.py` | Modify | S2 | Delegate to `RiskPolicy`; SELL-only assertions. |
| `app/api/v1/webhooks.py` | Modify | S2, S5 | Direction guard; `processed`-aware replay; `get_buy_flow(request, session)` dependency seam; enriched response. |
| `app/domain/enums.py` | Modify | S2 | Correct the `OrderType` docstring only. |
| `app/core/config.py` | Modify | S2, S3, S7 | New settings + validators + summary keys. |
| `.env.example` | Modify | S2, S3, S7 | New variables with safe defaults. |
| `tests/conftest.py` | Modify | S2, S5 | Required-cap env defaults + `test_settings`; `app_client`, `_override_session`, doubles, `seed_order`. |
| `app/domain/ports.py` | Create | S3 | `PaymentInstruction`, `BinanceBrowser`. |
| `app/infrastructure/clients/binance_browser.py` | Create | S3, S6 | Pure HTML parser (part 1) + lazy `PlaywrightBinanceBrowser` (part 2), `notify_paid` in S6. |
| `app/infrastructure/models.py` | Modify | S4 | Five nullable columns. |
| `alembic/versions/20261123_0002_buy_payment_telemetry.py` | Create | S4 | Additive revision (batch downgrade). |
| `app/services/sms_dispatch.py` | Create | S4 | `classify_gateway_exception`, `dispatch_bank_command`, `DispatchResult`. |
| `app/infrastructure/clients/sms_gateway.py` | Modify | S4 | Delete `build_payment_sms`; `send_sms` unchanged. |
| `app/domain/bank_replies.py` | Create | S5 | `BankReplyOutcome`, `BankReplyParse`, `parse_bank_reply`. |
| `app/domain/schemas.py` | Modify | S5, S7 | `SmsConfirmation`, `SmsWebhookResponse`, `BuyPayRequest`, `BuyPayResponse`. |
| `app/infrastructure/clients/binance_client.py` | Modify | S7 | Optional `trade_type`/`order_status` kwargs on `get_order_history`. |
| `app/api/v1/orders.py` | Create | S7 | `POST /orders/buy/{order_number}/pay`. |
| `app/main.py` | Modify | S7 | Browser lifecycle (A12), `_run_buy_worker`, cancellation-safe shutdown, router registration. |
| `tests/test_banks.py`, `test_config.py`, `test_direction_guard.py`, `test_browser_parser.py`, `test_buy_flow.py`, `test_migrations.py`, `test_bank_replies.py`, `test_buy_webhook.py`, `test_buy_close.py`, `test_buy_worker.py`, `test_orders_api.py` | Create | per slice | See §11. |
| `tests/fixtures/binance/*.html`, `tests/fixtures/sms/*.txt` | Create | S3, S5 | Offline fixtures. |

## 14. Testing Strategy

| Layer | What | Approach |
|---|---|---|
| Unit (pure) | `format_ves_amount`, `parse_ves_amount`, `national_digits`, `cedula_digits`, `render_bank_command`, `resolve_bank` (code/alias/ambiguous/unsupported), `parse_bank_reply` (4 outcomes), `parse_payment_instructions`, `classify_gateway_exception` | Exact-string and table-driven tests, no fixtures beyond strings. |
| Unit (DB) | Ledger reserve/release/rollover, exactly-one matcher (0/1/2, wrong sender, expired window), state machine transitions, `resume` no-op | In-memory SQLite (`engine`/`session`), freezegun, `seed_order`. |
| Contract | Real `SmsGatewayClient` and `BinanceC2CClient` against respx: path, bearer, `Idempotency-Key`, call counts, typed errors | respx; assert `len(route.calls)` to prove the no-retry rule. |
| Integration (ASGI) | SMS webhook outcomes, replay `duplicate=True`, duplicate-reference → `MANUAL_REVIEW`, direction guard, operator endpoint auth | `app_client` + `_override_session` + doubles on `app.state`. |
| Integration (migration) | `upgrade head` / `downgrade -1` on a temp SQLite file | Alembic `command` API with a patched `DATABASE_URL`. |
| Browser (opt-in) | `PlaywrightBinanceBrowser` smoke | `integration` + `skipif(not LIQUIDVE_BROWSER_TESTS)`; never in the default run. |

## 15. Slice Budget Check

| Slice | Forecast (authored lines) | Verdict |
|---|---|---|
| S1 | 180–260 | Fits. |
| S2 | 320–430 (risk policy + 2 services + webhook guard + config/env/conftest + 3 test files) | Fits under 800; **at risk of the 480 soft ceiling** — see split S2a/S2b below. |
| S3 | 380–490 (port + parser + adapter + fixtures) | **Likely over** — split. |
| S4 | 350–470 (dispatch + resume + classification + migration + 2 suites) | Fits; split if the classification suite grows. |
| S5 | 300–400 (parser + matcher + webhook contract + `app_client`) | Fits. |
| S6 | 160–240 | Fits. |
| S7 | 180–280 (worker + endpoint + lifecycle + settings) | Fits. |

Explicit splits (apply them if `sdd-tasks` forecasts > ~480 authored lines for the slice; both fit `auto-chain`'s 800-line review budget):

- **S3 → S3a** (`app/domain/ports.py` + pure parser + `tests/test_browser_parser.py` + HTML fixtures, no Playwright anywhere) and **S3b** (`PlaywrightBinanceBrowser`, its three settings, the gated smoke test). S3b depends on S3a; S4 depends on S3a only.
- **S2 → S2a** (`risk_policy.py` + both flows delegate + characterization test; SELL behaviour pinned) and **S2b** (buy caps/rate/settings co-change + direction guard + `test_direction_guard.py` + `test_config.py`). S2b is the independently cherry-pickable security fix the proposal requires.
- **S4 → S4a** (models + Alembic + `test_migrations.py`) and **S4b** (dispatch/resume/classification + `sms_dispatch.py`). S4a alone is inert (nullable columns nobody writes).

## Threat Matrix

N/A — no routing, shell, subprocess, VCS/PR automation, executable-file classification or process-integration boundary. The new surface is a FastAPI route behind `require_internal_api_key` and a browser adapter that navigates only URLs derived from settings (`binance_p2p_order_url_template`); no `subprocess`, no shell, no user-controlled command composition. The one adjacent risk (a headless Chromium launch) is contained by A12 (`binance_browser_enabled=false` default), the lazy import, and the opt-in-only adapter test.

## Migration / Rollout

1. S2 lands the security guard first if the slices are reordered — it is a live defect independent of Flow B.
2. Every new setting ships with `.env.example` + `conftest` + `test_config` in the same commit (§7); `Settings()` is built at import, so there is no safe intermediate state for required fields.
3. Schema: `alembic upgrade head` in staging before enabling `BUY_AUTO_PAY_ENABLED`; `create_all()` (dev only, `main.py:88-89`) already covers the columns for local runs, so the migration test is the guard against a missing revision.
4. Enablement order: `BUY_AUTO_PAY_ENABLED=false` + `BUY_WORKER_ENABLED=false` → deploy → flip `BUY_WORKER_ENABLED=true` (detection only, orders park in `MANUAL_REVIEW`) → run operator approvals end-to-end with a small cap → flip `BUY_AUTO_PAY_ENABLED=true` → enable `BINANCE_BROWSER_ENABLED=true` once the session state file is provisioned.
5. Rollback: `BUY_WORKER_ENABLED=false`, `BUY_AUTO_PAY_ENABLED=false`, `BINANCE_BROWSER_ENABLED=false`, remove the `orders` router registration (`main.py:217-218`). In-flight `SENDING_SMS`/`MANUAL_REVIEW` rows are preserved for manual handling; nothing is deleted.
6. `alembic downgrade -1` is safe (§8) and is only needed if the code revert also lands.

## Open Questions

- [ ] **Real BDV/Banesco reply wording** (D2). Shipped patterns are provisional; every unparsed reply with exactly one candidate becomes `bank_reply_unparseable` → `MANUAL_REVIEW`. This is safe but turns wording drift into manual work.
- [ ] **No operator path out of `SENDING_SMS`.** R-BUY-010 permits a review transition carrying `sms_confirmation_timeout`, but the sweep only flags (per the proposal) and the pay endpoint refuses `SENDING_SMS` (per R-BUY-002). A `POST /orders/{order_number}/review` action is out of scope here; until it exists, operators resolve these rows with direct SQL/a runbook step.
- [ ] **Reply-carried amount.** Shipped patterns only capture `Referencia:`; the amount match therefore uses the candidate's own baseline. If real replies expose the paid amount, add a `(?P<amount>…)` group and compare with `parse_ves_amount`, tightening the matcher without a schema change.
- [ ] **`VES_SMS` reporting.** The pseudo-currency must be documented for whoever builds daily reconciliation; a `channel` column (exploration option B) is the documented follow-up.
- [ ] **`binance_p2p_order_url_template` accuracy.** The order-detail URL path is unverified against the live UI (same class of `TODO(integration)` as `binance_client.py:36-41`); it is a setting precisely so it is fixable without a release.

# Design: BUY payment rail via Banesco Online + Telegram Clave Dinámica

- **Change**: `buy-usdt-banesco-payment-rail`
- **Phase**: design
- **Store**: `openspec` (filesystem mirror at `sdd/buy-usdt-banesco-payment-rail/design`)
- **Delivery**: `auto-chain` stacked-to-main, **800 changed lines per slice**, strict TDD
- **Runner**: `.\.venv\Scripts\python.exe -m pytest -q`; lint `.\.venv\Scripts\python.exe -m ruff check app tests`
- **Baseline**: 263 passed, 2 skipped; ruff clean
- **Supersedes**: archived SMS rail (read-only; never modified)
- **Security**: credentials and security answers are referenced **only by environment-variable name**. No value, question text, fixture, example, log line, audit payload, or Engram observation in this document contains a credential. The real values exist only in the operator's gitignored `.env`.

> **Document size note**: this design is intentionally long because the orchestrator requires 16 concrete answers. The 800-line budget applies to each implementation PR slice, not to this design document.

---

## Piloto supervisado del 27 de noviembre de 2026

- El endpoint del operador inicia una sola compra. El worker solo registra órdenes.
- Telegram envía el aviso a un chat privado. La clave se ingresa en un formulario
  HTTPS de liquidVE con un enlace de un solo uso. El plazo máximo es de 30
  segundos desde la primera apertura del formulario HTTPS. El enlace tiene un plazo
  independiente de cinco minutos por defecto para abrirlo. Banesco controla la
  vigencia del código y de su sesión. Esto actualiza el diseño de la sección 8.
- La evidencia de Banesco deja la orden en `PAYMENT_CONFIRMED`. El paso a
  `AWAITING_RELEASE` se reserva para la futura notificación con imagen en Binance.
  El formato del comprobante sigue pendiente de validación en una operación real
  supervisada.
- Se retiró la pasarela Android y su procesamiento de mensajes. Las migraciones
  y los registros de auditoría históricos se conservan para bases existentes.

## 1. Technical Approach

Mirror the proven `BinanceBrowser` split — a pure domain layer, thin page objects over a `PageLike` protocol, and a driver that lazily imports Playwright — so the default test run never imports Playwright, launches Chromium, opens a network socket, or needs a credential.

The BUY tail changes from "render an SMS command and wait for an inbound bank SMS" to:

```
extract instruction → build two-control target → reserve VES + VES_PAY
→ commit SENDING_PAYMENT + payment_attempt_id
→ Banesco Online login/questions/modal → Pago Móvil form → double Pagar
→ Clave Dinámica via Telegram → receipt capture + hash
→ upload evidence + notify_paid → AWAITING_RELEASE
→ release watcher → COMPLETED | APPEALED | MANUAL_REVIEW
```

Every external money action is **at-most-once and never retried**. Every unknown outcome fails closed to `MANUAL_REVIEW`. Money is `Decimal` end-to-end. Every third-party boundary lives behind an adapter under `app/infrastructure/clients`.

### Capability mapping

| Capability | Design section | Primary artifacts |
|---|---|---|
| `banesco-payment` | §2–§5, §9, §10 | `app/domain/banesco.py`, `app/infrastructure/clients/banesco_pages.py`, `banesco_browser.py`, `app/infrastructure/receipt_store.py` |
| `telegram-handoff` | §6 | `app/infrastructure/clients/telegram_bot.py`, `app/services/operator_challenges.py`, `operator_challenges` table |
| `release-appeal` | §11–§12 | `app/services/release_watcher.py`, `app/services/appeal.py`, `binance_client.py`, `binance_browser.py` |
| `buy-flow` delta | §7, §8, §10, §11 | `enums.py`, `buy_flow.py`, `risk_policy.py`, `models.py`, migrations |
| `bank-sms` delta | §4, §5 | `banks.py` (pure mapping retained; rendering/reply parsing removed in S9) |

---

## 2. Transaction Boundaries and Idempotency

The design keeps the proposal's B-boundary scheme and adds the two new consequential boundaries (evidence upload and appeal). **A boundary commits before the external action it protects.**

| Boundary | Commit contents (one transaction) | Idempotency mechanism | Failure behavior |
|---|---|---|---|
| **B1 — order** | `P2POrder(status=PENDING)` + `ORDER_CREATED` | `order_number` UNIQUE | Duplicate rejected before provider side effects |
| **B2 — payment intent** | `SENDING_PAYMENT`, `VES` + `VES_PAY` reservation, `payment_attempt_id = "{order_number}:payment"` UNIQUE, `PAYMENT_INITIATED` | Attempt identity UNIQUE; a second invocation is refused; **no browser action before commit** | Crash leaves `SENDING_PAYMENT` that can never auto-reinvoke the rail |
| **B3 — operator challenge** | `operator_challenges(status=PENDING, nonce_hash, expires_at)` **before** Telegram `sendMessage` | One `PENDING` per order (`BANESCO_MAX_CONCURRENT_PAYMENTS=1`); `nonce_hash` UNIQUE | Timeout → `EXPIRED`, order parks; never re-prompts across restart |
| **B4 — receipt** | receipt file + SHA-256 + `OUTGOING` `BankTransaction` + `bank_reference` + `bank_confirmed_at` + `PAYMENT_CAPTURED`/`PAYMENT_VERIFIED` | Digest dedupe (same digest = no-op); different digest = `receipt_conflict`; `bank_reference` UNIQUE | Amount mismatch parks; missing receipt = outcome unknown, never success |
| **B5 — evidence** | `EVIDENCE_UPLOADED` + `SENDING_PAYMENT → AWAITING_RELEASE` + `release_deadline_at` **only after upload + confirm both succeed** | At most once per receipt; an unknown outcome is not re-invoked automatically | `evidence_upload_failed` / `binance_close_failed` parks; no appeal without evidence |
| **B6 — release** | `AWAITING_RELEASE → COMPLETED` + `RELEASE_DETECTED` + `STATUS_CHANGED` | Guarded transition; idempotent no-op if already terminal; row lock / optimistic `version` | Unknown status → `MANUAL_REVIEW` |
| **B7 — appeal** | `appeal_attempt_id = "{order_number}:appeal"` UNIQUE + `APPEALED` + `appeal_filed_at` **before** the submit click | UNIQUE attempt identity; pre-click status re-check; durable `APPEALED` blocks a second click | Unknown outcome stays `APPEALED` with `appeal_status="unknown"`; operator verifies; never auto-retried |

### Which side effects are idempotent

**Idempotent / safe to repeat:** database transitions (guarded by `ALLOWED_TRANSITIONS` and the `version` column), audit appends, receipt digest computation, `WebhookEvent` dedupe, read-only `get_order_detail` polls, ledger release (guarded by a committed status), challenge expiry sweeps.

**Non-idempotent / at-most-once (never retried):** Banesco form submits, the double `Pagar`, Clave submission, Binance evidence upload, `notify_paid`, the appeal click, Telegram `sendMessage` for a challenge. These are protected by a durable intent row committed first, not by provider retries.

**Reservation semantics:** a dispatched BUY reserves `VES` and `VES_PAY` at B2. The reservation is released **only** on a classified permanent **pre-submission** failure (for example `banesco_payment_rejected` before the final `Pagar`, or a typed login/layout failure). It is never released on `clave_dinamica_timeout`, `payment_outcome_unknown`, `receipt_amount_mismatch`, or `release_timeout`.

---

## 3. Page-Object Architecture

### 3.1 Three-layer split

```
app/domain/banesco.py                  pure: constants, normalization, DTO, parsers, option maps
        ▲ no I/O, no Playwright, no settings object
        │
app/infrastructure/clients/banesco_pages.py   page objects over PageLike
        ▲ semantic actions + selector tables + screen-shape assertions
        │
app/infrastructure/clients/banesco_browser.py  driver: lazy `import playwright.async_api`
```

`banesco_pages.py` must be importable without Playwright. It talks to a narrow protocol so tests can inject a recorded-HTML fake.

### 3.2 Page objects (one class per screen)

| Class | Responsibility | Key semantic methods |
|---|---|---|
| `LoginPage` | `Usuario` entry; captcha/lock/maintenance detection | `open()`, `submit_user(user)`, `assert_login_entry()` |
| `SecurityQuestionsPage` | Read displayed question texts; pair each label with its answer input; submit | `read_questions()`, `answer(mapping)`, `submit()` |
| `PasswordPage` | Online-banking `Clave` submission | `submit_clave(clave)` |
| `AdModalPage` | Detect and dismiss advertisement/overlay | `dismiss_if_present()`, `assert_menu_reachable()` |
| `MenuPage` | `Pago Móvil → Enviar pago` navigation | `open_pago_movil()` |
| `PagoMovilFormPage` | Read live option sets; fill phone/id/bank/amount/concept | `read_option_sets()`, `fill(target)`, `submit_first()` |
| `ConfirmationPage` | Observe the confirmation screen; perform the second submit | `assert_confirmation()`, `submit_second()` |
| `ClaveDinamicaPage` | Detect the key form; submit the accepted key exactly once | `assert_key_form()`, `submit_key(key)` |
| `ReceiptPage` | Capture screenshot; parse reference/amount/timestamp | `capture() -> PaymentReceipt` |
| `AppealPage` (Binance, S8) | Re-check, click appeal, choose reason, attach evidence, submit | `file(reason, evidence_path)` |

Common base (signatures only):

```python
class BasePage:
    async def _expect(self, locator: LocatorLike, *, name: str) -> None: ...
    async def _await_screen(self, screen: ScreenKind) -> None: ...
    async def assert_shape(self) -> None: ...
```

Every `_expect` uses an explicit timeout and raises a typed fail-closed error; there is no "try/except and continue".

`PageLike` protocol (what fakes implement): `goto`, `fill`, `click`, `select_option`, `get_by_role`, `get_by_label`, `get_by_text`, `locator`, `content`, `screenshot`, `wait_for_load_state`, `wait_for_selector`, `wait_for_url`, `url`.

### 3.3 Randomized 2-of-4 security questions

The bank displays **exactly two** of four questions, in randomized order, with randomized positions. The answer flow is:

1. `SecurityQuestionsPage.read_questions()` reads each **visible question label text** and returns the label strings. It does not assume order or position.
2. Pure domain `normalize_question(text)` performs NFKD decomposition, strips combining marks, casefolds, collapses whitespace, and strips trailing `?`, `:`, `.`.
3. Pure domain `resolve_question_keys(texts)` exact-matches each normalized text against a pinned alias table: `normalized_text → QuestionKey`. The four keys are `HOBBIE`, `CHILDHOOD_HERO`, `FAVOURITE_DESSERT`, `COUNTRY_TO_VISIT`. Matching is **exact after normalization**; no fuzzy, partial, or edit-distance matching.
4. Validation (fail closed, no login):
   - fewer than 2 recognized keys → `BanescoLayoutUnexpected`;
   - more than 2 recognized keys → `BanescoLayoutUnexpected`;
   - a repeated key → `BanescoLayoutUnexpected`;
   - any label that maps to 0 keys → `BanescoLayoutUnexpected` (unknown question);
   - a recognized key whose configured answer is missing or empty → `BanescoLayoutUnexpected` (configuration failure, names the variable only).
5. `answer(mapping)` pairs each question label with the **answer input in the same form group** (DOM adjacency), not by index. If the label→input pairing is ambiguous, fail closed.
6. The page object submits once. The audit event records the recognized **keys only**, never answers. Answers are never persisted, echoed, or reused across sessions.

The exact question strings are **not reproduced in this design**. They are pinned as sanitized aliases in `app/domain/banesco.py` and mirrored in `tests/fixtures/banesco/security_questions_a.html` / `_b.html`.

### 3.4 Driver

`BanescoBrowser` lazily imports `playwright.async_api` inside `__aenter__`; validates required settings before the import; owns one browser context per payment attempt; and composes the page objects in order. It exposes the `PaymentRail` shape:

```python
class BanescoBrowser:
    async def __aenter__(self) -> "BanescoBrowser": ...
    async def aclose(self) -> None: ...
    async def pay(self, attempt: PaymentAttempt, target: BanescoPaymentTarget) -> PaymentReceipt: ...
```

`pay()` is the only public money path. It refuses a reused `attempt.attempt_id` before opening a browser (B2 support), and it aborts the session on any typed failure.

---

## 4. ASP.NET WebForms Mechanics

### 4.1 Why the DOM, never hand-crafted POSTs

The login, questions, and payment pages are WebForms postbacks carrying hidden `__VIEWSTATE`, `__VIEWSTATEGENERATOR`, and `__EVENTVALIDATION`. These values change per postback and encode control state. The rail **must** use Playwright `fill`, `select_option`, and `click`; the browser then submits the current hidden fields automatically. Hand-crafting POSTs would require re-scraping and re-submitting hidden fields per step, reconstructing `__EVENTTARGET`/`__EVENTARGUMENT`, and accounting for server-side validation; that is brittle and a correctness hazard on a money path. Hand-crafted POSTs are explicitly forbidden.

### 4.2 Postback waiting

After every `click`/submit:

1. `wait_for_load_state("domcontentloaded")` — never `networkidle`; ASP.NET keeps connections open and uses partial postbacks.
2. An explicit wait for the **next screen's anchor** (`_await_screen(...)` / `_expect(...)`).
3. `wait_for_url(...)` where the URL is deterministic (for example a return to the login page).
4. For asynchronous UpdatePanel postbacks, wait for the specific element/state to change rather than for a navigation.

The timeout is `BANESCO_BROWSER_TIMEOUT_MS` (default 30000). A timeout is a drift/session signal, not a retry trigger.

### 4.3 Session and idle timeout

The bank session can expire on an idle timer or invalidate `__VIEWSTATE`. Detection after a postback:

- the expected anchor is absent **and** the `Usuario` field or the login URL is present → `BanescoSessionExpired`;
- an explicit bank error page/banner (`account locked`, `maintenance`) → the matching typed error;
- a postback rejected with a validation/session error → `BanescoSessionExpired`.

Placement in the flow determines the outcome:

| When the stale session is detected | Outcome |
|---|---|
| Before the final `Pagar` | `SENDING_PAYMENT → MANUAL_REVIEW` with `banesco_session_expired`; no payment submitted; no automatic re-login within the same attempt |
| After the final `Pagar` | `banesco_payment_outcome_unknown`; never re-click, never re-submit |
| During the Clave wait (page idle) | The key submission fails or cannot be confirmed → outcome unknown; never re-enter the payment |

Sessions are kept short: login → form → first `Pagar` → second `Pagar` happen back-to-back. The only long wait is the human Clave step **after** the second submit. If the bank's idle timeout is shorter than `TELEGRAM_CLAVE_TIMEOUT_SECONDS`, the attempt fails closed (a live-enablement check, not a design change).

### 4.4 Stale-session reporting

`BanescoSessionExpired` carries `service="banesco_online"`, `code="banesco_session_expired"`, and a safe detail (`{"stage": "login|questions|form|post_submit"}`). It is audited as `EXTERNAL_ERROR`; after the final submit it is converted to `BanescoPaymentOutcomeUnknown` with `code="banesco_payment_outcome_unknown"`. No credential, URL token, or page HTML is logged.

---

## 5. Selector Strategy and DOM Drift

### 5.1 Declaration

Selectors live in **one place per screen**, in a `Selectors` table (class attributes or a module-level mapping) in `banesco_pages.py`. They are never inline in action methods.

Preference order:

1. `get_by_role(..., name=...)` with an exact accessible name.
2. `get_by_label(...)` / `get_by_text(..., exact=True)` for labeled controls and menu entries.
3. A stable `name` attribute (`input[name="..."]`) **only if** observed stable in a fixture.
4. A CSS structural anchor for containers (for example "the form containing this label").
5. XPath only for label→input adjacency where no accessible association exists; pinned in the same table.

Generated ASP.NET IDs (`ctl00_...`) are **never** a primary selector. If one is unavoidable, it must be observed stable, wrapped in the page object, and covered by a fixture.

### 5.2 Drift detection

Every page object declares:

- `required_anchors`: roles/text/labels that must be present;
- `assert_shape()`: a page-shape assertion run after each navigation (for example the Pago Móvil form must expose the phone prefix select, the phone-rest input, the ID letter select, the ID digits input, the bank select, the amount input, and the concept input; the bank select must expose exactly the supported labels);
- `_expect(...)`: an explicit wait for the action's target element.

A missing/extra critical element, a changed label, a select whose option set no longer contains the expected option, or a timeout all raise the same fail-closed error family.

### 5.3 Fail-closed error type

All drift surfaces as **`BanescoLayoutUnexpected`** (`service="banesco_online"`, `code="banesco_layout_unexpected"`), a `PaymentRailError` in `app/core/exceptions.py`. Money-critical mapping failures use their more specific codes (`unsupported_phone_prefix`, `id_type_missing`, `id_type_unsupported`, `unsupported_destination_bank`, `destination_bank_ambiguous`, `payment_data_unavailable`).

### 5.4 Anti-brittleness policy

- No auto-healing selectors, no fuzzy matching for money-critical fields, no positional selection for bank/ID options.
- Prefer accessible names/roles; pin every change in a recorded fixture and a test. The fixtures are the contract.
- A live DOM change requires a **fixture update + test update**; that is the reviewable drift event.
- Live-only selectors are explicitly marked and verified by opt-in smoke, never by CI.
- No retry on a selector timeout; a timeout means "unknown/fail closed".
- Option maps (phone prefixes, ID letters, destination banks) are versioned data, not branching code.

---

## 6. Two-Control Payment Mapping

### 6.1 DTO

`app/domain/banesco.py` (frozen, slots, money as `Decimal`):

```python
@dataclass(frozen=True, slots=True)
class BanescoPaymentTarget:
    phone_prefix: str          # exactly 4 national digits, present in the live option set
    phone_rest: str            # exactly 7 digits
    id_type: str               # one of V, E, J, G, R
    id_digits: str             # 6-9 digits
    destination_bank_code: str # 4 digits, 0102 or 0134
    destination_bank_option: str  # exact reviewed live option label/value
    amount_ves: Decimal        # positive, finite
    concept: str               # non-empty, no PII
```

### 6.2 Split and validation rules

| Control | Source | Rule | Failure |
|---|---|---|---|
| Phone prefix | `PaymentInstruction.phone` | `national_digits(phone)[:4]`; must be one of the six known mobile prefixes **and** present in the live select options | `unsupported_phone_prefix` / `payment_data_unavailable` |
| Phone rest | same | `national_digits(phone)[4:]`; exactly 7 digits | same |
| ID letter | order-page scrape or operator override | uppercase leading `V/E/J/G/R`; must be in the live offered set | `id_type_missing` (absent) / `id_type_unsupported` (P or other) |
| ID digits | `PaymentInstruction.id_number` | extended `cedula_digits()`; 6–9 bare digits | `payment_data_unavailable` |
| Destination bank | `PaymentInstruction.bank_numeric_code` | pure registry lookup + reviewed option map (§7) | `unsupported_destination_bank` / `destination_bank_ambiguous` |
| Amount | `PaymentInstruction.amount_ves` | `Decimal`-only; `format_ves_amount()` renders `1500,50`; finite > 0 | `payment_data_unavailable` |
| Concept | `BANESCO_PAYMENT_CONCEPT` (default `"Pago"`) | non-empty; no counterparty PII | `payment_data_unavailable` |

The rail never types a full phone into one control and never picks a prefix by guess.

### 6.3 `R` / `P` normalization

Fix the existing mismatch in one place:

- `banks._CEDULA_PREFIX_RE` becomes `^[VEJGPR]`; `logging._CEDULA_PATTERN` becomes `[VEJGPR]`; `schemas._ID_PATTERN` accepts `R`. `cedula_digits()` therefore normalizes `R-12345678` to `12345678`.
- The Banesco mapper accepts only `V/E/J/G/R`. `P` normalizes at the domain layer but the rail rejects it with `id_type_unsupported`; the letter is never substituted.
- The two-control projection is exposed as a pure helper: `project_phone(phone) -> (prefix, rest)` and `project_document(document) -> (letter, digits)`.

### 6.4 `id_type_missing` path (never default to `V`)

Order of attempts:

1. **Order-page capture.** Extend the Binance parser to capture the letter when the page exposes it: a prefixed value (`V-12345678`) or a separate label such as `Tipo de documento` / `Documento`. `PaymentInstruction` stays provider-neutral; the parser stores the observed letter in `raw_fields` and the DTO gains an optional `id_type` field (or the raw field is read by the mapping layer).
2. **Operator override via Telegram.** If the letter is absent, open an `ID_TYPE` challenge through the notifier:
   - persist the challenge (B3) before sending;
   - message: order number + "reply with V, E, J, G or R";
   - accept only `^[VEJGR]$` correlated to that challenge;
   - timeout/refusal → `MANUAL_REVIEW` with `id_type_missing`.
3. **Fail closed.** If no letter is captured and no override arrives, park with `id_type_missing`. `BANESCO_DEFAULT_ID_TYPE` defaults to `""`; a non-empty value is an explicit, audited operator choice and is validated against `V/E/J/G/R` (never `P`). The rail **never** defaults to `V`.

This all happens **before** B2 (before the browser session and before reservation), so a blocked order never pays.

---

## 7. Destination Bank Mapping

The order carries a four-digit destination code, but the Banesco `Banco Destino` control lists **names**, not codes. The map is reviewed, versioned data.

```python
@dataclass(frozen=True, slots=True)
class BanescoBankOption:
    numeric_code: str   # "0102" | "0134"
    label: str          # exact reviewed <option> label, captured from a sanitized fixture
    value: str | None   # exact <option value> when the page sets one

BANESCO_DESTINATION_BANK_OPTIONS: Mapping[str, BanescoBankOption]
```

Rules:

- Only `0102` (Banco de Venezuela) and `0134` (Banesco) are supported; any other code fails at risk assessment (`unsupported_destination_bank`, R-BUY-004) and again at mapping.
- The adapter reads the **live** `<option>` set from the bank select and matches the reviewed label/value after whitespace/accents normalization.
- **Zero matches** → `unsupported_destination_bank`.
- **Multiple matches** → `destination_bank_ambiguous`.
- **A changed live label/value** (no match) → `unsupported_destination_bank`; fail closed, never fuzzy-match, never select by index, never substitute a nearest bank.
- `tests/fixtures/banesco/bank_options.json` is the sanitized snapshot; a test asserts the domain map equals the snapshot, so a live change is a reviewable fixture+code change.
- "Banesco only" means Banesco is the **payer rail**; the destination bank is still the counterparty's bank.

---

## 8. Telegram Handoff

### 8.1 Transport

Raw Bot API over the existing `httpx==0.28.1`; **no new dependency**. `app/infrastructure/clients/telegram_bot.py`:

```python
class TelegramBotClient:
    async def send_message(self, chat_id: int, text: str, *,
                           reply_markup: Mapping[str, Any] | None = None,
                           protect_content: bool = True) -> TelegramMessage: ...
    async def get_updates(self, *, offset: int | None,
                          timeout_seconds: int) -> list[TelegramUpdate]: ...
```

A long-poll task in `app/main.py` (`_run_telegram_poller`, gated by `TELEGRAM_ENABLED`) consumes `getUpdates`, advances the offset, ignores non-operator chats, and resolves matching pending challenges. `TELEGRAM_BOT_TOKEN` is a `SecretStr`; the token-bearing URL is never logged. `respx` covers the adapter in tests.

### 8.2 Message format

Clave Dinámica:

```
liquidVE | Orden <order_number> | Clave Dinámica
Responde a este mensaje con los 6 dígitos.
Ref: <nonce>
```

ID-type override:

```
liquidVE | Orden <order_number> | Tipo de documento
Responde solo con V, E, J, G o R.
Ref: <nonce>
```

Neither message contains amount, phone, cedula, receipt text, or any credential.

### 8.3 Correlation scheme

A reply is accepted only when **all** hold:

1. `message.chat.id == TELEGRAM_OPERATOR_CHAT_ID` (the only accepted chat).
2. It is a **reply** to the challenge message (`reply_to_message.message_id == challenge.telegram_message_id`) **or** its text carries the one-time nonce.
3. The text contains the order number (or the reply-to binding already proves it).
4. The nonce matches `sha256(nonce) == nonce_hash` for a `PENDING` challenge.
5. The body is exactly six digits (`^\d{6}$`) for Clave, or exactly `^[VEJGR]$` for ID type.
6. The challenge is `PENDING` and not expired.

At most one challenge is pending because `BANESCO_MAX_CONCURRENT_PAYMENTS=1` serializes payments. The nonce is random (128-bit), one-time, never reused, and stored only as a hash.

### 8.4 Persisted `operator_challenges`

| Column | Type | Notes |
|---|---|---|
| `id` | BigInteger PK | |
| `order_id` | FK `p2p_orders.id` | indexed |
| `challenge_type` | String(32) | `CLAVE_DINAMICA` \| `ID_TYPE` |
| `channel` | String(16) | `telegram` |
| `chat_id` | BigInteger | operator chat |
| `telegram_message_id` | BigInteger nullable | reply-to anchor |
| `nonce_hash` | String(64) UNIQUE | SHA-256 of the one-time nonce |
| `status` | String(16) | `PENDING` \| `ANSWERED` \| `EXPIRED` \| `CANCELLED` |
| `expires_at` | DateTime(tz) | |
| `attempt_count` | Integer | default 0 |
| `clarification_count` | Integer | default 0 |
| `answered_at` / `cancelled_at` | DateTime(tz) nullable | |
| `created_at` / `updated_at` | DateTime(tz) | |

The answer/key is **never stored**. Index `(order_id, status)`; a service guard refuses a second `PENDING` challenge for the same order; `nonce_hash` UNIQUE is the durable backstop.

**State machine:**

```
PENDING ──valid correlated reply──▶ ANSWERED   (terminal)
PENDING ──timeout / startup───────▶ EXPIRED    (terminal)
PENDING ──operator/shutdown/abort─▶ CANCELLED  (terminal)
```

A reply arriving after a terminal state is ignored and audited (`TELEGRAM_CHALLENGE_REJECTED`), never applied.

### 8.5 Timeout

`TELEGRAM_CLAVE_TIMEOUT_SECONDS` (default 300) and `TELEGRAM_ID_TYPE_TIMEOUT_SECONDS` (default 300). On expiry: mark `EXPIRED`, abort the browser page, park `SENDING_PAYMENT → MANUAL_REVIEW` with `clave_dinamica_timeout` (or `id_type_missing`), and send an informational operator notice (no new challenge).

### 8.6 Wrong, ambiguous, and multiple replies

| Situation | Behavior |
|---|---|
| Wrong chat | Ignore; audit `TELEGRAM_CHALLENGE_REJECTED` with a masked chat id; never reply with details |
| Malformed body (not 6 digits / not a valid letter) | Reply once with a clarification; do **not** consume the challenge; increment `clarification_count`; after `TELEGRAM_MAX_CLARIFICATIONS=2`, fail closed |
| No reply-to and more than one pending challenge | Ask the operator to reply to the specific message; never guess. Serialization makes this practically impossible |
| Multiple valid replies for one challenge | First accepted reply wins (`ANSWERED`); later ones are ignored/audited |
| Bank rejects the key | `attempt_count += 1`; with `BANESCO_CLAVE_RETRY_LIMIT=0` (default) park `clave_dinamica_rejected`; never retry indefinitely |
| Reply for an unknown/expired challenge | Ignore and audit; never attach it to another order |

### 8.7 Restart rule

A browser page and an `asyncio.Future` cannot survive a restart. Therefore:

1. On startup, mark every `PENDING` challenge `EXPIRED`.
2. Every order still in `SENDING_PAYMENT` with a challenge and no live session → `MANUAL_REVIEW` with `payment_interrupted`.
3. **Never auto-resume a payment and never re-send a challenge.** Send a one-way informational notice ("order <n> interrupted; verify the bank state") and stop.
4. `AWAITING_RELEASE` and `APPEALED` are safe to resume because their next action is a read-only poll; the watcher continues, but it never re-invokes a payment or an appeal.

---

## 9. Status Migration

### 9.1 Enum changes

`OrderStatus` (`native_enum=False`, stored as plain `VARCHAR(32)`, **no CHECK constraint** — confirmed at `models.py:111-115` and the initial migration):

- rename `SENDING_SMS` → `SENDING_PAYMENT`;
- add `AWAITING_RELEASE` and `APPEALED`.

No DDL is needed for the status column. `SENDING_PAYMENT` (15 chars) fits `VARCHAR(32)`. `AuditLog.from_status`/`to_status` are also `VARCHAR(32)` with no CHECK.

### 9.2 New `ALLOWED_TRANSITIONS`

```
PENDING              → {PAYMENT_DETECTED, FAILED, MANUAL_REVIEW}
PAYMENT_DETECTED     → {VERIFYING_CACHICAMO, MANUAL_REVIEW, FAILED}
VERIFYING_CACHICAMO  → {SENDING_PAYMENT, COMPLETED, MANUAL_REVIEW, FAILED}
SENDING_PAYMENT      → {AWAITING_RELEASE, MANUAL_REVIEW, FAILED}
AWAITING_RELEASE     → {COMPLETED, APPEALED, MANUAL_REVIEW, FAILED}
APPEALED             → {COMPLETED, FAILED, MANUAL_REVIEW}
COMPLETED            → {}            (terminal)
FAILED               → {}            (terminal)
MANUAL_REVIEW        → {PENDING, FAILED}
```

- **`SENDING_PAYMENT → COMPLETED` is removed.** Completion requires an observed counterparty release; a captured receipt is payment evidence, not completion.
- `VERIFYING_CACHICAMO → COMPLETED` remains for Flow A.
- `AWAITING_RELEASE` requires `bank_reference IS NOT NULL` and `bank_confirmed_at IS NOT NULL` (the existing financial anchor).
- `APPEALED` is entered only after a durable `appeal_attempt_id` is committed.
- `MANUAL_REVIEW → PENDING` is allowed **only** for pre-payment reasons (`awaiting_operator_approval`, `payment_data_unavailable`, a risk block). It is refused for `clave_dinamica_timeout`, `payment_outcome_unknown`, `receipt_amount_mismatch`, and a manual `release_timeout`. The guard lives in `BuyFlow.resume`/`_dispatch` and in `orders.py`, and is tested.

### 9.3 Alembic data migration

`alembic/versions/20261125_0003_status_ledger_rename.py` (data-only; `down_revision = "20261123_0002"`, S1):

```sql
-- upgrade
UPDATE p2p_orders  SET status      = 'SENDING_PAYMENT' WHERE status      = 'SENDING_SMS';
UPDATE audit_logs  SET from_status = 'SENDING_PAYMENT' WHERE from_status = 'SENDING_SMS';
UPDATE audit_logs  SET to_status   = 'SENDING_PAYMENT' WHERE to_status   = 'SENDING_SMS';
```

Downgrade (lossy only for the new tail; never back to `SENDING_SMS`):

```sql
-- downgrade: collapse new-tail states first, then reverse the rename
UPDATE p2p_orders  SET status      = 'MANUAL_REVIEW'
  WHERE status IN ('AWAITING_RELEASE', 'APPEALED');
UPDATE audit_logs  SET from_status = 'MANUAL_REVIEW'
  WHERE from_status IN ('AWAITING_RELEASE', 'APPEALED');
UPDATE audit_logs  SET to_status   = 'MANUAL_REVIEW'
  WHERE to_status IN ('AWAITING_RELEASE', 'APPEALED');

UPDATE p2p_orders  SET status      = 'SENDING_SMS' WHERE status      = 'SENDING_PAYMENT';
UPDATE audit_logs  SET from_status = 'SENDING_SMS' WHERE from_status = 'SENDING_PAYMENT';
UPDATE audit_logs  SET to_status   = 'SENDING_SMS' WHERE to_status   = 'SENDING_PAYMENT';
```

- Historical `AuditLog.payload` JSON strings mentioning `SENDING_SMS` are **not** rewritten (append-only audit).
- The downgrade never maps a paid order back to `SENDING_SMS`; it parks the new tail in `MANUAL_REVIEW` and never auto-resumes.
- Works on SQLite and PostgreSQL because the columns are strings.
- `tests/test_migrations.py` seeds `SENDING_SMS`, upgrades (asserts `SENDING_PAYMENT`), downgrades (asserts `SENDING_SMS`), and separately seeds `AWAITING_RELEASE`/`APPEALED` and proves the downgrade maps them to `MANUAL_REVIEW`.

A separate schema revision `20261125_0004_banesco_rail_schema.py` adds the nullable rail/receipt/release/appeal columns and the `operator_challenges` table (S5/S6/S8). Nullable-only additions keep the downgrade a simple reverse drop.

---

## 10. Ledger

- Rename the channel constant `LEDGER_VES_SMS = "VES_SMS"` → `LEDGER_VES_PAY = "VES_PAY"` (7 chars, fits `DailyVolumeLedger.currency String(8)`; no column widening).
- `LEDGER_VES = "VES"` and `CHANNELS = {VES, VES_PAY}`. The **double cap is preserved**: a dispatched BUY reserves `VES` **and** `VES_PAY`.
- `reserve_buy(amount_ves)` keeps the all-or-nothing behavior: reserve `VES`, then `VES_PAY`; if the second refuses, release the first and raise `RiskLimitExceededError`.
- `release` is called for both channels only on a confirmed permanent **pre-submission** failure (for example login/layout/`banesco_payment_rejected` before the final `Pagar`). It is never called on timeout or unknown outcome.
- `_channel_limit("VES_PAY")` uses `DAILY_PAY_LIMIT_VES` (renamed from `DAILY_SMS_LIMIT_VES`; see §13). The per-order cap remains the existing `MAX_BUY_AMOUNT_VES`.
- Setting rename: `DAILY_SMS_LIMIT_VES` → `DAILY_PAY_LIMIT_VES`; the old name is accepted as a deprecated alias for one release.

### Migration for existing dev rows

In the same `20261125_0003` data revision, or a dedicated `0003b`:

1. For every `ledger_date` that has **both** a `VES_SMS` and a `VES_PAY` row: add `VES_SMS.total_volume` into `VES_PAY.total_volume`, add `transaction_count`, then delete the `VES_SMS` row. This preserves the `UNIQUE(ledger_date, currency)` anchor.
2. For every remaining `VES_SMS` row: `UPDATE daily_volume_ledgers SET currency='VES_PAY' WHERE currency='VES_SMS'`.
3. Downgrade reverses it with the same aggregation guard (`VES_PAY` → `VES_SMS`, aggregating if both exist, then delete the stale `VES_PAY` row).

Implemented with `op.get_bind()` and parameterized SQL; portable on SQLite and PostgreSQL; atomic per migration transaction. Tests cover a same-date collision, a rename-only date, and UTC rollover.

---

## 11. Receipt Evidence

### Storage

- Directory: `RECEIPT_STORAGE_DIR` (default `./data/receipts`), created at runtime with restrictive permissions (0700). `data/*` is already gitignored (`.gitignore:55`); no receipt file is ever committed.
- File name: `data/receipts/{order_number}/{sha256}.png`.
- Only the **relative path** is persisted; bytes are never stored in the database.

### Persisted metadata (`p2p_orders`, nullable columns)

| Column | Type | Meaning |
|---|---|---|
| `payment_rail` | String(32) | `banesco_online` |
| `payment_attempt_id` | String(128) UNIQUE | B2 durable attempt identity |
| `receipt_path` | String(512) | relative path under the receipt dir |
| `receipt_sha256` | String(64) | hex digest; dedupe anchor |
| `receipt_amount_ves` | Numeric(24,2) | parsed `Decimal` amount |
| `receipt_paid_at` | DateTime(tz) | bank timestamp (UTC); adapter clock fallback only if audited |
| `evidence_uploaded_at` | DateTime(tz) | B5 evidence success |
| `release_deadline_at` | DateTime(tz) | release timeout anchor |
| `appeal_attempt_id` | String(128) UNIQUE | B7 durable appeal identity |
| `appeal_filed_at` | DateTime(tz) | |
| `appeal_status` | String(16) | `pending` \| `unknown` \| `filed` \| `skipped` |

The existing `bank_reference` (UNIQUE) and `bank_confirmed_at` remain the financial anchor; the receipt writes exactly one `OUTGOING` `BankTransaction` with `raw_payload = {"source": "banesco_online", "receipt_sha256": ...}` and no raw text/PII.

### Rules

- Compute the digest **before** persistence. Same digest for the same order = no-op. Different digest for an order that already has a receipt → `receipt_conflict`, park, never overwrite.
- Compare the parsed amount to the reserved/expected `Decimal`; mismatch → `receipt_amount_mismatch`, never mark confirmed.
- A submission with no receipt is `banesco_payment_outcome_unknown`, never success.
- **Raw receipt text, HTML, image bytes, and PII are never logged, audited, sent to Engram, or stored in the DB.** In-memory parsing only. Audit/API expose `receipt_sha256` and `mask_secret(reference, visible=4)`.
- A canary test proves no credential, key, or receipt PII appears in a rendered log or audit payload.
- Retention: `RECEIPT_RETENTION_DAYS` (default 30). A cleanup tick deletes files older than retention; metadata (hash/path) may remain for audit. The file path is not PII; the image is.

---

## 12. Binance Evidence Upload + Confirm

### Port extension

Extend the `BinanceBrowser` port (implementation stays in `binance_browser.py`, lazy Playwright import):

```python
class BinanceBrowser(Protocol):
    async def extract_payment_instruction(self, order_number: str) -> PaymentInstruction: ...
    async def upload_evidence(self, order_number: str, evidence_path: str) -> None: ...
    async def notify_paid(self, order_number: str, reference: str, *,
                          evidence_path: str) -> None: ...
    async def file_appeal(self, order_number: str, evidence_path: str,
                          reason: str) -> AppealResult: ...   # S8
```

`notify_paid` is the S7 money-adjacent path. It: opens the order, uploads the image through the file input (`set_input_files` with the path, or a temp file), waits for the uploaded attachment bubble, clicks "Transferido, notificar al vendedor", posts the reference in the order chat, and returns only when the reference bubble is visible. `upload_evidence` is factored out so the appeal path can reuse the same receipt without re-notifying.

### Sequence

1. B4 has already committed the receipt, `bank_reference`, `bank_confirmed_at`, and the `OUTGOING` transaction.
2. Call `notify_paid(order_number, reference, evidence_path=receipt_path)` **exactly once**.
3. On success: B5 commits `EVIDENCE_UPLOADED`, `SENDING_PAYMENT → AWAITING_RELEASE`, `release_deadline_at = now + RELEASE_TIMEOUT_SECONDS`, and `evidence_uploaded_at`.
4. On failure: park `MANUAL_REVIEW`; never enter `AWAITING_RELEASE`; never file an appeal.

### Failure modes

| Failure | Detection | Outcome |
|---|---|---|
| Upload fails | file input missing, upload timeout, no attachment bubble | `MANUAL_REVIEW` / `evidence_upload_failed`; no release tail, no appeal |
| Upload unknown | timeout after submit | park `evidence_upload_failed`; never auto-retry |
| Upload succeeded, confirm fails | close button missing / click error | `MANUAL_REVIEW` / `binance_close_failed`; evidence is already uploaded; only an operator may re-attempt |
| Confirm unknown | timeout / uncorrelated DOM | `MANUAL_REVIEW` / `binance_close_failed`; never auto-retry |
| No browser wired | adapter absent | `MANUAL_REVIEW` / `evidence_upload_failed` |

### Idempotency

At most once per receipt. The service checks `evidence_uploaded_at` and the absence of a prior `EVIDENCE_UPLOADED` audit before invoking. After an unknown outcome it does not re-invoke automatically; an operator-only re-attempt is explicit and audited. Uploading twice is not treated as success; a duplicate confirm is never issued.

---

## 13. Release Watcher

### Cadence and selection

- Task `_run_release_watcher` in `app/main.py`, gated by `RELEASE_WATCHER_ENABLED`; interval `RELEASE_POLL_INTERVAL_SECONDS` (default 30).
- Selects `AWAITING_RELEASE` orders **and** `APPEALED` orders (the latter only to observe a favorable resolution), with `bank_reference`/`bank_confirmed_at` non-null.
- Calls `BinanceC2CClient.get_order_detail(order_number)` — read-only, idempotent, retried by the base HTTP client.

### Conservative status mapping

| Observed Binance status | Meaning | Action |
|---|---|---|
| released / completed | counterparty released | `AWAITING_RELEASE → COMPLETED` (or `APPEALED → COMPLETED`), `RELEASE_DETECTED` |
| `APPEALING` / appeal open | already appealed (possibly by the counterparty) | record; `AWAITING_RELEASE → APPEALED` **without** filing another appeal |
| `CANCELLED` / `EXPIRED` | order gone after payment | `MANUAL_REVIEW` / `external_order_cancelled` (serious anomaly) |
| `PENDING_PAYMENT` / `PAID` | not released yet | continue waiting |
| unknown / unrecognized | vocabulary drift | `MANUAL_REVIEW` / `external_status_unknown` |

The exact provider vocabulary is verified against real payloads before the watcher is enabled; the watcher never guesses.

### Timeout policy

When `now > release_deadline_at`, apply `RELEASE_APPEAL_MODE`:

- `auto` → run the appeal path (§14) after a pre-click re-check; release wins if observed;
- `alert` → `AWAITING_RELEASE → MANUAL_REVIEW` with `release_timeout`; notify the operator; no appeal;
- `off` → no appeal and no completion; leave the order flagged for the operator.

### At-most-once across restarts

- Polling is read-only. Every status change is a guarded transition; a repeated tick is a no-op.
- `release_deadline_at`, `appeal_attempt_id` (UNIQUE), `appeal_filed_at`, and `appeal_status` are durable. A restart re-polls and sees the committed state; it never re-files.
- Row locking (`SELECT ... FOR UPDATE` on PostgreSQL) plus the `P2POrder.version` optimistic column prevent two ticks/processes from completing or appealing twice. SQLite relies on its single-writer serialization and the guarded transition.
- If the process dies **after** committing `APPEALED` but **before** the click, the next tick sees `APPEALED` and does not click; the operator verifies. The attempt is at-most-once, not at-least-once.

---

## 14. Automatic Appeal

### What the click does

`AppealPage.file(reason, evidence_path)` opens the Binance order page, clicks the appeal control, selects the reviewed reason ("paid but crypto not received" — exact label verified live and pinned in a fixture), attaches the captured receipt image, optionally adds neutral text, submits, and waits for the confirmation banner / `APPEALING` status. Any missing/unknown control fails closed; it does not try another reason.

### Durable attempt identity before the click

1. **Pre-click re-check** (`get_order_detail`):
   - released → `COMPLETED`, `APPEAL_SKIPPED`, stop;
   - already `APPEALING` → record, `AWAITING_RELEASE → APPEALED`, do not click;
   - unknown → `MANUAL_REVIEW`, stop.
2. Commit **before** the click, in one transaction:
   - `appeal_attempt_id = "{order_number}:appeal"` (UNIQUE),
   - status `APPEALED`,
   - `appeal_filed_at = now`,
   - `appeal_status = "pending"`,
   - `APPEAL_FILED` audit.
3. Perform the click/submit.
4. On success: `appeal_status="filed"`. On failure/unknown: `appeal_status="unknown"`, stay `APPEALED`; the operator verifies. **No second click.**

### Kill switch and modes

`RELEASE_APPEAL_MODE` (`auto` | `alert` | `off`):

- `auto` — file at most once after the pre-click re-check;
- `alert` — notify the operator and park `MANUAL_REVIEW` / `release_timeout`; no click;
- `off` — disable appeal entirely; leave the order flagged.

`BANESCO_RAIL_ENABLED=false`, `RELEASE_WATCHER_ENABLED=false`, and `BUY_AUTO_PAY_ENABLED=false` are the other kill switches.

### Why a retry is forbidden

An appeal is consequential: it can freeze funds and damage account reputation. An unknown submit outcome is **not** evidence of failure. A retry could file a duplicate appeal or a contradictory one. The UNIQUE attempt identity plus the durable `APPEALED` state make a second attempt impossible; recovery is an explicit operator decision after checking Binance.

---

## 15. Settings

### New settings (names, types, safe defaults)

All new fields have defaults or are optional, so import-time `Settings()` never breaks. **No new required field is added.** The two BUY caps already exist (`MAX_BUY_AMOUNT_VES`, and the daily BUY cap currently named `DAILY_SMS_LIMIT_VES`); no new cap is introduced.

| Name | Type | Safe default | Notes |
|---|---|---|---|
| `BANESCO_RAIL_ENABLED` | `bool` | `False` | master kill switch; when false no Banesco session starts |
| `BANESCO_ONLINE_LOGIN_URL` | `str` | Banesco login URL | must be absolute `https` |
| `BANESCO_ONLINE_USER` | `SecretStr \| None` | `None` | required only when the rail is enabled |
| `BANESCO_ONLINE_PASSWORD` | `SecretStr \| None` | `None` | required only when enabled |
| `BANESCO_SECURITY_ANSWER_HOBBIE` | `SecretStr \| None` | `None` | required only when enabled |
| `BANESCO_SECURITY_ANSWER_CHILDHOOD_HERO` | `SecretStr \| None` | `None` | required only when enabled |
| `BANESCO_SECURITY_ANSWER_FAVOURITE_DESSERT` | `SecretStr \| None` | `None` | required only when enabled |
| `BANESCO_SECURITY_ANSWER_COUNTRY_TO_VISIT` | `SecretStr \| None` | `None` | required only when enabled |
| `BANESCO_BROWSER_HEADLESS` | `bool` | `True` | |
| `BANESCO_BROWSER_TIMEOUT_MS` | `int` | `30000` | must be > 0 |
| `BANESCO_MAX_CONCURRENT_PAYMENTS` | `int` | `1` | must be >= 1; serializes payments |
| `BANESCO_CLAVE_RETRY_LIMIT` | `int` | `0` | fail closed |
| `BANESCO_PAYMENT_CONCEPT` | `str` | `"Pago"` | non-empty; no PII |
| `BANESCO_DEFAULT_ID_TYPE` | `str` | `""` | empty = fail closed; only `""` or `V/E/J/G/R` accepted |
| `TELEGRAM_ENABLED` | `bool` | `False` | |
| `TELEGRAM_BOT_TOKEN` | `SecretStr \| None` | `None` | required when enabled |
| `TELEGRAM_OPERATOR_CHAT_ID` | `int \| None` | `None` | required when enabled; > 0 |
| `TELEGRAM_API_BASE_URL` | `str` | `https://api.telegram.org` | |
| `TELEGRAM_CLAVE_TIMEOUT_SECONDS` | `int` | `300` | > 0 |
| `TELEGRAM_ID_TYPE_TIMEOUT_SECONDS` | `int` | `300` | > 0 |
| `TELEGRAM_POLL_TIMEOUT_SECONDS` | `int` | `30` | long-poll window |
| `TELEGRAM_MAX_CLARIFICATIONS` | `int` | `2` | |
| `RECEIPT_STORAGE_DIR` | `str` | `./data/receipts` | gitignored |
| `RECEIPT_RETENTION_DAYS` | `int` | `30` | >= 0 |
| `RELEASE_WATCHER_ENABLED` | `bool` | `False` | |
| `RELEASE_POLL_INTERVAL_SECONDS` | `int` | `30` | > 0 |
| `RELEASE_TIMEOUT_SECONDS` | `int` | `900` | > 0 |
| `RELEASE_APPEAL_MODE` | `Literal["auto","alert","off"]` | `"auto"` | safe because the watcher defaults off |
| `BUY_PAYMENT_RAIL` | `Literal["banesco"]` | `"banesco"` | transitional S7 selector; S9 removes the SMS option |

Renames with a one-release deprecated alias: `DAILY_SMS_LIMIT_VES` → `DAILY_PAY_LIMIT_VES`; `SMS_CONFIRMATION_TIMEOUT_SECONDS` → `PAYMENT_CONFIRMATION_TIMEOUT_SECONDS`.

Existing settings reused unchanged: `MAX_BUY_AMOUNT_VES`, `BUY_AUTO_PAY_ENABLED`, `BINANCE_BROWSER_ENABLED`, `BINANCE_BROWSER_HEADLESS`, `BINANCE_BROWSER_TIMEOUT_MS`, `BUY_WORKER_ENABLED`.

### Validators (`Settings._validate_consistency`)

Gate on **enablement flags**, never on a defaulted truthy field:

- `if banesco_rail_enabled:` require non-empty `BANESCO_ONLINE_USER`, `BANESCO_ONLINE_PASSWORD`, all four answers; `BANESCO_ONLINE_LOGIN_URL` absolute `https`; `BANESCO_BROWSER_TIMEOUT_MS > 0`; `BANESCO_MAX_CONCURRENT_PAYMENTS >= 1`; `TELEGRAM_ENABLED`; `BINANCE_BROWSER_ENABLED` (evidence upload).
- `if telegram_enabled or banesco_rail_enabled:` require `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OPERATOR_CHAT_ID > 0`, `TELEGRAM_CLAVE_TIMEOUT_SECONDS > 0`, `TELEGRAM_POLL_TIMEOUT_SECONDS > 0`.
- `if release_watcher_enabled:` require `RELEASE_POLL_INTERVAL_SECONDS > 0` and `RELEASE_TIMEOUT_SECONDS > 0`; if `release_appeal_mode == "auto"`, require `BINANCE_BROWSER_ENABLED` (the appeal is a browser action).
- `BANESCO_DEFAULT_ID_TYPE` in `{"", "V", "E", "J", "G", "R"}`.
- `RELEASE_APPEAL_MODE` is the `Literal` itself; an invalid value fails pydantic.
- `base_settings_summary()` adds only non-secret values: `banesco_rail_enabled`, `telegram_enabled`, `release_watcher_enabled`, `release_interval/timeout/mode`, `receipt_retention_days`, `banesco_browser_headless`, `banesco_browser_timeout_ms`, `banesco_max_concurrent_payments`. **Never** user/password/answers/token/chat id.

### Exact co-change set (so import-time `Settings()` never breaks)

1. `app/core/config.py` — new optional/defaulted fields, validators, summary.
2. `.env.example` — every name above with placeholders only (`BANESCO_ONLINE_USER=replace-with-banesco-user`, `BANESCO_ONLINE_PASSWORD=replace-with-banesco-password`, `BANESCO_SECURITY_ANSWER_HOBBIE=replace-with-answer`, …; `TELEGRAM_BOT_TOKEN=replace-with-bot-token`, `TELEGRAM_OPERATOR_CHAT_ID=0`). No real values.
3. `tests/conftest.py` — add `os.environ.setdefault(...)` for every new non-secret name and clearly fake secret values (for example `test-banesco-user`, `test-banesco-password`, `test-banesco-answer-hobbie`), `TELEGRAM_OPERATOR_CHAT_ID=123456789`, and a `tmp_path`-based `RECEIPT_STORAGE_DIR`; extend the `test_settings` fixture with the same values.
4. `tests/test_config.py` — defaults; rail-enabled-without-secret fails; rail-enabled-without-telegram fails; `auto` appeal without watcher fails; `BANESCO_DEFAULT_ID_TYPE="P"` fails; secrets never appear in `base_settings_summary()`; the deprecated alias works.
5. `tests/test_logging.py` — canary for the new sensitive keys (`security_answer`, `answer`, `clave`, `clave_dinamica`, `R-` document).
6. `README.md` — statuses and setting names with placeholders.

---

## 16. Test Seams

### Browser automation with no browser

1. **Pure domain tests** — `test_banesco_domain.py`: question normalization/validation, phone/document projection, amount rendering, DTO invariants, bank option map, destination matching (0/1/many), page-shape parsers over recorded HTML.
2. **Page-object fakes** — `test_banesco_pages.py`: a `FakePage`/`FakeBanescoContext` serves a named fixture and records actions (`goto`, `fill`, `select_option`, `click`, `screenshot`). Covers login entry, 2-of-4 questions (both orders/subsets, unknown, repeated, wrong count), ad modal present/absent/undismissable, form values (both controls per field), double-`Pagar` ordering, clave-form detection, receipt capture.
3. **Driver import guard** — `test_banesco_browser.py`: importing `banesco_browser` / `banesco_pages` never imports `playwright` (in-process assertion and a subprocess assertion, mirroring `test_browser_adapter.py`); settings validation fails before any lazy import.
4. **Fake confirmation provider** — `FakeOperatorNotifier` implements the port and scripts: correlated reply, timeout, wrong chat, malformed, ambiguous, multiple replies, wrong key.
5. **Fake Telegram transport** — `FakeTelegramBotClient` at the port plus `respx` tests for the raw httpx client (`sendMessage` payload: chat id, text, reply markup, `protect_content`; `getUpdates` offset handling; 429/5xx; update dedupe by `update_id`; token never logged).
6. **Fake receipt store** — records persisted metadata and bytes; tests digest dedupe, conflict, amount mismatch, PII-free audit.
7. **Fake Binance / Fake OrderActions** — `FakeBinance` scripts status sequences (`PAID → PAID → COMPLETED`, `PAID → APPEALING`, unknown, cancelled); `FakeOrderActions` records `upload_evidence` / `notify_paid` / `file_appeal` and asserts ordering and at-most-once.
8. **Time** — `freezegun` for challenge `expires_at`, `release_deadline_at`, timeout sweeps, and restart expiry.
9. **Migrations** — `test_migrations.py` covers the status rename, new-tail downgrade remap, and the ledger collision aggregation on a temp SQLite file.
10. **Fixture hygiene** — a test scans `tests/fixtures/banesco/**` for known canary secrets and real-looking PII and fails if found. Fixtures are sanitized fake data only.

### Recorded fixtures

`tests/fixtures/banesco/` (mirroring `tests/fixtures/binance/*.html`):

`login.html`, `security_questions_a.html`, `security_questions_b.html` (different subset/order; exactly 2 of 4), `ad_modal.html`, `payment_form.html` (all phone prefixes, all ID letters, bank options), `confirmation.html`, `clave_form.html`, `receipt.html`, `error_captcha.html`, `error_locked.html`, `error_maintenance.html`, `error_session_expired.html`, `bank_options.json`.

Rules: sanitized fake names/phones/cedulas/references/amounts; no credentials; selectors pinned to accessible names/roles/labels; a live DOM change is a fixture + test change.

### Opt-in live only (never CI)

- `LIQUIDVE_BROWSER_TESTS=1` — the existing Binance browser gate; also gates Banesco read-only smoke (login + questions + ad modal + navigate to the Pago Móvil form **without submitting**).
- `LIQUIDVE_BANESCO_PAYMENT_TESTS=1` — a full low-value, manually supervised real payment in a controlled window; never CI.
- All are marked `@pytest.mark.integration` and skipped unless the env var is set. They assert no credential is printed and never commit screenshots/HTML.

What can only be verified live: captcha/anti-bot behavior, the real ad modal, exact bank option labels, the exact `Pagar`/confirmation DOM, the Clave form, the receipt DOM, and the Binance appeal UI labels.

### Default-run invariants

No network, no Chromium, no Playwright import, no real `.env`, no Telegram bot; `pytest -q` stays fast and deterministic; `ruff check app tests` clean.

---

## 17. Sequence Diagrams

> The `rules.design` requirement for buy, sell, and webhook diagrams is satisfied below. Only the BUY tail changes; the sell and Cachicamo webhook flows are recorded as unchanged.

### (a) Happy path — login → pay → key → receipt → upload → release → COMPLETED

```mermaid
sequenceDiagram
    autonumber
    participant W as BuyFlow
    participant DB as DB
    participant B as BanescoBrowser
    participant BO as Banesco Online
    participant T as Telegram poller
    participant BI as Binance browser
    participant API as Binance C2C

    W->>DB: B2 commit SENDING_PAYMENT + VES/VES_PAY + payment_attempt_id
    W->>B: pay(attempt, target)
    B->>BO: open login; fill Usuario; click Aceptar
    BO-->>B: 2 of 4 security questions
    B->>B: normalize question text -> keys; resolve answers from SecretStr
    B->>BO: fill answers; submit; fill Clave; submit
    BO-->>B: authenticated area + ad modal
    B->>BO: dismiss modal; Pago Movil -> Enviar pago
    B->>BO: fill phone prefix/rest, ID letter/digits, bank, amount, concept
    B->>BO: Pagar (1st) -> confirmation -> Pagar (2nd)
    BO-->>B: Clave Dinamica form
    B->>DB: B3 commit challenge PENDING + nonce_hash
    B->>T: sendMessage challenge (reply-to)
    T-->>B: correlated 6-digit reply
    B->>DB: challenge ANSWERED
    B->>BO: submit key exactly once
    BO-->>B: receipt
    B->>DB: B4 receipt file + SHA-256 + OUTGOING + bank_reference + bank_confirmed_at
    W->>BI: notify_paid(reference, evidence_path) [upload + confirm]
    BI-->>W: reference bubble visible
    W->>DB: B5 commit AWAITING_RELEASE + release_deadline_at
    loop every RELEASE_POLL_INTERVAL_SECONDS
        W->>API: get_order_detail (read-only)
        API-->>W: released
    end
    W->>DB: B6 commit COMPLETED + RELEASE_DETECTED
```

### (b) Clave Dinámica timeout

```mermaid
sequenceDiagram
    autonumber
    participant W as BuyFlow
    participant B as BanescoBrowser
    participant DB as DB
    participant T as Telegram

    W->>B: submit 2nd Pagar
    B->>DB: B3 challenge PENDING (expires_at)
    B->>T: send challenge (reply-to, nonce)
    Note over B,T: no correlated 6-digit reply within TELEGRAM_CLAVE_TIMEOUT_SECONDS
    B->>DB: challenge EXPIRED
    B->>B: abort page; no key submitted
    B-->>W: ClaveDinamicaTimeout
    W->>DB: SENDING_PAYMENT -> MANUAL_REVIEW / clave_dinamica_timeout
    W->>T: informational notice (no new challenge, no resume)
```

### (c) Release timeout → appeal

```mermaid
sequenceDiagram
    autonumber
    participant RW as ReleaseWatcher
    participant API as Binance C2C
    participant DB as DB
    participant BI as Binance browser
    participant OP as Operator (Telegram)

    RW->>DB: select AWAITING_RELEASE past release_deadline_at
    RW->>API: get_order_detail (pre-click re-check)
    API-->>RW: still paid / not released
    alt RELEASE_APPEAL_MODE = auto
        RW->>DB: B7 commit appeal_attempt_id UNIQUE + APPEALED BEFORE click
        RW->>BI: file_appeal(order, receipt, reason)
        BI-->>RW: APPEALING / unknown
        RW->>DB: APPEALED (unknown -> appeal_status=unknown; operator verifies; no retry)
    else RELEASE_APPEAL_MODE = alert
        RW->>DB: AWAITING_RELEASE -> MANUAL_REVIEW / release_timeout
        RW->>OP: alert
    else RELEASE_APPEAL_MODE = off
        RW->>DB: leave AWAITING_RELEASE flagged; no appeal
    end
```

If the pre-click re-check observes a release, the watcher records `APPEAL_SKIPPED` and transitions to `COMPLETED` instead.

### (d) Restart mid-payment recovery

```mermaid
sequenceDiagram
    autonumber
    participant S as Startup
    participant DB as DB
    participant T as Telegram
    participant OP as Operator

    S->>DB: load PENDING operator_challenges
    S->>DB: mark PENDING -> EXPIRED
    S->>DB: find orders in SENDING_PAYMENT
    S->>DB: SENDING_PAYMENT -> MANUAL_REVIEW / payment_interrupted
    S->>T: informational notice (no challenge)
    Note over S,OP: never auto-resume a payment; operator verifies the bank state
    Note over S,DB: AWAITING_RELEASE and APPEALED are safe: the watcher resumes read-only polling only
```

### (e) Sell and Cachicamo webhook — unchanged

```mermaid
sequenceDiagram
    autonumber
    participant BIN as Binance
    participant SF as SellFlow
    participant CH as Cachicamo
    participant DB as DB

    BIN-->>SF: SELL order detected
    SF->>DB: PENDING
    CH-->>SF: signed webhook / verification
    SF->>DB: verify, release, COMPLETED
    Note over SF,DB: unchanged by this design; the Banesco rail and Telegram do not apply to Flow A
```

---

## 18. File Changes

| File | Action | Description |
|---|---|---|
| `app/domain/banesco.py` | Create | Pure constants, question normalization/validation, two-control DTO + projection, destination-bank map, amount rendering, page-text parsers |
| `app/domain/enums.py` | Modify | Rename `SENDING_SMS`; add `AWAITING_RELEASE`, `APPEALED`; transition graph; new `AuditEvent`/`AuditActor` members |
| `app/domain/ports.py` | Modify | `BanescoPaymentTarget`, `PaymentReceipt`, `PaymentAttempt`, `PaymentRail`, `OperatorNotifier`, `ReceiptStore`; extend `BinanceBrowser` |
| `app/domain/banks.py` | Modify | Extend `cedula_digits` prefix set to `V/E/J/G/P/R`; add two-control projection; add destination option data; keep `resolve_bank`/`format_ves_amount` |
| `app/domain/schemas.py` | Modify | Accept `R` in the ID pattern; operator response fields for `AWAITING_RELEASE`/`APPEALED` (no PII) |
| `app/infrastructure/clients/banesco_pages.py` | Create | Page objects, selector tables, screen-shape assertions |
| `app/infrastructure/clients/banesco_browser.py` | Create | Lazy Playwright driver composing the page objects; `pay()` |
| `app/infrastructure/clients/telegram_bot.py` | Create | Raw Bot API over `httpx`; long polling |
| `app/infrastructure/receipt_store.py` | Create | Restricted storage, SHA-256, dedupe/conflict rules, retention cleanup |
| `app/services/operator_challenges.py` | Create | Challenge persistence, state machine, correlation, timeout, restart expiry |
| `app/services/release_watcher.py` | Create | Watcher tick, status mapping, timeout policy |
| `app/services/appeal.py` | Create | Pre-click re-check, durable identity, click, outcome |
| `app/services/buy_flow.py` | Modify | Replace SMS tail with rail/receipt/evidence/release/appeal; tighten resume guards |
| `app/services/risk_policy.py` | Modify | `VES_SMS` → `VES_PAY`; preserve double cap/reserve/release |
| `app/infrastructure/models.py` | Modify | New nullable rail/receipt/release/appeal columns; `operator_challenges` table |
| `app/infrastructure/clients/binance_browser.py` | Modify | Add `upload_evidence`, extended `notify_paid`, `file_appeal` |
| `app/infrastructure/clients/binance_client.py` | Modify | Document/implement conservative release status mapping |
| `app/core/config.py` | Modify | New settings, validators, safe summary |
| `app/core/logging.py` | Modify | Mask security answers, `clave`, `R-` documents; never log receipt text/bytes |
| `app/core/exceptions.py` | Modify | `PaymentRailError` subtree and all spec codes |
| `app/api/v1/orders.py` | Modify | New outcomes; payment-related resume guard; appeal/reconcile action |
| `app/api/v1/health.py` | Modify | Banesco/Telegram probes when enabled |
| `app/api/v1/webhooks.py` | Modify | `/webhooks/sms` returns `410` then removed in S9; Cachicamo untouched |
| `app/main.py` | Modify | Telegram poller; release watcher; startup reconciliation; keep the BUY worker |
| `alembic/versions/20261125_0003_status_ledger_rename.py` | Create | Status + ledger data migration with downgrade |
| `alembic/versions/20261125_0004_banesco_rail_schema.py` | Create | New columns + `operator_challenges` |
| `tests/fixtures/banesco/*` | Create | Sanitized DOM + `bank_options.json` |
| `tests/test_banesco_*`, `test_telegram_*`, `test_operator_challenges.py`, `test_receipt_store.py`, `test_release_watcher.py`, `test_appeal.py` | Create | New seams |
| `tests/conftest.py`, `tests/test_config.py`, `tests/test_logging.py`, `tests/test_migrations.py`, SMS-era tests | Modify | New settings/fakes/fixtures; rename refs |
| `.env.example`, `README.md` | Modify | Placeholder-only settings; new statuses/rail description |
| `app/domain/bank_replies.py`, `app/services/sms_dispatch.py` | Delete (S9) | No BUY caller |
| `app/domain/banks.render_bank_command`, SMS webhook, `_run_sms_poller`, BuyFlow SMS methods | Delete (S9) | Decommission the SMS BUY path |

---

## 19. Data Flow

```
BinanceBrowser.extract_payment_instruction(order)
        │  PaymentInstruction (Decimal amount, phone, id_number, bank code)
        ▼
BuyFlow._extract
        │
        ├─ domain.banesco.build_banesco_target(
        │      instruction,
        │      live phone prefixes, live ID letters,
        │      destination option map, concept)
        │   └─ missing letter -> ID_TYPE challenge -> operator override
        │   └─ unmatched bank -> MANUAL_REVIEW / unsupported_destination_bank
        ▼
B2 commit: SENDING_PAYMENT + VES + VES_PAY + payment_attempt_id
        │
        ▼
BanescoBrowser.pay
  LoginPage → SecurityQuestionsPage → PasswordPage → AdModalPage
  → MenuPage → PagoMovilFormPage → ConfirmationPage → ClaveDinamicaPage
        ↕  OperatorNotifier (httpx Bot API + long poll)  ↔  operator_challenges
  → ReceiptPage → PaymentReceipt(path, sha256, reference, Decimal amount, paid_at)
        │
        ▼
ReceiptStore.persist → data/receipts/{order}/{sha256}.png
        │               + p2p_orders receipt metadata
        │               + OUTGOING BankTransaction + bank_reference + bank_confirmed_at
        ▼
BinanceBrowser.notify_paid(reference, evidence_path) [upload + confirm]
        │
        ▼
B5 commit: AWAITING_RELEASE + release_deadline_at
        │
        ▼
ReleaseWatcher (get_order_detail, read-only)
        ├─ released → COMPLETED
        ├─ APPEALING → APPEALED (no second appeal)
        ├─ cancelled/expired → MANUAL_REVIEW / external_order_cancelled
        ├─ unknown → MANUAL_REVIEW / external_status_unknown
        └─ deadline passed → RELEASE_APPEAL_MODE (auto/alert/off)
```

---

## 20. Interfaces / Contracts

### Ports (pseudocode)

```python
@dataclass(frozen=True, slots=True)
class PaymentAttempt:
    attempt_id: str          # "{order_number}:payment", UNIQUE
    order_number: str
    order_amount_ves: Decimal
    deadline_at: datetime

@dataclass(frozen=True, slots=True)
class PaymentReceipt:
    order_number: str
    bank_code: BankCode
    bank_reference: str
    amount_ves: Decimal
    paid_at: datetime
    screenshot_path: str
    screenshot_sha256: str
    raw_text: str            # in-memory only; never persisted/logged

class PaymentRail(Protocol):
    async def pay(self, attempt: PaymentAttempt,
                  target: BanescoPaymentTarget) -> PaymentReceipt: ...

class OperatorNotifier(Protocol):
    async def request_clave(self, order_number: str, attempt_id: str,
                            expires_at: datetime) -> str: ...
    async def request_id_type(self, order_number: str, attempt_id: str,
                              expires_at: datetime) -> str: ...
    async def notify(self, text: str) -> None: ...

class ReceiptStore(Protocol):
    async def persist(self, receipt: PaymentReceipt, image: bytes) -> StoredReceipt: ...

class BinanceBrowser(Protocol):
    async def extract_payment_instruction(self, order_number: str) -> PaymentInstruction: ...
    async def upload_evidence(self, order_number: str, evidence_path: str) -> None: ...
    async def notify_paid(self, order_number: str, reference: str, *,
                          evidence_path: str) -> None: ...
    async def file_appeal(self, order_number: str, evidence_path: str,
                          reason: str) -> AppealResult: ...
```

### Classification

```python
class PaymentFate(str, Enum):
    CAPTURED = "CAPTURED"   # receipt captured and verified
    REJECTED = "REJECTED"   # permanent pre-submission failure (release reservation)
    ABORTED  = "ABORTED"    # operator/restart abort (park, do not release)
    UNKNOWN  = "UNKNOWN"    # outcome unknown (park, never retry)
```

### Typed errors (codes only; all under `PaymentRailError`)

`banesco_login_failed`, `banesco_layout_unexpected`, `banesco_captcha_detected`, `banesco_account_locked`, `banesco_maintenance`, `banesco_session_expired`, `banesco_ad_modal_blocked`, `banesco_payment_rejected`, `banesco_payment_outcome_unknown`, `unsupported_phone_prefix`, `id_type_missing`, `id_type_unsupported`, `payment_data_unavailable`, `unsupported_destination_bank`, `destination_bank_ambiguous`, `clave_dinamica_timeout`, `clave_dinamica_rejected`, `receipt_amount_mismatch`, `receipt_conflict`, `receipt_unavailable`, `evidence_upload_failed`, `binance_close_failed`, `appeal_already_filed`, `release_timeout`.

---

## 21. Testing Strategy

| Layer | What to Test | Approach |
|---|---|---|
| Unit | Question normalization/2-of-4 validation; phone/document projection (`R`/`P`); amount rendering; DTO invariants; destination map 0/1/many; receipt digest; state machine graph; ledger double cap; challenge state machine | Pure pytest; no I/O |
| Integration | Page objects over recorded HTML; Banesco driver with fake page/context; BuyFlow tail with fake rail/notifier/browser; evidence upload ordering; release watcher sequences; appeal at-most-once; migrations up/down; Bot API via `respx`; ASGI endpoints | Fakes + fixtures + `respx` + `freezegun`; in-memory SQLite |
| E2E | None in CI. Opt-in live smoke (`LIQUIDVE_BROWSER_TESTS`, `LIQUIDVE_BANESCO_PAYMENT_TESTS`) only | `@pytest.mark.integration`; skipped by default |

Default-run invariant: no browser, no network, no real credentials, no Playwright import.

---

## 22. Threat Matrix

**N/A — no routing, shell command, user-controlled subprocess, VCS/PR automation, executable-file classification, or process-integration boundary is introduced.** Playwright's managed browser launch is internal to the adapter and is not a user-controlled process boundary; the only external actions are typed provider calls behind adapters under `app/infrastructure/clients`. The security-sensitive surface (bank credentials, Telegram token, receipt PII) is handled by `SecretStr`, log/audit masking, fail-closed errors, and the canary tests described above.

---

## 23. Migration / Rollout

1. **S1 first (autonomous):** enums/config/`VES_PAY`/pure mapper/alembic data migration/typed errors/tests. Flow A unchanged; no new runtime behavior.
2. **S2–S6:** pure page contracts, login adapter, form + clave handoff, receipt capture, Telegram + durable challenges. Each slice lands behind disabled switches.
3. **S7:** buy-flow rewiring + evidence upload; `BUY_PAYMENT_RAIL` transitional selector; durable `payment_attempt_id`; resume guard.
4. **S8:** release watcher + automatic appeal; `RELEASE_APPEAL_MODE` (`auto`/`alert`/`off`); pre-click re-check; at-most-once identity.
5. **S9:** decommission the SMS BUY path (webhook, dispatch, reply parsing, poller, health probe), keep Cachicamo; docs updated.
6. **S10:** operator/observability polish.

Rollback: set `BANESCO_RAIL_ENABLED=false`, `RELEASE_WATCHER_ENABLED=false`, `BUY_AUTO_PAY_ENABLED=false`, `RELEASE_APPEAL_MODE=off`; never re-enable the SMS rail at runtime (D7). Schema downgrade maps the new tail to `MANUAL_REVIEW` and reverses the status/ledger renames; historical audit payloads are not rewritten; no auto-resume.

Live-enablement gates (not design blockers): record real, sanitized DOM for every screen and the exact question aliases; confirm the destination option labels; confirm the Binance release/appeal status vocabulary and appeal reason label; confirm the bank idle timeout vs the Clave wait; confirm the amount input format.

---

## 24. Slice Budget (re-validated, 800 changed lines each)

| # | Slice | Forecast (changed lines) | Budget | Split plan if > 800 |
|---|---|---:|---|---|
| **S1** | **Status/ledger/config groundwork + pure Banesco mapper — FIRST AUTONOMOUS SLICE** | ~650 | OK | **S1a** rename + migration + enum/ledger (~350); **S1b** Banesco DTO/mapper + settings + errors + tests (~300) |
| S2 | Pure Banesco page contracts + fixtures | ~500 | OK | — |
| S3 | Login/questions/ad-modal adapter | ~550 | OK | — |
| S4 | Payment form + double `Pagar` + clave handoff | ~600 | OK | **S4a** form/confirmation (~350); **S4b** clave handoff (~250) if needed |
| S5 | Receipt capture + storage | ~450 | OK | — |
| S6 | Telegram adapter + durable challenges | ~500 | OK | — |
| S7 | Buy-flow rewiring + Binance evidence upload | ~700 | OK (near limit) | **S7a** buy-flow tail/durable attempt/resume guard (~400); **S7b** evidence upload/`AWAITING_RELEASE` (~300) |
| S8 | Release watcher + automatic appeal | ~650 | OK | **S8a** watcher/status mapping (~350); **S8b** appeal/at-most-once (~300) if needed |
| S9 | Decommission SMS BUY path | ~450 (deletion-heavy) | OK | **S9a** remove webhook/`sms_dispatch`/`bank_replies`; **S9b** remove poller/BuyFlow SMS methods/health/docs |
| S10 | Operator/observability polish | ~300 | OK | — |

Each slice begins with RED tests, then GREEN, then refactor. Each PR targets the previous slice until the diff is clean. S1 is the first autonomous slice; `sdd-tasks` must re-validate these counts and introduce the splits only when a slice actually exceeds 800 changed lines.

---

## 25. Open Questions

None blocking the design. The following are **live-enablement gates**, verified before turning the rail on, not design decisions:

- [ ] Sanitized real DOM snapshots and the exact normalized security-question aliases.
- [ ] Exact `Banco Destino` option labels/values for `0102`/`0134`.
- [ ] Binance release/appeal status vocabulary and the exact appeal reason label.
- [ ] Bank idle timeout relative to `TELEGRAM_CLAVE_TIMEOUT_SECONDS`.
- [ ] Amount input format (comma-decimal vs thousands-grouped) confirmed from the live field.

---

## Live DOM corrections (PR 3)

The PR 3 captures in `tests/fixtures/banesco/01_*.html` … `08_*.html` and `bank_options.json` are the authoritative sanitized DOM evidence. They force four corrections to the provisional contract:

1. **Spanish security-question aliases.** The live wording is Spanish: `02_security_questions.html` shows `hobbie` in `#lblPrimeraP` and `¿Qué país siempre has querido conocer?` in `#lblSegundaP`. `app/domain/banesco.py` now pins the normalized Spanish aliases (accent/case tolerant, same normalization pipeline) while retaining the English forms as harmless additional aliases. The four `QuestionKey` identities are unchanged.
2. **Real destination-bank map.** `#banco` in `06_pagomovil_form.html` exposes 25 options whose `value` is the four-digit code; `0102` is `BANCO DE VENEZUELA` and `0134` is `BANESCO BANCO UNIVERSAL S.A.C.A.`. The reviewed map is now version 2 with all 25 labels, consolidated into `tests/fixtures/banesco/bank_options.json`; the temporary `bank_options_live.json` is removed.
3. **Clave Dinámica length is 6–8 digits.** `08_clave_dinamica.html` pins `#otp` with `pattern="[0-9]{7,8}" maxlength="8"`, so an exactly-six-digit rule would reject valid keys. `CLAVE_DINAMICA_MIN_DIGITS`/`CLAVE_DINAMICA_MAX_DIGITS` and `validate_clave_dinamica` enforce 6–8 ASCII digits.
4. **Beneficiary name control exists.** `06_pagomovil_form.html` contains `#nom` (`maxlength="30"`, label “Nombre y Apellido del Beneficiario”) inside the `#nomb` group. It is filled from the order's counterparty name when available and is never invented, defaulted, or synthesized; with no counterparty name the control stays empty.

The frame contract is also confirmed: every login screen lives in the `ctl00_cp_frmaplicacion` iframe and the Pago Móvil form, confirmation and Clave Dinámica live in the `embedded` iframe; `aplicacion.aspx` is only the outer shell, so page objects target frames rather than the top-level page.

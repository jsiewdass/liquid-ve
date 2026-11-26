# Banesco recorded DOM fixture catalog

This directory holds the sanitized, recorded Banesco Online pages used by the
page-object tests. The fixture **names and the sanitization contract** are fixed
by PR 0 (task 0.2); the S2 slice materializes the actual files.

## Sanitization contract

- Every value is obviously fake: names, phones, cedulas, references, account
  numbers and amounts are synthetic test data only.
- No credential, security answer, token, real account, real phone, real cedula
  or real bank reference may ever appear in a fixture.
- Selectors stay pinned to accessible names, roles and labels; a live DOM
  change is a fixture change plus a test change.
- Fixtures contain markup only. No screenshot, no session cookie and no
  network response body is committed.

A fixture-hygiene canary scans this directory and fails if it finds a known
canary secret or real-looking PII.

## Catalog

| Fixture | Purpose |
|---|---|
| `login.html` | Login entry screen |
| `security_questions_a.html` | First two-of-four question subset and order |
| `security_questions_b.html` | A different two-of-four subset and order |
| `ad_modal.html` | Promotional modal shown after login |
| `payment_form.html` | Pago Movil form with phone prefixes, ID letters and bank options |
| `confirmation.html` | Confirmation screen required before the second `Pagar` |
| `clave_form.html` | Clave Dinamica handoff form |
| `receipt.html` | Payment receipt with reference, amount and timestamp |
| `error_captcha.html` | Captcha / anti-bot detection page |
| `error_locked.html` | Locked-account page |
| `error_maintenance.html` | Maintenance page |
| `error_session_expired.html` | Expired-session page |
| `bank_options.json` | Sanitized destination-bank option snapshot |

The Python mirror of this list is `BANESCO_FIXTURE_CATALOG` in
`tests/conftest.py`; a test asserts both stay in sync.

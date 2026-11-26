# banesco-rail Specification — New Capability

## Purpose

Outgoing Pago Móvil execution through Banesco Online: authenticated web session, randomized security-question login, two-control payment-form mapping, two-step confirmation, Clave Dinámica operator handoff, receipt capture, and a fail-closed error taxonomy. This capability moves real money, so every requirement that submits, confirms, or evidences a payment declares its idempotency and audit contract.

## Capability Invariants

- Credentials and security answers MUST come only from settings (`BANESCO_ONLINE_USER`, `BANESCO_ONLINE_PASSWORD`, `BANESCO_SECURITY_ANSWER_<KEY>`); their values MUST NEVER be logged, echoed, persisted, or placed in any artifact, fixture, audit payload, or database row.
- No payment MUST be submitted unless a well-formed Clave Dinámica correlated to that order and attempt has been received and accepted.
- A receipt MUST be captured for every payment the rail submits; a submission without a captured receipt is an unknown outcome, never a success.
- Every unrecognized state (captcha, unexpected layout, session expiry, lockout, maintenance, DOM drift, timeout, ambiguous input) MUST fail closed; the rail MUST NOT guess, default, fuzzy-match, or retry a payment.

## ADDED Requirements

### Requirement: Credential and secret isolation

- **ID**: R-BANESCO-001

The rail MUST read every credential and security answer exclusively from settings exposed as secret values, and MUST NOT write any credential value to logs, audit payloads, receipt metadata, database rows, or artifacts. Documentation and error messages MAY reference only environment-variable names.

**Idempotency**: Read-only configuration access; repeated reads yield the same resolved settings.
**Audit**: No secret value is ever audited; a configuration failure emits a typed error naming variables only.

#### Scenario: Secrets never render

- GIVEN a configured rail and a rendered log line plus an audit payload
- WHEN both are inspected after a login and a payment attempt
- THEN no credential or security-answer value appears in either

#### Scenario: Missing secret blocks the session

- GIVEN a required credential setting is empty while the rail is enabled
- WHEN a session is requested
- THEN the rail fails before opening a browser session and names the missing variable only

### Requirement: Login entry

- **ID**: R-BANESCO-002

The rail MUST open the configured login page, enter the configured user identifier, and submit the entry step before proceeding. A missing user field, rejected identifier, or unexpected post-submit state MUST fail closed with `banesco_layout_unexpected` or `banesco_login_failed`.

**Idempotency**: At most one login entry submission per session; a failed login is not retried within the same attempt.
**Audit**: One authentication event per attempt recording the outcome and no user-identifying value.

#### Scenario: User entry advances the login

- GIVEN a reachable login page exposing the user field
- WHEN the configured user identifier is submitted
- THEN the rail advances to the security-question step

#### Scenario: Missing user field fails closed

- GIVEN a login page without a detectable user field
- WHEN login starts
- THEN the rail fails with `banesco_layout_unexpected` and no session proceeds

### Requirement: Randomized security-question challenge

- **ID**: R-BANESCO-003

The rail MUST read the displayed security-question texts, MUST require exactly two distinct questions recognized from the configured set of four, MUST resolve each answer from settings by normalized question identity, and MUST submit the resolved answers. Fewer than two, more than two, a repeated question, an unrecognized question, or an unresolvable answer MUST fail closed with `banesco_layout_unexpected` without a login. The rail MUST NOT assume question order or position and MUST NOT use a default answer.

**Idempotency**: One question challenge per session; answers are never persisted or reused across sessions.
**Audit**: One challenge event recording recognized question keys only, never answers.

#### Scenario: Two recognized questions are answered

- GIVEN a login challenge displaying two recognized questions from the configured set
- WHEN the rail resolves and submits both answers
- THEN the challenge advances and no answer value is recorded anywhere

#### Scenario: Unknown question fails closed

- GIVEN a challenge containing a question text that maps to no configured answer
- WHEN the rail evaluates the challenge
- THEN it fails with `banesco_layout_unexpected` and no answers are submitted

#### Scenario: Repeated or wrong-count questions fail closed

- GIVEN a challenge showing the same question twice or a count other than two
- WHEN the rail evaluates the challenge
- THEN it fails closed and the session does not log in

### Requirement: Online banking clave submission

- **ID**: R-BANESCO-004

The rail MUST submit the configured online-banking clave exactly once per session and MUST NOT log, echo, or persist it. A rejected or unparseable clave outcome MUST fail closed with `banesco_login_failed` and MUST NOT be retried automatically.

**Idempotency**: At most one clave submission per session; a failed submission ends the attempt.
**Audit**: One authentication outcome event with no secret value.

#### Scenario: Clave advances to the authenticated area

- GIVEN accepted security answers and a configured clave
- WHEN the clave is submitted
- THEN the rail reaches the authenticated area

#### Scenario: Rejected clave is not retried

- GIVEN a clave rejected by the bank
- WHEN the submission returns
- THEN the rail fails with `banesco_login_failed` and makes no second submission

### Requirement: Advertisement modal dismissal

- **ID**: R-BANESCO-005

When an advertisement or overlay is detected after authentication, the rail MUST dismiss it through its own close control before navigating. If a blocking modal is present but cannot be dismissed, or it prevents reaching the payment menu, the rail MUST fail closed with `banesco_ad_modal_blocked`. An absent modal MUST NOT be treated as an error.

**Idempotency**: Dismissal is attempted at most once per detected modal; absence is a no-op.
**Audit**: One dismissal event when a modal was handled; none when absent.

#### Scenario: Modal is dismissed

- GIVEN an authenticated session showing a closable advertisement modal
- WHEN the rail dismisses it
- THEN the payment menu becomes reachable

#### Scenario: Undismissable modal fails closed

- GIVEN a blocking modal whose close control cannot be found
- WHEN the rail attempts navigation
- THEN it fails with `banesco_ad_modal_blocked` and submits no payment

### Requirement: Pago Móvil navigation

- **ID**: R-BANESCO-006

From the authenticated area the rail MUST navigate `Pago Móvil` then `Enviar pago` and MUST wait for the payment form. A missing menu entry, missing form, or navigation to a different payment product MUST fail closed with `banesco_layout_unexpected`; the rail MUST NOT substitute another transfer product.

**Idempotency**: One navigation per payment attempt.
**Audit**: One navigation event naming the product and attempt identifier.

#### Scenario: Navigation reaches the form

- GIVEN an authenticated area exposing the Pago Móvil menu
- WHEN the rail selects `Enviar pago`
- THEN the Pago Móvil payment form is ready for input

#### Scenario: Missing form fails closed

- GIVEN an authenticated area without a reachable `Enviar pago` entry
- WHEN the rail attempts navigation
- THEN it fails with `banesco_layout_unexpected` and no form is submitted

### Requirement: Beneficiary phone two-control mapping

- **ID**: R-BANESCO-007

The rail MUST enter the beneficiary phone as two controls: a prefix selected from the options the form offers and the remaining national digits typed into the second control. The prefix MUST be exactly the first four normalized national digits and MUST match an offered option; the remainder MUST be exactly seven digits. An unoffered prefix, a remainder of the wrong length, or a form without the two-control structure MUST fail closed with `unsupported_phone_prefix` or `payment_data_unavailable`. The rail MUST NOT type a full phone into a single control and MUST NOT choose a prefix by guess.

**Idempotency**: The mapping is deterministic for identical normalized input; no partial form is submitted on failure.
**Audit**: One payment-target event with a masked phone and the attempt identifier.

#### Scenario: Phone splits across both controls

- GIVEN the normalized beneficiary phone `04141234567` and a form offering prefix `0414`
- WHEN the rail fills the phone controls
- THEN the prefix control is `0414` and the remainder control is `1234567`

#### Scenario: Unsupported prefix fails closed

- GIVEN a normalized phone whose first four digits are not an offered prefix
- WHEN the rail fills the phone controls
- THEN it fails with `unsupported_phone_prefix` and does not submit

### Requirement: Document identity two-control mapping

- **ID**: R-BANESCO-008

The rail MUST enter the counterparty document as a letter selected from the offered `V`, `E`, `J`, `G`, `R` options plus bare document digits in a second control. A missing or ambiguous letter, a letter outside the offered set (including `P`), or a non-numeric digit control MUST fail closed with `id_type_missing`, `id_type_unsupported`, or `payment_data_unavailable`. The rail MUST NOT default the letter to `V`.

**Idempotency**: Deterministic mapping; a rejected document never yields a submitted payment.
**Audit**: One payment-target event with masked document and the attempt identifier.

#### Scenario: Letter and digits map to the form

- GIVEN document `V-12345678` and a form offering the letter `V`
- WHEN the rail fills the document controls
- THEN the letter control is `V` and the digit control is `12345678`

#### Scenario: Missing letter fails closed

- GIVEN an instruction without a document letter
- WHEN the rail maps the document
- THEN it fails with `id_type_missing` and no payment is submitted

#### Scenario: Unsupported letter is rejected

- GIVEN document `P-12345678`
- WHEN the rail maps the document for Banesco
- THEN it fails with `id_type_unsupported` and never substitutes another letter

### Requirement: Destination bank option mapping

- **ID**: R-BANESCO-009

The rail MUST select exactly one `Banco Destino` option that maps the order's supported four-digit numeric code through the reviewed versioned option map. Zero matches, multiple matches, or a changed option label/value outside the map MUST fail closed with `unsupported_destination_bank` or `destination_bank_ambiguous`. The rail MUST NOT fuzzy-match, select by position, or substitute a nearest bank.

**Idempotency**: Pure lookup against a fixed map; identical code yields the same option or a typed failure.
**Audit**: One destination-bank event naming the numeric code and the attempt identifier.

#### Scenario: Supported code selects its option

- GIVEN numeric code `0102` present exactly once in the reviewed option map
- WHEN the rail selects the destination bank
- THEN the matching option is selected

#### Scenario: Ambiguous option fails closed

- GIVEN a numeric code matching two options in the map
- WHEN the rail selects the destination bank
- THEN it fails with `destination_bank_ambiguous` and does not submit

#### Scenario: Unknown option fails closed

- GIVEN a numeric code absent from the reviewed option map
- WHEN the rail selects the destination bank
- THEN it fails with `unsupported_destination_bank` and does not submit

### Requirement: Amount, concept and beneficiary name mapping

- **ID**: R-BANESCO-010

The rail MUST enter the order's `Decimal` VES amount rendered in the form's accepted Venezuelan format and the configured neutral concept text. When the form presents the optional beneficiary name control (`#nom`, maximum 30 characters), the rail MUST fill it from the order's counterparty name when that name is available and MUST NOT invent, default, or synthesize a name; with no counterparty name the control MUST remain empty. A non-positive amount, a float amount, an unrenderable amount, or an absent concept MUST fail closed with `payment_data_unavailable` and MUST NOT submit.

**Idempotency**: Deterministic rendering; no partial submission on failure.
**Audit**: One amount event with the amount and attempt identifier; no counterparty PII.

#### Scenario: Decimal amount is entered

- GIVEN `Decimal("1500.50")` and a configured concept
- WHEN the rail fills the form
- THEN the amount control carries `1500,50` and the concept control carries the configured text

#### Scenario: Non-positive amount fails closed

- GIVEN an amount of `Decimal("0")`
- WHEN the rail fills the form
- THEN it fails with `payment_data_unavailable` and no submit occurs

#### Scenario: Beneficiary name comes from the order

- GIVEN an order with a counterparty name and a form exposing the `#nom` control
- WHEN the rail fills the form
- THEN the name control carries the order's counterparty name and no synthetic name is used

### Requirement: Two-step payment confirmation

- **ID**: R-BANESCO-011

The rail MUST perform the first submit, MUST wait for and observe the confirmation screen, and MUST perform the second confirmation submit before a payment is considered initiated. The first submit alone MUST NOT be treated as final. If the confirmation screen is absent or unexpected, the rail MUST fail closed with `banesco_layout_unexpected` and MUST NOT perform the second submit.

**Idempotency**: Exactly one pair of submits per payment attempt; the second submit MUST NOT execute unless the confirmation screen was observed.
**Audit**: One event per submit with the attempt identifier and step label, containing no payment data.

#### Scenario: Both submits occur in order

- GIVEN a filled payment form and an observed confirmation screen
- WHEN the rail confirms
- THEN the first submit is followed by exactly one second submit

#### Scenario: Missing confirmation prevents the second submit

- GIVEN a first submit that returns no confirmation screen
- WHEN the rail looks for the confirmation
- THEN it fails with `banesco_layout_unexpected` and makes no second submit

### Requirement: Clave Dinámica handoff

- **ID**: R-BANESCO-012

After the second confirmation the rail MUST pause and request the Clave Dinámica through the operator confirmation provider, correlated to this order and this attempt. It MUST accept only a response that is six to eight ASCII digits, originates from the configured operator channel, is a reply to the challenge it sent, and carries the matching one-time nonce; it MUST submit the accepted key exactly once. A malformed, uncorrelated, ambiguous, expired, or rejected key MUST fail closed with `clave_dinamica_timeout` or `clave_dinamica_rejected`. The rail MUST NOT store, echo, log, or guess the key, and MUST NOT auto-resume a payment across a restart.

**Idempotency**: At most one key acceptance and one key submission per attempt; an accepted key is never reused; timeout parks the attempt and restart never resumes it.
**Audit**: Challenge-sent, challenge-answered, and challenge-rejected events with the challenge identifier only, never the key.

#### Scenario: Correlated key is submitted once

- GIVEN a pending challenge correlated by operator channel, reply target, order, and nonce
- WHEN the operator replies with six to eight digits
- THEN the rail accepts the key and submits it exactly once

#### Scenario: Timeout fails closed

- GIVEN a pending challenge with no accepted reply within the configured window
- WHEN the window expires
- THEN the rail fails with `clave_dinamica_timeout` and no key is submitted

#### Scenario: Malformed or uncorrelated reply is refused

- GIVEN a reply with fewer than six or more than eight digits, from a non-operator channel, or not replying to the challenge
- WHEN the rail evaluates it
- THEN the key is not accepted and no submission occurs

### Requirement: Receipt capture and evidence metadata

- **ID**: R-BANESCO-013

On a successful submission the rail MUST capture a receipt image, parse the reference, amount, and bank timestamp, and persist receipt evidence metadata: screenshot path, SHA-256 digest, reference, `Decimal` amount, and paid-at timestamp. It MUST compare the parsed amount to the expected amount and MUST fail closed with `receipt_amount_mismatch` on mismatch. A submission with no receipt MUST be treated as `banesco_payment_outcome_unknown`. Raw image bytes and raw receipt text MUST NOT be stored in the database or logged.

**Idempotency**: The digest is computed before persistence; recapturing an identical receipt is a no-op; a different digest for an existing order parks `receipt_conflict` and never overwrites.
**Audit**: One receipt-captured event with the digest and a masked reference only.

#### Scenario: Receipt is captured and verified

- GIVEN a successful submission showing a receipt
- WHEN the rail captures it
- THEN the path, SHA-256, reference, amount, and timestamp are persisted and the amount matches the expected `Decimal`

#### Scenario: Amount mismatch is refused

- GIVEN a receipt whose parsed amount differs from the expected amount
- WHEN the rail verifies it
- THEN it fails with `receipt_amount_mismatch` and the payment is not marked confirmed

#### Scenario: Missing receipt is unknown

- GIVEN a submission after which no receipt is available
- WHEN the rail finalizes the outcome
- THEN the outcome is `banesco_payment_outcome_unknown`, never success

### Requirement: Fail-closed error taxonomy

- **ID**: R-BANESCO-014

The rail MUST detect and fail closed on at least: captcha or anti-bot challenge (`banesco_captcha_detected`), unexpected layout or DOM drift (`banesco_layout_unexpected`), session expiry (`banesco_session_expired`), locked account (`banesco_account_locked`), and maintenance (`banesco_maintenance`). After the final submit, any unexpected or unknown state MUST be `banesco_payment_outcome_unknown` and MUST NOT be re-clicked. The rail MUST NOT retry a login or a payment automatically and MUST NOT attempt to bypass a captcha.

**Idempotency**: Detection is repeatable without side effects; a detected failure ends the attempt and no retry is scheduled.
**Audit**: One typed failure event per attempt with the classification reason.

#### Scenario: Captcha parks the attempt

- GIVEN a login page presenting a captcha challenge
- WHEN the rail detects it
- THEN it fails with `banesco_captcha_detected` and no bypass is attempted

#### Scenario: Session expiry after submission is unknown

- GIVEN a session that expires after the final submit
- WHEN the rail cannot read a receipt
- THEN the outcome is `banesco_payment_outcome_unknown` and no re-click occurs

#### Scenario: DOM drift fails closed

- GIVEN a payment form whose expected field labels changed
- WHEN the rail attempts to map the form
- THEN it fails with `banesco_layout_unexpected` and submits nothing

### Requirement: At-most-once payment submission

- **ID**: R-BANESCO-015

The rail MUST accept a durable attempt identity from its caller, MUST refuse a second invocation for an already-used attempt identity, and MUST NOT perform any browser action before that identity is durably recorded. An unknown outcome MUST NOT be retried; only an operator may verify the bank state.

**Idempotency**: Attempt identity is UNIQUE; a repeated invocation is refused with a typed already-attempted error and opens no browser session.
**Audit**: One invocation event with the attempt identifier; every refusal is audited with no payment action.

#### Scenario: First invocation proceeds

- GIVEN an unused attempt identity
- WHEN the rail is invoked
- THEN one browser session starts and the attempt is recorded

#### Scenario: Repeat invocation is refused

- GIVEN an attempt identity already recorded
- WHEN the rail is invoked again
- THEN a typed already-attempted error is returned and no browser session starts

#### Scenario: Restart never resumes a payment

- GIVEN a process stop during an in-flight payment
- WHEN the rail starts again
- THEN it does not resume the payment and leaves the order for operator verification

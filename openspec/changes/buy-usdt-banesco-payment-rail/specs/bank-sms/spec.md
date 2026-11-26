# Delta for bank-sms

## Disposition Summary

The capability is retained but narrowed to pure bank mapping and normalization. The bank catalog is still required because the Banesco rail maps the destination bank and normalizes counterparty data. SMS command rendering and inbound-reply parsing are decommissioned with the SMS BUY path.

| Requirement | Disposition | Note |
|---|---|---|
| R-BANK-001 Declarative bank SMS profile | MODIFIED | Profile keeps identity, numeric code, `cedula_format`; SMS routing/reply fields are dropped |
| R-BANK-002 Supported bank registry | MODIFIED | Bank catalog remains; short-code column is dropped |
| R-BANK-003 Bank resolution semantics | UNCHANGED | `resolve_bank` remains valid and is reused by the rail |
| R-BANK-004 Venezuelan amount formatting | UNCHANGED | `format_ves_amount` remains valid and is reused by the rail |
| R-BANK-005 National phone and cedula normalization | MODIFIED | Also yields the two-control split used by the Banesco form |
| R-BANK-006 Exact command rendering | REMOVED | No SMS command is rendered |
| R-BANK-007 Bank reply parsing contract | REMOVED | No inbound bank reply is parsed |

## MODIFIED Requirements

### Requirement: Declarative bank SMS profile

- **ID**: R-BANK-001

Each supported bank MUST be one immutable profile holding bank identity, a four-digit `numeric_code`, an optional `cedula_format`, and the exact destination-bank option entry used by the payment rail. The profile MUST NOT carry short codes, command templates, or success/failure reply patterns; those belonged to the decommissioned SMS rail and MUST NOT be reintroduced. Profiles MUST be data, not branching code.
(Previously: the profile also carried ordered short codes, a command template, and success/failure reply patterns.)

**Idempotency**: Pure lookup; identical input yields identical values.
**Audit**: None at this layer (structural data only).

#### Scenario: Profile carries only mapping data

- GIVEN a resolved profile
- WHEN its fields are read
- THEN `numeric_code` is four digits, the destination option entry is present, and no short code, command template, or reply pattern exists

#### Scenario: Profile is pure data

- GIVEN a profile whose destination option entry is updated in the map
- WHEN the rail reads it
- THEN the new entry applies with no code-path branching

### Requirement: Supported bank registry

- **ID**: R-BANK-002

The registry, keyed by numeric code, MUST contain at least these banks and MUST treat every other bank as unsupported:

| Bank | Numeric code |
|---|---|
| Banco de Venezuela | `0102` |
| Banesco | `0134` |

The registry MUST NOT expose short codes or SMS routing metadata.

**Idempotency**: Pure registry reads; the table MUST NOT be mutated at runtime.
**Audit**: None at this layer.

#### Scenario: Shipped banks resolve

- GIVEN the shipped registry
- WHEN `0102` and `0134` are resolved
- THEN Banco de Venezuela and Banesco profiles are returned

#### Scenario: Unsupported bank is absent

- GIVEN the known-but-unsupported code `0105`
- WHEN the registry is queried
- THEN no profile is returned and no substitute bank is produced

### Requirement: National phone and cedula normalization

- **ID**: R-BANK-005

The system MUST normalize a phone to national digits (`0` plus ten digits) and a document identifier to bare digits with any `V`/`E`/`J`/`G`/`P`/`R` prefix or separator removed, then apply the profile's optional `cedula_format` when present. It MUST also expose a deterministic two-control projection for the payment rail: the phone prefix (first four national digits) plus the remaining exactly seven digits, and the document letter plus the bare digits. The letter set accepted by the Banesco rail MUST be `V`, `E`, `J`, `G`, `R`; `P` MUST normalize but MUST NOT be accepted by the rail. Non-normalizable input MUST raise a typed error.
(Previously: produced a single phone string and a single cedula string, and accepted `P` but not `R`.)

**Idempotency**: Pure; an already-normalized value MUST be returned unchanged, and repeated projections MUST be identical.
**Audit**: None at this layer.

#### Scenario: E.164 phone becomes national digits

- GIVEN the phone `+584141234567`
- WHEN it is normalized
- THEN the result is exactly `04141234567`

#### Scenario: Phone projects to two controls

- GIVEN the phone `+584141234567`
- WHEN the two-control projection is requested
- THEN the prefix is `0414` and the remainder is `1234567`

#### Scenario: Cedula digits and optional prefix

- GIVEN the cedula `V-12345678`, first without and then with a profile `cedula_format` of `V-{cedula}`
- WHEN each is normalized for a command
- THEN the results are `12345678` and `V-12345678` respectively

#### Scenario: R prefix normalizes and projects

- GIVEN the document `R-12345678`
- WHEN it is normalized and projected
- THEN the normalized digits are `12345678` and the projected letter is `R`

#### Scenario: P prefix normalizes but is rejected by the rail

- GIVEN the document `P-12345678`
- WHEN it is normalized
- THEN normalization succeeds with digits `12345678`, while the rail projection rejects the letter as unsupported

## REMOVED Requirements

### Requirement: Exact command rendering

- **ID**: R-BANK-006

(Reason: BUY no longer sends a Pago Móvil command to a bank short code; the Banesco rail fills the payment form directly — R-BANESCO-007 through R-BANESCO-011.)
(Migration: `render_bank_command` and its callers are removed with the SMS BUY path; no runtime fallback renders a command.)

### Requirement: Bank reply parsing contract

- **ID**: R-BANK-007

(Reason: BUY no longer receives an inbound bank SMS; a payment is confirmed from the captured receipt — R-BANESCO-013 and R-BUY-008.)
(Migration: `parse_bank_reply` and the reply pattern data are removed with the SMS BUY path; any remaining caller must be deleted, not stubbed.)

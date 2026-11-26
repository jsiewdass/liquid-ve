# bank-sms Specification

## Purpose

Pure, I/O-free capability: turn a supported bank plus a counterparty's payment data into one exact Pago Móvil command, and turn a bank reply into a reference or a typed failure. The only consumer is `buy-flow`.

Invariants: money is `Decimal` only (floats forbidden); every function is deterministic; rendering MUST NOT silently substitute a missing component; an unsupported bank or unparseable reply yields an explicit no-answer, never a guess; this layer performs no I/O and emits no audit events.

## Requirements

### Requirement: Declarative bank SMS profile

- **ID**: R-BANK-001

Each supported bank MUST be one immutable `BankSmsProfile` holding bank identity, 4-digit `numeric_code`, ordered `short_codes` (primary first), `command_template`, at least one success pattern, zero or more failure patterns, and an optional `cedula_format`. Profiles MUST be data, not branching code.

**Idempotency**: Pure lookup; identical input yields identical values.
**Audit**: None at this layer (structural data only).

#### Scenario: Profile carries routing data

- GIVEN a resolved profile
- WHEN its fields are read
- THEN `numeric_code` is four digits, `short_codes` is non-empty and ordered, and the template names every required field

#### Scenario: Wording edits need no code change

- GIVEN a profile whose failure patterns are edited
- WHEN a reply is parsed with it
- THEN the new patterns apply with no parser change

### Requirement: Supported bank registry

- **ID**: R-BANK-002

The registry, keyed by numeric code, MUST contain at least these banks and MUST treat every other bank as unsupported:

| Bank | Numeric code | Short codes (ordered) |
|---|---|---|
| Banco de Venezuela | `0102` | `2661`, `2662` |
| Banesco | `0134` | `2846` |

**Idempotency**: Pure registry reads; the table MUST NOT be mutated at runtime.
**Audit**: None at this layer.

#### Scenario: Shipped banks resolve to primary short codes

- GIVEN the shipped registry
- WHEN `0102` and `0134` are resolved
- THEN their first short codes are `2661` and `2846` respectively

### Requirement: Bank resolution semantics

- **ID**: R-BANK-003

`resolve_bank` MUST accept a trimmed 4-digit code, a bank member name, or a free-form alias and return the matching profile, and MUST return `None` for unknown, unsupported, empty, or ambiguous input. It MUST NOT fall back to `OTHER`, a nearest bank, or any default.

**Idempotency**: Pure; the same input always returns the same profile or `None`.
**Audit**: None here; refusal audit belongs to `buy-flow`.

#### Scenario: Code and alias both resolve

- GIVEN codes ` 0102 ` and alias `banesco`
- WHEN each is resolved
- THEN Banco de Venezuela and Banesco are returned

#### Scenario: Unsupported bank yields no profile

- GIVEN the known-but-unsupported code `0105`
- WHEN it is resolved
- THEN the result is `None` and no substitute bank is produced

### Requirement: Venezuelan amount formatting

- **ID**: R-BANK-004

`format_ves_amount` MUST render a `Decimal` with a comma decimal, exactly two decimals, no thousands separator, and no currency symbol, quantized half-up. A float or non-positive amount MUST raise a typed error.

**Idempotency**: Pure and total over valid input; identical `Decimal` yields identical bytes.
**Audit**: None at this layer.

#### Scenario: Amounts match Venezuelan formatting

- GIVEN `Decimal("1500")`, `Decimal("1500.5")` and `Decimal("25000.00")`
- WHEN each is formatted
- THEN the outputs are `1500,00`, `1500,50` and `25000,00`

#### Scenario: Float input is refused

- GIVEN the float `1500.0`
- WHEN it is formatted
- THEN a typed error is raised and no string is produced

### Requirement: National phone and cedula normalization

- **ID**: R-BANK-005

The system MUST normalize a phone to national digits (`0` plus ten digits) and a cedula to bare digits with any `V-`/`E-`/`J-` prefix removed, then apply the profile's optional `cedula_format` when present. Non-normalizable input MUST raise a typed error.

**Idempotency**: Pure; an already-normalized value MUST be returned unchanged.
**Audit**: None at this layer.

#### Scenario: E.164 phone becomes national digits

- GIVEN the phone `+584141234567`
- WHEN it is normalized
- THEN the result is exactly `04141234567`

#### Scenario: Cedula digits and optional prefix

- GIVEN the cedula `V-12345678`, first without and then with a profile `cedula_format` of `V-{cedula}`
- WHEN each is normalized for a command
- THEN the results are `12345678` and `V-12345678` respectively

### Requirement: Exact command rendering

- **ID**: R-BANK-006

`render_bank_command` MUST emit exactly `Pagar <numeric_code> <phone_national> <cedula_digits> <amount_ve>`, joined by single spaces with no leading, trailing, or repeated whitespace. If any component is missing or invalid it MUST raise a typed error and MUST NOT emit a partial command.

**Idempotency**: Pure; identical inputs render byte-identical text.
**Audit**: None here; the dispatched command is audited by `buy-flow` (R-BUY-006).

#### Scenario: Command matches the product example

- GIVEN Banco de Venezuela, phone `04141234567`, cedula `12345678`, amount `Decimal("1500")`
- WHEN rendered
- THEN the output equals `Pagar 0102 04141234567 12345678 1500,00`

#### Scenario: Missing cedula renders nothing

- GIVEN an instruction without a cedula
- WHEN rendering is attempted
- THEN a typed error is raised and no command string is returned

### Requirement: Bank reply parsing contract

- **ID**: R-BANK-007

Parsing MUST yield exactly one of: success with a single reference, failure with a typed reason, unparseable, or ambiguous. Success requires exactly one reference match; a body that also matches a failure pattern, or yields multiple references, MUST be ambiguous. Unparseable and ambiguous outcomes MUST NOT confirm and MUST NOT invent a reference.

**Idempotency**: Pure; replaying the same body yields the same outcome and reference.
**Audit**: None here; the outcome is audited by `buy-flow` (R-BUY-007).

#### Scenario: Success yields one reference

- GIVEN a reply containing one `Referencia: 123456789`
- WHEN parsed with the matching profile
- THEN the outcome is success with reference `123456789`

#### Scenario: Failure yields a typed reason

- GIVEN a reply matching a failure pattern
- WHEN parsed
- THEN the outcome is failure with a typed reason and no reference

#### Scenario: Ambiguous reply confirms nothing

- GIVEN a body matching both a success and a failure pattern
- WHEN parsed
- THEN the outcome is ambiguous and no confirmation is produced

#### Scenario: Unknown wording confirms nothing

- GIVEN a body matching no pattern
- WHEN parsed
- THEN the outcome is unparseable and no confirmation is produced

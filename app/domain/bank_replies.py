"""Pure, four-valued parser for inbound bank confirmation SMS.

The parser never guesses: an unknown wording is ``UNPARSEABLE``, a reply that
matches both a success and a failure vocabulary (or carries more than one
reference) is ``AMBIGUOUS``, and a ``SUCCESS`` always carries exactly one
reference taken verbatim from the body.

Success and failure patterns are *data*: they live on
:class:`~app.domain.banks.BankSmsProfile`, so changing a bank's wording is a
profile edit and never a code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterator

from app.core.exceptions import BankSmsParseError
from app.domain.banks import BankSmsProfile, parse_ves_amount

__all__ = [
    "BankReplyOutcome",
    "BankReplyFailureReason",
    "BankReplyParse",
    "parse_bank_reply",
]


class BankReplyOutcome(str, Enum):
    """Exhaustive result of parsing one bank reply."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    UNPARSEABLE = "UNPARSEABLE"
    AMBIGUOUS = "AMBIGUOUS"


class BankReplyFailureReason(str, Enum):
    """Machine-readable reason attached to a ``FAILURE`` outcome."""

    BANK_REJECTED_PAYMENT = "bank_rejected_payment"


@dataclass(frozen=True, slots=True)
class BankReplyParse:
    """Outcome of parsing one bank reply.

    Attributes:
        outcome: The four-valued classification.
        reference: Bank reference for a ``SUCCESS``; always ``None`` otherwise
            and never invented.
        failure_reason: Typed reason for a ``FAILURE``; ``None`` otherwise.
        amount: Paid amount when the reply exposes one and the profile pattern
            captures it; ``None`` when the caller must use a baseline amount.
    """

    outcome: BankReplyOutcome
    reference: str | None = None
    failure_reason: str | None = None
    amount: Decimal | None = None


def _iter_matches(body: str, patterns: tuple[str, ...]) -> Iterator[re.Match[str]]:
    """Yield every match of every pattern against ``body``.

    Args:
        body: Reply text.
        patterns: Regular expressions from the bank profile.

    Yields:
        One match per occurrence, in pattern order.
    """
    for pattern in patterns:
        yield from re.finditer(pattern, body, re.IGNORECASE)


def parse_bank_reply(body: str, profile: BankSmsProfile) -> BankReplyParse:
    """Classify an inbound bank reply against a bank profile.

    Args:
        body: Raw SMS body as received from the bank.
        profile: Bank whose success and failure patterns apply.

    Returns:
        A :class:`BankReplyParse` carrying exactly one of the four outcomes.
        ``SUCCESS`` requires exactly one reference; a reply matching both
        vocabularies, or one that exposes several references, is
        ``AMBIGUOUS``.  Any wording no pattern recognizes is ``UNPARSEABLE``.
    """
    if not isinstance(body, str) or not body.strip():
        return BankReplyParse(BankReplyOutcome.UNPARSEABLE)

    references: list[str] = []
    success_hits = 0
    amount: Decimal | None = None
    for match in _iter_matches(body, profile.success_patterns):
        success_hits += 1
        groups = match.groupdict()
        reference = (groups.get("reference") or "").strip()
        if reference and reference not in references:
            references.append(reference)
        raw_amount = (groups.get("amount") or "").strip()
        if raw_amount and amount is None:
            try:
                amount = parse_ves_amount(raw_amount)
            except BankSmsParseError:
                return BankReplyParse(BankReplyOutcome.UNPARSEABLE)

    failure_hits = 0
    failure_reason: str | None = None
    for match in _iter_matches(body, profile.failure_patterns):
        failure_hits += 1
        if failure_reason is None:
            reason = (match.groupdict().get("reason") or "").strip()
            failure_reason = (
                reason or BankReplyFailureReason.BANK_REJECTED_PAYMENT.value
            )

    if failure_hits and success_hits:
        return BankReplyParse(BankReplyOutcome.AMBIGUOUS)
    if len(references) > 1:
        return BankReplyParse(BankReplyOutcome.AMBIGUOUS)
    if failure_hits:
        return BankReplyParse(
            BankReplyOutcome.FAILURE,
            failure_reason=failure_reason,
        )
    if len(references) == 1:
        return BankReplyParse(
            BankReplyOutcome.SUCCESS,
            reference=references[0],
            amount=amount,
        )
    return BankReplyParse(BankReplyOutcome.UNPARSEABLE)

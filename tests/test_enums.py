"""Order state machine and bank code alias tests."""

from __future__ import annotations

import pytest

from app.domain.enums import (
    ALLOWED_TRANSITIONS,
    AuditActor,
    AuditEvent,
    BankCode,
    OrderStatus,
)

_PAYMENT_TAIL_AUDIT_EVENTS: tuple[str, ...] = (
    "PAYMENT_INITIATED",
    "PAYMENT_CAPTURED",
    "PAYMENT_VERIFIED",
    "EVIDENCE_UPLOADED",
    "RELEASE_DETECTED",
    "APPEAL_FILED",
    "APPEAL_SKIPPED",
    "TELEGRAM_CHALLENGE_REJECTED",
)


def test_payment_tail_transitions_and_direct_completion_rejected() -> None:
    """R-BUY-012: the payment tail reaches release and never completes directly."""
    assert ALLOWED_TRANSITIONS[OrderStatus.SENDING_PAYMENT] == frozenset(
        {
            OrderStatus.PAYMENT_CONFIRMED,
            OrderStatus.MANUAL_REVIEW,
            OrderStatus.FAILED,
        }
    )
    assert ALLOWED_TRANSITIONS[OrderStatus.AWAITING_RELEASE] == frozenset(
        {
            OrderStatus.COMPLETED,
            OrderStatus.APPEALED,
            OrderStatus.MANUAL_REVIEW,
            OrderStatus.FAILED,
        }
    )
    assert ALLOWED_TRANSITIONS[OrderStatus.APPEALED] == frozenset(
        {
            OrderStatus.COMPLETED,
            OrderStatus.FAILED,
            OrderStatus.MANUAL_REVIEW,
        }
    )

    assert not OrderStatus.SENDING_PAYMENT.can_transition_to(OrderStatus.COMPLETED)
    assert OrderStatus.SENDING_PAYMENT.can_transition_to(OrderStatus.PAYMENT_CONFIRMED)
    assert OrderStatus.PAYMENT_CONFIRMED.can_transition_to(OrderStatus.AWAITING_RELEASE)
    assert OrderStatus.AWAITING_RELEASE.can_transition_to(OrderStatus.APPEALED)
    assert OrderStatus.APPEALED.can_transition_to(OrderStatus.COMPLETED)

    assert OrderStatus.AWAITING_RELEASE.value == "AWAITING_RELEASE"
    assert OrderStatus.APPEALED.value == "APPEALED"
    assert OrderStatus.SENDING_PAYMENT.value == "SENDING_PAYMENT"


def test_flow_a_transition_graph_is_unchanged() -> None:
    """Flow A (SELL/Cachicamo) keeps exactly its pre-change edges.

    ``VERIFYING_CACHICAMO`` used to branch to ``SENDING_SMS``; that edge is now
    named ``SENDING_PAYMENT`` by the R-BUY-012 rename and nothing else moves.
    The new payment-tail statuses must not become reachable from Flow A.
    """
    flow_a_edges: dict[OrderStatus, frozenset[OrderStatus]] = {
        OrderStatus.PENDING: frozenset(
            {OrderStatus.PAYMENT_DETECTED, OrderStatus.FAILED, OrderStatus.MANUAL_REVIEW}
        ),
        OrderStatus.PAYMENT_DETECTED: frozenset(
            {OrderStatus.VERIFYING_CACHICAMO, OrderStatus.MANUAL_REVIEW, OrderStatus.FAILED}
        ),
        OrderStatus.VERIFYING_CACHICAMO: frozenset(
            {
                OrderStatus.SENDING_PAYMENT,
                OrderStatus.COMPLETED,
                OrderStatus.MANUAL_REVIEW,
                OrderStatus.FAILED,
            }
        ),
        OrderStatus.COMPLETED: frozenset(),
        OrderStatus.FAILED: frozenset(),
        OrderStatus.MANUAL_REVIEW: frozenset({OrderStatus.PENDING, OrderStatus.FAILED}),
    }

    for source, targets in flow_a_edges.items():
        assert ALLOWED_TRANSITIONS[source] == targets, source

    for source in flow_a_edges:
        assert OrderStatus.AWAITING_RELEASE not in ALLOWED_TRANSITIONS[source]
        assert OrderStatus.APPEALED not in ALLOWED_TRANSITIONS[source]


def test_payment_tail_audit_members_exist() -> None:
    """Later slices address these audit events and the operator actor."""
    for name in _PAYMENT_TAIL_AUDIT_EVENTS:
        assert name in AuditEvent.__members__, name
        assert AuditEvent.__members__[name].value == name
    assert AuditActor.OPERATOR.value == "OPERATOR"


def test_legal_transitions() -> None:
    """Document the allowed lifecycle transitions."""
    assert OrderStatus.PENDING.can_transition_to(OrderStatus.PAYMENT_DETECTED)
    assert OrderStatus.PAYMENT_DETECTED.can_transition_to(OrderStatus.VERIFYING_CACHICAMO)
    assert OrderStatus.VERIFYING_CACHICAMO.can_transition_to(OrderStatus.COMPLETED)
    assert OrderStatus.VERIFYING_CACHICAMO.can_transition_to(OrderStatus.SENDING_PAYMENT)
    assert OrderStatus.SENDING_PAYMENT.can_transition_to(OrderStatus.PAYMENT_CONFIRMED)
    assert OrderStatus.PAYMENT_CONFIRMED.can_transition_to(OrderStatus.AWAITING_RELEASE)
    assert OrderStatus.MANUAL_REVIEW.can_transition_to(OrderStatus.PENDING)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (OrderStatus.PENDING, OrderStatus.COMPLETED),
        (OrderStatus.COMPLETED, OrderStatus.PENDING),
        (OrderStatus.FAILED, OrderStatus.MANUAL_REVIEW),
        (OrderStatus.SENDING_PAYMENT, OrderStatus.VERIFYING_CACHICAMO),
        (OrderStatus.SENDING_PAYMENT, OrderStatus.COMPLETED),
        (OrderStatus.PAYMENT_DETECTED, OrderStatus.COMPLETED),
        (OrderStatus.AWAITING_RELEASE, OrderStatus.PENDING),
        (OrderStatus.APPEALED, OrderStatus.PENDING),
    ],
)
def test_illegal_transitions(source: OrderStatus, target: OrderStatus) -> None:
    """Reject transitions that would corrupt the lifecycle."""
    assert not source.can_transition_to(target)


def test_terminal_states() -> None:
    """Completed and failed orders are terminal."""
    assert OrderStatus.COMPLETED.is_terminal()
    assert OrderStatus.FAILED.is_terminal()
    assert not OrderStatus.MANUAL_REVIEW.is_terminal()
    assert not OrderStatus.AWAITING_RELEASE.is_terminal()
    assert not OrderStatus.APPEALED.is_terminal()
    assert OrderStatus.COMPLETED.is_successful()
    assert not OrderStatus.FAILED.is_successful()


def test_every_status_has_a_transition_entry() -> None:
    """The transition table covers every status exactly once."""
    assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)
    for targets in ALLOWED_TRANSITIONS.values():
        assert isinstance(targets, frozenset)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("banesco", BankCode.BANESCO),
        ("BANESCO", BankCode.BANESCO),
        ("Banco de Venezuela", BankCode.BANCO_DE_VENEZUELA),
        ("banco de venezuela", BankCode.BANCO_DE_VENEZUELA),
        ("Banco del Tesoro", BankCode.BANCO_DEL_TESORO),
        ("banco exteríor", BankCode.BANCO_EXTERIOR),
        ("BBVA Provincial", BankCode.PROVINCIAL),
        ("Bancamiga Banco Universal", BankCode.BANCAMIGA),
        ("unknown bank", BankCode.OTHER),
        ("", BankCode.OTHER),
    ],
)
def test_bank_code_from_aliases(raw: str, expected: BankCode) -> None:
    """Resolve aliases case- and accent-insensitively."""
    assert BankCode.from_aliases(raw) is expected


def test_bank_display_name() -> None:
    """Every bank exposes a non-empty display name."""
    for bank in BankCode:
        assert bank.display_name

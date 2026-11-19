"""Order state machine and bank code alias tests."""

from __future__ import annotations

import pytest

from app.domain.enums import ALLOWED_TRANSITIONS, BankCode, OrderStatus


def test_legal_transitions() -> None:
    """Document the allowed lifecycle transitions."""
    assert OrderStatus.PENDING.can_transition_to(OrderStatus.PAYMENT_DETECTED)
    assert OrderStatus.PAYMENT_DETECTED.can_transition_to(OrderStatus.VERIFYING_CACHICAMO)
    assert OrderStatus.VERIFYING_CACHICAMO.can_transition_to(OrderStatus.COMPLETED)
    assert OrderStatus.VERIFYING_CACHICAMO.can_transition_to(OrderStatus.SENDING_SMS)
    assert OrderStatus.SENDING_SMS.can_transition_to(OrderStatus.COMPLETED)
    assert OrderStatus.MANUAL_REVIEW.can_transition_to(OrderStatus.PENDING)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (OrderStatus.PENDING, OrderStatus.COMPLETED),
        (OrderStatus.COMPLETED, OrderStatus.PENDING),
        (OrderStatus.FAILED, OrderStatus.MANUAL_REVIEW),
        (OrderStatus.SENDING_SMS, OrderStatus.VERIFYING_CACHICAMO),
        (OrderStatus.PAYMENT_DETECTED, OrderStatus.COMPLETED),
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

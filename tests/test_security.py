"""HMAC verification and masking helper tests."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

import pytest

from app.core.exceptions import ReplayAttackError, SignatureVerificationError
from app.core.security import (
    compute_hmac_signature,
    hash_payload,
    mask_id_number,
    mask_phone,
    mask_secret,
    verify_hmac_signature,
)

_SECRET = "unit-test-webhook-secret-0123456789abcdef"


def test_hmac_round_trip() -> None:
    """A signature produced locally must verify."""
    payload = b'{"event_id":"evt-1","amount_ves":"100.00"}'
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    signature = compute_hmac_signature(_SECRET, payload, timestamp=timestamp)
    verify_hmac_signature(_SECRET, payload, signature, timestamp=timestamp)


def test_hmac_accepts_sha256_prefix() -> None:
    """Accept the ``sha256=`` prefix used by several providers."""
    payload = b"payload"
    signature = compute_hmac_signature(_SECRET, payload)
    verify_hmac_signature(_SECRET, payload, f"sha256={signature}")


def test_tampered_payload_is_rejected() -> None:
    """A modified payload must not verify."""
    payload = b'{"amount_ves":"100.00"}'
    signature = compute_hmac_signature(_SECRET, payload)
    with pytest.raises(SignatureVerificationError):
        verify_hmac_signature(_SECRET, b'{"amount_ves":"900.00"}', signature)


def test_wrong_secret_is_rejected() -> None:
    """A signature computed with another secret must not verify."""
    payload = b"payload"
    signature = compute_hmac_signature("another-secret", payload)
    with pytest.raises(SignatureVerificationError):
        verify_hmac_signature(_SECRET, payload, signature)


def test_replayed_timestamp_is_rejected() -> None:
    """A signature outside the tolerance window is flagged as a replay."""
    payload = b"payload"
    stale_timestamp = str(int(datetime.now(timezone.utc).timestamp()) - 10_000)
    signature = compute_hmac_signature(_SECRET, payload, timestamp=stale_timestamp)
    with pytest.raises(ReplayAttackError):
        verify_hmac_signature(
            _SECRET,
            payload,
            signature,
            timestamp=stale_timestamp,
            tolerance_seconds=300,
        )


def test_invalid_timestamp_is_rejected() -> None:
    """Non numeric timestamps are rejected as malformed signatures."""
    payload = b"payload"
    signature = compute_hmac_signature(_SECRET, payload, timestamp="not-a-number")
    with pytest.raises(SignatureVerificationError):
        verify_hmac_signature(_SECRET, payload, signature, timestamp="not-a-number")


def test_mask_helpers() -> None:
    """Masking never reveals more than the configured suffix."""
    assert mask_secret("supersecretvalue", visible=4) == "************alue"
    assert mask_secret("abc") == "***"
    assert mask_phone("+584121234567").endswith("4567")
    assert "+584121234567" not in mask_phone("+584121234567")
    assert mask_id_number("V-12345678") == "V-****"
    assert mask_id_number("e-87654321") == "E-****"


def test_hash_payload_is_stable() -> None:
    """The payload hash is deterministic SHA-256."""
    payload = b"liquidve"
    assert hash_payload(payload) == hashlib.sha256(payload).hexdigest()
    assert hmac.compare_digest(hash_payload(payload), hash_payload(payload))

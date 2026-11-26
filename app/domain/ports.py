"""Structural ports for provider integrations.

Ports describe what the service layer needs from an external provider without
depending on a concrete adapter.  They are :class:`typing.Protocol` classes, so
an implementation conforms by *shape* (same method names and signatures); no
inheritance or ``isinstance`` registration is required or performed.

The DTOs in this module are frozen and carry money as
:class:`decimal.Decimal`, never as ``float``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol

__all__ = ["PaymentInstruction", "BinanceBrowser"]


@dataclass(frozen=True, slots=True)
class PaymentInstruction:
    """Payment data scraped from a Binance P2P order view.

    The fields are kept exactly as scraped and trimmed; normalization
    (national phone digits, cedula digits, amount parsing) and bank resolution
    happen in the domain/service layer.  ``bank`` is deliberately absent so an
    unsupported bank stays a business decision instead of a parser error.

    Attributes:
        order_number: Provider order number supplied by the caller.
        bank_numeric_code: Scraped four-digit bank code such as ``"0102"``.
        phone: Scraped counterparty phone, E.164 or national.
        id_number: Scraped identity document such as ``"V-12345678"``.
        amount_ves: Exact amount in VES; always a ``Decimal``.
        raw_fields: Read-only snapshot of the scraped label/value pairs.

    Raises:
        TypeError: If ``amount_ves`` is not a ``Decimal``.
        ValueError: If ``amount_ves`` is not finite and positive.
    """

    order_number: str
    bank_numeric_code: str
    phone: str
    id_number: str
    amount_ves: Decimal
    raw_fields: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Enforce the money invariant and freeze ``raw_fields``.

        Raises:
            TypeError: If ``amount_ves`` is not a ``Decimal`` or ``raw_fields``
                is not a mapping.
            ValueError: If ``amount_ves`` is not a finite positive amount.
        """
        if not isinstance(self.amount_ves, Decimal):
            raise TypeError("amount_ves must be a Decimal")
        if not self.amount_ves.is_finite() or self.amount_ves <= 0:
            raise ValueError("amount_ves must be a finite positive Decimal")
        if not isinstance(self.raw_fields, Mapping):
            raise TypeError("raw_fields must be a mapping")
        object.__setattr__(
            self, "raw_fields", MappingProxyType(dict(self.raw_fields))
        )


class BinanceBrowser(Protocol):
    """Structural port for the Binance P2P browser automation adapter.

    Any object exposing these two coroutines with matching signatures conforms;
    no inheritance or ``isinstance`` check is required.
    """

    async def extract_payment_instruction(
        self, order_number: str
    ) -> PaymentInstruction:
        """Scrape the payment instruction for one P2P order.

        Args:
            order_number: Provider order number.

        Returns:
            The scraped, complete payment instruction.

        Raises:
            Exception: Implementations raise a typed provider error when the
                page cannot be read; a partial result is never returned.
        """
        ...

    async def notify_paid(self, order_number: str, reference: str) -> None:
        """Mark an order as paid on the provider side with a bank reference.

        Args:
            order_number: Provider order number.
            reference: Bank transaction reference to post in the order chat.
        """
        ...

"""Verify and save a receipt image without exposing its contents in logs."""

import hashlib
import os
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.core.exceptions import ReceiptAmountMismatch, ReceiptUnavailable
from app.infrastructure.clients.banesco_browser import BanescoReceipt


def store_receipt(
    directory: str, expected_amount: Decimal, receipt: BanescoReceipt
) -> str:
    if receipt.amount != expected_amount:
        raise ReceiptAmountMismatch()
    if not receipt.image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ReceiptUnavailable()
    digest = hashlib.sha256(receipt.image).hexdigest()
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(dir=root, delete=False) as temp:
            temporary_path = Path(temp.name)
            temp.write(receipt.image)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temporary_path, root / f"{digest}.png")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return digest

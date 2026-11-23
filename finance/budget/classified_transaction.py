"""Classified Plaid transaction shared by the budget reader and ledger renderer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ClassifiedTransaction:
    """A transaction assigned to a budget bucket. A missing currency uses the ledger default."""

    transaction_id: str
    date: date
    # Plaid sign convention: outflows positive, inflows negative.
    amount: Decimal
    name: str
    account_id: str
    bucket_id: str
    merchant_name: str | None = None
    pfc_primary: str | None = None
    pfc_detailed: str | None = None
    iso_currency_code: str | None = None

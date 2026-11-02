"""Synthetic Plaid transactions for spend calculator tests."""

from datetime import date, datetime
from decimal import Decimal

from finance.plaid.spend.allowance import Transaction


def make_transaction(
    *,
    account_id: str,
    transaction_id: str,
    date: date,
    amount: Decimal,
    name: str,
    pending: bool = False,
    pending_transaction_id: str | None = None,
    currency: str | None = "USD",
    merchant_name: str | None = None,
    pfc_primary: str | None = None,
    pfc_detailed: str | None = None,
    authorized_date: date | None = None,
    authorized_datetime: datetime | None = None,
    posted_datetime: datetime | None = None,
) -> Transaction:
    return Transaction(
        account_id=account_id,
        transaction_id=transaction_id,
        pending_transaction_id=pending_transaction_id,
        date=date,
        authorized_date=authorized_date,
        authorized_datetime=authorized_datetime,
        posted_datetime=posted_datetime,
        amount=amount,
        pending=pending,
        name=name,
        merchant_name=merchant_name,
        pfc_primary=pfc_primary,
        pfc_detailed=pfc_detailed,
        currency=currency,
    )

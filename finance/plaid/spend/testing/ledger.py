"""Synthetic Plaid transaction facts shared by Spend visual scenarios."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from finance.plaid.spend.allowance import Transaction
from finance.plaid.spend.models import (
    PlaidPaymentMeta,
    PlaidPersonalFinanceCategory,
    PlaidTransactionDetails,
    PlaidTransactionLocation,
)

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
LAST_SYNCED = datetime(2026, 10, 15, 11, tzinfo=UTC)
CARD = "synthetic-card"
NEW_CARD = "synthetic-new-card"
CHECKING = "synthetic-checking"
ACCOUNT_LABELS = {CARD: "Example card", NEW_CARD: "New example card", CHECKING: "Example checking"}


def _transaction(
    account_id: str,
    transaction_id: str,
    day: date,
    amount: str,
    name: str,
    *,
    merchant_name: str | None = None,
    pfc_primary: str | None = None,
    pfc_detailed: str | None = None,
    merchant_category_code: str | None = None,
    currency: str = "USD",
    pending: bool = False,
    authorized_date: date | None = None,
    counterparties: str | None = None,
) -> Transaction:
    return Transaction.model_validate(
        {
            "account_id": account_id,
            "transaction_id": transaction_id,
            "pending_transaction_id": None,
            "date": day,
            "authorized_date": authorized_date,
            "amount": Decimal(amount),
            "pending": pending,
            "name": name,
            "merchant_name": merchant_name,
            "pfc_primary": pfc_primary,
            "pfc_detailed": pfc_detailed,
            "merchant_category_code": merchant_category_code,
            "currency": currency,
            "counterparties": counterparties,
        }
    )


def transactions() -> list[Transaction]:
    return [
        _transaction(
            CARD,
            "cafe",
            date(2026, 10, 15),
            "15.00",
            "EXAMPLE CAFE PURCHASE",
            merchant_name="Example Cafe",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_COFFEE",
            merchant_category_code="5812",
            pending=True,
        ),
        _transaction(
            CARD,
            "shipping",
            date(2026, 10, 14),
            "18.50",
            "UPS SHIPPING",
            merchant_name="UPS",
            pfc_primary="TRANSPORTATION",
            pfc_detailed="TRANSPORTATION_SHIPPING",
            merchant_category_code="4215",
            authorized_date=date(2026, 10, 13),
            counterparties=json.dumps(
                [
                    {
                        "name": "Example Shipping",
                        "type": "merchant",
                        "confidence_level": "HIGH",
                        "entity_id": "synthetic-merchant",
                        "website": "https://example.invalid/shipping",
                    },
                    {"name": "Example Marketplace", "type": "marketplace"},
                ]
            ),
        ),
        _transaction(
            CHECKING,
            "groceries",
            date(2026, 10, 10),
            "72.50",
            "EXAMPLE GROCERIES",
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_GROCERIES",
        ),
        _transaction(
            CARD,
            "refund",
            date(2026, 10, 13),
            "-42.00",
            "EXAMPLE REFUND",
            pfc_primary="GENERAL_MERCHANDISE",
            pfc_detailed="GENERAL_MERCHANDISE_OTHER",
        ),
        _transaction(
            CARD,
            "courier",
            date(2026, 10, 12),
            "228.50",
            "UPS SHIPPING SERVICE",
            merchant_name="Example Courier",
            pfc_primary="TRANSPORTATION",
            pfc_detailed="TRANSPORTATION_SHIPPING",
            merchant_category_code="4215",
        ),
        _transaction(
            NEW_CARD,
            "new-purchase",
            date(2026, 10, 11),
            "39.00",
            "EXAMPLE INITIAL PURCHASE",
            merchant_name="Example Shop",
            pfc_primary="GENERAL_MERCHANDISE",
            pfc_detailed="GENERAL_MERCHANDISE_OTHER",
        ),
        _transaction(
            CHECKING,
            "reversed-original",
            date(2026, 10, 11),
            "120.00",
            "EXAMPLE OWN ACCOUNT TRANSFER",
            pfc_primary="TRANSFER_OUT",
            pfc_detailed="TRANSFER_OUT_OTHER",
        ),
        _transaction(
            CHECKING,
            "reversal",
            date(2026, 10, 12),
            "-120.00",
            "EXAMPLE OWN ACCOUNT REVERSAL",
            pfc_primary="TRANSFER_IN",
            pfc_detailed="TRANSFER_IN_OTHER",
        ),
        _transaction(
            CHECKING,
            "travel",
            date(2026, 10, 2),
            "412.50",
            "EXAMPLE TRAVEL PURCHASE",
            merchant_name="Example Travel",
            pfc_primary="TRAVEL",
            pfc_detailed="TRAVEL_OTHER",
        ),
        _transaction(
            CHECKING,
            "historical",
            date(2026, 9, 20),
            "50.00",
            "EXAMPLE PURCHASE BEFORE ACTIVATION",
            pfc_primary="GENERAL_MERCHANDISE",
            pfc_detailed="GENERAL_MERCHANDISE_OTHER",
        ),
        _transaction(
            CHECKING,
            "transfer-debit",
            date(2026, 10, 10),
            "125.00",
            "EXAMPLE OWN TRANSFER DEBIT",
            pfc_primary="TRANSFER_OUT",
            pfc_detailed="TRANSFER_OUT_OTHER",
        ),
        _transaction(
            CHECKING,
            "transfer-credit",
            date(2026, 10, 11),
            "-125.00",
            "EXAMPLE OWN TRANSFER CREDIT",
            pfc_primary="TRANSFER_IN",
            pfc_detailed="TRANSFER_IN_OTHER",
        ),
        _transaction(
            CHECKING,
            "tax",
            date(2026, 10, 12),
            "415.00",
            "EXAMPLE TAX PAYMENT",
            pending=True,
            pfc_primary="TAXES",
            pfc_detailed="TAXES_OTHER_TAXES",
        ),
        _transaction(
            CHECKING,
            "tax-cad",
            date(2026, 10, 13),
            "72.00",
            "EXAMPLE TAX PAYMENT CAD",
            currency="CAD",
            pfc_primary="TAXES",
            pfc_detailed="TAXES_OTHER_TAXES",
        ),
    ]


def details() -> dict[tuple[str, str], PlaidTransactionDetails]:
    return {
        (CARD, "shipping"): PlaidTransactionDetails(
            account_id=CARD,
            transaction_id="shipping",
            amount=Decimal("18.50"),
            iso_currency_code="USD",
            original_description="EXAMPLE SHIPPING PAYMENT",
            authorized_date=date(2026, 10, 13),
            payment_channel="in store",
            location=PlaidTransactionLocation(city="Example City", region="CA", country="US"),
            payment_meta=PlaidPaymentMeta(reference_number="synthetic-reference"),
            personal_finance_category=PlaidPersonalFinanceCategory(
                primary="TRANSPORTATION", detailed="TRANSPORTATION_SHIPPING", confidence_level="HIGH"
            ),
        )
    }

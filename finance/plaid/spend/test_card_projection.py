"""The card projector uses the same statement accounting in service and fixtures."""

from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from zoneinfo import ZoneInfo

import pytest_bazel

from finance.plaid.spend.allowance import Transaction
from finance.plaid.spend.card_projection import CardAccount, project_card
from finance.plaid.spend.configuration import CardConfig
from finance.plaid.spend.models import AlertState, StatementReason


def transaction(
    transaction_id: str,
    amount: str,
    *,
    when: date = date(2026, 10, 15),
    pending: bool = False,
    pending_transaction_id: str | None = None,
    currency: str = "USD",
    pfc_detailed: str | None = None,
) -> Transaction:
    return Transaction(
        account_id="card",
        transaction_id=transaction_id,
        pending_transaction_id=pending_transaction_id,
        date=when,
        amount=Decimal(amount),
        pending=pending,
        name=transaction_id,
        merchant_name=None,
        pfc_primary=None,
        pfc_detailed=pfc_detailed,
        currency=currency,
    )


def test_statement_totals_and_reasons_follow_posting_dates() -> None:
    account = CardAccount("card", "Card", "Bank", "1234", "USD", datetime(2026, 10, 15, tzinfo=UTC))
    config = CardConfig(
        account_id="card", label="Example card", enabled=True, limit_minor_units=5_000, alert_threshold_percent=80
    )
    card, decisions = project_card(
        account,
        config,
        cycle_start=date(2026, 10, 1),
        statement_available=True,
        transactions=[
            transaction("purchase", "30.00"),
            transaction("refund", "-5.00"),
            transaction("pending", "10.00", pending=True),
            transaction("replacement", "12.00", pending_transaction_id="pending"),
            transaction("foreign", "20.00", currency="EUR"),
            transaction("payment", "100.00", pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
            transaction("before", "11.00", when=date(2026, 9, 30)),
            transaction("after", "1.00", when=date(2026, 10, 16)),
        ],
        time_zone=ZoneInfo("America/Los_Angeles"),
        today=date(2026, 10, 15),
    )
    assert card.statement_period.kind == "statement"
    assert (card.posted_minor_units, card.pending_minor_units, card.spend_minor_units) == (3_700, 0, 3_700)
    assert card.spend_percent == Decimal(74)
    assert card.alert_state == AlertState.NORMAL
    assert decisions == {
        ("card", "purchase"): (StatementReason.COUNTED, 3_000),
        ("card", "refund"): (StatementReason.COUNTED, -500),
        ("card", "pending"): (StatementReason.SUPERSEDED_PENDING, 0),
        ("card", "replacement"): (StatementReason.COUNTED, 1_200),
        ("card", "foreign"): (StatementReason.OTHER_CURRENCY, 0),
        ("card", "payment"): (StatementReason.CARD_PAYMENT, 0),
        ("card", "before"): (StatementReason.OUTSIDE_CYCLE, 0),
        ("card", "after"): (StatementReason.OUTSIDE_CYCLE, 0),
    }


def test_provisional_and_unavailable_periods() -> None:
    account = CardAccount("card", "Card", None, None, "USD", None)
    config = CardConfig(
        account_id="card", label="Example card", enabled=True, limit_minor_units=1_000, alert_threshold_percent=80
    )
    project = partial(
        project_card,
        account,
        config,
        transactions=[transaction("purchase", "12.00")],
        time_zone=ZoneInfo("UTC"),
        today=date(2026, 10, 15),
    )

    unavailable, no_decisions = project(cycle_start=None, statement_available=False)
    assert unavailable.statement_period.kind == "unavailable"
    assert unavailable.spend_minor_units is None
    assert unavailable.alert_state == AlertState.UNAVAILABLE
    assert no_decisions == {}

    provisional, decisions = project(cycle_start=date(2026, 10, 1), statement_available=False)
    assert provisional.statement_period.kind == "provisional"
    assert provisional.spend_minor_units == 1_200
    assert provisional.spend_percent is None
    assert provisional.alert_state == AlertState.UNAVAILABLE
    assert decisions[("card", "purchase")] == (StatementReason.COUNTED, 1_200)

    exceeded, _ = project(cycle_start=date(2026, 10, 1), statement_available=True)
    assert exceeded.alert_state == AlertState.EXCEEDED


if __name__ == "__main__":
    pytest_bazel.main()

"""Synthetic transaction responses built from Spend's fixture facts."""

from __future__ import annotations

from finance.plaid.spend.allowance import PeriodId, TransactionPeriodId
from finance.plaid.spend.models import SpendTransactionsView
from finance.plaid.spend.testing import dashboard, ledger
from finance.plaid.spend.transaction_views import project_transactions


def view(period: TransactionPeriodId = PeriodId.ROLLING_30D) -> SpendTransactionsView:
    scenario = dashboard.scenario()
    card_ids = {card.account_id for card in scenario.configuration.cards if card.enabled}
    card_transactions = [transaction for transaction in scenario.transactions if transaction.account_id in card_ids]
    return project_transactions(
        configuration=scenario.configuration,
        view=scenario.view,
        period=period,
        decisions=scenario.decisions,
        account_labels=ledger.ACCOUNT_LABELS,
        card_transactions=card_transactions,
        statement_decisions=scenario.statement_decisions,
        transaction_details=ledger.details(),
    )

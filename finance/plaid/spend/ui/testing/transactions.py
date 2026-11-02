"""Synthetic transaction responses derived from the Spend service projection."""

from finance.plaid.spend.allowance import PeriodId, TransactionPeriodId
from finance.plaid.spend.models import SpendTransactionsView
from finance.plaid.spend.testing import dashboard, ledger
from finance.plaid.spend.transaction_projection import project_transactions_view


def view(period: TransactionPeriodId = PeriodId.ROLLING_30D) -> SpendTransactionsView:
    scenario = dashboard.scenario()
    return project_transactions_view(
        configuration=scenario.configuration,
        view=scenario.view,
        decisions=scenario.decisions,
        card_transactions=[
            transaction
            for transaction in scenario.transactions
            if transaction.account_id in {ledger.CARD, ledger.NEW_CARD}
        ],
        statement_decisions=scenario.statement_decisions,
        details_by_key=ledger.details(),
        account_labels=ledger.ACCOUNT_LABELS,
        requested_period_id=period,
    )

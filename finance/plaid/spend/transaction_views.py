"""Pure response projection for the Spend transaction history."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

from babel.numbers import get_currency_precision

from finance.plaid.spend.allowance import (
    Disposition,
    Kind,
    Period,
    PeriodId,
    Transaction,
    TransactionDecision,
    TransactionPeriodId,
    round_minor_units,
)
from finance.plaid.spend.configuration import SpendConfiguration
from finance.plaid.spend.models import (
    AllowanceTransactionEffect,
    AnalysisCategoryView,
    CardStatementEffect,
    ExcludedFlowSummary,
    PaceEffect,
    PlaidTransactionDetails,
    SpendTransaction,
    SpendTransactionRow,
    SpendTransactionsView,
    SpendView,
    StatementReason,
    TransactionPeriodSummary,
)


def project_transactions(
    *,
    configuration: SpendConfiguration,
    view: SpendView,
    period: TransactionPeriodId,
    decisions: Sequence[TransactionDecision],
    account_labels: Mapping[str, str],
    card_transactions: Sequence[Transaction],
    statement_decisions: Mapping[tuple[str, str], tuple[StatementReason, int]],
    transaction_details: Mapping[tuple[str, str], PlaidTransactionDetails],
) -> SpendTransactionsView:
    """Build the transaction response from an already loaded view and ledger facts."""
    today = view.generated_at.astimezone(configuration.time_zone).date()
    cycle_report = (
        next((report for report in view.allowance.spend_periods if report.period.id == PeriodId.CREDIT_CYCLE), None)
        if view.allowance is not None
        else None
    )
    actual_period_id = PeriodId.ROLLING_30D if period == PeriodId.CREDIT_CYCLE and cycle_report is None else period
    report_period = Period.for_id(
        PeriodId(actual_period_id), today, cycle_report.period.start if cycle_report else None
    )
    card_currencies = {card.account_id: card.currency for card in view.cards}
    card_cycle_starts = {
        card.account_id: card.statement_period.start if card.statement_period.kind != "unavailable" else None
        for card in view.cards
    }
    card_ids = {card.account_id for card in configuration.cards if card.enabled}
    allowance_policy = configuration.allowance
    allowance_ids = allowance_policy.spending_account_ids if allowance_policy is not None else set()
    labels = dict(account_labels)
    labels.update({card.account_id: card.label for card in configuration.cards if card.enabled})
    rows: list[SpendTransactionRow] = []
    seen: set[tuple[str, str]] = set()

    def add_row(transaction: Transaction, decision: TransactionDecision | None) -> None:
        day = transaction.report_day(configuration.time_zone)
        if not report_period.start <= day <= today:
            return
        key = (transaction.account_id, transaction.transaction_id)
        if key in seen:
            return
        seen.add(key)
        statement = statement_decisions.get(key)
        details = transaction_details.get(key, PlaidTransactionDetails())
        is_card = transaction.account_id in card_ids
        currency = transaction.currency or card_currencies.get(transaction.account_id) or "USD"
        card_cycle_start = card_cycle_starts.get(transaction.account_id)
        if statement is not None:
            statement_reason = statement[0]
        elif is_card and card_cycle_start is not None and transaction.date < card_cycle_start:
            statement_reason = StatementReason.OUTSIDE_CYCLE
        elif is_card:
            statement_reason = StatementReason.UNAVAILABLE
        else:
            statement_reason = None
        category = None
        if decision is not None and allowance_policy is not None:
            category_id = (
                decision.rule.analysis_category
                if decision.rule is not None and decision.rule.analysis_category is not None
                else "unclassified"
            )
            category_config = allowance_policy.analysis_categories[category_id]
            category = AnalysisCategoryView(id=category_id, label=category_config.label, color=category_config.color)
        rows.append(
            SpendTransactionRow(
                transaction=SpendTransaction(
                    date=day,
                    plaid_date=transaction.date,
                    account_label=labels.get(transaction.account_id, "Account"),
                    name=transaction.name,
                    merchant_name=transaction.merchant_name,
                    amount_minor_units=round_minor_units(transaction.amount, get_currency_precision(currency)),
                    currency=currency,
                    pending=transaction.pending,
                    pfc_primary=transaction.pfc_primary,
                    pfc_detailed=transaction.pfc_detailed,
                    merchant_category_code=transaction.merchant_category_code,
                    counterparties=transaction.counterparties or [],
                    details=details,
                ),
                allowance=AllowanceTransactionEffect(
                    in_scope=transaction.account_id in allowance_ids,
                    disposition=decision.disposition if decision else None,
                    rule_number=decision.rule_number if decision else None,
                    rule=decision.rule if decision else None,
                    reversal_pair=decision.reversal_pair if decision else None,
                    effective_kind=decision.effective_kind if decision else None,
                    amount_minor_units=decision.allowance_minor_units if decision else 0,
                    pace_effects=[
                        PaceEffect(
                            period_id=period_id,
                            amount_minor_units=decision.pace_effects_minor_units.get(period_id, 0) if decision else 0,
                        )
                        for period_id in PeriodId
                        if period_id.rolling_days is not None
                    ],
                    category=category,
                ),
                statement=CardStatementEffect(
                    amount_minor_units=statement[1]
                    if statement
                    else 0
                    if statement_reason == StatementReason.OUTSIDE_CYCLE
                    else None,
                    reason=statement_reason,
                ),
            )
        )

    for decision in decisions:
        add_row(decision.transaction, decision)
    for transaction in card_transactions:
        add_row(transaction, None)
    rows.sort(key=lambda row: (row.transaction.date, abs(row.transaction.amount_minor_units)), reverse=True)
    unmatched_rows = [
        row
        for row in rows
        if row.allowance.in_scope
        and row.allowance.disposition == Disposition.COUNTED
        and row.transaction.amount_minor_units > 0
        and (row.allowance.effective_kind is None or row.allowance.effective_kind == Kind.REVIEW)
    ]
    return SpendTransactionsView(
        generated_at=view.generated_at,
        requested_period_id=period,
        period=report_period,
        summary=TransactionPeriodSummary(
            transaction_count=len(rows),
            net_allowance_spend_minor_units=sum(row.allowance.amount_minor_units for row in rows),
            unmatched_charge_count=len(unmatched_rows),
            unmatched_charge_minor_units=sum(row.allowance.amount_minor_units for row in unmatched_rows),
            excluded_flows=summarize_excluded_flows(rows),
        ),
        allowance=view.allowance,
        rows=rows,
    )


def summarize_excluded_flows(rows: Sequence[SpendTransactionRow]) -> list[ExcludedFlowSummary]:
    """Summarize policy exclusions separately from other accounting dispositions."""
    groups: dict[tuple[str, str], list[SpendTransactionRow]] = defaultdict(list)
    uncategorized = AnalysisCategoryView(id="uncategorized", label="Uncategorized", color="#64748B")
    for row in rows:
        if row.allowance.disposition == Disposition.EXCLUDED:
            category = row.allowance.category or uncategorized
            groups[category.id, row.transaction.currency].append(row)
    return [
        ExcludedFlowSummary(
            category=group[0].allowance.category or uncategorized,
            currency=currency,
            debit_minor_units=sum(max(row.transaction.amount_minor_units, 0) for row in group),
            credit_minor_units=sum(max(-row.transaction.amount_minor_units, 0) for row in group),
            transaction_count=len(group),
            pending_count=sum(row.transaction.pending for row in group),
        )
        for (_, currency), group in sorted(groups.items())
    ]

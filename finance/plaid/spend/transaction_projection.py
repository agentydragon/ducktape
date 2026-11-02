"""Pure projection of collected spend transactions into the transaction response."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal

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
    AnalysisCategoryView,
    ExcludedFlowSummary,
    PaceEffect,
    PlaidTransactionDetails,
    SpendTransactionRow,
    SpendTransactionsView,
    SpendView,
    StatementReason,
    TransactionPeriodSummary,
)


def summarize_excluded_flows(rows: list[SpendTransactionRow]) -> list[ExcludedFlowSummary]:
    """Summarize only policy exclusions; other accounting dispositions stay separate."""
    groups: dict[tuple[str, str], list[SpendTransactionRow]] = defaultdict(list)
    uncategorized = AnalysisCategoryView(id="uncategorized", label="Uncategorized", color="#64748B")
    for row in rows:
        if row.disposition == Disposition.EXCLUDED:
            category = row.category or uncategorized
            groups[category.id, row.currency].append(row)
    return [
        ExcludedFlowSummary(
            category=group[0].category or uncategorized,
            currency=currency,
            debit_minor_units=sum(max(row.amount_minor_units, 0) for row in group),
            credit_minor_units=sum(max(-row.amount_minor_units, 0) for row in group),
            transaction_count=len(group),
            pending_count=sum(row.pending for row in group),
        )
        for (_, currency), group in sorted(groups.items())
    ]


def project_transactions_view(
    *,
    configuration: SpendConfiguration,
    view: SpendView,
    decisions: Sequence[TransactionDecision],
    card_transactions: Sequence[Transaction],
    statement_decisions: Mapping[tuple[str, str], tuple[StatementReason, int]],
    details_by_key: Mapping[tuple[str, str], PlaidTransactionDetails],
    account_labels: Mapping[str, str],
    requested_period_id: TransactionPeriodId,
) -> SpendTransactionsView:
    """Project preloaded accounting and card evidence into the displayed transaction response."""
    today = view.generated_at.astimezone(configuration.time_zone).date()
    cycle_report = (
        next((report for report in view.allowance.spend_periods if report.period.id == PeriodId.CREDIT_CYCLE), None)
        if view.allowance is not None
        else None
    )
    actual_period_id = (
        PeriodId.ROLLING_30D
        if requested_period_id == PeriodId.CREDIT_CYCLE and cycle_report is None
        else requested_period_id
    )
    report_period = Period.for_id(
        PeriodId(actual_period_id), today, cycle_report.period.start if cycle_report else None
    )
    start = report_period.start
    card_currencies = {card.account_id: card.currency for card in view.cards}
    card_cycle_starts = {
        card.account_id: card.statement_period.start if card.statement_period.kind != "unavailable" else None
        for card in view.cards
    }
    card_ids = {card.account_id for card in configuration.cards if card.enabled}
    allowance_ids = configuration.allowance.spending_account_ids if configuration.allowance else set()
    allowance_policy = configuration.allowance
    labels = dict(account_labels)
    labels.update({card.account_id: card.label for card in configuration.cards if card.enabled})
    rows: list[SpendTransactionRow] = []
    seen: set[tuple[str, str]] = set()

    zone = configuration.time_zone

    def add_row(transaction: Transaction, decision: TransactionDecision | None) -> None:
        day = transaction.report_day(zone)
        if not start <= day <= today:
            return
        key = (transaction.account_id, transaction.transaction_id)
        if key in seen:
            return
        seen.add(key)
        statement = statement_decisions.get(key)
        details = details_by_key.get(key, PlaidTransactionDetails())
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
                date=day,
                plaid_date=transaction.date,
                account_label=labels.get(transaction.account_id, "Account"),
                name=transaction.name,
                merchant_name=transaction.merchant_name,
                amount_minor_units=_amount_minor_units(transaction.amount, currency),
                currency=currency,
                pending=transaction.pending,
                allowance_in_scope=transaction.account_id in allowance_ids,
                disposition=decision.disposition if decision else None,
                rule_number=decision.rule_number if decision else None,
                rule=decision.rule if decision else None,
                reversal_pair=decision.reversal_pair if decision else None,
                effective_kind=decision.effective_kind if decision else None,
                allowance_minor_units=decision.allowance_minor_units if decision else 0,
                pace_effects=[
                    PaceEffect(
                        period_id=period_id,
                        amount_minor_units=decision.pace_effects_minor_units.get(period_id, 0) if decision else 0,
                    )
                    for period_id in PeriodId
                    if period_id.rolling_days is not None
                ],
                statement_minor_units=statement[1]
                if statement
                else 0
                if statement_reason == StatementReason.OUTSIDE_CYCLE
                else None,
                statement_reason=statement_reason,
                pfc_primary=transaction.pfc_primary,
                pfc_detailed=transaction.pfc_detailed,
                merchant_category_code=transaction.merchant_category_code,
                category=category,
                counterparties=transaction.counterparties or [],
                details=details,
            )
        )

    for decision in decisions:
        add_row(decision.transaction, decision)
    for transaction in card_transactions:
        add_row(transaction, None)
    rows.sort(key=lambda row: (row.date, abs(row.amount_minor_units)), reverse=True)
    unmatched_rows = [
        row
        for row in rows
        if row.allowance_in_scope
        and row.disposition == Disposition.COUNTED
        and row.amount_minor_units > 0
        and (row.effective_kind is None or row.effective_kind == Kind.REVIEW)
    ]
    return SpendTransactionsView(
        generated_at=view.generated_at,
        requested_period_id=requested_period_id,
        period=report_period,
        summary=TransactionPeriodSummary(
            transaction_count=len(rows),
            net_allowance_spend_minor_units=sum(row.allowance_minor_units for row in rows),
            unmatched_charge_count=len(unmatched_rows),
            unmatched_charge_minor_units=sum(row.allowance_minor_units for row in unmatched_rows),
            excluded_flows=summarize_excluded_flows(rows),
        ),
        allowance=view.allowance,
        rows=rows,
    )


def _amount_minor_units(major_units: Decimal, currency: str) -> int:
    return round_minor_units(major_units, get_currency_precision(currency))

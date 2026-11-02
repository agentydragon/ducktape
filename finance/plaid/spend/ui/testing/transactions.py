"""Synthetic transaction responses built from Spend's fixture facts."""

from __future__ import annotations

from collections import defaultdict

from finance.plaid.spend.allowance import Disposition, Kind, Period, PeriodId, TransactionPeriodId, round_minor_units
from finance.plaid.spend.models import (
    AnalysisCategoryView,
    ExcludedFlowSummary,
    PaceEffect,
    PlaidTransactionDetails,
    SpendTransactionRow,
    SpendTransactionsView,
    TransactionPeriodSummary,
)
from finance.plaid.spend.testing import dashboard, ledger


def view(period: TransactionPeriodId = PeriodId.ROLLING_30D) -> SpendTransactionsView:
    scenario = dashboard.scenario()
    configuration, spend = scenario.configuration, scenario.view
    allowance = configuration.allowance
    assert allowance is not None
    allowance_view = spend.allowance
    assert allowance_view is not None
    today = spend.generated_at.astimezone(configuration.time_zone).date()
    cycle = next(report for report in allowance_view.spend_periods if report.period.id == PeriodId.CREDIT_CYCLE)
    report_period = Period.for_id(PeriodId(period), today, cycle.period.start)

    card_ids = {ledger.CARD, ledger.NEW_CARD}
    decisions = {(d.transaction.account_id, d.transaction.transaction_id): d for d in scenario.decisions}
    labels = ledger.ACCOUNT_LABELS | {card.account_id: card.label for card in configuration.cards}
    details = ledger.details()
    rows: list[SpendTransactionRow] = []

    for transaction in scenario.transactions:
        key = transaction.account_id, transaction.transaction_id
        decision = decisions.get(key)
        if decision is None and transaction.account_id not in card_ids:
            continue
        day = transaction.report_day(configuration.time_zone)
        if not report_period.start <= day <= today:
            continue

        category = None
        if decision is not None:
            category_id = (
                decision.rule.analysis_category if decision.rule and decision.rule.analysis_category else "unclassified"
            )
            config_category = allowance.analysis_categories[category_id]
            category = AnalysisCategoryView(id=category_id, label=config_category.label, color=config_category.color)
        statement = scenario.statement_decisions.get(key)
        rows.append(
            SpendTransactionRow(
                date=day,
                plaid_date=transaction.date,
                account_label=labels[transaction.account_id],
                name=transaction.name,
                merchant_name=transaction.merchant_name,
                amount_minor_units=round_minor_units(transaction.amount),
                currency=transaction.currency or "USD",
                pending=transaction.pending,
                allowance_in_scope=transaction.account_id in allowance.spending_account_ids,
                disposition=decision.disposition if decision else None,
                rule_number=decision.rule_number if decision else None,
                rule=decision.rule if decision else None,
                reversal_pair=decision.reversal_pair if decision else None,
                effective_kind=decision.effective_kind if decision else None,
                allowance_minor_units=decision.allowance_minor_units if decision else 0,
                pace_effects=[
                    PaceEffect(
                        period_id=p, amount_minor_units=decision.pace_effects_minor_units.get(p, 0) if decision else 0
                    )
                    for p in PeriodId
                    if p.rolling_days is not None
                ],
                statement_minor_units=statement[1] if statement else None,
                statement_reason=statement[0] if statement else None,
                pfc_primary=transaction.pfc_primary,
                pfc_detailed=transaction.pfc_detailed,
                merchant_category_code=transaction.merchant_category_code,
                category=category,
                counterparties=transaction.counterparties or [],
                details=details.get(key, PlaidTransactionDetails()),
            )
        )

    rows.sort(key=lambda row: (row.date, abs(row.amount_minor_units)), reverse=True)
    unmatched = [
        row
        for row in rows
        if row.allowance_in_scope
        and row.disposition == Disposition.COUNTED
        and row.amount_minor_units > 0
        and (row.effective_kind is None or row.effective_kind == Kind.REVIEW)
    ]
    return SpendTransactionsView(
        generated_at=spend.generated_at,
        requested_period_id=period,
        period=report_period,
        summary=TransactionPeriodSummary(
            transaction_count=len(rows),
            net_allowance_spend_minor_units=sum(row.allowance_minor_units for row in rows),
            unmatched_charge_count=len(unmatched),
            unmatched_charge_minor_units=sum(row.allowance_minor_units for row in unmatched),
            excluded_flows=_excluded_flows(rows),
        ),
        allowance=spend.allowance,
        rows=rows,
    )


def _excluded_flows(rows: list[SpendTransactionRow]) -> list[ExcludedFlowSummary]:
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

"""The visual fixtures remain valid and describe the rows they display."""

from collections import defaultdict
from typing import cast

import pytest
import pytest_bazel

from finance.plaid.spend.allowance import (
    Disposition,
    Kind,
    PeriodId,
    TransactionPeriodId,
    daily_reference,
    round_minor_units,
)
from finance.plaid.spend.models import StatementReason
from finance.plaid.spend.sustainability import SustainabilityView
from finance.plaid.spend.ui.testing import dashboard, sustainability, transactions


def test_card_responses_describe_the_same_accounts_and_statement_spend() -> None:
    view = dashboard.spend_view()
    config = dashboard.configuration()
    transaction_view = transactions.view(PeriodId.ROLLING_30D)
    configured_cards = {card.label: card for card in config.cards if card.enabled}

    assert {card.label for card in view.cards} == configured_cards.keys()
    assert config.allowance is not None
    assert config.allowance.spending_account_count == len(
        {row.account_label for row in transaction_view.rows if row.allowance_in_scope}
    )
    assert {row.account_label for row in transaction_view.rows if not row.allowance_in_scope} == {"New example card"}

    for card in view.cards:
        assert card.statement_period.kind in {"statement", "provisional"}
        assert card.limit_minor_units == configured_cards[card.label].limit_minor_units
        statement_rows = [
            row
            for row in transaction_view.rows
            if row.account_label == card.label
            and card.statement_period.start <= row.date <= card.statement_period.through
            and row.statement_reason == StatementReason.COUNTED
        ]
        assert statement_rows
        assert card.pending_minor_units == sum(row.statement_minor_units or 0 for row in statement_rows if row.pending)
        assert card.posted_minor_units == sum(
            row.statement_minor_units or 0 for row in statement_rows if not row.pending
        )
        assert card.spend_minor_units == card.pending_minor_units + card.posted_minor_units


@pytest.mark.parametrize("period", [PeriodId.CREDIT_CYCLE, PeriodId.ROLLING_7D, PeriodId.ROLLING_30D])
def test_transaction_summary_matches_shown_rows(period: TransactionPeriodId) -> None:
    view = transactions.view(period)
    assert view.summary.transaction_count == len(view.rows)
    assert view.summary.net_allowance_spend_minor_units == sum(row.allowance_minor_units for row in view.rows)
    unmatched = [
        row
        for row in view.rows
        if row.allowance_in_scope
        and row.disposition == Disposition.COUNTED
        and row.amount_minor_units > 0
        and (row.effective_kind is None or row.effective_kind == Kind.REVIEW)
    ]
    assert view.summary.unmatched_charge_count == len(unmatched)
    assert view.summary.unmatched_charge_minor_units == sum(row.allowance_minor_units for row in unmatched)

    groups = defaultdict(list)
    for row in view.rows:
        if row.disposition == Disposition.EXCLUDED:
            assert row.category is not None
            groups[row.category.id, row.currency].append(row)
    assert {(flow.category.id, flow.currency) for flow in view.summary.excluded_flows} == set(groups)
    for flow in view.summary.excluded_flows:
        rows = groups[flow.category.id, flow.currency]
        assert flow.debit_minor_units == sum(max(row.amount_minor_units, 0) for row in rows)
        assert flow.credit_minor_units == sum(max(-row.amount_minor_units, 0) for row in rows)
        assert flow.transaction_count == len(rows)
        assert flow.pending_count == sum(row.pending for row in rows)


@pytest.mark.parametrize("period", [PeriodId.CREDIT_CYCLE, PeriodId.ROLLING_7D, PeriodId.ROLLING_30D])
def test_allowance_matches_transaction_ledger(period: TransactionPeriodId) -> None:
    view = transactions.view(period)
    allowance = view.allowance
    assert allowance is not None
    spend = next(entry for entry in allowance.spend_periods if entry.period.id == period)
    assert spend.spend_minor_units == view.summary.net_allowance_spend_minor_units
    assert spend.counted_from == max(spend.period.start, allowance.activation_at)
    if period.rolling_days is not None:
        pace = next(entry for entry in allowance.recorded_pace_periods if entry.period.id == period)
        effects = [
            effect.amount_minor_units for row in view.rows for effect in row.pace_effects if effect.period_id == period
        ]
        assert pace.observed_daily_minor_units == sum(effects) // period.rolling_days
        assert pace.unmatched_charges is not None
        assert pace.unmatched_charges.count == view.summary.unmatched_charge_count
        assert pace.unmatched_charges.amount_minor_units == view.summary.unmatched_charge_minor_units


def test_allowance_balance_matches_current_cycle() -> None:
    view = transactions.view(PeriodId.CREDIT_CYCLE)
    allowance = view.allowance
    assert allowance is not None
    assert allowance.posted_minor_units == sum(row.allowance_minor_units for row in view.rows if not row.pending)
    assert allowance.pending_minor_units == sum(row.allowance_minor_units for row in view.rows if row.pending)
    assert (
        allowance.available_minor_units == allowance.monthly_minor_units - view.summary.net_allowance_spend_minor_units
    )
    assert allowance.review_transaction_count == view.summary.unmatched_charge_count
    assert allowance.review_minor_units == view.summary.unmatched_charge_minor_units
    assert allowance.daily_reference_minor_units == round_minor_units(daily_reference(allowance.monthly_minor_units), 0)


def test_pre_activation_purchase_informs_pace_without_allowance_debt() -> None:
    view = transactions.view(PeriodId.ROLLING_30D)
    assert view.allowance is not None
    history = [row for row in view.rows if row.date < view.allowance.activation_at]
    assert len(history) == 1
    assert history[0].disposition == Disposition.PACE_ONLY
    assert history[0].allowance_minor_units == 0
    assert any(effect.amount_minor_units > 0 for effect in history[0].pace_effects)


def test_sustainability_copies_validate() -> None:
    variants: tuple[sustainability.Variant, ...] = (
        "ready",
        "stale",
        "unavailable",
        "provisional",
        "tax_unmodeled",
        "low_draw",
    )
    views = [
        *(sustainability.view(variant) for variant in variants),
        *(sustainability.window(days) for days in (30, 90, 365)),
        sustainability.partial_window(),
        sustainability.with_partial_account_history(),
        sustainability.with_historical_coverage_gap(),
    ]
    for view in views:
        assert SustainabilityView.model_validate_json(view.model_dump_json()) == view


def test_unknown_sustainability_cases_fail() -> None:
    with pytest.raises(ValueError, match="Unknown sustainability variant"):
        sustainability.view(cast(sustainability.Variant, "unknown"))
    with pytest.raises(ValueError, match="Unsupported sustainability window"):
        sustainability.window(14)


if __name__ == "__main__":
    pytest_bazel.main()

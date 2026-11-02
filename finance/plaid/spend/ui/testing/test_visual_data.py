"""The visual fixtures remain valid and describe the rows they display."""

from collections import defaultdict
from typing import cast

import pytest
import pytest_bazel

from finance.plaid.spend.allowance import Disposition, Kind, PeriodId, TransactionPeriodId
from finance.plaid.spend.sustainability import SustainabilityView
from finance.plaid.spend.ui.testing import sustainability, transactions


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

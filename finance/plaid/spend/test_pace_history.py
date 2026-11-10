"""Synthetic coverage and accounting tests for the panel's trend data."""

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest_bazel

from finance.plaid.spend.allowance import (
    AllowancePolicy,
    AnalysisCategory,
    CategoryExact,
    Kind,
    PeriodId,
    Rule,
    calculate,
)
from finance.plaid.spend.pace_history import pace_history
from finance.plaid.spend.sustainability import AccountHistory, AccountOpening
from finance.plaid.spend.testing.transaction import make_transaction

NOW = datetime(2026, 4, 1, 12, tzinfo=UTC)
ZONE = ZoneInfo("UTC")


def policy() -> AllowancePolicy:
    return AllowancePolicy(
        monthly_minor_units=100_000,
        activation_at=date(2026, 1, 1),
        spending_account_ids={"card", "bank"},
        rules=[Rule(condition=CategoryExact(field="pfc_primary", value="FIXED"), kind=Kind.FIXED)],
        analysis_categories={"unclassified": AnalysisCategory(label="Review", color="#123456")},
    )


def row(account: str, day: date, amount: int, name: str, **changes: object):
    return make_transaction(
        account_id=account, transaction_id=name, date=day, amount=Decimal(amount), name=name
    ).model_copy(update=changes)


def test_history_matches_current_pace_after_pending_supersession_and_exclusions() -> None:
    rows = [
        row("card", date(2025, 12, 1), 0, "card history"),
        row("bank", date(2025, 12, 1), 0, "bank history"),
        row("card", date(2026, 4, 1), 42, "purchase"),
        row("card", date(2026, 4, 1), 30, "pending", pending=True),
        row("card", date(2026, 4, 1), 30, "posted", pending_transaction_id="pending"),
        row("card", date(2026, 4, 1), -20, "refund"),
        row("bank", date(2026, 4, 1), 100, "fixed", pfc_primary="FIXED"),
        row("bank", date(2026, 4, 1), 50, "other currency", currency="EUR"),
    ]
    result = pace_history(policy(), rows, time_zone=ZONE, now=NOW, last_synced_at=NOW, account_history={})
    current = calculate(policy(), rows, time_zone=ZONE, now=NOW, last_synced_at=NOW).view
    for window in result.windows:
        expected = next(p for p in current.recorded_pace_periods if p.period.id == window.period_id)
        assert window.points[-1].observed_daily_minor_units == expected.observed_daily_minor_units
    assert result.windows[0].points[-1].observed_daily_minor_units == 7_200 // 7
    assert result.windows[1].points[-1].observed_daily_minor_units == 7_200 // 30


def test_missing_account_history_is_a_gap_not_zero() -> None:
    rows = [row("card", date(2025, 12, 1), 0, "old"), row("bank", date(2026, 3, 31), 0, "recent")]
    result = pace_history(policy(), rows, time_zone=ZONE, now=NOW, last_synced_at=NOW, account_history={})
    assert all(point.observed_daily_minor_units is None for window in result.windows for point in window.points)


def test_known_empty_window_is_zero_and_includes_local_purchase_day() -> None:
    rows = [
        row("card", date(2025, 12, 1), 0, "old card"),
        row("bank", date(2025, 12, 1), 0, "old bank"),
        row("card", date(2026, 3, 2), 70, "late", authorized_datetime=datetime(2026, 3, 1, 23, 30, tzinfo=UTC)),
    ]
    result = pace_history(policy(), rows, time_zone=ZONE, now=NOW, last_synced_at=NOW, account_history={})
    seven = next(window for window in result.windows if window.period_id == PeriodId.ROLLING_7D)
    assert seven.points[-1].observed_daily_minor_units == 0
    assert seven.points[0].date == date(2026, 3, 3)
    assert seven.points[0].observed_daily_minor_units == 1_000


def test_conflicting_account_opening_evidence_leaves_history_unknown() -> None:
    rows = [row("card", date(2025, 12, 1), 0, "card"), row("bank", date(2025, 12, 1), 0, "bank")]
    evidence = AccountHistory(
        opening=AccountOpening(earliest=date(2026, 1, 1), latest=date(2026, 1, 1), source="synthetic", as_of=NOW.date())
    )
    result = pace_history(
        policy(), rows, time_zone=ZONE, now=NOW, last_synced_at=NOW, account_history={"bank": evidence}
    )
    assert all(point.observed_daily_minor_units is None for window in result.windows for point in window.points)
    assert any("predates" in issue for issue in result.issues)


if __name__ == "__main__":
    pytest_bazel.main()

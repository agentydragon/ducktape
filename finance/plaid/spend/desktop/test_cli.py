"""Synthetic desktop spend CLI display tests."""

from datetime import UTC, date, datetime

import pytest
import pytest_bazel

from finance.plaid.spend.allowance import (
    AllowanceSpendPeriod,
    AllowanceView,
    ForecastView,
    PaceAlert,
    Period,
    PeriodId,
    RecordedPacePeriod,
    Status,
    UnmatchedCharges,
)
from finance.plaid.spend.desktop.cli import _format_money, _format_view
from finance.plaid.spend.models import AlertState, CardView, SpendView, StatementCycle

NOW = datetime(2026, 1, 31, 16, tzinfo=UTC)


def sample_card() -> CardView:
    return CardView(
        account_id="example-card",
        label="Sample card",
        account_name="Example card",
        institution_name="Example bank",
        mask="1234",
        currency="USD",
        statement_period=StatementCycle(start=date(2026, 1, 1), through=NOW.date()),
        spend_minor_units=1200,
        posted_minor_units=900,
        pending_minor_units=300,
        limit_minor_units=10000,
        alert_threshold_percent=80,
        spend_percent=12.0,
        alert_state=AlertState.NORMAL,
        last_synced_at=NOW,
    )


def sample_allowance(status: Status = Status.ACTIVE) -> AllowanceView:
    active = status == Status.ACTIVE
    return AllowanceView(
        status=status,
        currency="USD",
        monthly_minor_units=10000,
        activation_at=date(2026, 1, 31),
        available_minor_units=8800 if active else None,
        next_credit_at=datetime(2026, 2, 28, tzinfo=UTC) if active else None,
        posted_minor_units=900 if active else 0,
        pending_minor_units=300 if active else 0,
        review_minor_units=0,
        review_transaction_count=0,
        unmatched_refunds_minor_units=0,
        spend_periods=[
            AllowanceSpendPeriod(
                period=Period.for_id(PeriodId.CREDIT_CYCLE, NOW.date(), NOW.date()),
                counted_from=NOW.date(),
                spend_minor_units=1200,
            )
        ]
        if active
        else [],
        recorded_pace_periods=[
            RecordedPacePeriod(
                period=Period.for_id(PeriodId.ROLLING_7D, NOW.date()),
                observed_daily_minor_units=1200,
                unmatched_charges=UnmatchedCharges(count=2, amount_minor_units=300),
            ),
            RecordedPacePeriod(period=Period.for_id(PeriodId.ROLLING_30D, NOW.date()), observed_daily_minor_units=1000),
        ]
        if active
        else [],
        forecast=ForecastView(
            basis_period=Period.for_id(PeriodId.ROLLING_7D, NOW.date()),
            daily_pace_minor_units=1200 if active else None,
            projected_cycle_end_minor_units=-200 if active else None,
            estimated_exhaustion_at=datetime(2026, 2, 14, tzinfo=UTC) if active else None,
            alert_state=PaceAlert.WARNING if active else PaceAlert.UNAVAILABLE,
        ),
        spending_signal=PaceAlert.WARNING if active else PaceAlert.UNAVAILABLE,
        last_synced_at=NOW if active else None,
        note=None if active else "Account coverage or sync freshness unavailable; do not rely on the allowance.",
    )


def test_formats_active_allowance_and_cards() -> None:
    output = _format_view(SpendView(generated_at=NOW, cards=[sample_card()], allowance=sample_allowance()), "ready", "")
    assert "Available: USD 88" in output
    assert "Monthly credit: USD 100" in output
    assert "Spent this credit cycle: USD 12" in output
    assert "Pending (included): USD 3" in output
    assert "Provisional leash signal: warning" in output
    assert "7-day recorded flexible pace: USD 12/day" in output
    assert "30-day recorded flexible pace: USD 10/day" in output
    assert "Provisional leash rate:" in output
    assert "7d unmatched: 2 (USD 3)" in output
    assert "Estimated balance before next credit:" in output
    assert "USD 2" in output
    assert "2026-02-28T00:00:00Z" in output
    assert "Sample card" in output


def test_formats_unavailable_allowance_without_inventing_balance() -> None:
    output = _format_view(
        SpendView(generated_at=NOW, cards=[], allowance=sample_allowance(Status.UNAVAILABLE)), "ready", ""
    )
    assert "Status: unavailable" in output
    assert "do not rely on the allowance" in output
    assert "Available:" not in output
    assert "No card data is available." in output


def test_without_allowance_keeps_existing_card_output() -> None:
    output = _format_view(SpendView(generated_at=NOW, cards=[sample_card()]), "ready", "")
    assert "Sample card" in output
    assert "Spend: USD 12" in output
    assert "Flexible allowance" not in output


@pytest.mark.parametrize(
    ("minor_units", "expected"),
    [
        (0, "USD 0"),
        (1, "<USD 1"),
        (-1, "-<USD 1"),
        (49, "<USD 1"),
        (50, "USD 1"),
        (1250, "USD 13"),
        (1299, "USD 13"),
        (-1299, "-USD 13"),
    ],
)
def test_compact_money_preserves_nonzero_sign(minor_units: int, expected: str) -> None:
    assert _format_money(minor_units, "USD") == expected


if __name__ == "__main__":
    pytest_bazel.main()

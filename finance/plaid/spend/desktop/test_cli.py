"""Synthetic desktop spend CLI display tests."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

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
from finance.plaid.spend.desktop.cli import _format_money, _format_timestamp, _format_view
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
        spend_percent=Decimal("12.0"),
        alert_state=AlertState.NORMAL,
        last_synced_at=NOW,
    )


def sample_allowance(status: Status = Status.ACTIVE) -> AllowanceView:
    active = status == Status.ACTIVE
    return AllowanceView(
        time_zone="UTC",
        status=status,
        currency="USD",
        monthly_minor_units=10000,
        # Deliberately differs from the monthly-derived rate to prove the CLI displays the server field.
        daily_reference_minor_units=2345,
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
    assert "Available: $88" in output
    assert "Monthly credit: $100" in output
    assert "Spent this credit cycle: $12" in output
    assert "Pending (included): $3.00" in output
    assert "Provisional leash signal: warning" in output
    assert "7-day recorded flexible pace: $12/day" in output
    assert "30-day recorded flexible pace: $10/day" in output
    assert "Provisional leash rate: ~$23/day" in output
    assert "7d unmatched: 2 ($3.00)" in output
    assert "Estimated balance before next credit:" in output
    assert "-$2.00" in output
    assert "Next credit:" in output
    assert "Sample card" in output
    assert "$12 / $100 · 12.0%" in output


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
    assert "Spend: $12" in output
    assert "Flexible allowance" not in output


def test_card_percent_uses_decimal_json_string() -> None:
    payload = sample_card().model_dump(mode="json")

    assert payload["spend_percent"] == "12.0"
    assert CardView.model_validate(payload).spend_percent == Decimal("12.0")


@pytest.mark.parametrize(
    ("minor_units", "expected"),
    [
        (0, "$0"),
        (1, "$0.01"),
        (-1, "-$0.01"),
        (49, "$0.49"),
        (50, "$0.50"),
        (999, "$9.99"),
        (1000, "$10"),
        (1250, "$13"),
        (1299, "$13"),
        (-1299, "-$13"),
    ],
)
def test_compact_money_preserves_nonzero_sign(minor_units: int, expected: str) -> None:
    assert _format_money(minor_units, "USD") == expected


def test_summary_timestamps_use_viewer_zone_and_conditional_precision() -> None:
    zone = timezone(timedelta(hours=-8))
    now = datetime(2026, 12, 20, 20, tzinfo=UTC)
    assert _format_timestamp(now + timedelta(days=14), now=now, tz=zone) == "Jan 3, 2027, 12:00"
    assert _format_timestamp(now + timedelta(days=14, seconds=1), now=now, tz=zone) == "Jan 3, 2027"
    assert _format_timestamp(now - timedelta(days=1), now=now, tz=zone) == "Dec 19, 12:00"
    assert _format_timestamp(None, now=now, tz=zone) == "unknown"


def test_compact_money_uses_currency_precision() -> None:
    assert _format_money(9, "JPY") == "¥9"
    assert _format_money(1, "KWD") == "KWD0.001"


def test_timestamp_cutoff_is_elapsed_time_across_dst() -> None:
    zone = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 3, 1, 20, tzinfo=UTC)
    assert _format_timestamp(now + timedelta(days=14), now=now, tz=zone) == "Mar 15, 13:00"
    assert _format_timestamp(now + timedelta(days=14, seconds=1), now=now, tz=zone) == "Mar 15"


if __name__ == "__main__":
    pytest_bazel.main()

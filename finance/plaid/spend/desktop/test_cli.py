"""Synthetic desktop spend CLI display tests."""

from datetime import UTC, date, datetime

import pytest
import pytest_bazel

from finance.plaid.spend.allowance import AllowanceView, PaceAlert, Status, Windows
from finance.plaid.spend.desktop.cli import _print_view
from finance.plaid.spend.models import AlertState, CardView, SpendView

NOW = datetime(2026, 1, 31, 16, tzinfo=UTC)


def sample_card() -> CardView:
    return CardView(
        account_id="example-card",
        label="Sample card",
        account_name="Example card",
        institution_name="Example bank",
        mask="1234",
        currency="USD",
        cycle_start=date(2026, 1, 1),
        spend_minor_units=1200,
        posted_minor_units=900,
        pending_minor_units=300,
        limit_minor_units=10000,
        alert_threshold_percent=80,
        spend_percent=12.0,
        alert_state=AlertState.NORMAL,
        last_synced_at=NOW,
        statement_available=True,
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
        windows_minor_units=Windows(
            current_credit_cycle_minor_units=1200,
            calendar_month_minor_units=1200,
            year_to_date_minor_units=1200,
            trailing_7_days_minor_units=1200,
            trailing_30_days_minor_units=1200,
        )
        if active
        else None,
        trailing_7_daily_minor_units=1200 if active else None,
        estimated_exhaustion_at=datetime(2026, 2, 14, tzinfo=UTC) if active else None,
        alert_state=PaceAlert.WARNING if active else PaceAlert.UNAVAILABLE,
        last_synced_at=NOW if active else None,
        projected_cycle_end_minor_units=-200 if active else None,
        note=None if active else "Account coverage or sync freshness unavailable; do not rely on the allowance.",
    )


def test_prints_active_allowance_and_cards(capsys: pytest.CaptureFixture[str]) -> None:
    _print_view(SpendView(generated_at=NOW, cards=[sample_card()], allowance=sample_allowance()), "ready", "")
    output = capsys.readouterr().out
    assert "Available: USD 88.00" in output
    assert "Monthly credit: USD 100.00" in output
    assert "Spent this credit cycle: USD 12.00" in output
    assert "Pending (included): USD 3.00" in output
    assert "Pace: warning" in output
    assert "Estimated balance before next credit:" in output
    assert "2.00" in output
    assert "2026-02-28T00:00:00Z" in output
    assert "Sample card" in output


def test_prints_unavailable_allowance_without_inventing_balance(capsys: pytest.CaptureFixture[str]) -> None:
    _print_view(SpendView(generated_at=NOW, cards=[], allowance=sample_allowance(Status.UNAVAILABLE)), "ready", "")
    output = capsys.readouterr().out
    assert "Status: unavailable" in output
    assert "do not rely on the allowance" in output
    assert "Available:" not in output
    assert "No card data is available." in output


def test_without_allowance_keeps_existing_card_output(capsys: pytest.CaptureFixture[str]) -> None:
    _print_view(SpendView(generated_at=NOW, cards=[sample_card()]), "ready", "")
    output = capsys.readouterr().out
    assert "Sample card" in output
    assert "Spend: USD 12.00" in output
    assert "Flexible allowance" not in output


if __name__ == "__main__":
    pytest_bazel.main()

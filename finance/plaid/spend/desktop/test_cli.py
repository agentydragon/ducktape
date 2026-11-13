"""Synthetic desktop spend CLI display tests."""

import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

import pytest
import pytest_bazel
from python.runfiles import Runfiles
from syrupy.assertion import SnapshotAssertion

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
from finance.plaid.spend.desktop import cli
from finance.plaid.spend.desktop.cli import _format_money, _format_summary, _format_timestamp, _format_view, _sparkline
from finance.plaid.spend.models import AlertState, CardView, SpendView, StatementCycle
from finance.plaid.spend.pace_history_models import PaceHistory, PaceHistoryPoint, PaceHistoryWindow
from util.date_presentation import format_calendar_date

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
    assert "Pending (included): $3" in output
    assert "Provisional leash signal: warning" in output
    assert "7-day recorded flexible pace: $12/day" in output
    assert "30-day recorded flexible pace: $10/day" in output
    assert "Provisional leash rate: ~$23/day" in output
    assert "7d unmatched: 2 ($3)" in output
    assert "Estimated balance before next credit:" in output
    assert "-$2" in output
    assert "Next credit:" in output
    assert "Projected exhaustion (no future credits):" in output
    assert "Oldest linked sync:" in output
    assert "Sample card" in output
    assert "$12 / $100 · 12.0%" in output


def test_compact_summary_preserves_pace_and_actionable_information() -> None:
    view = SpendView(generated_at=NOW, cards=[sample_card()], allowance=sample_allowance())
    output = _format_summary(view, "ready", "", "7d", "history")
    assert "Available: $88" in output
    assert "7d pace: $12/day · 51% of leash · leash ~$23/day" in output
    assert "Unmatched: 2 ($3)" in output
    assert "Sample card" in output
    assert "history" in output
    assert "Monthly credit" not in output
    assert "Forecast basis" not in output


def test_compact_summary_does_not_hide_unknown_source_sync() -> None:
    card = sample_card().model_copy(update={"last_synced_at": None})
    view = SpendView(generated_at=NOW, cards=[card], allowance=sample_allowance())
    assert "Oldest linked sync: unknown" in _format_summary(view, "ready", "", "7d", None)


def test_sparkline_scale_thresholds_gaps_and_history_span() -> None:
    values = [0, 100, 120, 121, None, 40, 50, 60]
    history = PaceHistory(
        generated_at=NOW,
        time_zone="UTC",
        currency="USD",
        status=Status.ACTIVE,
        last_synced_at=NOW,
        daily_reference_minor_units=100,
        windows=[
            PaceHistoryWindow(
                period_id=PeriodId.ROLLING_7D,
                points=[PaceHistoryPoint(date=NOW.date(), observed_daily_minor_units=v) for v in values],
            )
        ],
        issues=[],
    )
    graph = _sparkline(history, "7d", 7, "auto", True)
    assert "last 7 days" in graph
    assert "gaps: unknown" in graph
    assert "\x1b[32m" in graph
    assert "\x1b[33m" in graph
    assert "\x1b[31m" in graph
    assert _sparkline(history, "7d", 7, "zero", False) != _sparkline(history, "7d", 7, "auto", False)
    assert "\x1b[" not in _sparkline(history, "7d", 7, "zero", False)


@pytest.mark.asyncio
async def test_compact_cli_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], snapshot: SnapshotAssertion
) -> None:
    view = SpendView(generated_at=NOW, cards=[sample_card()], allowance=sample_allowance())
    history = PaceHistory(
        generated_at=NOW,
        time_zone="UTC",
        currency="USD",
        status=Status.ACTIVE,
        last_synced_at=NOW,
        daily_reference_minor_units=2345,
        windows=[
            PaceHistoryWindow(
                period_id=PeriodId.ROLLING_7D,
                points=[
                    PaceHistoryPoint(date=NOW.date() - timedelta(days=6 - i), observed_daily_minor_units=value)
                    for i, value in enumerate([900, 1200, 1600, None, 2200, 2500, 2800])
                ],
            )
        ],
        issues=[],
    )

    class Bus:
        async def connect(self) -> Bus:
            return self

        def disconnect(self) -> None:
            pass

    async def call(_bus: object, member: str) -> list[str]:
        return [history.model_dump_json() if member == "GetPaceHistory" else view.model_dump_json()]

    async def prop(_bus: object, name: str) -> str:
        return "ready" if name == "Status" else ""

    format_timestamp = cli._format_timestamp
    monkeypatch.setattr(cli, "MessageBus", Bus)
    monkeypatch.setattr(cli, "_call", call)
    monkeypatch.setattr(cli, "_property", prop)
    monkeypatch.setattr(cli, "_format_timestamp", lambda value: format_timestamp(value, now=NOW, tz=UTC))
    await cli._run("show", False)
    assert capsys.readouterr().out.rstrip("\n") == snapshot


@pytest.mark.asyncio
async def test_cli_history_failure_does_not_hide_current_view(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    view = SpendView(generated_at=NOW, cards=[sample_card()], allowance=sample_allowance()).model_dump(mode="json")
    calls: list[str] = []

    class Bus:
        async def connect(self) -> Bus:
            return self

        def disconnect(self) -> None:
            pass

    async def call(_bus: object, member: str) -> list[str]:
        calls.append(member)
        if member == "GetPaceHistory":
            raise RuntimeError("offline")
        return [json.dumps(view)]

    async def prop(_bus: object, name: str) -> str:
        return "ready" if name == "Status" else ""

    monkeypatch.setattr(cli, "MessageBus", Bus)
    monkeypatch.setattr(cli, "_call", call)
    monkeypatch.setattr(cli, "_property", prop)
    await cli._run("show", False)
    output = capsys.readouterr().out
    assert "Available: $88" in output
    assert "Pace history unavailable" in output
    assert calls == ["GetView", "GetPaceHistory"]
    calls.clear()
    await cli._run("show", True)
    assert json.loads(capsys.readouterr().out) == view
    assert calls == ["GetView"]


@pytest.mark.asyncio
async def test_history_response_uses_shared_wire_model(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "generated_at": NOW.isoformat(),
        "time_zone": "UTC",
        "currency": "USD",
        "status": "active",
        "last_synced_at": NOW.isoformat(),
        "daily_reference_minor_units": 100,
        "windows": [
            {
                "period_id": "rolling_7d",
                "points": [
                    {"date": NOW.date().isoformat(), "observed_daily_minor_units": 100},
                    {"date": NOW.date().isoformat(), "observed_daily_minor_units": 120},
                ],
            }
        ],
        "issues": [],
    }

    async def call(_bus: object, _member: str) -> list[str]:
        return [json.dumps(payload)]

    monkeypatch.setattr(cli, "_call", call)
    bus = cast(cli.MessageBus, object())
    assert "last 2 days" in await cli._history_line(bus, "7d", 30, "zero", False)
    del payload["generated_at"]
    assert await cli._history_line(bus, "7d", 30, "zero", False) == "Pace history unavailable"


def test_cli_distinguishes_unknown_and_zero_pace_forecasts() -> None:
    allowance = sample_allowance()
    warming_forecast = allowance.forecast.model_copy(
        update={
            "daily_pace_minor_units": None,
            "projected_cycle_end_minor_units": None,
            "estimated_exhaustion_at": None,
            "alert_state": PaceAlert.UNAVAILABLE,
        }
    )
    warming = allowance.model_copy(update={"forecast": warming_forecast, "spending_signal": PaceAlert.UNAVAILABLE})
    warming_output = _format_view(SpendView(generated_at=NOW, cards=[], allowance=warming), "ready", "")
    assert "Projected exhaustion (no future credits): unknown (pace warming up)" in warming_output
    assert "no recent spend" not in warming_output

    zero_forecast = allowance.forecast.model_copy(
        update={
            "daily_pace_minor_units": 0,
            "projected_cycle_end_minor_units": allowance.available_minor_units,
            "estimated_exhaustion_at": None,
            "alert_state": PaceAlert.NORMAL,
        }
    )
    zero_pace = allowance.model_copy(update={"forecast": zero_forecast, "spending_signal": PaceAlert.NORMAL})
    zero_output = _format_view(SpendView(generated_at=NOW, cards=[], allowance=zero_pace), "ready", "")
    assert "Projected exhaustion (no future credits): not projected (zero pace)" in zero_output

    missing_projection = zero_forecast.model_copy(update={"daily_pace_minor_units": 1})
    inconsistent = allowance.model_copy(update={"forecast": missing_projection})
    inconsistent_output = _format_view(SpendView(generated_at=NOW, cards=[], allowance=inconsistent), "ready", "")
    assert "Projected exhaustion (no future credits): unknown (forecast unavailable)" in inconsistent_output

    exhausted = allowance.model_copy(update={"available_minor_units": 0})
    exhausted_output = _format_view(SpendView(generated_at=NOW, cards=[], allowance=exhausted), "ready", "")
    assert "Projected exhaustion (no future credits): already exhausted" in exhausted_output


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
        (300, "$3"),
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


def test_shared_date_vectors_match_aiquota_and_javascript(runfiles: Runfiles) -> None:
    vectors = json.loads((runfiles.root() / "ducktape/util/presentation_vectors/date_policy_vectors.json").read_text())
    zone = ZoneInfo(vectors["timeZone"])
    for vector in vectors["instants"]:
        now = datetime.fromisoformat(vector["now"])
        when = datetime.fromisoformat(vector["value"])
        assert _format_timestamp(when, now=now, tz=zone) == vector["expected"], vector["name"]

    for vector in vectors["calendarDates"]:
        now = datetime.fromisoformat(vector["now"])
        value = date.fromisoformat(vector["value"])
        assert format_calendar_date(value, now=now, tz=zone) == vector["expected"], vector["name"]


if __name__ == "__main__":
    pytest_bazel.main()

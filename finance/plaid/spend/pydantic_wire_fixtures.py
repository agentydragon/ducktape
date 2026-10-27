"""Emit real Pydantic JSON responses for the frontend wire-decoder tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from finance.plaid.spend.allowance import Period, PeriodId
from finance.plaid.spend.models import (
    AlertState,
    CardConfigurationView,
    CardView,
    SpendConfigurationView,
    SpendTransactionsView,
    SpendView,
    StatementCycle,
    TransactionPeriodSummary,
)
from finance.plaid.spend.sustainability import SustainabilityDashboard, disabled_view


def main() -> None:
    now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    today = date(2026, 10, 27)
    window_start = date(2026, 9, 28)
    fixtures = {
        "spendView": SpendView(
            generated_at=now,
            cards=[
                CardView(
                    account_id="fixture-account",
                    label="Everyday",
                    account_name="Everyday checking",
                    institution_name="Example Bank",
                    mask="1234",
                    currency="USD",
                    statement_period=StatementCycle(start=window_start, through=today),
                    spend_minor_units=12345,
                    posted_minor_units=12000,
                    pending_minor_units=345,
                    limit_minor_units=500000,
                    alert_threshold_percent=80,
                    spend_percent=2.469,
                    alert_state=AlertState.NORMAL,
                    last_synced_at=now,
                )
            ],
        ).model_dump_json(),
        "spendConfiguration": SpendConfigurationView(
            time_zone="UTC",
            cards=[
                CardConfigurationView(
                    label="Everyday", enabled=True, limit_minor_units=500000, alert_threshold_percent=80
                )
            ],
            allowance=None,
        ).model_dump_json(),
        "spendTransactions": SpendTransactionsView(
            generated_at=now,
            requested_period_id=PeriodId.ROLLING_30D,
            period=Period(id=PeriodId.ROLLING_30D, start=window_start, end=today),
            summary=TransactionPeriodSummary(
                transaction_count=1,
                net_allowance_spend_minor_units=12345,
                unmatched_charge_count=0,
                unmatched_charge_minor_units=0,
            ),
            allowance=None,
            rows=[],
        ).model_dump_json(),
        "sustainabilityDashboard": SustainabilityDashboard(
            default_window_days=90, windows=[disabled_view(now, "UTC", today)]
        ).model_dump_json(),
    }
    print("export const pydanticWireJson = " + json.dumps(fixtures, separators=(",", ":")) + " as const;")


if __name__ == "__main__":
    main()

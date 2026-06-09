"""Frozen monthly historical series (data/*.csv) for series-derived tasks.

Months are keyed by first-of-month dates and may be missing — e.g. BLS
published no October-2025 CPI, so FRED has no row for it.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"


def add_months(month: date, n: int) -> date:
    """First-of-month date `n` months after `month` (itself first-of-month)."""
    year, month_index = divmod(month.year * 12 + month.month - 1 + n, 12)
    return date(year, month_index + 1, 1)


def month_end(month: date) -> date:
    return add_months(month, 1) - timedelta(days=1)


@dataclass(frozen=True)
class MonthlySeries:
    series_id: str
    description: str
    unit: str
    provenance: str
    values: dict[date, float]

    def max_observed_between(self, after: date, through: date) -> float | None:
        """Max value over observed months in (after, through]; None if no month is observed."""
        window = [value for month, value in self.values.items() if after < month <= through]
        return max(window) if window else None

    def last_month(self) -> date:
        return max(self.values)


def _load(filename: str, series_id: str, description: str, unit: str, provenance: str) -> MonthlySeries:
    with (_DATA_DIR / filename).open() as f:
        values = {date.fromisoformat(f"{row['month']}-01"): float(row["value"]) for row in csv.DictReader(f)}
    return MonthlySeries(series_id=series_id, description=description, unit=unit, provenance=provenance, values=values)


def default_series() -> tuple[MonthlySeries, ...]:
    return (
        _load(
            filename="sp500_monthly.csv",
            series_id="sp500",
            description="S&P 500 index (last daily close of the month)",
            unit="index points",
            provenance="Yahoo Finance ^GSPC daily closes aggregated to month-end, fetched 2026-06-09",
        ),
        _load(
            filename="btcusd_monthly.csv",
            series_id="btcusd",
            description="Bitcoin price in US dollars (last daily close of the month)",
            unit="USD",
            provenance="Yahoo Finance BTC-USD daily closes aggregated to month-end, fetched 2026-06-09",
        ),
        _load(
            filename="cpiaucsl_monthly.csv",
            series_id="cpi",
            description="US CPI-U index level, seasonally adjusted (FRED series CPIAUCSL)",
            unit="index points",
            provenance="FRED CPIAUCSL monthly observations, fetched 2026-06-09",
        ),
    )

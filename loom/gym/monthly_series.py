"""Monthly historical series for series-derived tasks, read from the augur-evidence checkout.

The repo vendors no market data: the augur-evidence scraper maintains raw
upstream files in the private augur-evidence Forgejo repo, and the shared
`finance/evidence` loaders parse them into typed monthly levels. Loom adapts
those to per-month dicts, trims to its history window, drops the partial
current month, and validates known-history values at load — so a bad scrape
or format drift fails loudly instead of poisoning task outcomes.

Months are keyed by first-of-month dates and may be missing (e.g. the
Oct-2025 BLS CPI gap).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from finance.evidence.loading import read_monthly_levels
from finance.evidence.sources import FRED_CPI, FRED_SP500, YAHOO_BTC, EvidenceSource

_HISTORY_START = date(2013, 1, 1)


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


@dataclass(frozen=True)
class SeriesSpec:
    series_id: str
    description: str
    unit: str
    source: EvidenceSource


SERIES_SPECS = (
    SeriesSpec(
        series_id="sp500",
        description="S&P 500 index (last observation of the month)",
        unit="index points",
        source=FRED_SP500,
    ),
    SeriesSpec(
        series_id="btcusd",
        description="Bitcoin price in US dollars (last observation of the month)",
        unit="USD",
        source=YAHOO_BTC,
    ),
    SeriesSpec(
        series_id="cpi",
        description="US CPI-U index level, seasonally adjusted (FRED series CPIAUCSL)",
        unit="index points",
        source=FRED_CPI,
    ),
)


@dataclass(frozen=True)
class KnownValue:
    series_id: str
    month: date
    value: float
    tolerance: float


# Famous month-end values; a load failing these is a bad scrape or format
# drift, not new data. BTC's tolerance is wide because Yahoo may serve weekly
# or monthly bars under range=max, shifting the "last observation" by days.
KNOWN_HISTORY = (
    KnownValue(series_id="sp500", month=date(2024, 12, 1), value=5881.63, tolerance=1.0),
    KnownValue(series_id="btcusd", month=date(2024, 12, 1), value=93429.0, tolerance=5000.0),
    KnownValue(series_id="cpi", month=date(2024, 11, 1), value=316.5, tolerance=1.0),
)


def validate_known_history(series_id: str, values: dict[date, float]) -> None:
    for known in KNOWN_HISTORY:
        if known.series_id != series_id:
            continue
        if abs(values[known.month] - known.value) > known.tolerance:
            raise ValueError(
                f"bad evidence data: {series_id} {known.month:%Y-%m} = {values[known.month]}, expected ~{known.value}"
            )


def load_series(evidence_dir: Path, today: date | None = None) -> tuple[MonthlySeries, ...]:
    current_month = (today or datetime.now(UTC).date()).replace(day=1)
    series = []
    for spec in SERIES_SPECS:
        levels = read_monthly_levels(evidence_dir, spec.source)
        values = {level.month: level.value for level in levels if _HISTORY_START <= level.month < current_month}
        validate_known_history(spec.series_id, values)
        series.append(
            MonthlySeries(
                series_id=spec.series_id,
                description=spec.description,
                unit=spec.unit,
                provenance=f"augur-evidence {spec.source.output_filename}",
                values=values,
            )
        )
    return tuple(series)

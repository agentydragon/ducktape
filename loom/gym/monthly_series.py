"""Monthly historical series for series-derived tasks, read from the augur-evidence checkout.

The repo vendors no market data: the augur-evidence scraper (source catalog in
`finance/augur/ingest/evidence_sources.py`) maintains raw upstream files in
the private augur-evidence Forgejo repo. Loom reads the subset it needs from a
checkout — shared *data*, not shared code — aggregating observations to
last-value-per-month. Known-history values are validated at load, so format
drift or a bad scrape fails loudly instead of poisoning task outcomes.

Months are keyed by first-of-month dates and may be missing (e.g. the
Oct-2025 BLS CPI gap). The current (partial) month is always dropped.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

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
    """Identity + evidence-file location of one series; `fred_series_id` None means a Yahoo chart JSON."""

    series_id: str
    description: str
    unit: str
    evidence_filename: str
    fred_series_id: str | None


SERIES_SPECS = (
    SeriesSpec(
        series_id="sp500",
        description="S&P 500 index (last observation of the month)",
        unit="index points",
        evidence_filename="fred_sp500.csv",
        fred_series_id="SP500",
    ),
    SeriesSpec(
        series_id="btcusd",
        description="Bitcoin price in US dollars (last observation of the month)",
        unit="USD",
        evidence_filename="yahoo_btc_chart_adjusted.json",
        fred_series_id=None,
    ),
    SeriesSpec(
        series_id="cpi",
        description="US CPI-U index level, seasonally adjusted (FRED series CPIAUCSL)",
        unit="index points",
        evidence_filename="fred_cpi_us.csv",
        fred_series_id="CPIAUCSL",
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
    KnownValue(series_id="sp500", month=date(2024, 11, 1), value=6032.38, tolerance=1.0),
    KnownValue(series_id="sp500", month=date(2024, 12, 1), value=5881.63, tolerance=1.0),
    KnownValue(series_id="btcusd", month=date(2024, 12, 1), value=93429.0, tolerance=5000.0),
    KnownValue(series_id="cpi", month=date(2024, 11, 1), value=316.5, tolerance=1.0),
)


def monthly_from_fred_csv(text: str, fred_series_id: str) -> dict[date, float]:
    """Last observation per month from a FRED CSV (header `observation_date,<SERIES_ID>`).

    Handles daily and monthly FRED series alike; empty values (e.g. the
    Oct-2025 BLS gap) and pre-window history are skipped.
    """
    values: dict[date, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        observed, value = row["observation_date"], row[fred_series_id]
        if value != "" and observed >= _HISTORY_START.isoformat():
            values[date.fromisoformat(observed).replace(day=1)] = float(value)
    return values


def monthly_from_yahoo_chart(payload: dict) -> dict[date, float]:
    """Last adjusted close per month from a Yahoo v8 chart JSON, at whatever granularity it carries."""
    result = payload["chart"]["result"][0]
    adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    values: dict[date, float] = {}
    for timestamp, value in sorted(zip(result["timestamp"], adjusted, strict=True)):
        if value is not None and value > 0:
            moment = datetime.fromtimestamp(timestamp, UTC)
            month = date(moment.year, moment.month, 1)
            if month >= _HISTORY_START:
                values[month] = round(float(value), 2)
    return values


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
        path = evidence_dir / spec.evidence_filename
        if spec.fred_series_id is not None:
            values = monthly_from_fred_csv(path.read_text(), spec.fred_series_id)
        else:
            values = monthly_from_yahoo_chart(json.loads(path.read_text()))
        values = {month: value for month, value in values.items() if month < current_month}
        validate_known_history(spec.series_id, values)
        series.append(
            MonthlySeries(
                series_id=spec.series_id,
                description=spec.description,
                unit=spec.unit,
                provenance=f"augur-evidence {spec.evidence_filename}",
                values=values,
            )
        )
    return tuple(series)

"""Monthly historical series for series-derived tasks, read from cluster S3.

The repo deliberately vendors no market data: `//loom/gym:fetch_series` pulls
the upstream sources (FRED; Yahoo daily closes aggregated to month-end),
validates known history, and uploads normalized `month,value` CSVs to
`s3://loom-gym/series/`. This loader reads them back — the session's default
`AWS_*` credentials (claude-reader) have read access to the bucket.

Months are keyed by first-of-month dates and may be missing — e.g. BLS
published no October-2025 CPI, so FRED has no row for it.
"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from typing import Any

import boto3

BUCKET = "loom-gym"
DEFAULT_ENDPOINT = "https://s3.allegedly.works"
SERIES_PREFIX = "series/"


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
    """Identity + wire location of one series; shared by the fetcher and the loader."""

    series_id: str
    description: str
    unit: str

    @property
    def object_key(self) -> str:
        return f"{SERIES_PREFIX}{self.series_id}_monthly.csv"


SERIES_SPECS = (
    SeriesSpec(series_id="sp500", description="S&P 500 index (last daily close of the month)", unit="index points"),
    SeriesSpec(
        series_id="btcusd", description="Bitcoin price in US dollars (last daily close of the month)", unit="USD"
    ),
    SeriesSpec(
        series_id="cpi",
        description="US CPI-U index level, seasonally adjusted (FRED series CPIAUCSL)",
        unit="index points",
    ),
)


def parse_monthly_csv(text: str) -> dict[date, float]:
    """Parse a normalized `month,value` CSV; `#`-prefixed lines are provenance comments."""
    rows = csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#"))
    return {date.fromisoformat(f"{row['month']}-01"): float(row["value"]) for row in rows}


def s3_read_client() -> Any:
    """Client on the session's default AWS_* credentials (claude-reader has Read:loom-gym)."""
    return boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL", DEFAULT_ENDPOINT))


@cache
def default_series() -> tuple[MonthlySeries, ...]:
    client = s3_read_client()
    series = []
    for spec in SERIES_SPECS:
        body = client.get_object(Bucket=BUCKET, Key=spec.object_key)["Body"].read().decode()
        series.append(
            MonthlySeries(
                series_id=spec.series_id,
                description=spec.description,
                unit=spec.unit,
                provenance=f"s3://{BUCKET}/{spec.object_key}",
                values=parse_monthly_csv(body),
            )
        )
    return tuple(series)

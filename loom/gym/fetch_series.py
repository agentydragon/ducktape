"""Fetch the gym's series from their upstreams and publish them to cluster S3.

Pulls FRED (CPIAUCSL) and Yahoo Finance daily closes (^GSPC, BTC-USD;
aggregated to month-end; the current partial month is dropped), validates the
result against known history, and uploads normalized `month,value` CSVs to
`s3://loom-gym/series/` for `monthly_series.default_series()` to read.

The known-history validation is the gate that previously lived in unit tests
against vendored CSVs: an upstream format change or bad fetch fails here, at
ingest, instead of poisoning task outcomes.

Usage (writer credentials from the session env):

    bazelisk run //loom/gym:fetch_series
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from datetime import UTC, date

import httpx

from loom.gym.monthly_series import SERIES_SPECS, parse_monthly_csv
from loom.gym.results_store import BUCKET, results_client

logger = logging.getLogger(__name__)

_YAHOO_SYMBOLS = {"sp500": "%5EGSPC", "btcusd": "BTC-USD"}
_FRED_SERIES = {"cpi": "CPIAUCSL"}
_HISTORY_START = date(2013, 1, 1)


@dataclass(frozen=True)
class KnownValue:
    series_id: str
    month: date
    value: float
    tolerance: float


# Famous month-end closes; an upload failing these is a bad fetch, not new data.
KNOWN_HISTORY = (
    KnownValue(series_id="sp500", month=date(2024, 11, 1), value=6032.38, tolerance=1.0),
    KnownValue(series_id="sp500", month=date(2024, 12, 1), value=5881.63, tolerance=1.0),
    KnownValue(series_id="btcusd", month=date(2024, 12, 1), value=93429.0, tolerance=500.0),
    KnownValue(series_id="cpi", month=date(2024, 11, 1), value=316.5, tolerance=1.0),
)


def month_of_timestamp(timestamp: int) -> date:
    moment = datetime.datetime.fromtimestamp(timestamp, UTC)
    return date(moment.year, moment.month, 1)


def monthly_from_yahoo_chart(chart: dict, current_month: date) -> dict[date, float]:
    """Last daily close per month from a Yahoo chart payload; drops the partial current month."""
    result = chart["chart"]["result"][0]
    if (granularity := result["meta"]["dataGranularity"]) != "1d":
        raise ValueError(f"expected daily bars, got {granularity=} (range=max silently degrades to 3mo)")
    month_last: dict[date, float] = {}
    for timestamp, close in zip(result["timestamp"], result["indicators"]["quote"][0]["close"], strict=True):
        if close is not None:
            month_last[month_of_timestamp(timestamp)] = round(close, 2)
    return {month: value for month, value in month_last.items() if month < current_month}


def monthly_from_fred_csv(text: str) -> dict[date, float]:
    """Monthly observations from a FRED CSV; empty values (e.g. the Oct-2025 BLS gap) are skipped."""
    values: dict[date, float] = {}
    for line in text.splitlines()[1:]:
        observation_date, _, value = line.partition(",")
        if value != "" and observation_date >= _HISTORY_START.isoformat():
            values[date.fromisoformat(observation_date).replace(day=1)] = float(value)
    return values


def render_csv(values: dict[date, float], provenance: str) -> str:
    # str(float) round-trips exactly through parse_monthly_csv; .2f formatting would not.
    lines = [f"# {provenance}", "month,value"]
    lines += [f"{month:%Y-%m},{value}" for month, value in sorted(values.items())]
    return "\n".join(lines) + "\n"


def validate_known_history(series_id: str, values: dict[date, float]) -> None:
    for known in KNOWN_HISTORY:
        if known.series_id != series_id:
            continue
        if abs(values[known.month] - known.value) > known.tolerance:
            raise ValueError(
                f"bad fetch: {series_id} {known.month:%Y-%m} = {values[known.month]}, expected ~{known.value}"
            )


def fetch_all(client: httpx.Client, now: datetime.datetime) -> dict[str, dict[date, float]]:
    current_month = date(now.year, now.month, 1)
    history_start = datetime.datetime(_HISTORY_START.year, _HISTORY_START.month, _HISTORY_START.day, tzinfo=UTC)
    period = f"period1={int(history_start.timestamp())}&period2={int(now.timestamp())}"
    fetched: dict[str, dict[date, float]] = {}
    for series_id, symbol in _YAHOO_SYMBOLS.items():
        response = client.get(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?{period}&interval=1d",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        fetched[series_id] = monthly_from_yahoo_chart(response.json(), current_month)
    for series_id, fred_id in _FRED_SERIES.items():
        response = client.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={fred_id}")
        response.raise_for_status()
        fetched[series_id] = monthly_from_fred_csv(response.text)
    return fetched


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    now = datetime.datetime.now(UTC)
    with httpx.Client(timeout=60.0) as client:
        fetched = fetch_all(client, now)
    s3 = results_client()
    for spec in SERIES_SPECS:
        values = fetched[spec.series_id]
        validate_known_history(spec.series_id, values)
        provenance = f"{spec.description}; fetched {now:%Y-%m-%d} by //loom/gym:fetch_series"
        body = render_csv(values, provenance)
        assert parse_monthly_csv(body) == values  # the loader must round-trip what we publish
        s3.put_object(Bucket=BUCKET, Key=spec.object_key, Body=body.encode(), ContentType="text/csv")
        print(
            f"uploaded s3://{BUCKET}/{spec.object_key} ({len(values)} months, {min(values):%Y-%m}..{max(values):%Y-%m})"
        )


if __name__ == "__main__":
    main()

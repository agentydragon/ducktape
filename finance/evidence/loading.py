"""Typed loaders for the augur-evidence checkout: raw upstream bytes → polars frames → monthly levels.

Shared floor for every consumer of the evidence data (augur's fit/calibration
pipeline, loom's forecasting gym) — consumers never import each other, only
this package. The frame parsers are pure functions over bytes so callers
control file resolution (and tests can inject malformed data);
`source_bytes` reads from an explicit checkout directory and
`evidence_dir_from_env` resolves the conventional `AUGUR_EVIDENCE_DIR`.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from finance.evidence.sources import EvidenceKind, EvidenceSource


def evidence_dir_from_env() -> Path:
    """The conventional checkout location: `AUGUR_EVIDENCE_DIR` (git-synced in-cluster, a clone elsewhere)."""
    evidence_dir = os.environ.get("AUGUR_EVIDENCE_DIR")
    if evidence_dir is None:
        raise RuntimeError("AUGUR_EVIDENCE_DIR is unset; evidence is read from a checkout of augur-evidence")
    return Path(evidence_dir)


def source_bytes(evidence_dir: Path, source: EvidenceSource) -> bytes:
    """Raw bytes for one evidence series; a missing file raises rather than serving absent data."""
    path = evidence_dir / source.output_filename
    if not path.exists():
        raise RuntimeError(f"evidence not found in checkout: {path}")
    return path.read_bytes()


def fred_series_frame(data: bytes, source: EvidenceSource) -> pl.DataFrame:
    """Parse a FRED graph CSV down to a sorted, positive `(date, value)` frame."""
    column = source.series_id  # a FRED CSV is headed by its series id
    frame = pl.read_csv(data, infer_schema=False)
    if "observation_date" not in frame.columns or column not in frame.columns:
        raise ValueError(f"{source.provenance_label} must contain observation_date and {column}")
    out = (
        frame.select(
            pl.col("observation_date").str.to_date("%Y-%m-%d").alias("date"),
            pl.col(column).cast(pl.Float64, strict=False).alias("value"),
        )
        .drop_nulls("value")
        .filter(pl.col("value") > 0)
        .sort("date")
    )
    if out.is_empty():
        raise ValueError(f"{source.provenance_label} contains no positive observations for {column}")
    return out


def zillow_city_series_frame(data: bytes, source: EvidenceSource, *, region_name: str, state: str) -> pl.DataFrame:
    """One Zillow city's monthly `(month, value)` series.

    The Zillow city CSV is wide (a row per region, a column per month named YYYY-MM-DD); take the one
    city row, unpivot its date columns into rows, truncate each to its month, and keep positive values.
    """
    frame = pl.read_csv(data, infer_schema=False)
    date_columns = [c for c in frame.columns if len(c) == 10 and c[4] == "-" and c[7] == "-"]
    row = frame.filter(
        (pl.col("RegionType") == "city") & (pl.col("RegionName") == region_name) & (pl.col("State") == state)
    )
    if row.is_empty():
        raise ValueError(f"{source.provenance_label} does not contain a city row for {region_name}, {state}")
    monthly = (
        row.head(1)
        .select(date_columns)
        .unpivot(variable_name="month_str", value_name="value")
        .select(
            pl.col("month_str").str.to_date("%Y-%m-%d").dt.truncate("1mo").alias("month"),
            pl.col("value").cast(pl.Float64, strict=False),
        )
        .drop_nulls("value")
        .filter(pl.col("value") > 0)
        .sort("month")
    )
    if monthly.is_empty():
        raise ValueError(f"{source.provenance_label} row for {region_name}, {state} contains no monthly values")
    return monthly


DAILY_GRANULARITY = "1d"


def yahoo_adjusted_close_frame(data: bytes, source: EvidenceSource, *, minimum_samples: int = 36) -> pl.DataFrame:
    """Parse a Yahoo-Finance v8 chart JSON down to a sorted `(date, value)` adjusted-close frame.

    Every Yahoo source is requested at `interval=1d`, so a payload that came back coarser was
    silently downgraded by the API and is rejected here. That check is the point: monthly bars
    still parse, still collapse to the same months, and still clear `minimum_samples`, so the
    only place the downgrade shows is `meta.dataGranularity` — which nothing read until a
    `range=max` request started answering with 404 monthly rows for SPY instead of 8437 daily.
    `minimum_samples` cannot cover this: `read_monthly_levels` uses the 36 default and a coarse
    file passes it comfortably.

    SPY's daily history has ~8k rows. Multiple ticks on one calendar day collapse to the last
    (by timestamp), so a daily payload still yields one row per trading day.
    """
    payload = json.loads(data)
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, dict):
        raise ValueError(f"{source.provenance_label} does not contain a Yahoo chart result")
    granularity = (result.get("meta") or {}).get("dataGranularity")
    if granularity != DAILY_GRANULARITY:
        raise ValueError(
            f"{source.provenance_label} was served at {granularity!r} granularity, not "
            f"{DAILY_GRANULARITY!r}. Yahoo downgrades some window requests silently; the series "
            "would parse and fit on coarser data instead of failing. Re-fetch with explicit "
            "period1/period2 (see `sources._yahoo`)."
        )
    timestamps = result.get("timestamp") or []
    adjusted = (((result.get("indicators") or {}).get("adjclose") or [{}])[0]).get("adjclose") or []
    if len(timestamps) != len(adjusted):
        raise ValueError(f"{source.provenance_label} timestamp and adjusted-close arrays have different lengths")
    ts_seconds: list[int] = []
    dates: list[date] = []
    values: list[float] = []
    for timestamp, value in zip(timestamps, adjusted, strict=True):
        if value is None:
            continue
        number = float(value)
        if math.isfinite(number) and number > 0:
            ts_seconds.append(int(timestamp))
            dates.append(datetime.fromtimestamp(int(timestamp), tz=UTC).date())
            values.append(number)
    frame = (
        pl.DataFrame(
            {"ts": ts_seconds, "date": dates, "value": values},
            schema={"ts": pl.Int64, "date": pl.Date, "value": pl.Float64},
        )
        .group_by("date")
        .agg(pl.col("value").sort_by("ts").last())
        .sort("date")
    )
    if frame.height < minimum_samples:
        raise ValueError(
            f"{source.provenance_label} did not yield a credible adjusted-close history "
            f"({frame.height} samples < minimum {minimum_samples})"
        )
    return frame


def monthly_last(series: pl.DataFrame) -> pl.DataFrame:
    """Collapse a `(date, value)` frame to `(month, value)` keeping the last observation per month."""
    out = (
        series.with_columns(pl.col("date").dt.truncate("1mo").alias("month"))
        .group_by("month")
        .agg(pl.col("value").sort_by("date").last())
        .filter(pl.col("value") > 0)
        .sort("month")
    )
    if out.is_empty():
        raise ValueError("monthly series contains no positive observations")
    return out


def read_fred_series(evidence_dir: Path, source: EvidenceSource) -> pl.DataFrame:
    return fred_series_frame(source_bytes(evidence_dir, source), source)


@dataclass(frozen=True)
class MonthlyLevel:
    month: date  # first day of the calendar month the observation falls in
    value: float


def read_monthly_levels(evidence_dir: Path, source: EvidenceSource) -> list[MonthlyLevel]:
    """Last observation per calendar month (oldest first) for a single FRED/Yahoo level series."""
    match source.kind:
        case EvidenceKind.FRED:
            raw = fred_series_frame(source_bytes(evidence_dir, source), source)
        case EvidenceKind.YAHOO:
            raw = yahoo_adjusted_close_frame(source_bytes(evidence_dir, source), source)
        case EvidenceKind.ZILLOW:
            raise ValueError(f"{source.provenance_label}: Zillow is a wide city table, not a single level series")
    monthly = monthly_last(raw)
    return [
        MonthlyLevel(month=month, value=value)
        for month, value in zip(monthly["month"].to_list(), monthly["value"].to_list(), strict=True)
    ]

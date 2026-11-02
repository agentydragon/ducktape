"""Typed loaders for the augur-evidence checkout: raw upstream bytes → polars frames → monthly levels.

Shared floor for every consumer of the evidence data (augur's fit/calibration
pipeline, loom's forecasting gym) — consumers never import each other, only
this package. The frame parsers are pure functions over bytes so callers
control file resolution (and tests can inject malformed data);
`source_bytes` reads from an explicit checkout directory and
`evidence_dir_from_env` resolves the conventional `AUGUR_EVIDENCE_DIR`.
"""

from __future__ import annotations

import io
import json
import math
import os
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import reduce
from itertools import pairwise
from pathlib import Path
from typing import assert_never

import polars as pl

from finance.evidence import sources
from finance.evidence.snapshot import EvidenceSnapshot
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


def offset_calendar_month(month: date, offset: int) -> date:
    """Move a first-of-month date by a calendar-month offset."""

    absolute_month = month.year * 12 + month.month - 1 + offset
    year, zero_based_month = divmod(absolute_month, 12)
    return date(year, zero_based_month + 1, 1)


def is_next_calendar_month(earlier: date, later: date) -> bool:
    """Whether two monthly observations are exactly one calendar month apart."""

    return offset_calendar_month(earlier, 1) == later


class EvidenceFrameCache:
    """Read and parse each requested source once, retaining its source-shaped Polars frame."""

    def __init__(self, snapshot: EvidenceSnapshot) -> None:
        self.snapshot = snapshot
        self._frames: dict[EvidenceSource, pl.DataFrame] = {}
        self._monthly_frames: dict[EvidenceSource, pl.DataFrame] = {}

    def frame(self, source: EvidenceSource) -> pl.DataFrame:
        if source not in self._frames:
            self._frames[source] = evidence_series_frame(self.snapshot.source_bytes(source), source)
        return self._frames[source]

    def monthly_frame(self, source: EvidenceSource) -> pl.DataFrame:
        if source not in self._monthly_frames:
            self._monthly_frames[source] = monthly_last(self.frame(source))
        return self._monthly_frames[source]


def align_monthly_frames(frames: dict[str, pl.DataFrame], *, value_column: str) -> pl.DataFrame:
    """Inner-join `{name: (month, <value_column>)}` frames on month.

    Series are joined only after each consumer has selected and transformed the columns it
    needs from the source-shaped frames. Missing observations stay missing: the join keeps only
    months present in every selected series.
    """
    if not frames:
        raise ValueError("at least one monthly series is required")
    renamed = []
    for name, frame in frames.items():
        if name == "month":
            raise ValueError("monthly series names cannot be 'month'")
        if "month" not in frame.columns or value_column not in frame.columns:
            raise ValueError(f"{name} must contain 'month' and {value_column!r} columns")
        selected = frame.select("month", pl.col(value_column).cast(pl.Float64, strict=False).alias(name))
        if any(count > 0 for count in selected.null_count().row(0)):
            raise ValueError(f"{name} contains null month or value entries")
        if not bool(selected.select(pl.col(name).is_finite().all()).item()):
            raise ValueError(f"{name} contains a nonfinite value")
        if selected.get_column("month").n_unique() != selected.height:
            raise ValueError(f"{name} contains duplicate month keys")
        renamed.append(selected)
    aligned = reduce(lambda left, right: left.join(right, on="month", how="inner", validate="1:1"), renamed).sort(
        "month"
    )
    if aligned.is_empty():
        raise ValueError("monthly series have no shared months")
    return aligned


def require_contiguous_months(months: Sequence[date], *, series_name: str) -> None:
    """Reject an aligned monthly axis with missing or repeated months.

    Consumers that treat adjacent rows as adjacent months (for example, replay windows and
    trailing-year inflation) must not bridge a dropped month as if no time had passed.
    """
    if not months:
        raise ValueError(f"{series_name} has no shared months")
    segments = contiguous_month_segments(months)
    if len(segments) > 1:
        boundary = segments[1][0]
        raise ValueError(f"{series_name} has a gap or duplicate between {months[boundary - 1]} and {months[boundary]}")


def contiguous_month_segments(months: Sequence[date]) -> tuple[tuple[int, int], ...]:
    """Return half-open index ranges for each run of consecutive calendar months."""

    if not months:
        return ()
    segments = []
    start = 0
    for index, (earlier, later) in enumerate(pairwise(months), start=1):
        if not is_next_calendar_month(earlier, later):
            segments.append((start, index))
            start = index
    segments.append((start, len(months)))
    return tuple(segments)


def compound_monthly_returns(frame: pl.DataFrame, *, return_column: str) -> pl.DataFrame:
    """Compound one monthly simple-return column into a `(month, value)` level frame."""
    if "month" not in frame.columns or return_column not in frame.columns:
        raise ValueError(f"monthly return frame must contain 'month' and {return_column!r} columns")
    selected = frame.select("month", pl.col(return_column).cast(pl.Float64, strict=False).alias("value"))
    if any(count > 0 for count in selected.null_count().row(0)):
        raise ValueError(f"{return_column} contains null month or return entries")
    if not bool(selected.select(pl.col("value").is_finite().all()).item()):
        raise ValueError(f"{return_column} contains a nonfinite return")
    if selected.get_column("month").n_unique() != selected.height:
        raise ValueError(f"{return_column} contains duplicate month keys")
    require_contiguous_months(selected.get_column("month").to_list(), series_name=return_column)
    if selected.filter(pl.col("value") <= -1.0).height:
        raise ValueError(f"{return_column} contains a return at or below -100%")
    return selected.sort("month").with_columns((1.0 + pl.col("value")).cum_prod().alias("value"))


def monthly_levels(monthly: pl.DataFrame) -> list[MonthlyLevel]:
    """Convert an aligned monthly `(month, value)` frame at the model boundary."""
    if "month" not in monthly.columns or "value" not in monthly.columns:
        raise ValueError("monthly levels must contain 'month' and 'value' columns")
    return [
        MonthlyLevel(month=month, value=value)
        for month, value in zip(monthly["month"].to_list(), monthly["value"].to_list(), strict=True)
    ]


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


def evidence_series_frame(data: bytes, source: EvidenceSource) -> pl.DataFrame:
    """Parse an ordinary one-series FRED/Yahoo source or one supported French return file."""
    match source.kind:
        case EvidenceKind.FRED:
            return fred_series_frame(data, source)
        case EvidenceKind.YAHOO:
            return yahoo_adjusted_close_frame(data, source)
        case EvidenceKind.FRENCH:
            if source == sources.FRENCH_FACTORS:
                return french_factors_frame(data, source)
            if source == sources.FRENCH_INTERNATIONAL_INDICES:
                return french_international_market_frame(data, source)
            raise ValueError(f"no evidence frame parser for French source {source.provenance_label}")
        case EvidenceKind.ZILLOW:
            return zillow_city_table(data, source)
        case _ as unreachable:
            assert_never(unreachable)


def zillow_city_table(data: bytes, source: EvidenceSource) -> pl.DataFrame:
    """Parse Zillow's source-wide city table once, before choosing any city."""
    frame = pl.read_csv(data, infer_schema=False)
    required = {"RegionType", "RegionName", "State"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{source.provenance_label} city table is missing columns: {sorted(missing)}")
    return frame


def zillow_city_series_frame(
    frame: pl.DataFrame, source: EvidenceSource, *, region_name: str, state: str
) -> pl.DataFrame:
    """Select one Zillow city's monthly `(month, value)` series.

    The Zillow city CSV is wide (a row per region, a column per month named YYYY-MM-DD); take the one
    city row, unpivot its date columns into rows, truncate each to its month, and keep positive values.
    """
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


# A French factors file carries a monthly section AND an annual one, under identical column
# headers, separated only by a blank line and the words "Annual Factors". The two are told
# apart by the width of the date field: `192607` against `1927`.
FRENCH_MONTHLY_DATE_WIDTH = 6
_FRENCH_COLUMN_COUNT = 5


def french_factors_frame(data: bytes, source: EvidenceSource) -> pl.DataFrame:
    """Ken French's factors ZIP to a monthly `(month, market_total_return, risk_free_rate)` frame.

    Both returned columns are DECIMAL monthly simple returns; the file publishes percent.
    `market_total_return` is `Mkt-RF + RF` — the CRSP value-weighted return on all listed US
    equity, dividends included — and `risk_free_rate` is the one-month T-bill, which is why one
    fetch supplies both the equity leg and the short rate back to 1926-07.

    **The annual section is the trap.** It repeats the same four column names further down the
    same file, and its rows look like plausible monthly returns (`29.44` for 1927). Appending
    them would add 99 outliers to 1200 observations and inflate every fitted volatility without
    producing a single malformed value. Rows are therefore taken only where the date field is
    exactly six digits, and the count is asserted against the date span so a format change that
    silently drops the monthly section fails here rather than downstream.
    """

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != 1:
            raise ValueError(f"{source.provenance_label} archive holds {len(names)} members, expected exactly 1")
        # latin-1: the file carries a copyright sign, and it is not UTF-8.
        text = archive.read(names[0]).decode("latin-1")

    months: list[date] = []
    market: list[float] = []
    risk_free: list[float] = []
    for line in text.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != _FRENCH_COLUMN_COUNT:
            continue
        stamp = fields[0]
        if len(stamp) != FRENCH_MONTHLY_DATE_WIDTH or not stamp.isdigit():
            continue
        months.append(date(int(stamp[:4]), int(stamp[4:]), 1))
        market.append((float(fields[1]) + float(fields[4])) / 100.0)
        risk_free.append(float(fields[4]) / 100.0)

    if not months:
        raise ValueError(f"{source.provenance_label} contains no monthly rows")
    frame = pl.DataFrame(
        {"month": months, "market_total_return": market, "risk_free_rate": risk_free},
        schema={"month": pl.Date, "market_total_return": pl.Float64, "risk_free_rate": pl.Float64},
    ).sort("month")

    span = (months[-1].year - months[0].year) * 12 + months[-1].month - months[0].month + 1
    if frame.height != span:
        raise ValueError(
            f"{source.provenance_label} has {frame.height} monthly rows spanning {span} months "
            f"({months[0]}..{months[-1]}); the series must be gapless"
        )
    return frame


FRENCH_INTERNATIONAL_MEMBER = "Ind_all.Dat"
FRENCH_INTERNATIONAL_SECTION = "Value-Weight Dollar Returns All 4 Data Items Not Reqd"
FRENCH_INTERNATIONAL_COLUMN = "Mkt"
FRENCH_MISSING = -99.99


def french_international_market_frame(data: bytes, source: EvidenceSource) -> pl.DataFrame:
    """Ken French's international index ZIP to a monthly `(month, market_total_return)` frame.

    The return is `Ind_all.Dat`'s `Mkt` column (all index countries, value-weighted, USD,
    dividends included) as a DECIMAL monthly simple return; the file publishes percent.

    **Picking the section is the trap.** The member is fixed-width text holding the same
    `Mkt High Low ...` columns four times over monthly rows — dollar and LOCAL-currency returns,
    each over all firms and over only firms with all four valuation ratios — and again over
    annual rows under the very same titles. Every block parses as plausible returns, so the
    section is chosen by its exact title and six-digit dates, exactly one must match, and the
    rows are checked gapless against their date span.
    """

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if FRENCH_INTERNATIONAL_MEMBER not in archive.namelist():
            raise ValueError(
                f"{source.provenance_label} archive has no {FRENCH_INTERNATIONAL_MEMBER}: {archive.namelist()}"
            )
        text = archive.read(FRENCH_INTERNATIONAL_MEMBER).decode("latin-1")

    # Blocks are separated by blank lines: a title, one or two header lines, then data rows.
    blocks: list[list[list[str]]] = [[]]
    for line in text.splitlines():
        if line.strip():
            blocks[-1].append(line.split())
        elif blocks[-1]:
            blocks.append([])
    candidates = [
        block
        for block in blocks
        if block
        and " ".join(block[0]) == FRENCH_INTERNATIONAL_SECTION
        and any(len(row[0]) == FRENCH_MONTHLY_DATE_WIDTH and row[0].isdigit() for row in block[1:])
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"{source.provenance_label} has {len(candidates)} monthly '{FRENCH_INTERNATIONAL_SECTION}' "
            "sections, expected exactly 1"
        )
    (block,) = candidates
    rows = [row for row in block[1:] if row[0].isdigit()]
    headers = [row for row in block[1:] if not row[0].isdigit()]
    if not headers or headers[-1][0] != FRENCH_INTERNATIONAL_COLUMN:
        raise ValueError(f"{source.provenance_label} section does not lead with a {FRENCH_INTERNATIONAL_COLUMN} column")
    if any(len(row[0]) != FRENCH_MONTHLY_DATE_WIDTH for row in rows):
        raise ValueError(f"{source.provenance_label} monthly section holds a non-YYYYMM date")

    months = [date(int(row[0][:4]), int(row[0][4:]), 1) for row in rows]
    percent = [float(row[1]) for row in rows]
    if FRENCH_MISSING in percent:
        raise ValueError(
            f"{source.provenance_label} marks {FRENCH_INTERNATIONAL_COLUMN} missing in "
            f"{months[percent.index(FRENCH_MISSING)]}"
        )
    span = (months[-1].year - months[0].year) * 12 + months[-1].month - months[0].month + 1
    if len(months) != span or any(later <= earlier for earlier, later in pairwise(months)):
        raise ValueError(
            f"{source.provenance_label} has {len(months)} monthly rows spanning {span} months "
            f"({months[0]}..{months[-1]}); the series must be gapless and in order"
        )
    return pl.DataFrame(
        {"month": months, "market_total_return": [value / 100.0 for value in percent]},
        schema={"month": pl.Date, "market_total_return": pl.Float64},
    )


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
        case EvidenceKind.FRENCH:
            raise ValueError(
                f"{source.provenance_label}: a French file holds RETURN series, not one level series; "
                "use its frame parser and compound the column you want"
            )
        case _ as unreachable:
            assert_never(unreachable)
    monthly = monthly_last(raw)
    return [
        MonthlyLevel(month=month, value=value)
        for month, value in zip(monthly["month"].to_list(), monthly["value"].to_list(), strict=True)
    ]


def read_french_market_levels(evidence_dir: Path, source: EvidenceSource) -> list[MonthlyLevel]:
    """Ken French's CRSP total-market return, compounded into a synthetic level index (base
    1.0 at the first month) — the level-series form `read_monthly_levels` points FRENCH
    callers at ("compound the column you want"), fixed to `market_total_return` since there is
    no `risk_free_rate`-as-a-level need yet.
    """
    factors = french_factors_frame(source_bytes(evidence_dir, source), source)
    multiplier = 1.0
    levels: list[MonthlyLevel] = []
    for month, monthly_return in zip(factors["month"].to_list(), factors["market_total_return"].to_list(), strict=True):
        multiplier *= 1.0 + monthly_return
        levels.append(MonthlyLevel(month=month, value=multiplier))
    return levels

"""Generate a small, loader-valid synthetic evidence set for tests.

The real FRED/Yahoo/Zillow blobs are no longer vendored in the repo; the in-cluster
deployment reads live data from the git-synced `AUGUR_EVIDENCE_DIR`, and tests point
that same env var at a directory this module fills with deterministic synthetic series.

The synthetic data is shaped to satisfy every validation `fit/evidence_data.py` enforces:
each FRED CSV is headed by its series id, the Yahoo JSON matches the v8 chart schema (SPY
carries >=1000 daily samples), the Zillow CSVs are wide city tables for the cities the
loader reads, and all series share a contiguous monthly grid through 2026-05 so well over
`MINIMUM_ALIGNED_MONTHS` align across every exogenous factor.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from finance.augur.ingest import evidence_sources as es

# Monthly grid long enough that every inner-joined factor clears MINIMUM_ALIGNED_MONTHS (36)
# and the macro-anchor tests find observations on/before their 2026-05 anchor.
_START = date(2015, 1, 1)
_END = date(2026, 5, 1)
# Cities the Zillow loader reads (home value + rent), as (RegionName, State).
_ZILLOW_CITIES = (("San Francisco", "CA"), ("Vallejo", "CA"))


def _month_firsts(start: date, end: date) -> list[date]:
    months, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _daily(start: date, end: date) -> list[date]:
    days, day = [], start
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    return days


def _level(base: float, i: int) -> float:
    # Strictly positive, gently trending with a small wiggle so log-returns aren't degenerate.
    return base * (1.0 + 0.004 * i + 0.01 * math.sin(i / 5.0))


def _write_fred(dest: Path, source: es.EvidenceSource, base: float) -> None:
    lines = [f"observation_date,{source.series_id}"]
    lines += [f"{month.isoformat()},{_level(base, i):.4f}" for i, month in enumerate(_month_firsts(_START, _END))]
    (dest / source.output_filename).write_text("\n".join(lines) + "\n")


def _write_yahoo(dest: Path, source: es.EvidenceSource, base: float, days: list[date]) -> None:
    timestamps = [int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()) for d in days]
    closes = [round(_level(base, i), 4) for i in range(len(days))]
    payload = {"chart": {"result": [{"timestamp": timestamps, "indicators": {"adjclose": [{"adjclose": closes}]}}]}}
    (dest / source.output_filename).write_text(json.dumps(payload))


def _write_zillow(dest: Path, source: es.EvidenceSource, base: float) -> None:
    months = _month_firsts(_START, _END)
    rows = [["RegionType", "RegionName", "State", *[m.isoformat() for m in months]]]
    for city_index, (region_name, state) in enumerate(_ZILLOW_CITIES):
        values = [f"{_level(base * (1.0 + 0.1 * city_index), i):.2f}" for i in range(len(months))]
        rows.append(["city", region_name, state, *values])
    (dest / source.output_filename).write_text("\n".join(",".join(row) for row in rows) + "\n")


def write_synthetic_evidence(dest: Path) -> None:
    """Fill `dest` with one synthetic file per `EVIDENCE_SOURCES` entry (by `output_filename`)."""
    dest.mkdir(parents=True, exist_ok=True)
    _write_fred(dest, es.FRED_CPI, 240.0)
    _write_fred(dest, es.FRED_SP500, 2000.0)
    _write_fred(dest, es.FRED_MORTGAGE30, 4.0)
    _write_fred(dest, es.FRED_SFXRSA, 200.0)
    _write_fred(dest, es.FRED_FHFA_SF, 300.0)
    _write_fred(dest, es.FRED_SF_RENT_CPI, 320.0)
    # SPY must yield >=1000 daily samples (the loader guards against a truncated file).
    _write_yahoo(dest, es.YAHOO_SPY, 200.0, _daily(date(2018, 1, 1), date(2026, 5, 31)))
    _write_yahoo(dest, es.YAHOO_BTC, 20000.0, _month_firsts(_START, _END))
    _write_yahoo(dest, es.YAHOO_ETH, 1500.0, _month_firsts(_START, _END))
    _write_zillow(dest, es.ZILLOW_ZHVI, 1_000_000.0)
    _write_zillow(dest, es.ZILLOW_ZORI, 3000.0)


@pytest.fixture
def synthetic_evidence_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a synthetic evidence set and point `AUGUR_EVIDENCE_DIR` at it for the test."""
    dest = tmp_path / "evidence"
    write_synthetic_evidence(dest)
    monkeypatch.setenv("AUGUR_EVIDENCE_DIR", str(dest))
    return dest

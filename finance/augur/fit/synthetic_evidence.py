"""Generate a small, loader-valid synthetic evidence set for tests.

The real FRED/Yahoo/Zillow blobs are no longer vendored in the repo; the in-cluster
deployment reads live data from the git-synced `AUGUR_EVIDENCE_DIR`, and tests point
that same env var at a directory this module fills with deterministic synthetic series.

The synthetic data is shaped to satisfy every validation `fit/evidence_data.py` enforces:
each FRED CSV is headed by its series id, the Yahoo JSON matches the v8 chart schema (SPY
carries >=1000 daily samples), the Zillow CSVs are wide city tables for the cities the loader
reads, and all series share a contiguous monthly grid through 2026-05 so well over
`MINIMUM_ALIGNED_MONTHS` align across every exogenous factor. Each series gets a gentle,
mostly-decorrelated drift + low-amplitude wiggle; CPI in particular is kept to a realistic
~2%/yr so the trained state-space model's short-horizon CPI band stays sane.
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
# Daily span for the Yahoo SPY series (the loader requires >=1000 daily samples).
_SPY_START = date(2018, 1, 1)
_SPY_END = date(2026, 5, 31)
_TRADING_DAYS_PER_MONTH = 21
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


def _values(base: float, count: int, *, step_drift: float, amp: float, phase: float) -> list[float]:
    # Strictly positive (drift*i >= 0 and amp <= 0.05 keep the bracket > 0.95), gently trending,
    # with a low-frequency wiggle so log-returns are smooth and low-vol rather than degenerate.
    return [base * (1.0 + step_drift * i + amp * math.sin(i / 9.0 + phase)) for i in range(count)]


def _write_fred(dest: Path, source: es.EvidenceSource, base: float, *, drift: float, amp: float, phase: float) -> None:
    months = _month_firsts(_START, _END)
    values = _values(base, len(months), step_drift=drift, amp=amp, phase=phase)
    lines = [f"observation_date,{source.series_id}"]
    lines += [f"{month.isoformat()},{value:.4f}" for month, value in zip(months, values, strict=True)]
    (dest / source.output_filename).write_text("\n".join(lines) + "\n")


def _write_yahoo(
    dest: Path, source: es.EvidenceSource, base: float, days: list[date], *, step_drift: float, amp: float, phase: float
) -> None:
    timestamps = [int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()) for d in days]
    closes = [round(value, 4) for value in _values(base, len(days), step_drift=step_drift, amp=amp, phase=phase)]
    payload = {"chart": {"result": [{"timestamp": timestamps, "indicators": {"adjclose": [{"adjclose": closes}]}}]}}
    (dest / source.output_filename).write_text(json.dumps(payload))


def _write_zillow(
    dest: Path, source: es.EvidenceSource, base: float, *, drift: float, amp: float, phase: float
) -> None:
    months = _month_firsts(_START, _END)
    rows = [["RegionType", "RegionName", "State", *[m.isoformat() for m in months]]]
    for city_index, (region_name, state) in enumerate(_ZILLOW_CITIES):
        values = _values(base * (1.0 + 0.1 * city_index), len(months), step_drift=drift, amp=amp, phase=phase)
        rows.append(["city", region_name, state, *[f"{value:.2f}" for value in values]])
    (dest / source.output_filename).write_text("\n".join(",".join(row) for row in rows) + "\n")


def write_synthetic_evidence(dest: Path) -> None:
    """Fill `dest` with one synthetic file per `EVIDENCE_SOURCES` entry (by `output_filename`)."""
    dest.mkdir(parents=True, exist_ok=True)
    # CPI: ~2%/yr, tiny wiggle, so the trained model's short-horizon CPI band stays sane.
    _write_fred(dest, es.FRED_CPI, 250.0, drift=0.0017, amp=0.002, phase=0.0)
    _write_fred(dest, es.FRED_SP500, 2500.0, drift=0.005, amp=0.02, phase=0.5)
    _write_fred(dest, es.FRED_MORTGAGE30, 4.0, drift=0.0008, amp=0.03, phase=1.0)
    _write_fred(dest, es.FRED_SFXRSA, 250.0, drift=0.004, amp=0.02, phase=1.5)
    _write_fred(dest, es.FRED_FHFA_SF, 300.0, drift=0.004, amp=0.02, phase=2.0)
    _write_fred(dest, es.FRED_SF_RENT_CPI, 320.0, drift=0.0025, amp=0.01, phase=2.5)
    # SPY is daily; scale the per-step drift down so the monthly series isn't wildly steep.
    _write_yahoo(
        dest,
        es.YAHOO_SPY,
        300.0,
        _daily(_SPY_START, _SPY_END),
        step_drift=0.005 / _TRADING_DAYS_PER_MONTH,
        amp=0.02,
        phase=0.3,
    )
    _write_yahoo(dest, es.YAHOO_BTC, 30000.0, _month_firsts(_START, _END), step_drift=0.010, amp=0.05, phase=1.2)
    _write_yahoo(dest, es.YAHOO_ETH, 2000.0, _month_firsts(_START, _END), step_drift=0.012, amp=0.05, phase=2.2)
    _write_zillow(dest, es.ZILLOW_ZHVI, 1_200_000.0, drift=0.004, amp=0.02, phase=0.8)
    _write_zillow(dest, es.ZILLOW_ZORI, 3000.0, drift=0.0025, amp=0.01, phase=1.8)


@pytest.fixture
def synthetic_evidence_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a synthetic evidence set and point `AUGUR_EVIDENCE_DIR` at it for the test."""
    dest = tmp_path / "evidence"
    write_synthetic_evidence(dest)
    monkeypatch.setenv("AUGUR_EVIDENCE_DIR", str(dest))
    return dest

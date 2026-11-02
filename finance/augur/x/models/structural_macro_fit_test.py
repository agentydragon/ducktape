"""Offline contracts for the long-record macro evidence boundary."""

from __future__ import annotations

import io
import math
import zipfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.historical_windows import MacroHistory, load_macro_history
from finance.augur.x.models.macro_var import fit_macro_var
from finance.augur.x.models.structural_macro_fit import (
    LONG_RECORD_MACRO_SOURCES,
    LONG_RECORD_START,
    load_long_record_macro_var_levels,
)
from finance.evidence import loading, sources


def _month_seq(start: date, end: date) -> list[date]:
    months: list[date] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _write_evidence(directory: Path, *, end: date, corporate_end: date) -> None:
    factor_months = _month_seq(LONG_RECORD_START, end)
    factor_rows = [",Mkt-RF,SMB,HML,RF"]
    factor_rows.extend(
        f"{month.year}{month.month:02d},{1.0 + (index * 7 % 13) * 0.03},0.0,0.0,{0.2 + (index * 5 % 11) * 0.03}"
        for index, month in enumerate(factor_months)
    )
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("F-F_Research_Data_Factors.csv", "\n".join(factor_rows) + "\n")
    (directory / sources.FRENCH_FACTORS.output_filename).write_bytes(archive_data.getvalue())

    def write_fred(source: sources.EvidenceSource, months: list[date], value_for: Callable[[int], float]) -> None:
        values = [f"{month.isoformat()},{value_for(index)}" for index, month in enumerate(months)]
        (directory / source.output_filename).write_text(
            f"observation_date,{source.series_id}\n" + "\n".join(values) + "\n"
        )

    early_months = _month_seq(date(1925, 1, 1), end)
    long_months = _month_seq(date(1953, 4, 1), end)
    cpi_months = _month_seq(LONG_RECORD_START, end)
    corporate_months = _month_seq(date(1919, 1, 1), corporate_end)
    write_fred(sources.FRED_LTGOVTBD, early_months, lambda index: 4.8 + (index * 7 % 17) * 0.04)
    write_fred(sources.FRED_GS10, long_months, lambda index: 4.2 + (index * 11 % 19) * 0.04)
    write_fred(
        sources.FRED_CPI_NSA, cpi_months, lambda index: 100.0 * math.exp(index * 0.001 + (index * 13 % 17) * 0.0001)
    )
    write_fred(sources.FRED_AAA, corporate_months, lambda index: 6.0 + (index * 7 % 13) * 0.03)
    write_fred(sources.FRED_BAA, corporate_months, lambda index: 7.0 + (index * 5 % 11) * 0.04)


def _legacy_levels(history: MacroHistory) -> tuple[list[loading.MonthlyLevel], ...]:
    months = history.months
    short = history.short_rate
    spread = history.term_spread
    cpi = history.cpi_level
    return (
        [
            loading.MonthlyLevel(month=month, value=float(value) * 100.0)
            for month, value in zip(months, short, strict=True)
        ],
        [
            loading.MonthlyLevel(month=month, value=float(rate + term_spread) * 100.0)
            for month, rate, term_spread in zip(months, short, spread, strict=True)
        ],
        [loading.MonthlyLevel(month=month, value=float(value)) for month, value in zip(months, cpi, strict=True)],
    )


def test_long_var_matches_replay_values_and_span_when_all_sources_overlap(tmp_path: Path) -> None:
    end = date(1993, 12, 1)
    _write_evidence(tmp_path, end=end, corporate_end=end)

    history = load_macro_history(tmp_path)
    levels = load_long_record_macro_var_levels(tmp_path)
    legacy = _legacy_levels(history)

    assert levels.short_rate_percent[0].month == LONG_RECORD_START
    assert levels.short_rate_percent[-1].month == end
    assert [level.month for level in levels.short_rate_percent] == [level.month for level in legacy[0]]
    for actual, expected in zip(
        (levels.short_rate_percent, levels.long_rate_percent, levels.cpi_level), legacy, strict=True
    ):
        np.testing.assert_allclose([level.value for level in actual], [level.value for level in expected], rtol=1e-13)

    old_fit = fit_macro_var(short_rate_percent=legacy[0], long_rate_percent=legacy[1], cpi_level=legacy[2])
    new_fit = fit_macro_var(
        short_rate_percent=levels.short_rate_percent,
        long_rate_percent=levels.long_rate_percent,
        cpi_level=levels.cpi_level,
    )
    assert (new_fit.first_month, new_fit.latest_month, new_fit.sample_months) == (
        old_fit.first_month,
        old_fit.latest_month,
        old_fit.sample_months,
    )
    np.testing.assert_allclose(new_fit.intercept, old_fit.intercept, rtol=1e-10, atol=1e-12)
    np.testing.assert_allclose(new_fit.transition, old_fit.transition, rtol=1e-10, atol=1e-12)
    np.testing.assert_allclose(new_fit.shock_cholesky, old_fit.shock_cholesky, rtol=1e-10, atol=1e-12)


def test_long_var_uses_its_three_inputs_when_a_corporate_series_ends_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    end = date(1993, 12, 1)
    corporate_end = date(1990, 12, 1)
    _write_evidence(tmp_path, end=end, corporate_end=corporate_end)
    # The previous all-series replay ended at the shorter corporate record. The fit now uses
    # only its own short-rate, long-rate, and CPI inputs, so its endpoint intentionally extends.
    previous_replay = load_macro_history(tmp_path)
    assert previous_replay.months[-1] == corporate_end
    reads: list[sources.EvidenceSource] = []
    real_source_bytes = loading.source_bytes

    def count_source_bytes(directory: Path, source: sources.EvidenceSource) -> bytes:
        reads.append(source)
        return real_source_bytes(directory, source)

    monkeypatch.setattr(loading, "source_bytes", count_source_bytes)
    levels = load_long_record_macro_var_levels(tmp_path)

    assert set(reads) == set(LONG_RECORD_MACRO_SOURCES)
    assert len(reads) == len(LONG_RECORD_MACRO_SOURCES)
    assert levels.short_rate_percent[-1].month == end
    assert levels.short_rate_percent[-1].month > corporate_end


def test_long_var_inner_aligns_a_gap_in_one_required_input(tmp_path: Path) -> None:
    end = date(1993, 12, 1)
    _write_evidence(tmp_path, end=end, corporate_end=end)
    source = sources.FRED_CPI_NSA
    rows = (tmp_path / source.output_filename).read_text().splitlines()
    (tmp_path / source.output_filename).write_text("\n".join(row for row in rows if row[:7] != "1970-06") + "\n")

    levels = load_long_record_macro_var_levels(tmp_path)
    month_axes = [
        [level.month for level in levels.short_rate_percent],
        [level.month for level in levels.long_rate_percent],
        [level.month for level in levels.cpi_level],
    ]
    assert month_axes[0] == month_axes[1] == month_axes[2]
    assert date(1970, 6, 1) not in month_axes[0]


if __name__ == "__main__":
    pytest_bazel.main()

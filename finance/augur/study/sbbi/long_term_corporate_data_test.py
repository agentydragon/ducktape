"""Offline contract tests for the SBBI Aaa evidence boundary."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.study.sbbi.long_term_corporate import reproduce
from finance.evidence import sources
from finance.evidence.snapshot import EvidenceSnapshot


def test_reproduction_reads_only_aaa_and_keeps_the_original_study_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    months: list[date] = []
    year, month = 1926, 7
    while (year, month) <= (1987, 12):
        months.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    rows = "\n".join(f"{observed.isoformat()},6.0" for observed in months)
    (tmp_path / sources.FRED_AAA.output_filename).write_text(f"observation_date,AAA\n{rows}\n")

    real_source_bytes = EvidenceSnapshot.source_bytes
    reads: list[sources.EvidenceSource] = []

    def count_source_bytes(snapshot: EvidenceSnapshot, source: sources.EvidenceSource) -> bytes:
        reads.append(source)
        return real_source_bytes(snapshot, source)

    monkeypatch.setattr(EvidenceSnapshot, "source_bytes", count_source_bytes)
    annual_returns = reproduce(tmp_path)

    assert reads == [sources.FRED_AAA]
    assert min(annual_returns) == 1927
    assert max(annual_returns) == 1987
    assert len(annual_returns) == 61


if __name__ == "__main__":
    pytest_bazel.main()

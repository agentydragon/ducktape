from __future__ import annotations

import csv
import io
from datetime import date
from pathlib import Path

import pytest_bazel

from loom.gym.dossier import materialize_dossier, series_dossier
from loom.gym.monthly_series import MonthlySeries, add_months

RAMP = MonthlySeries(
    series_id="ramp",
    description="test ramp",
    unit="units",
    provenance="synthetic",
    values={add_months(date(2024, 1, 1), n): 100.0 + n for n in range(8)},
)


def test_dossier_truncates_strictly_before_as_of() -> None:
    dossier = series_dossier([RAMP], as_of=date(2024, 7, 1))
    months = [row["month"] for row in csv.DictReader(io.StringIO(dossier["ramp_monthly.csv"]))]
    assert months
    # 2024-06 closes are knowable on 2024-07-01; nothing later may appear.
    assert max(months) == "2024-06"


def test_dossier_lists_series_with_readme() -> None:
    dossier = series_dossier([RAMP], as_of=date(2024, 7, 1))
    assert set(dossier) == {"ramp_monthly.csv", "README.txt"}
    assert "test ramp" in dossier["README.txt"]


def test_materialize_writes_files(tmp_path: Path) -> None:
    dossier = series_dossier([RAMP], as_of=date(2024, 7, 1))
    materialize_dossier(dossier, tmp_path / "inputs")
    assert sorted(p.name for p in (tmp_path / "inputs").iterdir()) == sorted(dossier)
    assert (tmp_path / "inputs" / "README.txt").read_text() == dossier["README.txt"]


if __name__ == "__main__":
    pytest_bazel.main()

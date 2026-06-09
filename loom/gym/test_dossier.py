from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest_bazel

from loom.gym.dossier import materialize_dossier, series_dossier


def test_dossier_truncates_strictly_before_as_of() -> None:
    dossier = series_dossier(as_of=date(2024, 7, 1))
    for filename, content in dossier.items():
        if filename == "README.txt":
            continue
        months = [line.split(",")[0] for line in content.splitlines()[1:]]
        assert months, filename
        assert max(months) == "2024-06", filename
        # 2024-06 closes are knowable on 2024-07-01; nothing later may appear.
        assert all(month <= "2024-06" for month in months), filename


def test_dossier_lists_all_series_with_readme() -> None:
    dossier = series_dossier(as_of=date(2024, 7, 1))
    assert set(dossier) == {"sp500_monthly.csv", "btcusd_monthly.csv", "cpi_monthly.csv", "README.txt"}
    assert "S&P 500" in dossier["README.txt"]


def test_materialize_writes_files(tmp_path: Path) -> None:
    dossier = series_dossier(as_of=date(2024, 7, 1))
    materialize_dossier(dossier, tmp_path / "inputs")
    assert sorted(p.name for p in (tmp_path / "inputs").iterdir()) == sorted(dossier)
    assert (tmp_path / "inputs" / "README.txt").read_text() == dossier["README.txt"]


if __name__ == "__main__":
    pytest_bazel.main()

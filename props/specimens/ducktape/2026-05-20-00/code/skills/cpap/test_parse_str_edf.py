"""Tests for the STR.EDF parser recipe.

Generates a synthetic CPAP STR.EDF with known values, parses it back,
and verifies roundtrip accuracy and report output.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_bazel

from skills.cpap.examples.generate_test_edf import generate_str_edf
from skills.cpap.examples.parse_str_edf import read_str_edf, report

START = datetime(2026, 4, 10)

TEST_DAYS: list[dict[str, float]] = [
    {
        "Duration": 480,
        "AHI": 2.5,
        "HI": 1.0,
        "OAI": 1.0,
        "CAI": 0.5,
        "MaskPress.50": 8.0,
        "MaskPress.95": 12.0,
        "Leak.50": 0.1,
        "Leak.95": 0.3,
        "RespRate.50": 16.0,
        "TidVol.50": 0.45,
        "SpO2.50": 95.0,
    },
    {"Duration": 360, "AHI": 1.2, "HI": 0.5, "OAI": 0.5, "CAI": 0.2},
    {"Duration": 0},  # skipped night
    {"Duration": 420, "AHI": 8.0, "HI": 3.0, "OAI": 4.0, "CAI": 1.0},
    {"Duration": 180, "AHI": 0.5},
]


@pytest.fixture
def edf_path(tmp_path: Path) -> Path:
    path = tmp_path / "STR.EDF"
    generate_str_edf(path, TEST_DAYS, start_date=START)
    return path


def test_column_count(edf_path: Path) -> None:
    df = read_str_edf(edf_path)
    # 15 signals minus 2 multi-sample (MaskOn, MaskOff) = 13 columns
    assert len(df.columns) == 13
    assert len(df) == 5


def test_dates(edf_path: Path) -> None:
    df = read_str_edf(edf_path)
    for i, row in df.iterrows():
        expected = START + timedelta(days=i)
        assert row["Date"].date() == expected.date()


def test_roundtrip_values(edf_path: Path) -> None:
    df = read_str_edf(edf_path)
    for day_in, (_, row) in zip(TEST_DAYS, df.iterrows(), strict=True):
        assert abs(row["Duration"] - day_in.get("Duration", 0)) < 0.1
        assert abs(row["AHI"] - day_in.get("AHI", 0)) < 0.05


def test_report_json(edf_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    df = read_str_edf(edf_path)
    report(df, days=10)
    nights = json.loads(capsys.readouterr().out)

    assert len(nights) == 4  # excludes Duration=0 night
    assert nights[0]["Date"] == "2026-04-10"
    assert abs(nights[0]["AHI"] - 2.5) < 0.1
    assert "MaskPress.50" in nights[0]


if __name__ == "__main__":
    pytest_bazel.main()

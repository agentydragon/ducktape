"""The documented panel format maps columns to sleeves and rejects incomplete or non-finite years."""

from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.study.guyton_klinger.panel import AnnualPanel, Sleeve, load_panel

HEADER = "year,cash,bonds,equity,inflation"


def test_columns_map_to_named_sleeves(tmp_path: Path) -> None:
    path = tmp_path / "panel.csv"
    path.write_text(f"{HEADER}\n1990,0.01,0.02,0.03,0.04\n1991,0.05,-0.06,-0.5,0\n")
    assert load_panel(path) == AnnualPanel(
        first_year=1990,
        returns={Sleeve.CASH: (0.01, 0.05), Sleeve.BONDS: (0.02, -0.06), Sleeve.EQUITY: (0.03, -0.5)},
        inflation=(0.04, 0.0),
    )


@pytest.mark.parametrize(
    ("text", "match"),
    [
        pytest.param("year,equity,bonds,cash,inflation\n1990,0,0,0,0\n", "header", id="reordered-header"),
        pytest.param(f"{HEADER}\n", "at least one year", id="no-years"),
        pytest.param(f"{HEADER}\n1990,0,0,0,0\n1992,0,0,0,0\n", "consecutive", id="gap"),
        pytest.param(f"{HEADER}\n1991,0,0,0,0\n1990,0,0,0,0\n", "consecutive", id="descending"),
        pytest.param(f"{HEADER}\n1990,0,0,0\n", "cells", id="short-row"),
        pytest.param(f"{HEADER}\n1990,0,,0,0\n", "convert", id="blank"),
        pytest.param(f"{HEADER}\n1990,0,nan,0,0\n", "finite return", id="non-finite"),
        pytest.param(f"{HEADER}\n1990,0,0,-1,0\n", "above -1", id="total-loss"),
    ],
)
def test_rejects_malformed_panels(tmp_path: Path, text: str, match: str) -> None:
    path = tmp_path / "panel.csv"
    path.write_text(text)
    with pytest.raises(ValueError, match=match):
        load_panel(path)


if __name__ == "__main__":
    pytest_bazel.main()

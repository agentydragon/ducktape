"""The documented panel format rejects incomplete or impossible years; CPI ratios compound between panel years."""

from fractions import Fraction
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.study.guyton_klinger.panel import AnnualPanel, Sleeve, load_panel

HEADER = "year,cash_income,bonds_income,bonds_price,equity_income,equity_price,inflation"


def test_cpi_ratio_compounds_the_years_between_two_januaries() -> None:
    # CPI rises 10% over 1990 and 20% over 1991: January 1992 stands at 1.32 of January 1990.
    panel = AnnualPanel(
        first_year=1990,
        income=dict.fromkeys(Sleeve, (0.0, 0.0, 0.0)),
        price=dict.fromkeys((Sleeve.BONDS, Sleeve.EQUITY), (0.0, 0.0, 0.0)),
        inflation=(0.1, 0.2, 0.0),
    )
    assert (panel.cpi_ratio(1992, 1990), panel.cpi_ratio(1990, 1992), panel.cpi_ratio(1991, 1991)) == (
        Fraction(33, 25),
        Fraction(25, 33),
        Fraction(1),
    )
    with pytest.raises(ValueError, match="does not cover 1993"):
        panel.cpi_ratio(1990, 1993)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        pytest.param("year,cash,bonds,equity,inflation\n1990,0,0,0,0\n", "header", id="total-return-header"),
        pytest.param(
            "year,cash_income,bonds_price,bonds_income,equity_income,equity_price,inflation\n1990,0,0,0,0,0,0\n",
            "header",
            id="reordered-header",
        ),
        pytest.param(f"{HEADER}\n", "at least one year", id="no-years"),
        pytest.param(f"{HEADER}\n1990,0,0,0,0,0,0\n1992,0,0,0,0,0,0\n", "consecutive", id="gap"),
        pytest.param(f"{HEADER}\n1991,0,0,0,0,0,0\n1990,0,0,0,0,0,0\n", "consecutive", id="descending"),
        pytest.param(f"{HEADER}\n1990,0,0,0,0,0\n", "cells", id="short-row"),
        pytest.param(f"{HEADER}\n1990,0,,0,0,0,0\n", "convert", id="blank"),
        pytest.param(f"{HEADER}\n1990,0,0,nan,0,0,0\n", "finite", id="non-finite"),
        pytest.param(f"{HEADER}\n1990,0,-0.01,0,0,0,0\n", "at least 0", id="negative-coupon"),
        pytest.param(f"{HEADER}\n1990,0,0,0,0,-1,0\n", "above -1", id="total-loss"),
    ],
)
def test_rejects_malformed_panels(tmp_path: Path, text: str, match: str) -> None:
    path = tmp_path / "panel.csv"
    path.write_text(text)
    with pytest.raises(ValueError, match=match):
        load_panel(path)


if __name__ == "__main__":
    pytest_bazel.main()

"""The documented panel format maps columns to sleeves and rejects incomplete or impossible years."""

from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.study.guyton_klinger.panel import AnnualPanel, Sleeve, load_panel

HEADER = "year,cash_income,bonds_income,bonds_price,equity_income,equity_price,inflation"


def test_columns_map_to_named_sleeves_and_sum_to_total_returns(tmp_path: Path) -> None:
    path = tmp_path / "panel.csv"
    path.write_text(f"{HEADER}\n1990,0.01,0.02,0.03,0.04,0.05,0.06\n1991,0.07,0.08,-0.5,0,-0.25,0\n")
    panel = load_panel(path)
    assert panel == AnnualPanel(
        first_year=1990,
        income={Sleeve.CASH: (0.01, 0.07), Sleeve.BONDS: (0.02, 0.08), Sleeve.EQUITY: (0.04, 0.0)},
        price={Sleeve.BONDS: (0.03, -0.5), Sleeve.EQUITY: (0.05, -0.25)},
        inflation=(0.06, 0.0),
    )
    assert [panel.total_return(sleeve) for sleeve in Sleeve] == [
        (0.01, 0.07),
        pytest.approx((0.05, -0.42)),
        pytest.approx((0.09, -0.25)),
    ]


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

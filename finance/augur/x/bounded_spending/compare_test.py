"""Exercise the Python compiler -> native function -> financial engine composition."""

import json
from pathlib import Path

import numpy as np
import pytest_bazel

from finance.augur.model.series import InflationKey, SecurityKey
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.study.trinity.replay import EQUITY, HORIZON_MONTHS
from finance.augur.x.bounded_spending.compare import compare


def test_bounded_spending_reacts_to_each_path_and_preserves_the_fixed_real_control(tmp_path: Path) -> None:
    # Stipulated two-path stress case, not sampled market history or a forecast.
    prices = np.full((2, HORIZON_MONTHS + 1), 100.0)
    prices[0, 12:] = 200.0
    prices[1, 12:] = 50.0
    cpi = np.ones_like(prices)
    cpi[:, 12:] = 1.25
    paths = ExternalSeriesContext.from_level_blocks(
        [(SecurityKey(symbol=EQUITY), prices), (InflationKey(), cpi)], rollout_count=2, horizon_months=HORIZON_MONTHS
    )
    output = tmp_path / "comparison"
    compare(
        external_series=paths,
        rollout_count=2,
        equity_share=1.0,
        rate_bps=400,
        max_cut_bps=1000,
        max_raise_bps=500,
        output_dir=output,
    )
    fixed = json.loads((output / "fixed_real.json").read_text())
    bounded = json.loads((output / "bounded.json").read_text())
    for document, second_year in ((fixed, (5_000_000, 5_000_000)), (bounded, (5_250_000, 4_500_000))):
        for rollout, expected in zip(document["rollouts"], second_year, strict=True):
            withdrawals = rollout["obligations"]
            assert withdrawals[0]["amount_paid"] == 4_000_000
            assert withdrawals[1]["month"] == 12
            assert withdrawals[1]["amount_paid"] == expected
            assert rollout["dispositions"]  # actual funding sales, not a portfolio-value calculator
            assert rollout["tax_accruals"] == []  # this control is intentionally tax-free


if __name__ == "__main__":
    pytest_bazel.main()

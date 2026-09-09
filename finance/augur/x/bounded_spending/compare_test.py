"""Exercise the Python compiler -> native function -> financial engine composition."""

import json
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey, SecurityKey
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.study.trinity.replay import EQUITY, HORIZON_MONTHS
from finance.augur.x.bounded_spending.compare import compare


@pytest.mark.parametrize("equity_share", [-0.2, 1.2, float("nan"), float("inf")])
def test_invalid_equity_share_is_rejected_before_compilation(tmp_path: Path, equity_share: float) -> None:
    with pytest.raises(ValueError, match="equity_share"):
        compare(
            external_series=ExternalSeriesContext(),
            rollout_count=1,
            equity_share=equity_share,
            rate_bps=400,
            max_cut_bps=1000,
            max_raise_bps=500,
            output_dir=tmp_path / "invalid",
        )
    assert not (tmp_path / "invalid").exists()


def test_bounded_spending_reacts_to_each_path_and_preserves_the_fixed_real_control(tmp_path: Path) -> None:
    # Stipulated three-path stress case, not sampled market history or a forecast.
    prices = np.full((3, HORIZON_MONTHS + 1), 100.0)
    prices[0, 12:] = 200.0
    prices[1, 12:] = 50.0
    prices[2, 12:] = 130.0
    cpi = np.ones_like(prices)
    cpi[:, 12:] = 1.25
    paths = ExternalSeriesContext.from_level_blocks(
        [(SecurityKey(symbol=EQUITY), prices), (InflationKey(), cpi)], rollout_count=3, horizon_months=HORIZON_MONTHS
    )
    output = tmp_path / "comparison"
    compare(
        external_series=paths,
        rollout_count=3,
        equity_share=1.0,
        rate_bps=400,
        max_cut_bps=1000,
        max_raise_bps=500,
        output_dir=output,
    )
    fixed = json.loads((output / "fixed_real.json").read_text())
    bounded = json.loads((output / "bounded.json").read_text())
    policies = json.loads((output / "policies.json").read_text())
    assert policies["bounded"] == {"max_cut_bps": 1000, "max_raise_bps": 500}
    for document, second_year, third_year in (
        (fixed, (5_000_000, 5_000_000, 5_000_000), (5_000_000, 5_000_000, 5_000_000)),
        (bounded, (5_250_000, 4_500_000, 4_992_000), (5_512_500, 4_050_000, 4_792_320)),
    ):
        for rollout, expected, next_expected in zip(document["rollouts"], second_year, third_year, strict=True):
            withdrawals = rollout["obligations"]
            assert withdrawals[0]["amount_paid"] == 4_000_000
            assert withdrawals[1]["month"] == 12
            assert withdrawals[1]["amount_paid"] == expected
            assert withdrawals[2]["month"] == 24
            assert withdrawals[2]["amount_paid"] == next_expected
            assert rollout["dispositions"]  # actual funding sales, not a portfolio-value calculator
            assert rollout["tax_accruals"] == []  # this control is intentionally tax-free


if __name__ == "__main__":
    pytest_bazel.main()

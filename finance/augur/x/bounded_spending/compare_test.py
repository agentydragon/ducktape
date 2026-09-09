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
        trace_rollouts=(0, 1, 2),
    )
    fixed = json.loads((output / "fixed_real.json").read_text())
    bounded = json.loads((output / "bounded.json").read_text())
    policies = json.loads((output / "policies.json").read_text())
    assert policies["bounded"] == {"max_cut_bps": 1000, "max_raise_bps": 500}
    for name, document, second_year, third_year in (
        ("fixed_real", fixed, (5_000_000, 5_000_000, 5_000_000), (5_000_000, 5_000_000, 5_000_000)),
        ("bounded", bounded, (5_250_000, 4_500_000, 4_992_000), (5_512_500, 4_050_000, 4_792_320)),
    ):
        for rollout_id, (expected, next_expected) in enumerate(zip(second_year, third_year, strict=True)):
            rollout = json.loads((output / f"{name}.trace-{rollout_id}.json").read_text())
            withdrawals = rollout["obligations"]
            assert withdrawals[0]["amount_paid"] == 4_000_000
            assert withdrawals[1]["month"] == 12
            assert withdrawals[1]["amount_paid"] == expected
            assert withdrawals[2]["month"] == 24
            assert withdrawals[2]["amount_paid"] == next_expected
            assert rollout["dispositions"]  # actual funding sales, not a portfolio-value calculator
            assert rollout["tax_accruals"] == []  # this control is intentionally tax-free
            failed_month = rollout["failed_month"]
            observed_months = HORIZON_MONTHS if failed_month is None else failed_month + 1
            requested = document["consumption_requested"][rollout_id]
            paid = document["consumption_paid"][rollout_id]
            assert len(requested) == len(paid) == observed_months
            receipts = {row["month"]: row for row in withdrawals}
            for month in range(observed_months):
                receipt = receipts.get(month)
                assert requested[month] == (receipt["amount_due"] if receipt else 0)
                assert paid[month] == (receipt["amount_paid"] if receipt else 0)
            assert document["product_metrics"]["failed_month"][rollout_id] == (
                -1 if failed_month is None else failed_month
            )
        distribution = json.loads((output / f"{name}.consumption.json").read_text())
        assert distribution["component"] == document["component"]
        assert distribution["months"][0]["observed_path_count"] == 3
        assert distribution["months"][0]["consumption_paid"] == [4_000_000] * 3
        assert distribution["months"][12]["consumption_paid"][1] == sorted(second_year)[1]


def test_live_zero_consumption_is_not_confused_with_post_stop_absence(tmp_path: Path) -> None:
    # Spend the whole flat portfolio at month 0. The control cannot pay next year;
    # the bounded rule permits cutting to zero and continues observing live months.
    prices = np.full((1, HORIZON_MONTHS + 1), 100.0)
    paths = ExternalSeriesContext.from_level_blocks(
        [(SecurityKey(symbol=EQUITY), prices), (InflationKey(), np.ones_like(prices))],
        rollout_count=1,
        horizon_months=HORIZON_MONTHS,
    )
    output = tmp_path / "stop-comparison"
    compare(
        external_series=paths,
        rollout_count=1,
        equity_share=1.0,
        rate_bps=10_000,
        max_cut_bps=10_000,
        max_raise_bps=0,
        output_dir=output,
    )
    assert not list(output.glob("*.trace-*.json"))  # Population output does not request full traces.
    fixed = json.loads((output / "fixed_real.json").read_text())
    bounded = json.loads((output / "bounded.json").read_text())
    assert fixed["product_metrics"]["failed_month"] == [12]
    assert fixed["consumption_requested"] == [[100_000_000, *([0] * 11), 100_000_000]]
    assert fixed["consumption_paid"] == [[100_000_000, *([0] * 12)]]
    assert bounded["product_metrics"]["failed_month"] == [-1]
    assert (
        bounded["consumption_requested"]
        == bounded["consumption_paid"]
        == [[100_000_000, *([0] * (HORIZON_MONTHS - 1))]]
    )
    fixed_distribution = json.loads((output / "fixed_real.consumption.json").read_text())
    bounded_distribution = json.loads((output / "bounded.consumption.json").read_text())
    assert fixed_distribution["months"][12]["observed_path_count"] == 1
    assert fixed_distribution["months"][12]["consumption_paid"] == [0, 0, 0]
    assert fixed_distribution["months"][13]["observed_path_count"] == 0
    assert fixed_distribution["months"][13]["consumption_requested"] is None
    assert fixed_distribution["months"][13]["consumption_paid"] is None
    assert bounded_distribution["months"][13]["observed_path_count"] == 1
    assert bounded_distribution["months"][13]["consumption_paid"] == [0, 0, 0]


if __name__ == "__main__":
    pytest_bazel.main()

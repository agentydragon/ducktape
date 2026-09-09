"""The example's unit paths fund real obligations through Augur's Rust engine."""

import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.rust.backend import RustEngine
from finance.augur.x.bond_policies.construction import ProxyConstruction
from finance.augur.x.bond_policies.run import HOUSEHOLD, compile_construction, run_experiment


@pytest.fixture
def controlled_construction() -> ProxyConstruction:
    prices = np.full((1, 74), 100.0)
    coupons = np.zeros_like(prices)
    coupons[:, 12:73:12] = 2.0
    return ProxyConstruction(price=prices, coupon=coupons)


def test_zero_withdrawals_preserve_units_and_pay_the_last_in_horizon_coupon(
    controlled_construction: ProxyConstruction,
) -> None:
    run = compile_construction(controlled_construction, annual_spending=Decimal(0))
    engine = RustEngine()
    events = engine.events(run)
    metrics = engine.product_metrics(run, primary_agent_id=HOUSEHOLD).metric_arrays()
    assert events.lot_dispositions.is_empty()
    assert events.obligation_settlements.is_empty()
    assert events.tax_accruals.is_empty()
    assert metrics["holding_value_quanta"][-1, 0] == 10_000_000
    assert metrics["cash_quanta"][-1, 0] == 1_200_000
    assert metrics["net_worth_quanta"][-1, 0] == 11_200_000


def test_current_coupon_funds_spending_before_units_are_sold(controlled_construction: ProxyConstruction) -> None:
    run = compile_construction(controlled_construction, annual_spending=Decimal(5000))
    events = RustEngine().events(run)
    sales = events.lot_dispositions.sort("month_index")
    assert sales.get_column("month_index").to_list() == [12, 24, 36, 48, 60, 72]
    assert sales.row(0, named=True)["proceeds_quanta"] == 300_000
    # After the first sale, 970 units earn $1,940; the second withdrawal needs $3,060.
    assert sales.row(1, named=True)["proceeds_quanta"] == 306_000
    assert events.obligation_settlements.get_column("amount_paid_quanta").sum() == 3_000_000
    assert events.rollout_failures.is_empty()
    assert events.tax_accruals.is_empty()


@pytest.mark.parametrize("spending", [Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_spending_is_rejected(controlled_construction: ProxyConstruction, spending: Decimal) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        compile_construction(controlled_construction, annual_spending=spending)


def test_example_exports_inputs_and_auditable_household_timelines(tmp_path: Path) -> None:
    output = tmp_path / "bond_example_output"
    run_experiment(output_dir=output, annual_spending=(Decimal(5000),))
    summary = json.loads((output / "summary.json").read_text())
    config = json.loads((output / "config.json").read_text())
    with np.load(output / "discount_curves.npz") as curves:
        assert set(curves.files) == set(config["path_names"])
    assert summary
    for cell in summary:
        cell_dir = output / cell["output"]
        rollout = config["path_names"].index(cell["path"])
        selected = pl.col("rollout_index") == rollout
        obligations = pl.read_parquet(cell_dir / "obligation_settlements.parquet").filter(selected)
        assert cell["spending_paid_quanta"] == obligations.get_column("amount_paid_quanta").sum()
        assert cell["spending_paid_quanta"] == 3_000_000
        assert cell["failed_month"] == -1
        assert pl.read_parquet(cell_dir / "tax_accruals.parquet").is_empty()
        with np.load(cell_dir / "metrics.npz") as metrics:
            assert cell["terminal_wealth_quanta"] == metrics["net_worth_quanta"][-1, rollout]
    with np.load(output / "constant_maturity_proxy" / "construction.npz") as proxy:
        # An approximation's unobserved internal trades must not masquerade as zero trades.
        assert "sale_proceeds" not in proxy.files
        assert "redemption" not in proxy.files


if __name__ == "__main__":
    pytest_bazel.main()

"""The documented Python policy/CLI settles unit-price and payout paths through Augur."""

import json
import subprocess
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.results import Finished, RejectedAction
from finance.augur.x.bond_policies.construction import ProxyConstruction, compare_constructions, stipulated_curves
from finance.augur.x.bond_policies.run import CELLS, compile_construction, execute, measurements
from util.bazel.runfiles import get_required_path, own_repo_rlocation


@pytest.fixture
def controlled_construction() -> ProxyConstruction:
    prices = np.full((1, 74), 100.0)
    coupons = np.zeros_like(prices)
    coupons[:, 12:73:12] = 2.0
    # The final point is valuation-only; a payout there must not enter cash.
    coupons[:, 73] = 999
    return ProxyConstruction(price=prices, coupon=coupons)


def test_zero_withdrawals_preserve_units_and_pay_the_last_in_horizon_coupon(
    controlled_construction: ProxyConstruction,
) -> None:
    run = compile_construction(controlled_construction, annual_spending=Decimal(0))
    result = execute(run, rollout_ids=[0], capture="forensic")[0]
    summary = result.summary
    financial = result.trace
    assert financial is not None
    assert result.stop is None
    assert financial.receipts == []
    assert financial.events.lot_dispositions.is_empty()
    assert summary.payments == []
    assert summary.tax_accruals == []
    assert summary.public_holdings[0].values[-1] == 10_000_000
    assert summary.cash[0].values[-1] == 1_200_000
    assert measurements(result).terminal_wealth_quanta == 11_200_000
    assert financial.books[-1].lots[0].units_remaining == 1_000_000_000


def test_current_coupon_funds_spending_before_units_are_sold(controlled_construction: ProxyConstruction) -> None:
    run = compile_construction(controlled_construction, annual_spending=Decimal(5000))
    result = execute(run, rollout_ids=[0], capture="forensic")[0]
    financial = result.trace
    assert financial is not None
    sales = financial.events.lot_dispositions
    assert sales.get_column("month_index").to_list() == [12, 24, 36, 48, 60, 72]
    assert sales.select("proceeds_quanta", "cost_basis_consumed_quanta").row(0) == (300_000, 300_000)
    # After the first sale, 970 units earn $1,940; the second withdrawal needs $3,060.
    assert sales.select("proceeds_quanta", "cost_basis_consumed_quanta").row(1) == (306_000, 306_000)
    assert [row.action.kind for row in financial.receipts] == ["Sell", "PayClaim"] * 6
    assert measurements(result).spending_paid_quanta == 3_000_000
    assert result.stop is None
    assert result.summary.tax_accruals == []


def test_zero_yield_proxy_funds_withdrawals_from_principal_without_coupon_income() -> None:
    proxy = compare_constructions(np.ones((1, 74, 37)))["constant_maturity_proxy"]
    run = compile_construction(proxy, annual_spending=Decimal(5000))
    result = execute(run, rollout_ids=[0], capture="forensic")[0]
    summary = result.summary
    # Six $5,000 withdrawals consume $30,000 of the opening $100,000, with no income.
    assert result.trace is not None
    assert result.trace.events.lot_dispositions.get_column("proceeds_quanta").to_list() == [500_000] * 6
    assert measurements(result).spending_paid_quanta == 3_000_000
    assert summary.public_holdings[0].values[-1] == 7_000_000
    assert summary.cash[0].values[-1] == 0
    assert measurements(result).terminal_wealth_quanta == 7_000_000
    assert result.stop is None
    assert summary.tax_accruals == []


@pytest.mark.parametrize(
    ("construction_name", "last_coupon", "coupon_cash", "investment_value"),
    [("hold", 36, 1_200_000, 10_000_000), ("sell_at_18", 12, 400_000, 10_198_000)],
)
def test_internal_sale_or_redemption_stays_in_nav_not_household_income(
    construction_name: str, last_coupon: int, coupon_cash: int, investment_value: int
) -> None:
    construction = compare_constructions(stipulated_curves()["flat"][None, ...])[construction_name]
    result = execute(
        compile_construction(construction, annual_spending=Decimal(0)), rollout_ids=[0], capture="forensic"
    )[0]
    summary = result.summary
    financial = result.trace
    assert financial is not None
    assert financial.events.lot_dispositions.is_empty()  # Internal trades are not household unit sales.
    assert summary.cash[0].values[-1] == coupon_cash
    # Flat 4%: maturity returns $100/unit to NAV; sale at 18 marks 4/sqrt(1.04)+104/1.04**1.5,
    # rounded to $101.98/unit at the price boundary. Neither is a coupon distribution.
    assert summary.public_holdings[0].values[-1] == investment_value
    assert summary.cash[0].values[last_coupon + 1 :] == [coupon_cash] * (73 - last_coupon)


def test_nondivisible_unit_sale_keeps_exact_proceeds_and_basis() -> None:
    prices = np.full((1, 14), 3.0)
    prices[:, 0] = 100
    result = execute(
        compile_construction(ProxyConstruction(prices, np.zeros_like(prices)), annual_spending=Decimal(1)),
        rollout_ids=[0],
        capture="forensic",
    )[0]
    financial = result.trace
    assert financial is not None
    assert financial.events.lot_dispositions.select("proceeds_quanta", "cost_basis_consumed_quanta").row(0) == (
        100,
        3333,
    )
    assert financial.events.lot_dispositions.get_column("realized_gain_quanta").to_list() == [-3233]
    # 1,000 opening units minus ceil(1/3 * 1,000,000) micro-units, with original $100/unit basis.
    lot = financial.books[-1].lots[0]
    assert (lot.units_remaining, lot.basis_remaining) == (999_666_666, 9_996_667)
    assert result.summary.cash[0].values[-1] == 0
    assert result.stop is None


def test_failed_payment_preserves_sale_prefix_without_stopping_other_paths() -> None:
    prices = np.full((2, 14), 100.0)
    prices[1, 12:] = 200
    coupons = np.zeros_like(prices)
    coupons[:, 12] = 2
    run = compile_construction(ProxyConstruction(prices, coupons), annual_spending=Decimal(120_000))
    results = execute(run, rollout_ids=[0, 1], capture="forensic")
    failed, completed = results
    report = measurements(failed)
    assert failed.stop == RejectedAction(month=12, action_index=1)
    assert report.ending_mark_month == 12
    assert report.terminal_wealth_quanta is None
    assert report.ending_assets_quanta == 10_200_000
    assert report.spending_requested_quanta == report.attempted_spending_shortfall_quanta == 12_000_000
    assert report.spending_paid_quanta == 0
    assert report.unpaid_claims[0].amount_due == 12_000_000
    assert failed.trace is not None
    assert failed.trace.events.lot_dispositions.get_column("proceeds_quanta").to_list() == [10_000_000]
    # Canonical books retain the exhausted lot's identity, with no units or basis left.
    lot = failed.summary.ending_book.lots[0]
    assert (lot.units_remaining, lot.basis_remaining) == (0, 0)
    assert failed.summary.ending_book.month == 13
    assert completed.stop is None
    assert measurements(completed).spending_paid_quanta == 12_000_000
    assert measurements(completed).terminal_wealth_quanta == 8_200_000
    selected = execute(run, rollout_ids=[1, 0])
    assert [row.rollout_id for row in selected] == [1, 0]
    for original, replay in zip(reversed(results), selected, strict=True):
        assert replay.trace is None
        assert replay.summary == original.summary
        assert replay.stop == original.stop


@pytest.mark.parametrize("spending", [Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_spending_is_rejected(controlled_construction: ProxyConstruction, spending: Decimal) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        compile_construction(controlled_construction, annual_spending=spending)


def test_cli_exports_inputs_compact_results_and_selected_original_timelines(tmp_path: Path) -> None:
    output = tmp_path / "bond_example_output"
    subprocess.run(
        [
            get_required_path(own_repo_rlocation("finance/augur/x/bond_policies/run_bin")),
            "--output-dir",
            output,
            "--annual-spending",
            "5000",
            "200000",
            "--trace-rollouts",
            "4",
            "0",
        ],
        check=True,
    )
    reports = CELLS.validate_json((output / "summary.json").read_text())
    config = json.loads((output / "config.json").read_text())
    assert config["withdrawal_months"] == [12, 24, 36, 48, 60, 72]
    assert config["trace_rollouts"] == [4, 0]
    with np.load(output / "discount_curves.npz") as curves:
        assert set(curves.files) == set(config["path_names"])
        np.testing.assert_array_equal(curves["zero"][6:], 1)
    assert len(reports) == 40
    for cell in reports:
        cell_dir = output / cell.output
        document = json.loads((cell_dir / "execution_input.json").read_text())
        assert not document["scenario"]["target_allocation_policies"]
        results = Finished.model_validate_json((cell_dir / "rollouts.json").read_text()).rollouts
        assert [row.rollout_id for row in results] == list(range(5))
        assert all(row.trace is None for row in results)
        report = cell.measurements
        original = results[report.rollout_id]
        assert cell.path == config["path_names"][report.rollout_id]
        assert measurements(original) == report
        if cell.annual_spending_usd == "5000":
            assert report.stop is None
            assert report.spending_paid_quanta == 3_000_000
        else:
            assert report.stop is not None
            assert report.spending_paid_quanta == 0
            assert report.ending_mark_month == 12
            assert report.terminal_wealth_quanta is None
        assert original.summary.tax_accruals == []
        traces = Finished.model_validate_json((cell_dir / "traces.json").read_text()).rollouts
        assert [row.rollout_id for row in traces] == [4, 0]
        for replay in traces:
            assert replay.summary == results[replay.rollout_id].summary
            assert replay.stop == results[replay.rollout_id].stop
            assert replay.trace is not None
            for entry in replay.trace.journal:
                assert sum(posting.amount for posting in entry.postings) == 0
    with np.load(output / "constant_maturity_proxy" / "construction.npz") as proxy:
        assert "sale_proceeds" not in proxy.files
        assert "redemption" not in proxy.files


if __name__ == "__main__":
    pytest_bazel.main()

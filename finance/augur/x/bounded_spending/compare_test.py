"""Exercise compiler -> Python batch actions -> canonical financial results."""

import json
import subprocess
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey, SecurityKey
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.results import Finished, RejectedAction, Rollout
from finance.augur.sim.scenario import InitialLot, ObligationType, ScheduledObligation
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import checking
from finance.augur.study.trinity.replay import EQUITY, HORIZON_MONTHS
from finance.augur.x.bounded_spending.compare import _write_consumption_distribution, compare
from finance.augur.x.bounded_spending.python_policy import BatchPolicy, Parameters, SpendingPolicy, consumption, run
from util.bazel.runfiles import get_required_path


@pytest.fixture
def early_claim_failure() -> Finished:
    stock = SecurityKey(symbol="test-bill-funding")
    case = Case(
        scenario(
            checking(("retiree", Decimal(0)), ("world", Decimal(0))),
            horizon_months=2,
            tax_profiles=[],
            initial_lots=[
                InitialLot(
                    lot_id="test-bill-lot",
                    agent_id="retiree",
                    account_id="brokerage",
                    asset=stock,
                    purchase_month_index=-24,
                    quantity=1,
                    cost_basis_per_unit=Decimal(100),
                )
            ],
            scheduled_obligations=[
                ScheduledObligation(
                    month=0,
                    obligation_id="test-large-bill",
                    obligation_type=ObligationType.OUTSIDE_RENT,
                    agent_id="retiree",
                    from_account_id="checking",
                    to_agent_id="world",
                    to_account_id="checking",
                    amount_due=Decimal(200),
                )
            ],
        ),
        rollout_count=2,
        series={stock: np.array([[100.0] * 3, [300.0] * 3]), InflationKey(): np.ones((2, 3))},
    )
    return run(
        json.dumps(case.compiled_run.execution_input),
        SpendingPolicy(BatchPolicy(Parameters(400, 0, 0), 2), {("brokerage", str(stock.symbol)): 1}),
        [0, 1],
    )


def test_unattempted_consumption_has_known_zero_paid_on_observed_stop(early_claim_failure: Finished) -> None:
    stopped = early_claim_failure.rollouts[0]
    assert stopped.stop == RejectedAction(month=0, action_index=1)
    assert [row.action.kind for row in stopped.summary.last_receipts] == ["Sell", "PayClaim"]
    assert consumption(early_claim_failure) == ([[None], [1_200, 0]], [[0], [1_200, 0]])


def test_paid_distribution_keeps_zero_when_request_is_unattempted(
    early_claim_failure: Finished, tmp_path: Path
) -> None:
    path = tmp_path / "consumption.json"
    _write_consumption_distribution(
        early_claim_failure, path, horizon_months=2, currency_code="USD", currency_quantum="0.01"
    )
    months = json.loads(path.read_text())["months"]
    assert months[0]["observed_path_count"] == 2
    assert months[0]["consumption_requested_path_count"] == 1
    assert months[0]["consumption_requested"] == [1_200] * 3
    assert months[0]["consumption_paid"] == [60, 600, 1_140]
    assert months[1]["observed_path_count"] == 1
    assert months[1]["consumption_paid"] == [0] * 3


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
    fixed = Finished.model_validate_json((output / "fixed_real.json").read_text())
    bounded = Finished.model_validate_json((output / "bounded.json").read_text())
    policies = json.loads((output / "policies.json").read_text())
    assert policies["bounded"] == {"max_cut_bps": 1000, "max_raise_bps": 500}
    for name, document, second_year, third_year in (
        ("fixed_real", fixed, (5_000_000, 5_000_000, 5_000_000), (5_000_000, 5_000_000, 5_000_000)),
        ("bounded", bounded, (5_250_000, 4_500_000, 4_992_000), (5_512_500, 4_050_000, 4_792_320)),
    ):
        for rollout_id, (expected, next_expected) in enumerate(zip(second_year, third_year, strict=True)):
            rollout = Rollout.model_validate_json((output / f"{name}.trace-{rollout_id}.json").read_text())
            financial = rollout.trace
            assert financial is not None
            withdrawals = rollout.summary.payments
            assert withdrawals[0].receipt.amount_paid == 4_000_000
            assert withdrawals[1].month == 12
            assert withdrawals[1].receipt.amount_paid == expected
            assert withdrawals[2].month == 24
            assert withdrawals[2].receipt.amount_paid == next_expected
            assert not financial.events.lot_dispositions.is_empty()  # actual funding sales
            assert rollout.summary.tax_accruals == []  # this control is intentionally tax-free
            failed_month = rollout.stop.month if rollout.stop is not None else None
            observed_months = HORIZON_MONTHS if failed_month is None else failed_month + 1
            requests, payments = consumption(document)
            requested = requests[rollout_id]
            paid = payments[rollout_id]
            assert len(requested) == len(paid) == observed_months
            receipts = {row.month: row.receipt for row in rollout.summary.payments}
            for month in range(observed_months):
                receipt = receipts.get(month)
                assert requested[month] == (receipt.amount_requested if receipt else 0)
                assert paid[month] == (receipt.amount_paid if receipt else 0)
            assert document.rollouts[rollout_id].summary == rollout.summary
        distribution = json.loads((output / f"{name}.consumption.json").read_text())
        assert distribution["component"] == "annual_consumption"
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
    fixed = Finished.model_validate_json((output / "fixed_real.json").read_text())
    bounded = Finished.model_validate_json((output / "bounded.json").read_text())
    assert fixed.rollouts[0].stop == RejectedAction(month=12, action_index=0)
    assert consumption(fixed) == ([[100_000_000, *([0] * 11), 100_000_000]], [[100_000_000, *([0] * 12)]])
    assert bounded.rollouts[0].stop is None
    assert consumption(bounded) == ([[100_000_000, *([0] * (HORIZON_MONTHS - 1))]],) * 2
    fixed_distribution = json.loads((output / "fixed_real.consumption.json").read_text())
    bounded_distribution = json.loads((output / "bounded.consumption.json").read_text())
    assert fixed_distribution["months"][12]["observed_path_count"] == 1
    assert fixed_distribution["months"][12]["consumption_paid"] == [0, 0, 0]
    assert fixed_distribution["months"][13]["observed_path_count"] == 0
    assert fixed_distribution["months"][13]["consumption_requested"] is None
    assert fixed_distribution["months"][13]["consumption_paid"] is None
    assert bounded_distribution["months"][13]["observed_path_count"] == 1
    assert bounded_distribution["months"][13]["consumption_paid"] == [0, 0, 0]


def test_actual_study_cli_runs_on_generated_placeholder_paths(tmp_path: Path) -> None:
    output = tmp_path / "cli-comparison"
    subprocess.run(
        [
            get_required_path("_main/finance/augur/x/bounded_spending/compare_bin"),
            "--synthetic",
            "--equity-share",
            "1",
            "--rate-bps",
            "400",
            "--max-cut-bps",
            "1000",
            "--max-raise-bps",
            "500",
            "--trace-rollout",
            "2",
            "--output-dir",
            str(output),
        ],
        check=True,
    )
    population = Finished.model_validate_json((output / "bounded.json").read_text())
    replay = Rollout.model_validate_json((output / "bounded.trace-2.json").read_text())
    assert replay.summary == population.rollouts[2].summary
    assert consumption(population)[1][2][12] == 4_992_000


if __name__ == "__main__":
    pytest_bazel.main()

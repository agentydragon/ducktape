"""Exercise the documented paired sweep and its financial composition boundaries."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.sim.results import Paid
from finance.augur.x.bounded_spending.python_policy import Parameters, run
from finance.augur.x.joint_spending_allocation.compare import Output, measurements
from finance.augur.x.joint_spending_allocation.policy import JointPolicy
from finance.augur.x.joint_spending_allocation.scenario import prepare, sample
from util.bazel.runfiles import get_required_path, own_repo_rlocation


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("joint-study") / "results"
    subprocess.run(
        [
            get_required_path(own_repo_rlocation("finance/augur/x/joint_spending_allocation/compare_bin")),
            "--output-dir",
            directory,
        ],
        check=True,
    )
    return directory


def test_same_paths_and_reserves_fund_bills_and_chosen_consumption(results: Path) -> None:
    manifest = json.loads((results / "experiment.json").read_text())
    for cell in manifest["cells"]:
        output = Output.model_validate_json((results / f"{cell['name']}.json").read_text())
        assert [row.rollout_id for row in output.rollouts] == [0, 1, 2]
        expected = 11_000_000 * cell["spending"]["rate_bps"] // 10_000
        for rollout, report in zip(output.rollouts, output.measurements.paths, strict=True):
            opening = report.months[0]
            assert opening.intended_consumption == opening.consumption_requested == opening.consumption_paid == expected
            assert opening.consumption_shortfall == opening.cut_from_fixed_real_anchor == 0
            first = rollout.summary.payments[:2]
            assert [row.receipt.outcome for row in first] == [Paid(), Paid()]
            assert [row.receipt.amount_requested for row in first] == [100_000, expected]
            assert len(report.months) == rollout.summary.ending_book.month
        assert any(path.tax_paid > 0 for path in output.measurements.paths)


def test_both_policy_dimensions_change_the_joint_financial_result(results: Path) -> None:
    fixed = Output.model_validate_json((results / "r800-fixed_real-constant.json").read_text()).measurements.paths
    bounded = Output.model_validate_json((results / "r800-bounded-constant.json").read_text()).measurements.paths
    glide = Output.model_validate_json((results / "r800-bounded-glide.json").read_text()).measurements.paths
    assert fixed[0].months[12].intended_consumption == 924_000
    assert bounded[0].months[12].intended_consumption == 739_200
    assert bounded[0].months[12].cut_from_fixed_real_anchor == 184_800
    bounded_amount = bounded[1].months[12].intended_consumption
    fixed_amount = fixed[1].months[12].intended_consumption
    assert bounded_amount is not None
    assert fixed_amount is not None
    assert bounded_amount > fixed_amount
    assert any(a.ending_assets != b.ending_assets for a, b in zip(bounded, glide, strict=True))
    assert any(
        a.intended_consumption != b.intended_consumption
        for constant, changing in zip(bounded, glide, strict=True)
        for a, b in zip(constant.months, changing.months, strict=False)
    )


def test_selected_original_ids_replay_identical_intentions_and_financial_prefixes(results: Path) -> None:
    manifest = json.loads((results / "experiment.json").read_text())
    for cell in manifest["cells"]:
        compact = Output.model_validate_json((results / f"{cell['name']}.json").read_text())
        traces = Output.model_validate_json((results / f"{cell['name']}-traces.json").read_text())
        assert [row.rollout_id for row in traces.rollouts] == [2, 0]
        for replay, report in zip(traces.rollouts, traces.measurements.paths, strict=True):
            original = compact.rollouts[replay.rollout_id]
            assert replay.summary == original.summary
            assert replay.stop == original.stop
            assert report == compact.measurements.paths[replay.rollout_id]
            assert replay.trace is not None
            for entry in replay.trace.journal:
                assert sum(posting.amount for posting in entry.postings) == 0


def test_tax_free_control_keeps_tax_payments_separate_from_consumption() -> None:
    paths = sample(horizon_months=25)
    reports = []
    for taxable in (False, True):
        prepared = prepare(paths, rollout_count=3, horizon_months=25, taxable=taxable)
        policy = JointPolicy(Parameters(800, 0, 0), rollout_count=3, annual_step=5)
        output = run(prepared, policy, [2])
        reports.append(measurements(output, policy).paths[0])
    untaxed, taxed = reports
    assert untaxed.stop is taxed.stop is None
    assert untaxed.tax_paid == untaxed.tax_assessed == 0
    assert taxed.tax_paid > 0
    assert [row.consumption_paid for row in taxed.months] == [row.consumption_paid for row in untaxed.months]
    assert taxed.terminal_assets is not None
    assert untaxed.terminal_assets is not None
    assert taxed.terminal_assets < untaxed.terminal_assets


@pytest.mark.parametrize("bill", [100_000, 12_000_000])
def test_exhaustion_distinguishes_rejected_consumption_from_unattempted_intention(bill: int) -> None:
    prepared = prepare(sample(horizon_months=13), rollout_count=3, horizon_months=13, taxable=True)
    obligations = prepared.scenario.obligations
    prepared = replace(
        prepared,
        scenario=replace(prepared.scenario, obligations=(replace(obligations[0], amount_due=bill), *obligations[1:])),
    )
    policy = JointPolicy(Parameters(10_000, 0, 0), rollout_count=3, annual_step=0)
    output = run(prepared, policy, [1], capture="forensic")
    report = measurements(output, policy).paths[0]
    assert report.terminal_assets is None
    assert report.ending_mark_month == 0
    assert len(report.months) == 1
    month = report.months[0]
    assert month.intended_consumption == 11_000_000
    if bill == 100_000:
        assert month.consumption_requested == month.consumption_shortfall == 11_000_000
        assert month.consumption_paid == 0
        assert report.ending_assets == 10_900_000
    else:
        assert month.consumption_requested is None
        assert month.consumption_paid == 0
        assert month.consumption_shortfall == 11_000_000
        assert report.ending_assets == 11_000_000
    financial = output.rollouts[0].trace
    assert financial is not None
    assert financial.events.lot_dispositions.get_column("proceeds_quanta").sum() == 10_000_000
    assert financial.events.lot_dispositions.get_column("cost_basis_consumed_quanta").sum() == 8_000_000


if __name__ == "__main__":
    pytest_bazel.main()

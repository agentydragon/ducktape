"""Exercise the documented paired sweep and its financial composition boundaries."""

import json
import subprocess
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.sim.results import Paid
from finance.augur.x.bounded_spending.python_policy import Parameters
from finance.augur.x.joint_spending_allocation.compare import Measurements, Traces, replay_cell, run_cell
from finance.augur.x.joint_spending_allocation.situation import sample, situation
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
        output = Measurements.model_validate_json((results / f"{cell['name']}.json").read_text())
        assert [row.rollout_id for row in output.paths] == [0, 1, 2]
        expected = 11_000_000 * cell["spending"]["rate_bps"] // 10_000
        for report in output.paths:
            opening = report.months[0]
            assert opening.intended_consumption == opening.consumption_requested == opening.consumption_paid == expected
            assert opening.consumption_shortfall == opening.cut_from_fixed_real_anchor == 0
            first = report.payments[:2]
            assert [row.receipt.outcome for row in first] == [Paid(), Paid()]
            assert [row.receipt.amount_requested for row in first] == [100_000, expected]
            assert len(report.months) == report.closed_months
        assert any(path.tax_paid > 0 for path in output.paths)


def test_both_policy_dimensions_change_the_joint_financial_result(results: Path) -> None:
    fixed = Measurements.model_validate_json((results / "r800-fixed_real-constant.json").read_text()).paths
    bounded = Measurements.model_validate_json((results / "r800-bounded-constant.json").read_text()).paths
    glide = Measurements.model_validate_json((results / "r800-bounded-glide.json").read_text()).paths
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
        compact = Measurements.model_validate_json((results / f"{cell['name']}.json").read_text())
        traces = Traces.model_validate_json((results / f"{cell['name']}-traces.json").read_text())
        assert [replay.measurements.rollout_id for replay in traces.replays] == [2, 0]
        for replay in traces.replays:
            assert replay.measurements == compact.paths[replay.measurements.rollout_id]
            assert replay.journal
            for entry in replay.journal:
                assert sum(posting.amount for posting in entry.postings) == 0


def test_tax_free_control_keeps_tax_payments_separate_from_consumption() -> None:
    paths = sample(horizon_months=25)
    reports = []
    for taxable in (False, True):
        case = situation(paths, rollout_count=3, horizon_months=25, taxable=taxable)
        reports.append(run_cell(case, [2], parameters=Parameters(800, 0, 0), annual_step=5).paths[0])
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
    case = situation(sample(horizon_months=13), rollout_count=3, horizon_months=13, taxable=True, annual_bill=bill)
    [replay] = replay_cell(case, [1], parameters=Parameters(10_000, 0, 0), annual_step=0).replays
    report = replay.measurements
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
    assert sum(row.proceeds for row in replay.dispositions) == 10_000_000
    assert sum(row.basis for row in replay.dispositions) == 8_000_000


if __name__ == "__main__":
    pytest_bazel.main()

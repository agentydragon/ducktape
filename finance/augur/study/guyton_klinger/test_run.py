"""Hand-checked annual controls: $100, flat CPI, a fixed $10 withdrawal each January.

Expected wealth is independent arithmetic; proxy rounding is bounded, never folded
into the expectations.
"""

import json
import subprocess
import textwrap
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.sim.ids import AssetId
from finance.augur.sim.results import Finished, Rollout
from finance.augur.study.guyton_klinger.panel import Sleeve, load_panel
from finance.augur.study.guyton_klinger.paths import (
    ADAPTATION_TARGET_PERCENT,
    QUANTUM,
    AnnualWindows,
    annual_windows,
    sleeve_targets,
)
from finance.augur.study.guyton_klinger.run import fixed_nominal_withdrawal, run
from util.bazel.runfiles import get_required_path, own_repo_rlocation

# Window 2001: every sleeve +10% then 0%. Window 2003: cash +5% then 0%, the other sleeves
# -50% so that only a cash-only book reaches the control. Window 2005: 0% then 0%.
CONTROLS = textwrap.dedent(
    """\
    year,cash,bonds,equity,inflation
    2001,0.10,0.10,0.10,0
    2002,0,0,0,0
    2003,0.05,-0.5,-0.5,0
    2004,0,0,0,0
    2005,0,0,0,0
    2006,0,0,0,0
    """
)
WITHDRAWAL = 10_000_000  # $10 in micro-dollar quanta.
CASH_ONLY = {Sleeve.CASH: 1, Sleeve.BONDS: 0, Sleeve.EQUITY: 0}
# Per-lot ceiling sales and marks each round at most half a micro-dollar per sleeve.
ROUNDING = Decimal("0.00001")


@pytest.fixture
def panel_path(tmp_path: Path) -> Path:
    path = tmp_path / "controls.csv"
    path.write_text(CONTROLS)
    return path


@pytest.fixture
def windows(panel_path: Path) -> AnnualWindows:
    return annual_windows(load_panel(panel_path), start_years=[2001, 2003, 2005], years=2)


def simulate(
    windows: AnnualWindows, weights: Mapping[Sleeve, int], rollout_ids: list[int] | None = None
) -> list[Rollout]:
    return run(
        windows,
        fixed_nominal_withdrawal(WITHDRAWAL, targets=sleeve_targets(weights)),
        wealth=Decimal(100),
        weights=weights,
        rollout_ids=rollout_ids,
        capture="forensic",
    )


def wealth(rollout: Rollout, *months: int) -> list[Decimal]:
    """Holdings plus checking at each month's mark, in dollars."""
    summary = rollout.summary
    marks = list(zip(*(row.values for row in (*summary.public_holdings, *summary.cash)), strict=True))
    return [sum(marks[month]) * QUANTUM for month in months]


def dollars(*values: str) -> object:
    return pytest.approx([Decimal(value) for value in values], abs=ROUNDING)


def withdrawals(rollout: Rollout) -> list[tuple[int, int]]:
    return [(row.month, row.receipt.amount_paid) for row in rollout.summary.payments]


def test_withdrawal_leaves_at_the_opening_and_the_final_mark_earns_without_a_third(windows: AnnualWindows) -> None:
    rollout = simulate(windows, ADAPTATION_TARGET_PERCENT)[0]
    # 100 - 10 = 90; 90 * 1.1 = 99 opens year two; 99 - 10 = 89, flat to the terminal mark.
    assert wealth(rollout, 0, 1, 11, 12, 13, 24) == dollars("100", "90", "90", "99", "89", "89")
    assert withdrawals(rollout) == [(0, WITHDRAWAL), (12, WITHDRAWAL)]
    assert (rollout.stop, rollout.summary.ending_mark_month) == (None, 24)
    assert len(rollout.summary.cash[0].values) == 25


def test_cash_sleeve_earns_its_return(windows: AnnualWindows) -> None:
    [rollout] = simulate(windows, CASH_ONLY, [1])
    # (100 - 10) * 1.05 = 94.5 closes year one; 94.5 - 10 = 84.5 closes year two.
    assert wealth(rollout, 12, 24) == dollars("94.5", "84.5")
    assert rollout.summary.public_holdings[0].asset_id == AssetId(Sleeve.CASH)
    assert rollout.stop is None


def test_selected_replay_returns_the_original_paths(windows: AnnualWindows) -> None:
    population = simulate(windows, ADAPTATION_TARGET_PERCENT)
    assert wealth(population[2], 12, 24) == dollars("90", "80")
    selected = simulate(windows, ADAPTATION_TARGET_PERCENT, [2, 0])
    assert [row.rollout_id for row in selected] == [2, 0]
    assert wealth(selected[0], 24) == dollars("80")
    for row in selected:
        assert (row.summary, row.stop) == (population[row.rollout_id].summary, population[row.rollout_id].stop)


def test_cli_replays_reordered_start_years_from_a_panel_file(tmp_path: Path, panel_path: Path) -> None:
    output = tmp_path / "study"
    subprocess.run(
        [
            get_required_path(own_repo_rlocation("finance/augur/study/guyton_klinger/run_bin")),
            *("--panel", panel_path, "--years", "2", "--initial-wealth", "100", "--withdrawal", "10"),
            *("--start-year", "2005", "--start-year", "2001", "--start-year", "2003"),
            *("--output-dir", output, "--trace-rollout", "2", "--trace-rollout", "0"),
        ],
        check=True,
    )
    study = json.loads((output / "study.json").read_text())
    assert study["start_years"] == [2005, 2001, 2003]
    outcomes = Finished.model_validate_json((output / "outcomes.json").read_text()).rollouts
    traces = Finished.model_validate_json((output / "traces.json").read_text()).rollouts
    # Window 2003 at 10/25/65: 9 * 1.05 + 22.5 * 0.5 + 58.5 * 0.5 = 49.95, less 10 in year two.
    assert [wealth(row, 24)[0] for row in outcomes] == dollars("80", "89", "39.95")
    assert [row.rollout_id for row in traces] == [2, 0]
    for trace in traces:
        assert trace.summary == outcomes[trace.rollout_id].summary
        assert trace.trace is not None
        assert {row.action.kind for row in trace.trace.receipts} == {"Sell", "Consume"}


def test_cli_runs_the_generated_placeholder_panel(tmp_path: Path) -> None:
    output = tmp_path / "study"
    subprocess.run(
        [
            get_required_path(own_repo_rlocation("finance/augur/study/guyton_klinger/run_bin")),
            *("--synthetic", "--years", "5", "--initial-wealth", "1000000", "--withdrawal", "40000"),
            *("--output-dir", output),
        ],
        check=True,
    )
    study = json.loads((output / "study.json").read_text())
    assert (study["source"], study["start_years"]) == ("synthetic placeholder panel", [1930, 1931, 1932, 1933])
    outcomes = Finished.model_validate_json((output / "outcomes.json").read_text()).rollouts
    assert [row.rollout_id for row in outcomes] == [0, 1, 2, 3]
    assert len({wealth(row, 60)[0] for row in outcomes}) == 4


if __name__ == "__main__":
    pytest_bazel.main()

"""Hand-checked annual controls through the Guyton-Klinger policy and its CLI.

The two-year controls open at $100 with w0 = 10% and flat CPI; every year's $10 stays
between the guardrails, so wealth is plain arithmetic. Expected amounts are independent
arithmetic; proxy rounding is bounded, never folded into the expectations.
"""

import json
import subprocess
import textwrap
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
import pytest_bazel

from finance.augur.sim.ids import AssetId
from finance.augur.sim.results import Finished, Rollout
from finance.augur.study.guyton_klinger.panel import Sleeve, load_panel
from finance.augur.study.guyton_klinger.paths import ADAPTATION_TARGET_PERCENT, QUANTUM, AnnualWindows, annual_windows
from finance.augur.study.guyton_klinger.policy import Cell, Guardrail, Inflation, Stage
from finance.augur.study.guyton_klinger.run import Records, YearRecordView, run
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
DOLLAR = 1_000_000  # Micro-dollar quanta.
WITHDRAWAL = 10 * DOLLAR
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
    rollouts, _ = run(
        windows,
        Cell(initial_rate=Fraction(1, 10), years=2),
        wealth=Decimal(100),
        weights=weights,
        rollout_ids=rollout_ids,
        capture="forensic",
    )
    return rollouts


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


def cli(output: Path, *args: str | Path) -> None:
    subprocess.run(
        [
            get_required_path(own_repo_rlocation("finance/augur/study/guyton_klinger/run_bin")),
            *args,
            *("--output-dir", output),
        ],
        check=True,
    )


def test_cli_replays_reordered_start_years_from_a_panel_file(tmp_path: Path, panel_path: Path) -> None:
    output = tmp_path / "study"
    cli(
        output,
        *("--panel", panel_path, "--years", "2", "--initial-wealth", "100", "--initial-rate", "0.1"),
        *("--start-year", "2005", "--start-year", "2001", "--start-year", "2003"),
        *("--trace-rollout", "2", "--trace-rollout", "0"),
    )
    study = json.loads((output / "study.json").read_text())
    assert study["start_years"] == [2005, 2001, 2003]
    outcomes = Finished.model_validate_json((output / "outcomes.json").read_text()).rollouts
    traces = Finished.model_validate_json((output / "traces.json").read_text()).rollouts
    # Window 2003 at 10/25/65: year 0's $10 spends the whole cash sleeve; 25 * 0.5 + 65 * 0.5 = 45
    # opens year two, whose 22% rate cannot be cut inside the final fifteen years: 45 - 10 = 35.
    assert [wealth(row, 24)[0] for row in outcomes] == dollars("80", "89", "35")
    records = Records.model_validate_json((output / "records.json").read_text()).paths
    assert [(row.rollout_id, row.start_year) for row in records] == [(0, 2005), (1, 2001), (2, 2003)]
    assert [[year.withdrawal for year in row.years] for row in records] == [[WITHDRAWAL, WITHDRAWAL]] * 3
    assert [row.rollout_id for row in traces] == [2, 0]
    for trace in traces:
        assert trace.summary == outcomes[trace.rollout_id].summary


# $1000 at w0 = 5%. Year 0 spends $50 of the $100 cash sleeve. Equity halves while CPI rises 25%:
# year 1 opens at 50 + 250 + 325 = 625, below the 950 book, and 62.5 exceeds 5% of 625, so the
# increase freezes at 50, again from cash. Equity quadruples: year 2 opens at 250 + 1300 = 1550,
# where 50 is under 80% of 77.5 = 62, so prosperity raises it to 55. Equity rose and sits 292.5
# over its 1007.5 target: that funds the 55 and sweeps the other 237.5 into cash; 1495 remains.
GUARDRAILS = textwrap.dedent(
    """\
    year,cash,bonds,equity,inflation
    2011,0,0,-0.5,0.25
    2012,0,0,3,0
    2013,0,0,0,0
    """
)


def test_cli_records_a_freeze_and_a_prosperity_raise(tmp_path: Path) -> None:
    panel = tmp_path / "guardrails.csv"
    panel.write_text(GUARDRAILS)
    output = tmp_path / "study"
    cli(output, *("--panel", panel, "--years", "3", "--initial-wealth", "1000", "--initial-rate", "0.05"))
    [path] = Records.model_validate_json((output / "records.json").read_text()).paths
    assert (path.rollout_id, path.start_year) == (0, 2011)
    assert path.years == [
        YearRecordView(
            year=0,
            opening_wealth=1000 * DOLLAR,
            withdrawal=50 * DOLLAR,
            inflation=Inflation.INITIAL,
            guardrail=Guardrail.NONE,
            funding={Stage.CASH: 50 * DOLLAR},
        ),
        YearRecordView(
            year=1,
            opening_wealth=625 * DOLLAR,
            withdrawal=50 * DOLLAR,
            inflation=Inflation.FROZEN,
            guardrail=Guardrail.NONE,
            funding={Stage.CASH: 50 * DOLLAR},
        ),
        YearRecordView(
            year=2,
            opening_wealth=1550 * DOLLAR,
            withdrawal=55 * DOLLAR,
            inflation=Inflation.APPLIED,
            guardrail=Guardrail.RAISE,
            funding={Stage.OVERWEIGHT_EQUITY: 55 * DOLLAR},
        ),
    ]
    [rollout] = Finished.model_validate_json((output / "outcomes.json").read_text()).rollouts
    assert withdrawals(rollout) == [(0, 50 * DOLLAR), (12, 50 * DOLLAR), (24, 55 * DOLLAR)]
    assert wealth(rollout, 36) == dollars("1495")


def test_cli_runs_the_generated_placeholder_panel(tmp_path: Path) -> None:
    output = tmp_path / "study"
    cli(output, *("--synthetic", "--years", "5", "--initial-wealth", "1000000", "--initial-rate", "0.04"))
    study = json.loads((output / "study.json").read_text())
    assert (study["source"], study["start_years"]) == ("synthetic placeholder panel", [1930, 1931, 1932, 1933])
    outcomes = Finished.model_validate_json((output / "outcomes.json").read_text()).rollouts
    assert [row.rollout_id for row in outcomes] == [0, 1, 2, 3]
    assert len({wealth(row, 60)[0] for row in outcomes}) == 4
    records = Records.model_validate_json((output / "records.json").read_text()).paths
    assert [[year.year for year in row.years] for row in records] == [list(range(5))] * 4


if __name__ == "__main__":
    pytest_bazel.main()

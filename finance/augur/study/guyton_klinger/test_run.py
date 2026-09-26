"""Hand-checked annual controls through the Guyton-Klinger policy and its CLI.

The untaxed two-year controls open at $100 with w0 = 10% and flat CPI; every year's $10 stays
between the guardrails, so wealth is plain arithmetic. Expected amounts are independent
arithmetic, taxes included; proxy rounding is bounded, never folded into the expectations.
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
from finance.augur.study.guyton_klinger.paths import (
    ADAPTATION_TARGET_PERCENT,
    QUANTUM,
    AnnualWindows,
    Taxes,
    annual_windows,
)
from finance.augur.study.guyton_klinger.policy import Cell, Guardrail, Inflation, Stage
from finance.augur.study.guyton_klinger.run import Records, YearAmounts, YearRecordView, run
from util.bazel.runfiles import get_required_path, own_repo_rlocation

HEADER = "year,cash_income,bonds_income,bonds_price,equity_income,equity_price,inflation"
# Window 2001: every sleeve +10% then 0%. Window 2003: cash +5% then 0%, the other sleeves
# -50% so that only a cash-only book reaches the control. Window 2005: 0% then 0%.
CONTROLS = textwrap.dedent(
    f"""\
    {HEADER}
    2001,0.10,0.04,0.06,0.02,0.08,0
    2002,0,0,0,0,0,0
    2003,0.05,0,-0.5,0,-0.5,0
    2004,0,0,0,0,0,0
    2005,0,0,0,0,0,0
    2006,0,0,0,0,0,0
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
    return annual_windows(load_panel(panel_path), start_years=[2001, 2003, 2005], years=2, taxes=Taxes.NONE)


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


def quanta(value: str) -> int:
    return int(Decimal(value) / QUANTUM)


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
        *("--panel", panel_path, "--taxes", "none", "--years", "2", "--initial-wealth", "100", "--initial-rate", "0.1"),
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
    assert [[year.nominal.withdrawal for year in row.years] for row in records] == [[WITHDRAWAL, WITHDRAWAL]] * 3
    assert [row.rollout_id for row in traces] == [2, 0]
    for trace in traces:
        assert trace.summary == outcomes[trace.rollout_id].summary


# $1000 at w0 = 5%. Year 0 spends $50 of the $100 cash sleeve. Equity halves while CPI rises 25%:
# year 1 opens at 50 + 250 + 325 = 625, below the 950 book, and 62.5 exceeds 5% of 625, so the
# increase freezes at 50, again from cash. Equity quadruples: year 2 opens at 250 + 1300 = 1550,
# where 50 is under 80% of 77.5 = 62, so prosperity raises it to 55. Equity rose and sits 292.5
# over its 1007.5 target: that funds the 55 and sweeps the other 237.5 into cash; 1495 remains.
# In the window's January dollars, CPI 1.25 makes year 1's $50 worth $40 and year 2's $55 $44.
GUARDRAILS = textwrap.dedent(
    f"""\
    {HEADER}
    2011,0,0,0,0,-0.5,0.25
    2012,0,0,0,0,3,0
    2013,0,0,0,0,0,0
    """
)


def untaxed(withdrawal: int) -> YearAmounts:
    return YearAmounts(withdrawal=withdrawal, federal_tax=0, california_tax=0, tax=0, spendable=withdrawal)


def test_cli_records_a_freeze_and_a_prosperity_raise(tmp_path: Path) -> None:
    panel = tmp_path / "guardrails.csv"
    panel.write_text(GUARDRAILS)
    output = tmp_path / "study"
    cli(
        output,
        *("--panel", panel, "--taxes", "none", "--years", "3", "--initial-wealth", "1000", "--initial-rate", "0.05"),
    )
    [path] = Records.model_validate_json((output / "records.json").read_text()).paths
    assert (path.rollout_id, path.start_year) == (0, 2011)
    assert path.years == [
        YearRecordView(
            year=0,
            opening_wealth=1000 * DOLLAR,
            inflation=Inflation.INITIAL,
            guardrail=Guardrail.NONE,
            funding={Stage.CASH: 50 * DOLLAR},
            repaid=0,
            reserved=0,
            settled=0,
            nominal=untaxed(50 * DOLLAR),
            real=untaxed(50 * DOLLAR),
        ),
        YearRecordView(
            year=1,
            opening_wealth=625 * DOLLAR,
            inflation=Inflation.FROZEN,
            guardrail=Guardrail.NONE,
            funding={Stage.CASH: 50 * DOLLAR},
            repaid=0,
            reserved=0,
            settled=0,
            nominal=untaxed(50 * DOLLAR),
            real=untaxed(40 * DOLLAR),
        ),
        YearRecordView(
            year=2,
            opening_wealth=1550 * DOLLAR,
            inflation=Inflation.APPLIED,
            guardrail=Guardrail.RAISE,
            funding={Stage.OVERWEIGHT_EQUITY: 55 * DOLLAR},
            repaid=0,
            reserved=0,
            settled=0,
            nominal=untaxed(55 * DOLLAR),
            real=untaxed(44 * DOLLAR),
        ),
    ]
    [rollout] = Finished.model_validate_json((output / "outcomes.json").read_text()).rollouts
    assert withdrawals(rollout) == [(0, 50 * DOLLAR), (12, 50 * DOLLAR), (24, 55 * DOLLAR)]
    assert wealth(rollout, 36) == dollars("1495")


def test_cli_runs_the_generated_placeholder_panel(tmp_path: Path) -> None:
    output = tmp_path / "study"
    cli(
        output,
        *("--synthetic", "--taxes", "federal-ca", "--years", "5", "--initial-wealth", "1000000"),
        *("--initial-rate", "0.04"),
    )
    study = json.loads((output / "study.json").read_text())
    assert (study["source"], study["start_years"]) == ("synthetic placeholder panel", [1930, 1931, 1932, 1933])
    outcomes = Finished.model_validate_json((output / "outcomes.json").read_text()).rollouts
    assert [row.rollout_id for row in outcomes] == [0, 1, 2, 3]
    assert len({wealth(row, 60)[0] for row in outcomes}) == 4
    records = Records.model_validate_json((output / "records.json").read_text()).paths
    assert [[year.year for year in row.years] for row in records] == [list(range(5))] * 4
    for year in (year for row in records for year in row.years):
        nominal = year.nominal
        assert nominal.tax == (nominal.federal_tax or 0) + (nominal.california_tax or 0) > 0
        assert nominal.spendable == nominal.withdrawal - nominal.tax


# Taxed, $1M at w0 = 5% and flat CPI. 2001: bills pay 4%, bonds a 5% coupon and rise 20%, equity
# pays a 4% dividend and rises 25%. 2002: bills 2%, coupon 5%, dividend 6%, no price moves.
TAXED = textwrap.dedent(
    f"""\
    {HEADER}
    2001,0.04,0.05,0.20,0.04,0.25,0
    2002,0.02,0.05,0,0.06,0,0
    """
)


def test_cli_pays_each_years_federal_and_california_tax_out_of_its_withdrawal(tmp_path: Path) -> None:
    panel = tmp_path / "taxed.csv"
    panel.write_text(TAXED)
    common = ("--panel", panel, "--years", "2", "--initial-wealth", "1000000", "--initial-rate", "0.05")
    cli(tmp_path / "taxed", *common, "--taxes", "federal-ca")
    cli(tmp_path / "untaxed", *common, "--taxes", "none")
    [path] = Records.model_validate_json((tmp_path / "taxed" / "records.json").read_text()).paths
    [taxed] = Finished.model_validate_json((tmp_path / "taxed" / "outcomes.json").read_text()).rollouts
    [control] = Finished.model_validate_json((tmp_path / "untaxed" / "outcomes.json").read_text()).rollouts
    year_0, year_1 = path.years
    # Year 0 spends $50k of the $100k bills. December pays $2000 bill interest and a $12,500
    # coupon, Treasury interest California exempts, and a $26,000 qualified dividend. Federal:
    # the $14,500 of interest falls within the $14,600 deduction, and the dividend's $25,900 of
    # taxable income sits in the 0% bracket. California taxes the dividend as ordinary income:
    # 26,000 - 5363 = 20,637; 1% of 10,412 + 2% of 10,225 = 308.62.
    assert year_0.nominal == YearAmounts(
        withdrawal=quanta("50000"),
        federal_tax=0,
        california_tax=quanta("308.62"),
        tax=quanta("308.62"),
        spendable=quanta("49691.38"),
    )
    # Year 1 opens at 52,000 bills + 312,500 bonds + 838,500 equity, payouts included:
    # 1,203,000, so $50k stays between the guardrails. Equity is 56,550 over target: its
    # $26,000 dividend and 19,200 units ($24,000, a $4800 gain) fund the withdrawal. No
    # reserve covered year 0's tax, so the stages raise it too and $308.62 of the $50k repays
    # the portfolio; another $308.62, year 0's tax, is reserved, and $49,382.76 is spent.
    assert (year_1.opening_wealth, year_1.funding) == (quanta("1203000"), {Stage.OVERWEIGHT_EQUITY: quanta("50000")})
    assert (year_1.repaid, year_1.reserved, year_1.settled) == (quanta("308.62"), quanta("308.62"), 0)
    assert [(row.month, row.cause_id, row.receipt.amount_paid) for row in taxed.summary.payments] == [
        (0, "gk-y0-withdrawal", quanta("50000")),
        (12, "retiree_tax_true_up_y0", quanta("308.62")),
        (12, "gk-y1-withdrawal", quanta("49382.76")),
    ]
    # Year 0 left $50,000 - $308.62 to spend: paid in January, less what year 1 withheld.
    assert quanta("50000") - year_1.repaid == year_0.nominal.spendable
    # The sweep sells the other 5240 equity units ($6550, a $1310 gain) and draws $11,750 of the
    # bond coupon, then buys bills with them and the bill interest; the coupon's last $750 buys
    # bonds. 2002 pays $1406 bill interest, a $15,037.50 coupon and a $46,917 dividend, and the
    # year realized $6110 of long-term gain. Federal: the $16,443.50 of interest leaves 1843.50
    # over the deduction, 10%: 184.35; the dividend and gain stack from there to 54,870.50,
    # the 7845.50 above $47,025 at 15%: 1176.825. California taxes the dividend and gain as
    # ordinary income, 53,027 - 5363 = 47,664: 104.12 + 285.44 + 571 + 6% of 8705 = 1482.86.
    assert year_1.nominal == YearAmounts(
        withdrawal=quanta("50000"),
        federal_tax=quanta("1361.175"),
        california_tax=quanta("1482.86"),
        tax=quanta("2844.035"),
        spendable=quanta("47155.965"),
    )
    # The withdrawals are gross, so the portfolio matches the untaxed run's $1,216,360.50, less
    # the $2535.415 of year 1's tax its $308.62 reserve leaves to the next January.
    assert wealth(control, 24) == dollars("1216360.50")
    assert (path.terminal_wealth, path.real_terminal_wealth) == (quanta("1213825.085"), quanta("1213825.085"))
    headline = json.loads((tmp_path / "taxed" / "study.json").read_text())["headline"]
    assert (headline["median_real_lifetime_spendable"], headline["min_real_annual_spendable"]) == (
        "96847.35",
        "47155.97",
    )


if __name__ == "__main__":
    pytest_bazel.main()

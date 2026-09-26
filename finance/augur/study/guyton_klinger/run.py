"""Drive composed annual windows through the Guyton-Klinger policy, and the offline CLI.

Every window opens at the adaptation targets. The CLI runs a fresh <policy.py> `Policy` for
the population and another for the selected trace replay. `records.json` carries each path's
per-year intentions and what each year's withdrawal left to spend once its tax was paid; paid
amounts are the receipts' in `outcomes.json`.

    bbr run //finance/augur/study/guyton_klinger:run_bin -- --synthetic --years 30 --taxes federal-ca \\
      --initial-wealth 1000000 --initial-rate 0.05 --output-dir /tmp/gk --trace-rollout 2
"""

import argparse
import json
import statistics
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from math import floor
from pathlib import Path

from pydantic import BaseModel, Field

from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import Capture
from finance.augur.study.guyton_klinger.panel import Sleeve, load_panel
from finance.augur.study.guyton_klinger.paths import (
    ADAPTATION_TARGET_PERCENT,
    CALIFORNIA,
    FEDERAL,
    MONTHS_PER_YEAR,
    QUANTUM,
    RETIREE,
    TAX_RESERVE,
    AnnualWindows,
    Taxes,
    annual_windows,
    compose_world,
    eligible_start_years,
)
from finance.augur.study.guyton_klinger.policy import Cell, Guardrail, Inflation, Policy, Stage
from finance.augur.study.guyton_klinger.synthetic import synthetic_panel


def run(
    windows: AnnualWindows,
    cell: Cell,
    *,
    wealth: Decimal,
    weights: Mapping[Sleeve, int],
    rollout_ids: Sequence[int] | None = None,
    capture: Capture = "summary",
) -> tuple[list[Rollout], Policy]:
    """Selected original rollout IDs, in the given order, under a fresh `Policy` for `cell`.

    Selected replay reuses `windows`, never rematerializes them; the returned policy holds
    each path's `YearRecord`s.
    """
    ids = range(len(windows.start_years)) if rollout_ids is None else rollout_ids
    if len(set(ids)) != len(ids):
        raise ValueError(f"{rollout_ids=} must be distinct")
    policy = Policy(cell)
    session = ActionSession(
        {id_: compose_world(windows, id_, wealth=wealth, weights=weights) for id_ in ids}, RETIREE, capture=capture
    )
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(policy(batch))
        return batch.rollouts, policy
    finally:
        session.close()


class YearAmounts(BaseModel):
    """One year's withdrawal and the tax paid out of it, in currency quanta of one set of dollars.

    A tax is `None` where the path stopped before that tax year closed.
    """

    withdrawal: int = Field(description="W, what leaves the portfolio; the paid amounts are in the receipts")
    federal_tax: int | None = Field(default=None, description="The year's assessed federal income tax")
    california_tax: int | None = Field(default=None, description="The year's assessed California income tax")
    tax: int | None = Field(default=None, description="Federal plus California")
    spendable: int | None = Field(default=None, description="W less the year's tax: what the year leaves to spend")


class YearRecordView(BaseModel):
    """One review's `policy.YearRecord` and its year's tax as the CLI writes them; money in currency quanta."""

    year: int = Field(description="Zero-based year index within the window")
    opening_wealth: int
    inflation: Inflation
    guardrail: Guardrail
    funding: dict[Stage, int] = Field(description="Proceeds per funding stage, in funding order")
    repaid: int = Field(description="Withheld from W to repay tax the portfolio advanced; it stays invested")
    reserved: int = Field(description="Moved from W into the tax reserve toward this year's tax")
    settled: int = Field(description="The previous tax year's reserve left after its claims, spent at this review")
    nominal: YearAmounts
    real: YearAmounts = Field(description="`nominal` deflated by CPI to the window's January, half up")


class PathRecords(BaseModel):
    rollout_id: int
    start_year: int
    years: list[YearRecordView]
    terminal_wealth: int | None = Field(
        default=None,
        description=(
            "The portfolio at the horizon, less the final year's tax its reserve cannot cover and unrepaid tax "
            "advances; `None` for a stopped path"
        ),
    )
    real_terminal_wealth: int | None = Field(
        default=None, description="`terminal_wealth` in the window's January dollars"
    )


class Records(BaseModel):
    paths: list[PathRecords]


def _half_up(amount: Fraction) -> int:
    return floor(amount + Fraction(1, 2))


def _dollars(quanta: float) -> str:
    return str((Decimal(quanta) * QUANTUM).quantize(Decimal("0.01")))


def _amounts(withdrawal: int, taxes: Mapping[str, int] | None, deflator: Fraction) -> YearAmounts:
    def real(amount: int) -> int:
        return _half_up(amount / deflator)

    if taxes is None:
        return YearAmounts(withdrawal=real(withdrawal))
    federal, california = taxes.get(FEDERAL, 0), taxes.get(CALIFORNIA, 0)
    return YearAmounts(
        withdrawal=real(withdrawal),
        federal_tax=real(federal),
        california_tax=real(california),
        tax=real(federal + california),
        spendable=real(withdrawal - federal - california),
    )


def _terminal_wealth(rollout: Rollout, debt: int) -> int:
    """Portfolio at the horizon net of what its final tax year and unrepaid advances still take from it."""
    summary = rollout.summary
    end = summary.ending_mark_month
    reserve = sum(row.values[-1] for row in summary.cash if row.account.account_id == TAX_RESERVE)
    portfolio = sum(row.values[-1] for row in (*summary.public_holdings, *summary.cash)) - reserve
    final_tax = sum(row.total_tax for row in summary.tax_accruals if row.tax_year_end_month == end - 1)
    estimated = sum(row.amount_paid for row in summary.tax_payments if row.month > end - MONTHS_PER_YEAR)
    return portfolio - max(0, max(0, final_tax - estimated) - reserve) - debt


def path_records(windows: AnnualWindows, policy: Policy, rollouts: Sequence[Rollout]) -> Records:
    paths = []
    for rollout in rollouts:
        cpi = MarketPath(windows.series, rollout.rollout_id, rollout_count=len(windows.start_years)).path("inflation")
        taxes: dict[int, dict[str, int]] = {}
        for row in rollout.summary.tax_accruals:
            by_jurisdiction = taxes.setdefault(row.tax_year_end_month // MONTHS_PER_YEAR, {})
            by_jurisdiction[row.jurisdiction_id] = by_jurisdiction.get(row.jurisdiction_id, 0) + row.total_tax
        memory = policy.memory[rollout.rollout_id]
        years = []
        for record in memory.records:
            # A stopped path's last reviewed year never closed; an untaxed year owes nothing.
            closed = windows.taxes is Taxes.NONE or rollout.stop is None or record.year < len(memory.records) - 1
            year_taxes = taxes.get(record.year, {}) if closed else None
            years.append(
                YearRecordView(
                    year=record.year,
                    opening_wealth=record.opening_wealth,
                    inflation=record.spending.inflation,
                    guardrail=record.spending.guardrail,
                    funding=dict(record.funding),
                    repaid=record.repaid,
                    reserved=record.reserved,
                    settled=record.settled,
                    nominal=_amounts(record.requested, year_taxes, Fraction(1)),
                    real=_amounts(record.requested, year_taxes, Fraction(cpi[record.year * MONTHS_PER_YEAR], cpi[0])),
                )
            )
        terminal = None if rollout.stop is not None else _terminal_wealth(rollout, memory.debt)
        paths.append(
            PathRecords(
                rollout_id=rollout.rollout_id,
                start_year=windows.start_years[rollout.rollout_id],
                years=years,
                terminal_wealth=terminal,
                real_terminal_wealth=None if terminal is None else _half_up(Fraction(terminal * cpi[0], cpi[-1])),
            )
        )
    return Records(paths=paths)


def headline(records: Records) -> dict[str, object]:
    """Real spendable income first, in dollars over completed windows; GK success needs $1 at the horizon."""
    completed = [
        (terminal, real_terminal, [spendable for year in path.years if (spendable := year.real.spendable) is not None])
        for path in records.paths
        if (terminal := path.terminal_wealth) is not None and (real_terminal := path.real_terminal_wealth) is not None
    ]
    summary: dict[str, object] = {"windows": len(records.paths), "completed_windows": len(completed)}
    if completed:
        summary |= {
            "successful_windows": sum(terminal * QUANTUM >= 1 for terminal, _, _ in completed),
            "median_real_lifetime_spendable": _dollars(statistics.median(sum(years) for _, _, years in completed)),
            "min_real_annual_spendable": _dollars(min(value for _, _, years in completed for value in years)),
            "median_real_terminal_wealth": _dollars(statistics.median(real for _, real, _ in completed)),
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Annual three-sleeve replay of the Guyton-Klinger 2006 rules")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--panel", type=Path, help="Annual panel CSV; see panel.py for the format")
    source.add_argument("--synthetic", action="store_true", help="Generated placeholder panel, not evidence")
    parser.add_argument("--years", type=int, required=True)
    parser.add_argument("--start-year", type=int, action="append", help="Default: every complete window")
    parser.add_argument("--taxes", type=Taxes, choices=list(Taxes), required=True)
    parser.add_argument("--initial-wealth", type=Decimal, required=True)
    parser.add_argument("--initial-rate", type=Fraction, required=True, help="Year-0 withdrawal over wealth, e.g. 0.05")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--trace-rollout", type=int, action="append", default=[])
    args = parser.parse_args()
    panel = synthetic_panel(args.years) if args.synthetic else load_panel(args.panel)
    start_years = args.start_year or eligible_start_years(panel, args.years)
    windows = annual_windows(panel, start_years=start_years, years=args.years, taxes=args.taxes)
    cell = Cell(initial_rate=args.initial_rate, years=args.years)

    def replay(rollout_ids: Sequence[int] | None, capture: Capture) -> tuple[list[Rollout], Policy]:
        return run(
            windows,
            cell,
            wealth=args.initial_wealth,
            weights=ADAPTATION_TARGET_PERCENT,
            rollout_ids=rollout_ids,
            capture=capture,
        )

    outcomes, policy = replay(None, "summary")
    records = path_records(windows, policy, outcomes)
    summary = headline(records)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir / "outcomes.json").write_text(Finished(rollouts=outcomes).model_dump_json())
    (args.output_dir / "records.json").write_text(records.model_dump_json())
    (args.output_dir / "study.json").write_text(
        json.dumps(
            {
                "headline": summary,
                "source": "synthetic placeholder panel" if args.synthetic else str(args.panel),
                "policy": "Guyton-Klinger 2006 rules, declared three-sleeve adaptation",
                "taxes": args.taxes,
                "start_years": windows.start_years,
                "years": windows.years,
                "target_percent": ADAPTATION_TARGET_PERCENT,
                "initial_wealth": str(args.initial_wealth),
                "initial_rate": str(args.initial_rate),
            }
        )
    )
    if args.trace_rollout:
        traces, _ = replay(args.trace_rollout, "forensic")
        (args.output_dir / "traces.json").write_text(Finished(rollouts=traces).model_dump_json())
    print(f"Saved {len(outcomes)} overlapping windows to {args.output_dir}; not independent trials. {summary}")


if __name__ == "__main__":
    main()

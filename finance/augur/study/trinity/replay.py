"""Reproduce the Trinity study's 30-year portfolio success rates through augur's simulator.

Philip L. Cooley, Carl M. Hubbard and Daniel T. Walz, "Retirement Savings: Choosing a
Withdrawal Rate That Is Sustainable", *AAII Journal* XX(2), February 1998, pp. 16-21.
Their Table 3 — inflation-adjusted withdrawals, 1926 to 1995 — is the target.

The numerical attribution below records the earlier configured-runner investigation,
not a fresh measurement of the Python action-session migration. It is not exact paper
reproduction; the manual sourced tests retain their existing tolerances.

**Why reproduce a 1998 paper at all.** Every other check on augur is internal: the engines
agree with each other, the money math is exact, the fitted model scores well on its own
holdout. None of that can catch a portfolio simulator that is self-consistently wrong. The
Trinity table is an external number, computed from the same history by people who were not us,
and hitting it is evidence the whole stack — sampler, compiler, engine, liquidity policy —
composes into something that answers the question it claims to answer.

**What is deliberately NOT identical**, since a reproduction is only as useful as its
attribution:

- *Equity is the CRSP total market, not the S&P 500.* Ken French's factors are the longest
  broad-market total-return series reachable without a paid Ibbotson licence, and they start
  in 1926-07 rather than 1926-01, so the record is six months short at the front.
- *Bonds are a constant-maturity fund on Moody's Aaa, not the paper's Ibbotson series.* Aaa IS
  "long-term high-grade corporate" and reaches 1919 without a gap, so the sleeve earns the
  yield high-grade corporates actually paid rather than a government yield plus a guessed
  spread. What remains synthetic is the step from a yield to a total return, and that step is
  the standard one — `bond_fund.constant_maturity_fund_paths`, validated against Damodaran's
  published returns. Two conventions it inherits: annual-pay coupons, and repricing at the
  same maturity each month, so there is no roll-down return.
- ***The all-bond row is PART sensitivity and PART still unexplained.*** Run the paper's own
  annual arithmetic over the same 32 windows on three return series and the row reads:

      SBBI Exhibit A-3, what Trinity used   66 / 19 / 12 /  3   across 3-6%
      this sleeve, maturity 20              47 / 19 / 12 /  3
      Table 3                               80 / 20 / 17 / 12

  So of the 33 points between this sleeve and Table 3, **19 are the bond series and 14 are not
  accounted for by anything measured here.** Do not describe the whole gap as a sleeve
  difference.

  The 19 are, and they are an amplified quarter point. A 30-year CPI-indexed withdrawal
  survives iff the window's real return clears a break-even fixed by the rate, so a cell is
  sensitive exactly insofar as window mass sits near its own break-even:

      rate   break-even real   windows within +/-0.25pp of it
      3%          -0.71%                7 of 32
      4%          +1.31%                2 of 32
      5%          +3.08%                0 of 32
      6%          +4.70%                0 of 32

  At 5% and 6% no window is near, which is why those cells agree with the real series exactly
  and no perturbation moves them. At 3% a fifth of the windows are within a quarter point, and
  this sleeve runs 0.20 points a year under SBBI window by window — enough to carry several of
  them across. Confirmed independently: adding 20 basis points to the curve moves the cell 47%
  to 69% while leaving the sleeve's fit to the real return series untouched (RMSE 2.81 either
  way). Roughly a point of this cell per basis point a year, so read it in basis points and not
  in points of "success rate".

  The remaining 14 is Table 3 differing from what Ibbotson's own bond returns produce, and it
  is NOT explained. If anything it understates: these 32 windows start 1927-1958, where SBBI's
  published returns end, while the paper's 41 run to a 1965 start — and those later starts,
  which eat the 1965-82 inflation early, are the worst ones. On the paper's own window set the
  real series would land lower than 66%, not higher.

  The sleeve's 0.20 points a year is a real defect and is not about this cell — the likely
  cause is the yield source, Moody's Aaa being a narrower, higher-grade universe than the
  Aaa-and-Aa composite Ibbotson priced. Fix it against the return series, never against Table 3.
- *Windows start every month, not every year.* 474 of them against the paper's 41 — the same
  span, sampled 12x more finely, which makes each cell smoother rather than different.

  **This sets how close "close" can be, and it is worth knowing before reading any deviation
  here as a defect.** All 50 of Table 3's published 30-year cells land exactly on `k/41`, so
  the paper's grid has a step of 2.44 points and cannot express anything finer. One window
  flipping moves a cell of theirs by 2.44 points and one of ours by 0.21. Measured in units of
  their own resolution, this reproduction sits 0.41 of a window away over 3-4% — closer than
  their table can represent — and about 1.5 windows away over the full 3-12% range.

  Underneath both is the same thin record: 1926-07 to 1995-12 holds **2.3 independent 30-year
  observations**. Neither table is a probability; both are elaborate readings of roughly two
  non-overlapping experiments, which is also why the test's tolerances here are loose on
  purpose and should not be tightened toward the published digits.
- *Coupons sit in cash until the next withdrawal.* The Python funding policy sells only
  enough to pay due claims and never invests a surplus, so a bond sleeve yielding more than the withdrawal rate
  accumulates idle cash that Trinity would have reinvested.
- *Withdrawals are taken at the start of each year*, which is the more demanding convention:
  the money leaves before that year's return is earned on it. The paper does not say which end
  it withdraws at — not in the methodology, and its one falsifiable datum does not pin it
  either: the single failing 15-year period it names (100% stocks, 5% constant-dollar, 1929)
  comes out identical under both conventions, because an 11-month shift barely registers over
  15 years of flat withdrawals. Over 30 CPI-indexed ones it compounds, and there the choice is
  worth more than every other difference here combined.
  Shifting the whole schedule 11 months later — same real amounts, since the CPI reset lands
  on the same month either way — moves this table from below Table 3 to above it: start-of-year
  lands under the published rate in 25 of 40 equity-holding cells and over it in 1 (mean -3.6
  points), end-of-year over in 21 and under in 5 (mean +1.8). The paper's own convention lies
  between the two.

  So the table's systematic pessimism is a convention and not an unexplained residual. It is
  NOT a claim that timing is the only difference — the equity index, the missing roll-down and
  the monthly window starts are all still in there, and pinning the timing exactly would leave
  some of them. Neither reading dominates: start-of-year is closer over 3-4% (mean absolute
  deviation 1.0 against 2.5), end-of-year over the full 3-12% range (2.9 against 3.7).
  Start-of-year is kept because it is the conservative reading and the one a retiree actually
  lives, not because it fits better.

Taxes and transaction costs are absent from both, which is the paper's own statement of its
method rather than a difference.

    bbr run //finance/augur/study/trinity:replay_bin
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import numpy as np

from finance.augur.model.bond_fund import BondFundSpec, YieldCurve
from finance.augur.model.equity import EquitySpec
from finance.augur.model.historical_windows import (
    MACRO_HISTORY_SOURCES,
    HistoricalWindowsModel,
    HistoricalWindowsProviderConfig,
)
from finance.augur.model.series import (
    InflationKey,
    LevelSeriesKey,
    SecurityDistributionKey,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.rust.simulator import ActionSession, Finished
from finance.augur.sim.backend import CompiledRun, compile_run
from finance.augur.sim.external_series import ExternalSeriesContext, materialize_sampled_exogenous
from finance.augur.sim.scenario import (
    Agent,
    DistributionTaxSlice,
    InitialAccountBalance,
    InitialLot,
    ObligationType,
    Scenario,
    ScheduledObligation,
    SecurityDistribution,
    SeriesIndexedAmount,
)
from finance.augur.study.trinity.evidence_snapshot import snapshot_evidence
from finance.augur.study.trinity.policy import fund_claims
from finance.augur.study.trinity.synthetic import synthetic_history

logger = logging.getLogger(__name__)

MONTHS_PER_YEAR = 12
PAYOUT_YEARS = 30
HORIZON_MONTHS = PAYOUT_YEARS * MONTHS_PER_YEAR

# The paper's sample: "1926 to 1995, inclusively". The record's own start is later (see the
# module docstring), so the lower bound is the paper's intent rather than a reachable month.
STUDY_FIRST_MONTH = date(1926, 1, 1)
STUDY_LAST_MONTH = date(1995, 12, 1)

RETIREE = "retiree"
WORLD = "world"
BROKERAGE = "brokerage"
CHECKING = "checking"

INITIAL_PORTFOLIO = Decimal(1_000_000)
EQUITY = SecuritySymbol("STOCKS")
BONDS = SecuritySymbol("BONDS")
# Arbitrary and equal: only the ratio of a sleeve's value to the portfolio matters, and every
# window is rebased to a common start anyway.
UNIT_PRICE = Decimal(100)

# The paper's bond leg: long-term high-grade corporates. Moody's Aaa is that series, so the
# yield is read rather than constructed.
#
# 20 years is chosen against the asset class, NOT against the table below — fitting a maturity
# to the result being reproduced would make the reproduction circular. Ibbotson report ~5.7%/yr
# at ~8.5%/yr standard deviation for long-term corporates over 1926-1995; this fund on Moody's
# 20 because it is the paper's own number, not a fitted one. Ibbotson & Sinquefield document
# the series Trinity used (SBBI 1926-1987, "Description of the Basic Series"): monthly returns
# for 1926-68 "calculated from yields assuming (at the beginning of each monthly holding
# period) a 20-year maturity, a bond price equal to par, and a coupon equal to the yield",
# income one-twelfth of the coupon. That is `constant_maturity_fund_paths` at maturity 20,
# clause for clause. From 1969 it splices in the Salomon Long-Term High-Grade Corporate Bond
# Index, whose maturity is a question rather than a definition.
#
# Both blocks were checked against that series' published annual returns (SBBI Exhibit A-3).
# Fitting maturity to 1946-68 recovers exactly 20, which is what makes the method credible on
# the block where the answer is not documented; there, 1969-85, the fit lands at 20-25 and
# rejects anything short — the call and sinking-fund provisions of the era do not show up as a
# shorter effective maturity. At 20 the sleeve runs 0.2-0.4 points per year under SBBI
# compounded (4.99% against 5.17% over 1927-85) at a standard deviation of 7.9% against 8.8%.
#
# Do not shorten it to close the all-bond row. Maturity 10 lands that row on Table 3 almost
# exactly, and is wrong: the same 10 misses the actual return series by half again as much as
# 20 does, in both eras.
BOND_MATURITY_YEARS = 20.0

EQUITY_SPEC = EquitySpec(symbol=EQUITY, initial_price_usd=float(UNIT_PRICE))
BOND_SPEC = BondFundSpec(
    symbol=BONDS,
    maturity_years=BOND_MATURITY_YEARS,
    initial_price_usd=float(UNIT_PRICE),
    yield_curve=YieldCurve.CORPORATE_AAA,
)

EVIDENCE = MACRO_HISTORY_SOURCES
"""The record `load_macro_history` assembles; the study replays all of it."""

PUBLISHED_RATES = tuple(round(0.01 * percent, 2) for percent in range(3, 13))
"""The withdrawal rates Table 3 tabulates: 3% through 12%."""

TABLE_3_SUCCESS_PERCENT: dict[float, tuple[int, ...]] = {
    1.00: (100, 95, 85, 68, 59, 41, 34, 34, 27, 15),
    0.75: (100, 98, 83, 68, 49, 34, 22, 7, 2, 0),
    0.50: (100, 95, 76, 51, 17, 5, 0, 0, 0, 0),
    0.25: (100, 71, 27, 20, 5, 0, 0, 0, 0, 0),
    0.00: (80, 20, 17, 12, 0, 0, 0, 0, 0, 0),
}
"""Table 3's 30-year row per equity share, over `PUBLISHED_RATES`.

Transcribed from the paper's own PDF, whose table cells are single glyphs positioned by
kerning; the column boundaries are unambiguous there even though a naive text extraction runs
the digits together. These are the external contract this module is measured against, so they
are literals and not something derived.
"""

PAPER_COMPOUND_RETURN_PERCENT = {EQUITY: 10.5, BONDS: 5.7}
"""The paper's own 1926-1995 compound annual returns for large-company common stocks and
long-term corporate bonds, quoted in its opening section.

An anchor on the INPUTS: the success table can only be reproduced if the two return series
going in resemble the paper's, and a sleeve that is silently mispriced would otherwise show up
only as an unattributable disagreement in the output.
"""


def sleeve_targets(equity_share: float) -> dict[tuple[str, str], int]:
    """Study sales weights: whole percentage points, with absent sleeves excluded."""
    if not 0 <= equity_share <= 1:
        raise ValueError("equity_share must be finite and in [0, 1]")
    equity_points = round(equity_share * 100)
    return {
        (BROKERAGE, str(symbol)): points
        for symbol, points in ((EQUITY, equity_points), (BONDS, 100 - equity_points))
        if points > 0
    }


def build_scenario(*, equity_share: float, withdrawal_rate: float) -> Scenario:
    """One Trinity cell: `equity_share` of a $1M portfolio, drawn down at `withdrawal_rate`.

    The withdrawal is `withdrawal_rate` of the INITIAL portfolio, taken at the start of each
    of the 30 years and indexed to CPI thereafter — the paper's inflation-adjusted Table 3
    rather than its constant-dollar Table 1.

    Fixed indexed withdrawals are genuine scheduled claims; their funding is chosen by
    the Python policy. Exact exhaustion after the final paid withdrawal is a success.
    """

    annual_withdrawal = INITIAL_PORTFOLIO * Decimal(str(withdrawal_rate))
    sleeve_shares = ((EQUITY, equity_share), (BONDS, 1.0 - equity_share))
    holds_bonds = equity_share < 1.0
    if not 0 <= equity_share <= 1:
        raise ValueError("equity_share must be finite and in [0, 1]")

    return Scenario(
        agents=[Agent(agent_id=RETIREE), Agent(agent_id=WORLD)],
        initial_cash=[
            InitialAccountBalance(agent_id=RETIREE, account_id=CHECKING, balance=0),
            InitialAccountBalance(agent_id=WORLD, account_id=CHECKING, balance=0),
        ],
        initial_lots=[
            InitialLot(
                lot_id=f"{symbol}_initial",
                agent_id=RETIREE,
                account_id=BROKERAGE,
                asset=SecurityKey(symbol=symbol),
                purchase_month_index=-1,
                quantity=float(INITIAL_PORTFOLIO * Decimal(str(share)) / UNIT_PRICE),
                cost_basis_per_unit=UNIT_PRICE,
            )
            for symbol, share in sleeve_shares
            if share > 0.0
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=year * MONTHS_PER_YEAR,
                obligation_id=f"withdrawal_year_{year}",
                obligation_type=ObligationType.CASH_SPEND,
                agent_id=RETIREE,
                from_account_id=CHECKING,
                to_agent_id=WORLD,
                to_account_id=CHECKING,
                amount_due=SeriesIndexedAmount(
                    base_amount=annual_withdrawal, series=InflationKey(), adjustment_period_months=MONTHS_PER_YEAR
                ),
            )
            for year in range(PAYOUT_YEARS)
        ],
        # The all-stock cell excludes the bond product, including its payout declaration.
        security_distributions=[
            SecurityDistribution(
                asset=SecurityKey(symbol=BONDS),
                agent_id=RETIREE,
                holding_account_id=BROKERAGE,
                to_account_id=CHECKING,
                # The scenario carries no tax profile, so the character is inert here; it is
                # required because a payout that allocates less than all of itself would pay
                # out less than the fund distributes.
                tax_character=(DistributionTaxSlice(fraction=1.0),),
            )
        ]
        if holds_bonds
        else [],
        tax_profiles=[],
        horizon_months=HORIZON_MONTHS,
    )


def execute(
    run: CompiledRun,
    *,
    targets: dict[tuple[str, str], int],
    rollout_ids: Sequence[int],
    capture: Literal["summary", "dense", "forensic"] = "summary",
) -> list[dict[str, Any]]:
    """Python owns the monthly batch loop; native execution owns all financial effects."""
    session = ActionSession(json.dumps(run.execution_input), RETIREE, list(rollout_ids), capture=capture)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(fund_claims(batch, targets=targets, cash_account_id=CHECKING))
        results: list[dict[str, Any]] = json.loads(batch.rollouts_json)
        return results
    finally:
        session.close()


@dataclass(frozen=True)
class Replay:
    """The study's exogenous paths, sampled once and reused across every cell.

    One sample for the whole table, because a cell differs from its neighbour only in the
    portfolio: re-sampling per cell would let the sample vary underneath the comparison.
    """

    external_series: ExternalSeriesContext
    window_starts: tuple[date, ...]
    record_start: date
    record_end: date
    # Read against `PAPER_COMPOUND_RETURN_PERCENT`. Held as the numbers rather than as a
    # second copy of the paths, which the context already carries in the simulator's shape.
    compound_return_percent: dict[SecuritySymbol, float]

    @property
    def window_count(self) -> int:
        return len(self.window_starts)

    def run(
        self,
        *,
        equity_share: float,
        withdrawal_rate: float,
        rollout_ids: Sequence[int] | None = None,
        capture: Literal["summary", "dense", "forensic"] = "summary",
    ) -> list[dict[str, Any]]:
        """Run a cell or selected original window IDs on the same supplied population."""
        scenario = build_scenario(equity_share=equity_share, withdrawal_rate=withdrawal_rate)
        run = compile_run(
            scenario,
            rollout_count=self.window_count,
            external_series=self.external_series,
            jurisdictions={},
            locations={},
        )
        return execute(
            run,
            targets=sleeve_targets(equity_share),
            rollout_ids=range(self.window_count) if rollout_ids is None else rollout_ids,
            capture=capture,
        )

    def success_rate(self, *, equity_share: float, withdrawal_rate: float) -> float:
        """Fraction of windows completing all scheduled withdrawals, including exact depletion."""
        results = self.run(equity_share=equity_share, withdrawal_rate=withdrawal_rate)
        return sum(row["stop"] is None for row in results) / len(results)

    def safemax(self, *, equity_share: float, grid: tuple[float, ...]) -> float | None:
        """Highest rate in `grid` that every window survived, or `None` if even the lowest fails.

        Bisects rather than scanning: raising the withdrawal takes strictly more out of the
        portfolio in every month of every window, so a rate that fails cannot be rescued by
        raising it further. The predicate is monotone and 31 candidates cost 5 simulations.
        """

        if self.success_rate(equity_share=equity_share, withdrawal_rate=grid[0]) < 1.0:
            return None
        low, high = 0, len(grid) - 1
        while low < high:
            middle = (low + high + 1) // 2
            if self.success_rate(equity_share=equity_share, withdrawal_rate=grid[middle]) == 1.0:
                low = middle
            else:
                high = middle - 1
        return grid[low]


def _whole_record_compound_returns(model: HistoricalWindowsModel) -> dict[SecuritySymbol, float]:
    """Each sleeve's compound annual TOTAL return over the ENTIRE record, in percent.

    The whole record and not a 30-year window, because that is the period the paper's own
    10.5% / 5.7% cover: long yields in the record's first half are less than half those of its
    second, so window 0 alone understates a bond sleeve by nearly two points and would make
    this anchor a comparison between different decades.

    Total, not price: a bond fund's price omits the coupon, which over such a span is most of
    its return. Units compound by `distribution / price`, so the index is units times price.

    One window spanning everything is how the sampler is asked for it — a horizon one month
    short of the record leaves exactly one start month, so this is the same replay path the
    study runs on rather than a second way of reading the history.
    """

    horizon = len(model.history.months) - 1
    bundle = model.materialize(window_starts=(model.history.months[0],), horizon_months=horizon)

    def matrix(key: LevelSeriesKey) -> np.ndarray:
        return bundle.level_matrix(key, rollout_count=1, horizon_months=horizon)

    returns: dict[SecuritySymbol, float] = {}
    for symbol in PAPER_COMPOUND_RETURN_PERCENT:
        index = matrix(SecurityKey(symbol=symbol))[0]
        if symbol == BONDS:
            index = index * np.cumprod(1.0 + matrix(SecurityDistributionKey(symbol=symbol))[0] / index)
        returns[symbol] = float(100.0 * ((index[-1] / index[0]) ** (MONTHS_PER_YEAR / horizon) - 1.0))
    return returns


def sample_replay(evidence_dir: Path) -> Replay:
    """Sample every 30-year window the study period supplies, from an evidence checkout."""

    model = HistoricalWindowsProviderConfig(
        evidence_dir=evidence_dir,
        record_start=STUDY_FIRST_MONTH,
        record_end=STUDY_LAST_MONTH,
        equity=EQUITY_SPEC,
        instruments=(BOND_SPEC,),
    ).realize_model()
    return replay_model(model)


def replay_model(model: HistoricalWindowsModel) -> Replay:
    """Materialize every eligible window once, retaining its start-date identity."""
    window_starts = model.window_starts(HORIZON_MONTHS)
    bundle = model.materialize(window_starts=window_starts, horizon_months=HORIZON_MONTHS)
    return Replay(
        external_series=materialize_sampled_exogenous(bundle),
        window_starts=window_starts,
        record_start=model.history.months[0],
        record_end=model.history.months[-1],
        compound_return_percent=_whole_record_compound_returns(model),
    )


SAFEMAX_GRID = tuple(round(0.025 + 0.001 * step, 3) for step in range(31))
"""2.5% to 5.5% in tenths of a point — finer than Table 3's whole points, which resolve
SAFEMAX no better than "somewhere in [3%, 4%)" for every allocation."""


def main() -> None:
    parser = argparse.ArgumentParser(description="Trinity-style historical replay through Python batch actions")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--evidence-dir", type=Path)
    source.add_argument("--synthetic", action="store_true", help="Generated placeholder history, not paper evidence")
    parser.add_argument("--equity-share", type=float)
    parser.add_argument("--withdrawal-rate", type=float)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--trace-rollout", type=int, action="append", default=[])
    args = parser.parse_args()
    cell = args.equity_share is not None or args.withdrawal_rate is not None
    if cell and (args.equity_share is None or args.withdrawal_rate is None or args.output_dir is None):
        parser.error("a selected cell requires --equity-share, --withdrawal-rate and --output-dir")
    if not cell and (args.synthetic or args.output_dir is not None or args.trace_rollout):
        parser.error("synthetic history and output/trace options require a selected cell")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.synthetic:
        replay = replay_model(
            HistoricalWindowsModel(
                history=synthetic_history(HORIZON_MONTHS), equity=EQUITY_SPEC, instruments=(BOND_SPEC,)
            )
        )
    elif args.evidence_dir is not None:
        replay = sample_replay(args.evidence_dir)
    else:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            asyncio.run(snapshot_evidence(directory, EVIDENCE))
            replay = sample_replay(directory)
    if cell:
        outcomes = replay.run(equity_share=args.equity_share, withdrawal_rate=args.withdrawal_rate)
        args.output_dir.mkdir(parents=True, exist_ok=False)
        (args.output_dir / "outcomes.json").write_text(json.dumps(outcomes))
        (args.output_dir / "study.json").write_text(
            json.dumps(
                {
                    "source": "synthetic placeholder history" if args.synthetic else "historical evidence",
                    "window_starts": [start.isoformat() for start in replay.window_starts],
                    "equity_share": args.equity_share,
                    "withdrawal_rate": args.withdrawal_rate,
                    "success_rate": sum(row["stop"] is None for row in outcomes) / len(outcomes),
                }
            )
        )
        if args.trace_rollout:
            traces = replay.run(
                equity_share=args.equity_share,
                withdrawal_rate=args.withdrawal_rate,
                rollout_ids=args.trace_rollout,
                capture="forensic",
            )
            (args.output_dir / "traces.json").write_text(json.dumps(traces))
        print(f"Saved {len(outcomes)} overlapping windows to {args.output_dir}; not independent probability samples.")
        return
    print_table(replay)


def print_table(replay: Replay) -> None:
    """Compare the sourced record against published cells without changing either input."""
    print(
        f"\nrecord {replay.record_start}..{replay.record_end}, "
        f"{replay.window_count} overlapping {PAYOUT_YEARS}-year windows"
    )
    print("compound annual total return over the whole record (paper's 1926-1995 figure):")
    for symbol, paper in PAPER_COMPOUND_RETURN_PERCENT.items():
        print(f"  {symbol:>8}: {replay.compound_return_percent[symbol]:5.1f}%   (paper {paper:.1f}%)")

    print(f"\nsuccess rate, augur vs Table 3, {PAYOUT_YEARS}-year payout")
    print("  equity  " + "  ".join(f"{rate:>9.0%}" for rate in PUBLISHED_RATES))
    for equity_share, published in TABLE_3_SUCCESS_PERCENT.items():
        cells = [
            f"{100 * replay.success_rate(equity_share=equity_share, withdrawal_rate=rate):3.0f}/{paper:<3d}"
            for rate, paper in zip(PUBLISHED_RATES, published, strict=True)
        ]
        print(f"  {equity_share:>5.0%}   " + "  ".join(f"{cell:>9}" for cell in cells))

    print("\nSAFEMAX — highest rate every window survived")
    for equity_share in TABLE_3_SUCCESS_PERCENT:
        safe = replay.safemax(equity_share=equity_share, grid=SAFEMAX_GRID)
        print(f"  {equity_share:>5.0%}   " + (f"{safe:.1%}" if safe is not None else f"below {SAFEMAX_GRID[0]:.1%}"))


if __name__ == "__main__":
    main()

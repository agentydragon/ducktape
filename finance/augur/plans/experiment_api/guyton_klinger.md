# Guyton–Klinger: spending rules and purchasing power

Proposed Python. Source: [Guyton and Klinger (2006), methods and Tables 3–5](https://www.financialplanningassociation.org/sites/default/files/2021-11/2006%20-%20Guyton%20and%20Klinger%20-%20Decision%20Rules%20and%20SWR%20%281%29.PDF).
The paper uses 14,000 trials per cell, annual correlated lognormal returns and
inflation, and historical estimates from two periods. Withdrawals precede annual
returns. This first program uses the single-equity version and 40-year horizon;
the six-equity-class version would supply a different universe and weights.

The question is how rule combinations alter success, spending cuts/raises, and
real consumption. The paper conditions purchasing-power statistics on successful
paths; that conditioning is explicit below.

```python
from itertools import product
from datetime import date

import polars as pl

from proposed_augur.instruments import TotalReturnIndex
from proposed_augur.data import History
from proposed_augur.markets import AnnualJointLognormal
from proposed_augur.money import USD
from proposed_augur.markets import AnnualConvention, annual_study_grid
from proposed_augur.simulation import run as run_paths
from proposed_augur.state import Situation
from collections.abc import Callable
from dataclasses import dataclass

from proposed_augur.policies import Initialize
from proposed_augur.proposals import Portfolio
from proposed_augur.accounting import AccountRef
from proposed_augur.instruments import Instrument, Weights
from proposed_augur.money import Money
from proposed_augur.taxes import NoTax
from proposed_augur.accounting import Actor
from proposed_augur.money import PriceIndex, ReportingBasis
from proposed_augur.results import Runs, StudyResult, financial_observers


@dataclass(frozen=True)
class GuytonSettings:
    target: Weights
    equities: tuple[Instrument, ...]
    fixed_income: tuple[Instrument, ...]
    reserve: Instrument
    initial_withdrawal: Money
    price_index: PriceIndex
    convention: AnnualConvention
    rule_order: str
    portfolio_convention: str
    freeze: str = "negative_return_and_rate_above_initial"
    inflation_cap: float | None = None
    preserve_above_initial_ratio: float = 1.20
    preservation_cut: float = 0.10
    preservation_inactive_final_years: int = 15
    prosper_below_initial_ratio: float | None = None
    prosperity_raise: float = 0.10


# Author-owned algorithm, supplied after resolving the paper interpretations.
# The decision log is separate from actual execution receipts.
type MakeGuytonPolicy = Callable[
    [GuytonSettings, Portfolio, dict[tuple[str, int], dict[str, str | int | bool]]], Initialize
]


def guyton_klinger(
    histories: dict[str, History],
    *,
    make_policy: MakeGuytonPolicy,
    rule_order: str,
    portfolio_convention: str,
    stock_shares: tuple[float, ...] = (0.50, 0.65, 0.80),
    rate_percents: tuple[float, ...] = tuple(x / 10 for x in range(30, 81)),
) -> StudyResult:
    convention = AnnualConvention("withdraw-return-manage", 0, 11, 11)
    stocks = TotalReturnIndex("sp500", currency="USD")
    bonds = TotalReturnIndex("paper_fixed_income", currency="USD")
    bills = TotalReturnIndex("paper_cash", currency="USD")
    capital = USD("1000000")
    actor = Actor("investor")
    rows: list[pl.DataFrame] = []
    runs: Runs = {}

    for fit_period, history in histories.items():
        market = AnnualJointLognormal.fit(
            history,
            bindings={stocks: "equity", bonds: "fixed_income", bills: "cash"},
            price_index=PriceIndex("us_cpi"), inflation_column="cpi",
            moment_space="arithmetic_gross_returns",
        )
        forecast = market.condition({}, at=date(2000, 1, 1))  # Synthetic no-tax calendar.
        worlds = annual_study_grid(
            forecast.sample(years=40, step="year", paths=14000, seed=2006), convention=convention,
        )
        for stock_share, rate_percent, prosperity in product(
            stock_shares, rate_percents, (False, True)
        ):
            target = {stocks: stock_share, bonds: 1 - stock_share}
            initial = capital * rate_percent / 100
            situation = Situation.investor(
                actor=actor, basis=ReportingBasis("USD", market.price_index, worlds.calendar.start),
                capital=capital, weights=target, taxes=NoTax(), calendar=worlds.calendar
            )
            settings = GuytonSettings(
                target=target,
                equities=(stocks,),
                fixed_income=(bonds,),
                reserve=bills,
                initial_withdrawal=initial,
                price_index=market.price_index,
                freeze="negative_return_and_rate_above_initial",
                inflation_cap=None,
                preserve_above_initial_ratio=1.20,
                preservation_cut=0.10,
                preservation_inactive_final_years=15,
                prosper_below_initial_ratio=0.80 if prosperity else None,
                prosperity_raise=0.10,
                rule_order=rule_order,
                portfolio_convention=portfolio_convention,
                convention=convention,
            )
            decision_log: dict[tuple[str, int], dict[str, str | int | bool]] = {}
            portfolio = Portfolio(AccountRef(actor, "portfolio"), {
                instrument: AccountRef(actor, "portfolio") for instrument in (stocks, bonds, bills)
            })
            initialize = make_policy(settings, portfolio, decision_log)
            run = run_paths(
                situation,
                policies={actor: initialize}, reporting_actor=actor,
                worlds=worlds,
                observers=financial_observers(
                    "terminal_wealth_nominal", "total_spending_real",
                    "final_spending_real",
                ),
            )
            # One log row at each annual spending decision, keyed by original path.
            # Cut/freeze labels describe decisions, not assumed successful payments.
            counts = pl.DataFrame(
                list(decision_log.values()),
                schema={"path_id": pl.String, "year": pl.Int64,
                        "cuts": pl.Boolean, "raises": pl.Boolean, "freezes": pl.Boolean},
            ).group_by("path_id").agg(
                pl.col("cuts").sum(), pl.col("raises").sum(), pl.col("freezes").sum(),
            )
            paths = run.paths.join(counts, on="path_id", how="left").with_columns(
                pl.col("cuts", "raises", "freezes").fill_null(0),
            ).with_columns(
                success=(
                    pl.col("reached_horizon")
                    & ~pl.col("unfunded_withdrawal")
                    & (pl.col("terminal_wealth_nominal") >= 1).fill_null(False)
                )
            )
            success = pl.col("success")
            row = paths.select(
                success.mean().alias("success_fraction"),
                (success.cast(pl.Float64).std() / pl.len().sqrt()).alias("success_se"),
                pl.col("cuts").mean().alias("mean_cuts_all_paths"),
                pl.col("raises").mean().alias("mean_raises_all_paths"),
                pl.col("freezes").mean().alias("mean_freezes_all_paths"),
                (pl.col("total_spending_real").filter(success).median()
                 / (initial.to_number() * 40)).alias("median_total_pp_successful"),
                (pl.col("final_spending_real").filter(success).median()
                 / initial.to_number()).alias("median_final_pp_successful"),
            ).with_columns(
                fit_period=pl.lit(fit_period), stock_share=pl.lit(stock_share),
                rate_percent=pl.lit(rate_percent), prosperity=pl.lit(prosperity),
            )
            key = (fit_period, stock_share, rate_percent, prosperity)
            rows.append(row)
            runs[key] = run

    return pl.concat(rows), runs
```

`histories` supplies aligned equity, fixed-income, cash, and CPI observations for
1973–2004 and 1928–2004, with benchmark identities and evidence provenance.
`AnnualJointLognormal.fit` converts arithmetic moments into a valid joint
distribution of gross returns and inflation factors; it reports an incompatible
covariance instead of silently changing it. Its IID-year assumption is part of
this reproduction, not an endorsement for personal planning.

The supplied `make_policy` is experiment-owned Python, not an Augur policy enum
or a claim that this document implements the paper. It returns one ordinary
monthly function plus fresh actor/path-local memory. That function emits explicit
sales, consumption and reserve/reinvestment purchases in its chosen order, using
canonical preview/accounting. It never submits targets for an engine allocator.

The function writes each annual decision to `decision_log[path_id, year]`,
replacing that row during replay. This output-only sink is never read as policy
memory; reordering or replay cannot change decisions or double-count reviews.
Decision labels are not evidence of successful payments.

The portfolio algorithm raises reserves from eligible overweight assets and
follows the paper's funding order, not ordinary target rebalancing with two
spending knobs. Annual reviews retain prior withdrawal, initial rate, observed
portfolio/per-asset returns, current weights and years remaining. Skipped inflation
is not caught up; raised spending can exceed its initial real level. Other months
return empty actions. The synthetic grid changes index/CPI levels at month 11;
spending occurs at month 0. A different annual ordering requires an explicit
grid and authored-function change, not an executor strategy.

## Reproduction boundaries to resolve

The source gives triggers and adjustments but leaves enough interaction detail
that `rule_order` is required. It must name a documented interpretation of the
inflation adjustment, trigger-rate calculation, and capital/prosperity changes.
Compare interpretations on hand-checkable paths before selecting one. Likewise,
the exact benchmark record needs a sourced identity. `portfolio_convention`
specifies initial cash and excess-reserve reinvestment. Here it must agree with
the zero initial cash in the opening book, or the experiment must change that book
explicitly. It must not inherit an unrelated Augur cash-band default. Alternate
interpretations can be labeled variants, without blocking the capability example
on exact replication of the original numeric table.

The example's `NoTax` and zero transaction cost are declared reproduction
assumptions: the paper does not specify a tax-lot/payment implementation. Rule
counts above are all-path statistics; whether the published counts have that
denominator still needs verification. Keep raw counts to calculate either.
Compare the published rounded cells with Monte Carlo intervals, not exact equality.
The chosen rate grid is an experiment choice, not a transcription of a paper table.

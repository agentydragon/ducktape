# Allocation is a function, not necessarily a fixed weight

Proposed Python, inspired by [Pfau and Kitces (2014)](https://www.financialplanningassociation.org/article/journal/JAN14-reducing-retirement-risk-rising-equity-glide-path)
and [Blanchett (2015)](https://www.financialplanningassociation.org/article/journal/SEP15-initial-conditions-and-optimal-retirement-glide-paths).
The former compares changing equity allocations under several return assumptions;
the latter studies how starting yields and valuations change the answer. Together
they motivate crossing allocation functions with exogenous models, rather than
installing "rising allocation is best" as a library rule.

This shell uses supplied fitted models, not the papers' original calibrations.
Its grid and endpoint convention are explicit choices. It holds spending fixed
in real terms so the effect of changing allocation remains visible.

```python
from itertools import product
from pathlib import Path
from datetime import date

import polars as pl

from proposed_augur.markets import load_vecm
from proposed_augur.money import USD
from proposed_augur.markets import AnnualConvention, annual_study_grid
from proposed_augur.simulation import run as run_paths
from proposed_augur.state import Situation
from proposed_augur.proposals import Portfolio
from proposed_augur.accounting import AccountRef
from study_helpers import annual_policy
from proposed_augur.taxes import NoTax
from proposed_augur.accounting import Actor
from proposed_augur.data import NamedSeries
from proposed_augur.instruments import Instrument, InvestableUniverse, Weights
from proposed_augur.markets import MarketBinding
from proposed_augur.money import RealAmount, ReportingBasis
from collections.abc import Callable
from proposed_augur.results import Runs, StudyResult, financial_observers


def linear_allocation(stocks: Instrument, bonds: Instrument, start_share: float, end_share: float, ramp_years: int) -> Callable[[int], Weights]:
    def target(completed_years: int) -> Weights:
        elapsed = min(1.0, max(0.0, completed_years / ramp_years))
        share = start_share + elapsed * (end_share - start_share)
        return {stocks: share, bonds: 1 - share}
    return target


def glide_paths(
    artifacts: dict[str, Path], bindings: dict[str, MarketBinding],
    observations: NamedSeries, universe: InvestableUniverse, *, start: date,
) -> StudyResult:
    convention = AnnualConvention("withdraw-return-rebalance", 0, 11, 11)
    stocks = universe.instrument("equity")
    bonds = universe.instrument("bonds")
    capital = USD("1000000")
    actor = Actor("investor")
    rows: list[pl.DataFrame] = []
    runs: Runs = {}
    for model_name, artifact in artifacts.items():
        market = load_vecm(artifact).bind(bindings[model_name]).condition(observations, at=start)
        worlds = annual_study_grid(
            market.sample(years=40, step="year", paths=10000, seed=2014), convention=convention,
        )
        for years, initial_share, final_share, rate in product(
            (20, 30, 40), (0.3, 0.6, 0.8), (0.3, 0.6, 0.8), (0.04, 0.05)
        ):
            paths = worlds.prefix(years=years)
            initial = capital * rate
            situation = Situation.investor(
                actor=actor, basis=ReportingBasis("USD", market.price_index, start),
                capital=capital, weights={stocks: initial_share, bonds: 1 - initial_share},
                taxes=NoTax(), calendar=paths.calendar,
            )
            portfolio = Portfolio(AccountRef(actor, "portfolio"), {
                stocks: AccountRef(actor, "portfolio"), bonds: AccountRef(actor, "portfolio"),
            })
            initialize = annual_policy(
                RealAmount.at_base(initial, situation.basis), portfolio, convention,
                budget=lambda obs, previous: previous,
                target=linear_allocation(stocks, bonds, initial_share, final_share, years - 1),
            )
            run = run_paths(
                situation, policies={actor: initialize}, reporting_actor=actor, worlds=paths,
                observers=financial_observers("terminal_wealth_real", "total_spending_real"),
            )
            outcomes = run.paths.with_columns(
                failed=(
                    ~pl.col("reached_horizon") | pl.col("unfunded_withdrawal")
                    | (pl.col("terminal_wealth_real") <= 0).fill_null(True)
                ),
                legacy_or_unpaid=pl.when(pl.col("reached_horizon"))
                .then(pl.col("terminal_wealth_real"))
                .otherwise(-(initial.to_number() * years - pl.col("total_spending_real"))),
            )
            rows.append(outcomes.select(
                pl.col("failed").mean().alias("failure_fraction"),
                (pl.col("failed").cast(pl.Float64).std() / pl.len().sqrt()).alias("failure_se"),
                pl.col("legacy_or_unpaid").quantile(0.05).alias("p05_legacy_or_unpaid"),
                pl.col("terminal_wealth_real").filter(pl.col("reached_horizon"))
                .median().alias("median_terminal_completed"),
            ).with_columns(
                model=pl.lit(model_name), years=pl.lit(years), rate=pl.lit(rate),
                initial_share=pl.lit(initial_share), final_share=pl.lit(final_share),
            ))
            runs[model_name, years, initial_share, final_share, rate] = run
    return pl.concat(rows), runs
```

At annual rebalancing, the ordinary target function receives completed years,
starting at 1. The shared helper proposes explicit trades, not a weight command. The opening book has
the initial weight; the last spending year's target has the final weight. There
is no pointless terminal rebalance. Other timing conventions can be separate cells.
Replacing `target` with a valuation-sensitive function should not change the engine.

The negative part of `legacy_or_unpaid` is **unmet planned consumption**, not a
fictional loan allowing a depleted portfolio to keep trading. This severity
measure draws on Pfau–Kitces; the conditional median is deliberately labeled
and is not their unconditional median. Because this example has a fixed real
budget and no other income, unpaid scheduled consumption is calculable after a
stop. That shortcut is not valid for arbitrary future adaptive spending.

All cells here exclude taxes and explicit costs. Annual returns are placed at
synthetic month 11 opening, withdrawals at month 0, and rebalancing at month 11;
intervening calls are empty, not monthly spending. Reuse the allocation function
in the personal experiment to measure turnover, tax realization, and actual
after-tax consumption; a no-tax total-return study does not answer those questions.

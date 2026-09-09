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

import numpy as np
import polars as pl

from augur.markets.vecm import load_vecm
from augur.money import USD
from augur.simulation import AnnualConvention, Situation, simulate
from augur.strategies import AnnualRebalance, FixedWithdrawal, Strategy
from augur.taxes import NoTax


def linear_allocation(stocks, bonds, start_share, end_share, ramp_years):
    def target(obs):
        elapsed = np.clip(obs.next_period_index / ramp_years, 0, 1)
        share = start_share + elapsed * (end_share - start_share)
        return {stocks: share, bonds: 1 - share}
    return target


def glide_paths(artifacts, universe, *, start):
    stocks = universe.instrument("equity")
    bonds = universe.instrument("bonds")
    capital = USD("1000000")
    rows, runs = [], {}
    for model_name, artifact in artifacts.items():
        market = load_vecm(artifact).bind(universe)
        worlds = market.sample(start=start, years=40, step="year", paths=10000, seed=2014)
        for years, initial_share, final_share, rate in product(
            (20, 30, 40), (0.3, 0.6, 0.8), (0.3, 0.6, 0.8), (0.04, 0.05)
        ):
            paths = worlds.prefix(years=years)
            initial = capital * rate
            situation = Situation.investor(
                capital=capital, weights={stocks: initial_share, bonds: 1 - initial_share},
                taxes=NoTax(), calendar=paths.calendar,
            )
            strategy = Strategy(
                spending=FixedWithdrawal(initial=initial, index=market.price_index, interval="year"),
                trading=AnnualRebalance(
                    target=linear_allocation(stocks, bonds, initial_share, final_share, years - 1),
                    transaction_cost=0,
                ),
            )
            run = simulate(
                situation, strategy, worlds=paths,
                convention=AnnualConvention.withdraw_then_return_then_rebalance(),
                on_shortfall="stop", observe=("terminal_wealth_real", "total_spending_real"),
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

At end-of-year rebalancing, `next_period_index` starts at 1. The opening book has
the initial weight; the last spending year's target has the final weight. There
is no pointless terminal rebalance. Other timing conventions can be separate cells.
Replacing `target` with a valuation-sensitive function should not change the engine.

The negative part of `legacy_or_unpaid` is **unmet planned consumption**, not a
fictional loan allowing a depleted portfolio to keep trading. This severity
measure draws on Pfau–Kitces; the conditional median is deliberately labeled
and is not their unconditional median. Because this example has a fixed real
budget and no other income, unpaid scheduled consumption is calculable after a
stop. That shortcut is not valid for arbitrary future adaptive spending.

All cells here exclude taxes and explicit costs. Reuse the allocation function
in the personal experiment to measure turnover, tax realization, and actual
after-tax consumption; a no-tax total-return study does not answer those questions.

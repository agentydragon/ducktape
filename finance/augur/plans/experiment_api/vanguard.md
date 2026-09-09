# Vanguard-style bounded changes in real spending

Proposed Python. Target: Figure 6 of [Vanguard, March 2021, Sustainable spending
rates in turbulent markets](https://www.vanguard.co.uk/content/dam/intl/europe/documents/en/whitepapers/sustainable-spending-rates-in-turbulent-markets-uk-en-pro.pdf).
It compares fixed real spending with dynamic spending across allocations and
horizons. Its ceiling and floor limit the annual change in real spending; they
are not permanent limits relative to the initial budget. Figure 6's footnote
specifies gross withdrawals, an 85% success criterion, and portfolio weights.

Use our own joint forecast; reproducing proprietary Vanguard paths is not a goal.
This shell loads a fitted model, samples paths, and varies a spending function.
The policy comparison is useful without matching the paper's numerical table.

```python
from itertools import product
from pathlib import Path

import numpy as np
import polars as pl

from augur.instruments import InvestableUniverse
from augur.markets.vecm import load_vecm
from augur.money import GBP
from augur.simulation import AnnualConvention, Situation, simulate
from augur.strategies import AnnualRebalance, AnnualSpending, Strategy
from augur.taxes import NoTax


def bounded_spending(initial, fraction, down, up, price_index):
    initial_real = initial.to_number()

    def review(obs, previous_real):
        target = obs.wealth_real * fraction
        amount = np.clip(target, previous_real * (1 - down), previous_real * (1 + up))
        amount = np.where(obs.review_index == 0, initial_real, amount)
        return amount, amount

    return AnnualSpending(
        review=review, initial_state=initial_real, price_index=price_index
    )


def vanguard(artifact: Path, universe: InvestableUniverse, *, start, convention: AnnualConvention):
    market = load_vecm(artifact).bind(universe)
    worlds = market.sample(start=start, years=30, step="year", paths=10000, seed=2021)
    uk_stocks = universe.instrument("uk_equity")
    other_stocks = universe.instrument("international_equity")
    uk_bonds = universe.instrument("uk_fixed_income")
    other_bonds = universe.instrument("international_fixed_income")
    capital = GBP("1000000")
    rows, runs = [], {}

    for years, stock_share, dynamic, rate_percent in product(
        (10, 20, 30), (0.20, 0.50, 0.80), (False, True),
        tuple(x / 10 for x in range(10, 151)),
    ):
        weights = {
            uk_stocks: stock_share * 0.25,
            other_stocks: stock_share * 0.75,
            uk_bonds: (1 - stock_share) * 0.35,
            other_bonds: (1 - stock_share) * 0.65,
        }
        paths = worlds.prefix(years=years)
        situation = Situation.investor(
            capital=capital, weights=weights, taxes=NoTax(), calendar=paths.calendar
        )
        strategy = Strategy(
            spending=bounded_spending(
                initial=capital * rate_percent / 100,
                fraction=rate_percent / 100,
                down=0.025 if dynamic else 0.0,
                up=0.05 if dynamic else 0.0,
                price_index=worlds.price_index,
            ),
            trading=AnnualRebalance(target=weights, transaction_cost=0),
        )
        run = simulate(
            situation, strategy, worlds=paths, convention=convention,
            on_shortfall="stop",
            observe=("terminal_wealth_nominal", "total_spending_real", "final_spending_real"),
        )
        outcomes = run.paths.with_columns(
            success=(
                    pl.col("reached_horizon")
                    & ~pl.col("unfunded_withdrawal")
                    & (pl.col("terminal_wealth_nominal") > 0).fill_null(False)
            )
        )
        rows.append(
            outcomes.select(
                pl.col("success").mean().alias("success_fraction"),
                (pl.col("success").cast(pl.Float64).std() / pl.len().sqrt()).alias("success_se"),
                pl.col("total_spending_real").median().alias("median_paid_through_stop"),
            ).with_columns(
                years=pl.lit(years), stock_share=pl.lit(stock_share),
                dynamic=pl.lit(dynamic), rate_percent=pl.lit(rate_percent),
            )
        )
        runs[years, stock_share, dynamic, rate_percent] = run

    table = pl.concat(rows)
    grid_frontier = (
        table.filter(pl.col("success_fraction") >= 0.85)
        .group_by("years", "stock_share", "dynamic")
        .agg(pl.col("rate_percent").max().alias("largest_grid_rate"))
    )
    return table, grid_frontier, runs
```

The full table stays available: `largest_grid_rate` is a search result on a chosen
grid, not an exact maximum or a confidence bound. Missing qualifying groups mean
no rate in the grid qualified. Monte Carlo uncertainty around the boundary and
grid resolution must accompany any reported frontier.

## Policy and model substitutions

The function executes on the current batch, without inspecting future returns.
`previous_real` is path-local state initialized by broadcasting the opening budget;
the adapter converts the returned real budget to dated money using shared rounding.
With both limits zero this is fixed real spending. An unaffordable lower bound
produces a shortfall, not an extra automatic cut. The function can be replaced
without adding another engine policy variant.

The supplied universe uses gross-return study instruments. The artifact must
support their joint dynamics, UK inflation, and declared currency/hedging treatment.
It is our fitted VECM, not VCMM and not asserted to have equivalent calibration.
An incompatible artifact is an input gap, not an excuse to fabricate UK series.
Other providers from [the model shell](exogenous.md) can replace this loader.
Figure 6 has
different allocation notes from other figures in this paper; they must not be
mixed. Annual rebalancing, zero explicit fees, and the supplied withdrawal timing
are explicit choices here. Report these differences alongside the model's evidence
window and conditioning date; do not judge this experiment by equality to Figure 6.
The displayed standard error assumes independent outer draws; a zero estimate at
an all-success boundary is not a zero-width probability interval.

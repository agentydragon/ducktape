# Trinity: historical withdrawal and allocation sweep

Proposed Python, not executable against current Augur. Target: Cooley, Hubbard,
and Walz (1998), Tables 1 and 3. [Publisher's methods](https://www.aaii.com/journal/article/retirement-savings-choosing-a-withdrawal-rate-that-is-sustainable)
specify annual 1926–1995 records, S&P 500 and long-term high-grade corporate bonds,
four horizons, five allocations, and nominal versus CPI-adjusted withdrawals.
Taxes and transaction costs are excluded.

The caller supplies the corresponding return record and a sourced transcription
of the published cells. `convention` is required: the accessible article does not
unambiguously establish withdrawal timing. Run plausible conventions as labeled
variants until resolved. Missing original bond data is an input gap, not permission
to substitute a Treasury series under the same study label.

```python
from itertools import product
from datetime import date
from pathlib import Path

import polars as pl

from proposed_augur.instruments import TotalReturnIndex
from proposed_augur.data import History
from proposed_augur.markets import HistoricalMarket
from proposed_augur.money import USD
from proposed_augur.simulation import AnnualConvention, simulate
from proposed_augur.state import Situation
from proposed_augur.policies import AnnualRebalance, FixedWithdrawal, Strategy
from proposed_augur.taxes import NoTax
from proposed_augur.accounting import Actor
from proposed_augur.instruments import Weights
from proposed_augur.money import PriceIndex, ReportingBasis
from proposed_augur.results import Runs, StudyResult, financial_observers


def trinity(history_path: Path, published: pl.DataFrame, *, convention: AnnualConvention) -> StudyResult:
    history = History.load(history_path)
    stocks = TotalReturnIndex("sp500", currency="USD")
    bonds = TotalReturnIndex("long_high_grade_corporates", currency="USD")
    market = HistoricalMarket(
        history=history,
        bindings={stocks: "sp500_total_return", bonds: "corporate_total_return"},
        price_index=PriceIndex("us_cpi"), inflation_column="us_cpi",
        observation_period="year",
    )
    capital = USD("1000000")
    actor = Actor("investor")
    rows: list[pl.DataFrame] = []
    runs: Runs = {}

    for years in (15, 20, 25, 30):
        worlds = market.windows(
            first_year=1926, last_year=1995, years=years, stride_years=1,
            replay_start=date(2000, 1, 1),  # Common no-tax scenario clock; source dates remain metadata.
        )
        for stock_share, rate_percent, indexed in product(
            (0.0, 0.25, 0.50, 0.75, 1.0), range(3, 13), (False, True)
        ):
            weights: Weights = {stocks: stock_share, bonds: 1 - stock_share}
            situation = Situation.investor(
                actor=actor, basis=ReportingBasis("USD", market.price_index, worlds.calendar.start),
                capital=capital, weights=weights, taxes=NoTax(), calendar=worlds.calendar
            )
            strategy = Strategy(
                spending=FixedWithdrawal(
                    initial=capital * rate_percent / 100,
                    index=market.price_index if indexed else None,
                    interval="year",
                ),
                trading=AnnualRebalance(target=weights, transaction_cost=0),
            )
            run = simulate(
                situation,
                policies={actor: strategy}, reporting_actor=actor,
                worlds=worlds,
                convention=convention,
                on_shortfall="stop",
                observers=financial_observers("terminal_wealth_nominal",),
            )
            key = (years, stock_share, rate_percent, indexed)
            runs[key] = run
            rows.append(
                run.paths.select(
                    (
                        pl.col("reached_horizon")
                        & ~pl.col("unfunded_withdrawal")
                        & (pl.col("terminal_wealth_nominal") > 0).fill_null(False)
                    ).mean().alias("historical_success_fraction"),
                    pl.len().alias("overlapping_windows"),
                ).with_columns(
                    years=pl.lit(years),
                    stock_share=pl.lit(stock_share),
                    rate_percent=pl.lit(rate_percent),
                    indexed=pl.lit(indexed),
                    convention=pl.lit(convention.name),
                )
            )

    table = pl.concat(rows)
    comparison = table.join(
        published,
        on=["years", "stock_share", "rate_percent", "indexed"],
        how="left",
        validate="1:1",
    ).with_columns(
        comparison_available=pl.col("published_success_fraction").is_not_null(),
        difference_points=100 * (
            pl.col("historical_success_fraction") - pl.col("published_success_fraction")
        )
    )
    return comparison, runs
```

`published` has a success fraction and source locator for every requested cell;
missing matches must be reported as unavailable comparisons. `AnnualRebalance`
funds the withdrawal and restores target weights in the order named by the
convention. A zero-weight asset is absent, and there is no persistent idle-cash
buffer. All fractions describe the enumerated overlapping windows.

The existing [reproduction](../../study/trinity/replay.py) is evidence to consult
when implementing this program. This sketch intentionally asks for original
annual data and explicit conventions; it is not a claim that changing the API
will resolve that reproduction's discrepancies.

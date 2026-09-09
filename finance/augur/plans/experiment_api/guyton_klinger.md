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

import polars as pl

from augur.instruments import TotalReturnIndex
from augur.markets import AnnualJointLognormal, History
from augur.money import USD
from augur.simulation import AnnualConvention, Situation, simulate
from augur.strategies import GuytonPortfolioConvention, GuytonRuleOrder, guyton_klinger_policy
from augur.taxes import NoTax


def guyton_klinger(
    histories: dict[str, History],
    *,
    rule_order: GuytonRuleOrder,
    portfolio_convention: GuytonPortfolioConvention,
    stock_shares=(0.50, 0.65, 0.80),
    rate_percents=tuple(x / 10 for x in range(30, 81)),
):
    stocks = TotalReturnIndex("sp500", currency="USD")
    bonds = TotalReturnIndex("paper_fixed_income", currency="USD")
    bills = TotalReturnIndex("paper_cash", currency="USD")
    capital = USD("1000000")
    rows, runs = [], {}

    for fit_period, history in histories.items():
        market = AnnualJointLognormal.fit(
            history,
            bindings={stocks: "equity", bonds: "fixed_income", bills: "cash"},
            price_index="cpi",
            moment_space="arithmetic_gross_returns",
        )
        worlds = market.sample(years=40, paths=14000, seed=2006)
        for stock_share, rate_percent, prosperity in product(
            stock_shares, rate_percents, (False, True)
        ):
            target = {stocks: stock_share, bonds: 1 - stock_share}
            initial = capital * rate_percent / 100
            situation = Situation.investor(
                capital=capital, weights=target, taxes=NoTax(), calendar=worlds.calendar
            )
            strategy = guyton_klinger_policy(
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
                transaction_cost=0,
            )
            run = simulate(
                situation,
                strategy,
                worlds=worlds,
                convention=AnnualConvention.withdraw_then_return(),
                on_shortfall="stop",
                observe=(
                    "terminal_wealth_nominal", "total_spending_real",
                    "final_spending_real", "cuts", "raises", "freezes",
                ),
            )
            paths = run.paths.with_columns(
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

`guyton_klinger_policy` is a factory for a coordinated executable strategy, not an
engine enum. Its spending and portfolio callbacks can be replaced independently
or together; the factory is shorthand for a reusable implementation of this
particular study's algorithm. Its portfolio-management component
raises reserves from eligible overweight assets and follows the paper's funding
order. It is not the ordinary target-allocation policy with two spending knobs.
Annual decisions retain prior withdrawal, initial rate, prior portfolio return,
per-asset returns, current weights, and years remaining. Skipped inflation is not
subsequently caught up. Raised spending can exceed its initial real level.

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

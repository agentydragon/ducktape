# The experiment owns path loading and sampling

Proposed Python. An experiment assembles a joint external-world model as well as
the decision policies. This is not one mandatory provider hierarchy: loaders and
fit procedures can be specific to each model. Their output must agree with the
experiment's instruments, information dates, calendar, and reporting units.

The experiment names each dataset and selects the columns it consumes. A shared
loader may know a provider's format; it does not choose a global bundle of
"evidence". Here the author chooses two supplied total-return index records and
a particular CPI series. Another experiment can use unrelated sources or its own
loaders, then use the same alignment helper.

```python
from pathlib import Path

from augur.datasets import align_monthly, read_parquet_series
from augur.datasets.fred import read_fred_series
from augur.markets import HistoricalMarket, JointBlockBootstrap
from augur.markets.vecm import load_vecm


def load_study_series(sp500_path: Path, corporate_path: Path, cpi_snapshot: Path):
    return {
        "equity_tr_index": read_parquet_series(
            sp500_path, value="total_return_index", observed_at="month",
            released_at="published_at", revision_at="vintage_at",
        ),
        "corporate_tr_index": read_parquet_series(
            corporate_path, value="total_return_index", observed_at="month",
            released_at="published_at", revision_at="vintage_at",
        ),
        "cpi": read_fred_series("CPIAUCNS", snapshot=cpi_snapshot),
    }


def log_state(datasets, as_of):
    # Named, dated observations in exactly the variables this fitted model uses.
    return {
        "log_equity_tr": datasets["equity_tr_index"].available_by(as_of).log(),
        "log_corporate_tr": datasets["corporate_tr_index"].available_by(as_of).log(),
        "log_cpi": datasets["cpi"].available_by(as_of).log(),
    }


def study_worlds(
    datasets, artifact_path: Path, *, stocks, bonds, historical_as_of,
    history_start, history_end, first_year, last_year, start, years, paths, block_months,
):
    history = align_monthly(
        {name: series.available_by(historical_as_of) for name, series in datasets.items()},
        start=history_start, end=history_end, missing="raise",
    )
    bindings = {stocks: "equity_tr_index", bonds: "corporate_tr_index"}
    historical = HistoricalMarket.from_index_levels(
        history, bindings=bindings, price_index="cpi"
    )
    bootstrap = JointBlockBootstrap.from_index_levels(
        history, bindings=bindings, price_index="cpi",
        block_months=block_months, incomplete_blocks="exclude", circular=False,
    )
    conditional = load_vecm(artifact_path).bind(
        total_return_indices={stocks: "log_equity_tr", bonds: "log_corporate_tr"},
        price_index="log_cpi", encoding="log_levels",
    ).condition(log_state(datasets, start), at=start)
    return {
        "historical": historical.windows(
            first_year=first_year, last_year=last_year, years=years, stride_years=1
        ),
        "block_bootstrap": bootstrap.sample(
            start=start, years=years, step="month", paths=paths, seed=811
        ),
        "conditional_vecm": conditional.sample(
            start=start, years=years, step="month", paths=paths, seed=812
        ),
    }
```

`datasets` is an ordinary dictionary authored here, not an Augur-wide catalog.
Its three keys are this experiment's names. The supplied index files must identify
the S&P 500 and long-term high-grade corporate benchmarks, including construction
and units; a path or column name alone does not establish that identity. This
example selects CPIAUCNS explicitly, rather than asking a loader to choose a CPI.

`align_monthly` joins only the named series on observation month, over the requested
range. It checks duplicates, frequency and missing observations; it does not
invent a series, splice sources, resample daily values, or forward-fill. A caller
wanting those operations specifies them before alignment. Publication/revision
dates remain distinct from observation dates: vintage selection happens per
dataset before joining. Lack of historical vintages is a declared limitation,
not permission to invent publication dates.

The bootstrap resamples **the aligned vector** of asset returns and inflation,
not each column separately. It preserves dependence within a sampled block, not
arbitrary long-run dynamics. Vary block length as an experiment parameter. Missing
history, fund inception, and regime breaks need explicit handling; no zero-fill.
`from_index_levels` derives adjacent-period gross changes before resampling, not
blocks of unrelated index levels. Include the preceding month's index observations
to calculate the first requested month's changes.
Historical windows retain their actual dates; synthetic forecasts start at `start`.

This particular helper serves no-tax total-return studies. A taxed fund needs
prices, distributions, and tax character; an individual bond needs issuance terms,
coupon/redemption cashflows, and compatible valuation dynamics. Replaying a
total-return series does not supply those. A richer model can bind both studies
and real products, but cannot relabel missing cashflows into existence.

Fitting is also experiment code when the study varies its evidence window:

```python
import polars as pl

from augur.markets.vecm import fit_vecm


def fitted_worlds(
    datasets, *, stocks, bonds, fit_start, fit_end, fit_as_of, start, years, paths,
):
    levels = align_monthly(
        {name: series.available_by(fit_as_of) for name, series in datasets.items()},
        start=fit_start, end=fit_end, missing="raise",
    )
    state = levels.select(
        "month",
        pl.col("equity_tr_index").log().alias("log_equity_tr"),
        pl.col("corporate_tr_index").log().alias("log_corporate_tr"),
        pl.col("cpi").log().alias("log_cpi"),
    )
    fitted = fit_vecm(
        state, time="month",
        variables=("log_equity_tr", "log_corporate_tr", "log_cpi"),
        lagged_differences=2, cointegration_rank=1, deterministic="restricted_constant",
    )
    market = fitted.bind(
        total_return_indices={stocks: "log_equity_tr", bonds: "log_corporate_tr"},
        price_index="log_cpi", encoding="log_levels",
    ).condition(log_state(datasets, start), at=start)
    worlds = market.sample(start=start, years=years, step="month", paths=paths, seed=813)
    return fitted, worlds
```

The entire modeled state is visible: log equity total-return index, log corporate
bond total-return index, and log CPI. There is no implicit interest rate, yield
curve, dividend process or extra macro variable. The chosen rank and lag count are
illustrative experiment parameters, not statistically established properties of
these series. This small joint model is a baseline, not a model for pricing bonds
of arbitrary maturities. A richer experiment would name its additional datasets,
variables and financial bindings just as explicitly.

`fit_start`/`fit_end` are observation-month bounds; `fit_as_of` is the information
cutoff and cannot exceed the forecast start in an out-of-sample experiment.
Conditioning keeps each variable's observation dates, since CPI and market data
are not necessarily available for the same latest month. A provider must support
that dated, potentially incomplete information or reject the origin; alignment
does not authorize pretending a stale observation is current. Using a later fit
or later data vintage is a labeled retrospective variant, not a contemporaneous
forecast.

The author can instead implement a provider in their own experiment package and
return compatible joint paths. A fitted VECM is only one candidate. Comparing
regime models, alternative calibration windows, parameter uncertainty, stress
paths, and broader international evidence is legitimate experiment work. None
becomes institution-grade merely through being composable. Diagnostics should
include joint stock/bond/inflation behavior, persistence, tails and drawdowns, and
held-out forecast performance, not only matching unconditional means.

Sample once per model and reuse the worlds across policy cells. Changing models
changes the probability measure: report separate panels. Stress paths without
probability weights are scenarios, not extra Monte Carlo observations. Keeping
exogenous paths independent of the investor's actions is an explicit small-investor
assumption, compatible with several modeled agents and bilateral contracts.

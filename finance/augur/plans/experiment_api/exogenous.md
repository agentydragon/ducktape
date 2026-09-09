# The experiment owns path loading and sampling

Proposed Python. An experiment assembles a joint external-world model as well as
the decision policies. This is not one mandatory provider hierarchy: loaders and
fit procedures can be specific to each model. Their output must agree with the
experiment's instruments, information dates, calendar, and reporting units.

Here is a shell for comparing historical replay, a joint block bootstrap, and a
fitted macro model. Paths, bindings, dates, and block lengths are explicit inputs;
the programs in this directory do not supply invented evidence files.

```python
from pathlib import Path

from augur.evidence import EvidenceSnapshot
from augur.markets import HistoricalMarket, JointBlockBootstrap
from augur.markets.vecm import load_vecm


def study_worlds(
    evidence_path: Path, artifact_path: Path, *, universe, bindings,
    first_year, last_year, start, years, paths, block_months,
):
    evidence = EvidenceSnapshot.open(evidence_path)
    history = evidence.joint_returns(
        series=bindings, price_index="us_cpi", first_year=first_year, last_year=last_year
    )
    historical = HistoricalMarket(
        history=history, bindings=bindings, price_index="us_cpi", observation_period="month"
    )
    bootstrap = JointBlockBootstrap(
        history=history, bindings=bindings, price_index="us_cpi",
        block_months=block_months, incomplete_blocks="exclude", circular=False,
    )
    conditional = load_vecm(artifact_path).bind(universe).condition(
        evidence.observations_available_at(start)
    )
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

The bootstrap resamples **the aligned vector** of asset returns and inflation,
not each column separately. It preserves dependence within a sampled block, not
arbitrary long-run dynamics. Vary block length as an experiment parameter. Missing
history, fund inception, and regime breaks need explicit handling; no zero-fill.
Historical windows retain their actual dates; synthetic forecasts start at `start`.

This particular helper serves no-tax total-return studies. A taxed fund needs
prices, distributions, and tax character; an individual bond needs issuance terms,
coupon/redemption cashflows, and compatible valuation dynamics. Replaying a
total-return series does not supply those. A richer model can bind both studies
and real products, but cannot relabel missing cashflows into existence.

Fitting is also experiment code when the study varies its evidence window:

```python
from augur.markets.vecm import fit_vecm


def fitted_worlds(evidence_path, *, fit_spec, universe, fit_end, start, years, paths):
    evidence = EvidenceSnapshot.open(evidence_path)
    panel = evidence.macro_panel(fit_spec.series).available_by(fit_end)
    fitted = fit_vecm(panel, spec=fit_spec)
    market = fitted.bind(universe).condition(evidence.observations_available_at(start))
    worlds = market.sample(start=start, years=years, step="month", paths=paths, seed=813)
    return fitted, worlds
```

`fit_spec` pins the chosen transformations, variables, lag/rank choices, and fit
window; it is not an empty "good joint model" promise. A historical out-of-sample
experiment constrains both fitting and conditioning to information then available,
including evidence revisions. Using a later fit is allowed for a separately
labeled retrospective experiment, not for a claimed contemporaneous decision.

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

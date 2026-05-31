"""Trainer- and scorer-facing protocols for augur exogenous models.

`Fittable` and `Scorable` both extend `Sampler` (in `augur/model/exogenous.py`).
Every augur model is a Sampler — anything that can't be sampled is unusable
in the augur sim runtime. Adding `Fittable` means the model can be trained
offline from historical evidence; adding `Scorable` means it exposes a
predictive distribution the metric battery can project log-density / CRPS /
marginal density from.

A model can satisfy any subset:

  - `Sampler` only: test fixtures, future bootstrap-style models that
    refuse to expose density.
  - `Sampler & Scorable`: hand-configured providers like `IndependentExogenousModel`
    whose params are YAML-set; no fit step, but the predictive is a
    closed-form product of Gaussian marginals.
  - `Sampler & Fittable & Scorable`: the VECM NumPyro model — fit from a
    `HistoricalSeries`, then both score density and sample paths from
    the same generative definition + fitted params.
"""

from __future__ import annotations

from typing import Protocol

from numpyro import distributions as dist

from augur.model.exogenous import Sampler
from augur.model.path_models.scenarios import HistoricalSeries


class Fittable(Sampler, Protocol):
    """A `Sampler` that can be fitted offline from a `HistoricalSeries`.

    Consumed by the trainer in `augur/fit/main.py`. The metric battery
    additionally requires `Scorable`; rolling-origin scoring refits the
    model at each origin, so it requires `Fittable & Scorable`.
    """

    label: str
    factor_names: tuple[str, ...]

    def fit(self, historical: HistoricalSeries) -> None: ...


class Scorable(Sampler, Protocol):
    """A `Sampler` that exposes its predictive distribution. Consumed by
    the metric battery in `augur/fit/metrics.py` via the projection
    utilities in `augur/fit/scoring.py`.

    A `Scorable` need not be `Fittable` — a YAML-configured Independent
    provider exposes closed-form Gaussian predictives from hand-tuned
    GBM params, without ever calling `fit()`.
    """

    label: str
    factor_names: tuple[str, ...]

    def predictive(self, historical: HistoricalSeries, t: int, *, horizon: int = 1) -> dist.Distribution | None:
        """Joint predictive distribution over the cumulative `horizon`-step
        log-return at origin `t`, conditioned on the observed history
        `historical.levels[:t+1]`.

        For `horizon=1` the predictive describes `r_{t+1}` (the one-month
        log-return from t to t+1). For `horizon=h > 1` the predictive
        describes `Σ_{k=1..h} r_{t+k}` — the cumulative h-month log-return.
        Multi-step horizons reveal structural differences (vol clustering,
        cointegration pull, cascade dynamics) that one-step density smooths
        over.

        Returns `None` for models that decline to expose a tractable
        predictive (e.g. block-bootstrap-style empirical samplers). The
        metric scorer marks the result `Unscored` when this happens.

        Implementors typically return a `numpyro.distributions.MultivariateNormal`
        in closed form (VECM h=1, Independent provider) or as a Gaussian fit to
        a Monte-Carlo unroll (VECM h>1).
        """
        ...


class FittableScorable(Fittable, Scorable, Protocol):
    """A model that is both `Fittable` and `Scorable`. Python doesn't have
    an intersection-type expression for `Fittable & Scorable`, so the
    rolling-origin scorer (which refits at each origin and then scores)
    types its model parameter against this combined protocol instead.
    """

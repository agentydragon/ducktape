"""Model-agnostic projections of a NumPyro predictive distribution.

Augur's `Scorable` protocol exposes one method — `predictive(historical, t,
horizon=h)` — that returns a `numpyro.distributions.Distribution` describing
the joint predictive over the cumulative h-step log-return at origin t. Every
scoring metric (joint log-density, per-factor marginal log-densities,
continuous ranked probability score) is a projection of that distribution
against an observation. This module is the catalogue of projections.

For Gaussian predictives (every model currently in augur — VECM-NumPyro,
Independent provider) all projections have closed forms. For non-Gaussian
predictives we fall back to a sample-based estimate; the protocol allows a
model to return `None` from `predictive(...)` instead, in which case the
scorer marks the row Unscored.
"""

from __future__ import annotations

import math

import jax.numpy as jnp
import numpy as np
from jax.scipy.stats import norm as jstats_norm
from numpyro import distributions as dist


def joint_log_density(pred: dist.Distribution, observed: np.ndarray) -> float:
    """Joint log-density of `observed` under the predictive distribution."""

    return float(jnp.asarray(pred.log_prob(jnp.asarray(observed))))


def marginal_log_densities(
    pred: dist.Distribution, observed: np.ndarray, factor_names: tuple[str, ...]
) -> dict[str, float]:
    """Per-factor *univariate* log-densities of `observed[i]` under the
    marginal of `pred` at factor i.

    The sum over factors equals the joint only when factors are
    uncorrelated. The gap is what cross-factor structure (off-diagonal
    covariance, cointegration) buys.
    """

    mean, sd = _marginal_mean_sd(pred)
    obs = jnp.asarray(observed)
    if mean.shape != obs.shape or len(factor_names) != int(mean.shape[-1]):
        raise ValueError(
            f"shape mismatch: pred.mean {mean.shape}, observed {obs.shape}, factor_names ({len(factor_names)},)"
        )
    log_probs = jstats_norm.logpdf(obs, loc=mean, scale=sd)
    return {name: float(log_probs[i]) for i, name in enumerate(factor_names)}


def gaussian_crps(pred: dist.Distribution, observed: np.ndarray, factor_names: tuple[str, ...]) -> dict[str, float]:
    """Per-factor Continuous Ranked Probability Score under the marginal
    Gaussian predictive. Closed form:

        CRPS(N(μ, σ²), y) = σ · [z (2Φ(z) - 1) + 2φ(z) - 1/√π]

    where z = (y - μ) / σ. Lower is better (units of the observed variable —
    here, monthly log-return). The metric is proper: the only distribution
    that minimises the expected CRPS against future observations is the true
    predictive.
    """

    mean, sd = _marginal_mean_sd(pred)
    obs = jnp.asarray(observed)
    z = (obs - mean) / sd
    crps = sd * (z * (2.0 * jstats_norm.cdf(z) - 1.0) + 2.0 * jstats_norm.pdf(z) - 1.0 / math.sqrt(math.pi))
    return {name: float(crps[i]) for i, name in enumerate(factor_names)}


def _marginal_mean_sd(pred: dist.Distribution) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Return (μ, σ) of the per-factor marginal of `pred`.

    Closed form for any MultivariateNormal (σ_i = √Σ_ii). For other
    distributions we currently don't have a closed form — callers should
    handle the `predictive(...) -> None` case at the scorer level rather
    than here.
    """

    if isinstance(pred, dist.MultivariateNormal):
        mean = jnp.asarray(pred.mean)
        cov = jnp.asarray(pred.covariance_matrix)
        sd = jnp.sqrt(jnp.diagonal(cov, axis1=-2, axis2=-1))
        return mean, sd
    raise TypeError(
        f"predictive distribution type {type(pred).__name__!r} has no closed-form marginals; "
        "scorer should sample and fit Gaussian via empirical_marginal_mean_sd(...) or skip."
    )


def empirical_marginal_mean_sd(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Fit per-factor (mean, sd) to a (N, F) cloud of samples. Used as a
    fallback for predictives that aren't analytically MultivariateNormal
    (the closed-form scorers above then operate on these moments)."""

    arr = np.asarray(samples)
    if arr.ndim != 2:
        raise ValueError(f"samples must be 2-D (N, F); got shape {arr.shape}")
    mean = arr.mean(axis=0)
    sd = arr.std(axis=0, ddof=1)
    return mean, sd

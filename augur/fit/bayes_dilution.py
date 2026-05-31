"""Full-Bayesian (NUTS) dilution + decaying-valuation-drift fit (M2.2-D).

The OLS fit in `augur.fit.dilution_prior` is a log-linear regression on the ~4 in-window
(price, valuation) pairs. With so few points it extrapolates the observed boom as a *forward*
rate: on the openai evidence it returns `annual_dilution_rate ~ 0.48` and a ~244%/yr valuation
drift, which compounds (a constant-drift random walk) to an absurd ~8000x median 10-year mark
and fails the deployed `sample_sanity` bands.

This module fixes that in two coupled ways, both validated to bring the openai fit back inside
the deploy bands:

1.  **Bayesian inference with informative priors.** A small numpyro state-space model over ALL
    observations (not just the in-window pairs) with a latent log-company-value path `V(t)` and
    a deterministic log-share path. Each observation contributes through its own
    `uncertainty_log_sigma`. Informative priors regularize the few-point extrapolation, and the
    posterior gives an honest `annual_dilution_rate_log_sigma` (the posterior SD of `log(1+r)`)
    rather than the delta-method approximation the OLS admits is "weakly identified".

2.  **Decaying valuation drift.** The latent `V(t)` drift decays from a near-term `mu_0` toward
    a long-run mature `mu_inf` with a fitted half-life, so an explosive near-term growth rate
    informs the near term without compounding over the whole horizon. These map directly onto
    `PrivateEquityRiskIssuerConfig.valuation_monthly_log_return_mu` (= `mu_inf`),
    `valuation_monthly_log_return_mu_initial` (= `mu_0`), and
    `valuation_drift_decay_halflife_months`.

Generative model (monthly time axis `t`, months since the first observation):

    log_V0      ~ Normal(log V0_prior, log_v0_prior_sigma)
    mu_inf      ~ Normal(mu_inf_prior_mu, mu_inf_prior_sigma)        # long-run monthly log-drift
    excess0     ~ HalfNormal(excess0_prior_sigma)                    # mu_0 - mu_inf >= 0
    log_halflife~ Normal(log halflife_prior, halflife_prior_sigma)   # decay half-life (months)
    sigma_V     ~ HalfNormal(sigma_v_prior_sigma)                    # company-value monthly vol
    log_shares0 ~ Normal(log shares0_prior, log_shares0_prior_sigma)
    log1p_r     ~ Normal(log(1 + r_prior), log1p_r_prior_sigma)      # annual dilution log(1+r)

    mu(t)         = mu_inf + excess0 * 0.5 ** (t / halflife)         # decaying drift
    cum_drift(t)  = sum_{s<t} mu(s)                                   # discrete cumulative drift
    log_V(t)      = log_V0 + cum_drift(t) + sigma_V * sqrt(t) * z_t   (z_t ~ Normal(0,1))
    log_shares(t) = log_shares0 + (t / 12) * log1p_r
    log_price(t)  = log_V(t) - log_shares(t)
    valuation_obs ~ Normal(log_V(t_v),     uncertainty_log_sigma_v)
    price_obs     ~ Normal(log_price(t_p), uncertainty_log_sigma_p)

Generic: takes the project's `PriceObservation` / `ValuationObservation` (the same types
`train_private_equity` ingests), returns a `BayesianDilutionPrior`. NUTS is not run-to-run
bit-reproducible, so callers persist the returned posterior summary (means/SDs) rather than
re-running inference at deploy time -- mirroring how `augur/model/vecm.py` persists its fit.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import jax
import jax.numpy as jnp
import numpy as np
import numpyro
import numpyro.distributions as dist
from numpyro.infer import MCMC, NUTS

from augur.fit.private_equity import PriceObservation, ValuationObservation

# Mean Gregorian year / 12 — matches the day-count used across augur's time-axis math.
# (Consolidation into a shared augur.dates helper is tracked separately.)
_DAYS_PER_MONTH = 365.2425 / 12.0
_MONTHS_PER_YEAR = 12.0


@dataclass(frozen=True)
class BayesianDilutionPriors:
    """Hyperparameters of the informative priors. Defaults are calibrated for a high-growth
    private company (frontier-AI-scale): a long-run drift well below the observed boom, a
    dilution centered on a modest baseline mint, and a half-life of a couple of years over which
    the near-term boom decays. Tune per issuer if its reference class differs.
    """

    log_v0_usd: float = float(np.log(2.8e10))
    log_v0_sigma: float = 1.0
    mu_inf_mu: float = 0.02  # ~27%/yr long-run, monthly log
    mu_inf_sigma: float = 0.02
    excess0_sigma: float = 0.08  # near-term excess drift over mu_inf, HalfNormal scale
    halflife_months_mu: float = 24.0
    log_halflife_sigma: float = 0.5
    sigma_v_sigma: float = 0.10
    log_shares0: float = float(np.log(4.0e8))
    log_shares0_sigma: float = 0.5
    annual_dilution_rate_mu: float = 0.20
    log1p_r_sigma: float = 0.30


@dataclass(frozen=True)
class BayesianDilutionPrior:
    """Posterior summary of the M2.2-D fit, mapping directly onto the issuer config knobs.

    Each `*_mu` is the posterior mean (the value to deploy) and the paired `*_sd` is the
    posterior SD (the honest uncertainty). `annual_dilution_rate_log_sigma` is the posterior SD
    of `log(1 + r)` -- the per-rollout dispersion the sampler consumes -- NOT a posterior-of-a-
    parameter; it is the epistemic+sampling spread the M2.2-A sampler folds into each rollout.
    """

    annual_dilution_rate: float
    annual_dilution_rate_log_sigma: float
    valuation_monthly_log_return_mu: float  # mu_inf (long-run)
    valuation_monthly_log_return_mu_initial: float  # mu_0 (near-term) = mu_inf + excess0
    valuation_drift_decay_halflife_months: float
    valuation_monthly_log_return_sigma: float
    shares0: float
    n_price_observations: int
    n_valuation_observations: int
    num_divergences: int


def _generative(
    *,
    price_t: jnp.ndarray,
    price_y: jnp.ndarray,
    price_s: jnp.ndarray,
    val_t: jnp.ndarray,
    val_y: jnp.ndarray,
    val_s: jnp.ndarray,
    grid_t: jnp.ndarray,
    priors: BayesianDilutionPriors,
) -> None:
    """numpyro model: latent decaying-drift log-value RW + deterministic log-share path.

    `grid_t` is the sorted unique observation months; the latent `log_V` lives on that grid and
    observations index into it. `price_t` / `val_t` are each observation's month offset.
    """

    log_v0 = numpyro.sample("log_v0", dist.Normal(priors.log_v0_usd, priors.log_v0_sigma))
    mu_inf = numpyro.sample("mu_inf", dist.Normal(priors.mu_inf_mu, priors.mu_inf_sigma))
    excess0 = numpyro.sample("excess0", dist.HalfNormal(priors.excess0_sigma))
    halflife = numpyro.sample(
        "halflife_months", dist.LogNormal(jnp.log(priors.halflife_months_mu), priors.log_halflife_sigma)
    )
    sigma_v = numpyro.sample("sigma_v", dist.HalfNormal(priors.sigma_v_sigma))
    log_shares0 = numpyro.sample("log_shares0", dist.Normal(priors.log_shares0, priors.log_shares0_sigma))
    log1p_r = numpyro.sample(
        "log1p_r", dist.Normal(jnp.log(1.0 + priors.annual_dilution_rate_mu), priors.log1p_r_sigma)
    )
    numpyro.deterministic("annual_dilution_rate", jnp.exp(log1p_r) - 1.0)
    numpyro.deterministic("mu_0", mu_inf + excess0)

    # Cumulative decaying drift to each grid month: cum_drift(t) = integral_0^t mu(s) ds, with
    # mu(s) = mu_inf + excess0 * 0.5 ** (s / halflife). Closed form of the integral keeps it
    # smooth in `halflife` (no discrete-sum gradient noise):
    #   integral_0^t excess0 * 0.5**(s/H) ds = excess0 * H / ln2 * (1 - 0.5**(t/H)).
    ln2 = jnp.log(2.0)
    decayed = excess0 * (halflife / ln2) * (1.0 - jnp.power(0.5, grid_t / halflife))
    cum_drift = mu_inf * grid_t + decayed

    n_grid = grid_t.shape[0]
    z = numpyro.sample("z", dist.Normal(jnp.zeros(n_grid), 1.0).to_event(1))
    log_v = log_v0 + cum_drift + sigma_v * jnp.sqrt(jnp.maximum(grid_t, 1e-6)) * z

    def _grid_index(ts: jnp.ndarray) -> jnp.ndarray:
        return jnp.argmin(jnp.abs(grid_t[None, :] - ts[:, None]), axis=1)

    log_v_at_val = log_v[_grid_index(val_t)]
    log_v_at_price = log_v[_grid_index(price_t)]
    log_shares_at_price = log_shares0 + (price_t / _MONTHS_PER_YEAR) * log1p_r
    log_price_model = log_v_at_price - log_shares_at_price

    numpyro.sample("val_obs", dist.Normal(log_v_at_val, val_s), obs=val_y)
    numpyro.sample("price_obs", dist.Normal(log_price_model, price_s), obs=price_y)


def _months_since(observed_at: dt.date, origin: dt.date) -> float:
    return (observed_at - origin).days / _DAYS_PER_MONTH


def fit_bayesian_dilution_prior(
    prices: list[PriceObservation],
    valuations: list[ValuationObservation],
    *,
    priors: BayesianDilutionPriors | None = None,
    num_warmup: int = 1500,
    num_samples: int = 3000,
    num_chains: int = 2,
    seed: int = 0,
) -> BayesianDilutionPrior:
    """Fit the M2.2-D Bayesian dilution + decaying-drift prior via NUTS.

    Raises `ValueError` when there are too few observations to identify the latent path
    (need at least 2 valuations to pin the value level over time and 2 prices for the
    per-share slope).
    """

    priors = priors or BayesianDilutionPriors()
    if len(valuations) < 2 or len(prices) < 2:
        raise ValueError(
            f"Bayesian dilution fit needs >= 2 price and >= 2 valuation observations; "
            f"got {len(prices)} prices, {len(valuations)} valuations"
        )

    origin = min(obs.observed_at for obs in [*prices, *valuations])
    price_t = np.array([_months_since(obs.observed_at, origin) for obs in prices], dtype=np.float64)
    price_y = np.array([np.log(obs.price_usd_per_share) for obs in prices], dtype=np.float64)
    price_s = np.array([obs.uncertainty_log_sigma for obs in prices], dtype=np.float64)
    val_t = np.array([_months_since(obs.observed_at, origin) for obs in valuations], dtype=np.float64)
    val_y = np.array([np.log(obs.valuation_usd) for obs in valuations], dtype=np.float64)
    val_s = np.array([obs.uncertainty_log_sigma for obs in valuations], dtype=np.float64)
    grid_t = np.unique(np.concatenate([price_t, val_t]))

    # A non-centered RW with a `sigma_v * sqrt(t) * z` scale has mild funnel geometry; a higher
    # target acceptance probability shrinks the step size and curbs the occasional divergence.
    mcmc = MCMC(
        NUTS(_generative, target_accept_prob=0.95),
        num_warmup=num_warmup,
        num_samples=num_samples,
        num_chains=num_chains,
        progress_bar=False,
    )
    mcmc.run(
        jax.random.PRNGKey(seed),
        price_t=jnp.asarray(price_t),
        price_y=jnp.asarray(price_y),
        price_s=jnp.asarray(price_s),
        val_t=jnp.asarray(val_t),
        val_y=jnp.asarray(val_y),
        val_s=jnp.asarray(val_s),
        grid_t=jnp.asarray(grid_t),
        priors=priors,
        extra_fields=("diverging",),
    )
    samples = mcmc.get_samples()
    rate = np.asarray(samples["annual_dilution_rate"])
    mu_inf = np.asarray(samples["mu_inf"])
    mu_0 = np.asarray(samples["mu_0"])
    halflife = np.asarray(samples["halflife_months"])
    sigma_v = np.asarray(samples["sigma_v"])
    log_shares0 = np.asarray(samples["log_shares0"])
    extra = mcmc.get_extra_fields() if hasattr(mcmc, "get_extra_fields") else {}
    num_divergences = int(np.sum(np.asarray(extra["diverging"]))) if "diverging" in extra else 0

    return BayesianDilutionPrior(
        annual_dilution_rate=float(np.mean(rate)),
        # Per-rollout dispersion = posterior SD of log(1+r): folds the epistemic uncertainty over
        # the dilution rate into the sampler's median-anchored LogNormal spread.
        annual_dilution_rate_log_sigma=float(np.std(np.log1p(rate))),
        valuation_monthly_log_return_mu=float(np.mean(mu_inf)),
        valuation_monthly_log_return_mu_initial=float(np.mean(mu_0)),
        valuation_drift_decay_halflife_months=float(np.mean(halflife)),
        valuation_monthly_log_return_sigma=float(np.mean(sigma_v)),
        shares0=float(np.exp(np.mean(log_shares0))),
        n_price_observations=len(prices),
        n_valuation_observations=len(valuations),
        num_divergences=num_divergences,
    )

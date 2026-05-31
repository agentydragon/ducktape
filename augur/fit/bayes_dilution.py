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

2.  **Scale-dependent mean-reverting valuation drift.** The latent `V(t)` drift declines from a
    hot `mu_young` (when the company is small) toward a mature `mu_mature` as the realized log
    enterprise value grows past an onset -- so the boom is tamed by the company's observed SIZE,
    not a calendar prior. These map onto a `ValuationDriftScaleReversion` submodel on the issuer
    config (`mu_mature = valuation_monthly_log_return_mu`; `mu_young` / `log_value_onset_usd` /
    `log_value_scale`). The fit integrates V(t) as an SDE, mirroring the deployment sampler.

Generative model (dense monthly grid `m = 0..n_months`):

    log_V0          ~ Normal(log V0_prior, log_v0_prior_sigma)
    mu_mature       ~ Normal(mu_mature_prior_mu, mu_mature_prior_sigma)   # mature monthly log-drift
    mu_young_excess ~ HalfNormal(mu_young_excess_prior_sigma)             # mu_young - mu_mature >= 0
    log_value_onset ~ Normal(log_value_onset_prior, ...)                  # size at which reversion begins
    log_value_scale ~ LogNormal(log scale_prior, ...)                     # e-folding in log-value
    sigma_V         ~ LogNormal(log sigma_v_prior_mu, sigma_v_prior_log_sigma)  # company-value monthly vol
    log_shares0     ~ Normal(log shares0_prior, log_shares0_prior_sigma)
    log1p_r         ~ Normal(log(1 + r_prior), log1p_r_prior_sigma)       # annual dilution log(1+r)

    mu(s)          = mu_mature + mu_young_excess * exp(-max(0, s - log_value_onset)/log_value_scale)
    log_V(m+1)     = log_V(m) + mu(log_V(m)) + sigma_V * z_m              (SDE; z_m ~ Normal(0,1))
    log_shares(t)  = log_shares0 + (t / 12) * log1p_r
    log_price(t)   = log_V(t) - log_shares(t)
    valuation_obs  ~ Normal(log_V(t_v),     uncertainty_log_sigma_v)
    price_obs      ~ Normal(log_price(t_p), uncertainty_log_sigma_p)

Generic: takes the project's `PriceObservation` / `ValuationObservation` (the same types
`train_private_equity` ingests), returns a `BayesianDilutionPrior`. NUTS is not run-to-run
bit-reproducible, so callers persist the returned posterior summary (means/SDs) rather than
re-running inference at deploy time -- mirroring how `augur/model/vecm.py` persists its fit.
"""

from __future__ import annotations

import datetime as dt
import math
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
    """Hyperparameters of the informative priors (M2.2-D; see
    augur/plans/prediction_market_calibration.md).

    Governing philosophy -- every default below is a FORWARD belief about a high-growth private
    company maturing toward a mega-cap, NOT an in-sample fit to the observed boom. The handful of
    (price, valuation) points a single issuer gives us all sit in one explosive growth episode;
    fit naively (the OLS prior) they extrapolate ~240%/yr drift and ~0.48 dilution forever and
    blow out the 10-year mark by ~1000x. The priors here are the regularizers that pull the
    forward path back to something a maturing company plausibly does, and they come in two
    flavors:

      * IDENTIFIABLE-from-this-issuer (the posterior moves these): level `log_v0`, volatility
        `sigma_v`, share count `log_shares0`, dilution `log1p_r`. Their priors are deliberately
        WIDE -- weak anchors that let the data speak.
      * SHAPE-of-the-reversion (mu_mature, mu_young, onset, scale): a single issuer whose data is
        all in one size regime CANNOT identify these (fitting them diverges), so in the default
        `fit_scale_reversion_shape=False` mode they are FIXED at the centers below. Their values
        are therefore load-bearing educated guesses, justified per-field, to be replaced by a
        population/hierarchical fit (augur/TODO.md).

    Numbers are monthly log-units unless noted; annualized figures use `exp(12*mu)-1` for drift
    and `sigma*sqrt(12)` for vol. Override per issuer reference class.
    """

    # --- Level at the observation-window origin (IDENTIFIABLE; weak anchor) -------------------
    # Generic ~$28B starting value; `sigma=1.0` is ~1 order of magnitude (e^1 ~ 2.7x) either way,
    # so the valuation observations -- not this prior -- pin the level. Override per issuer (the
    # openai deploy anchors near its current round, ~$850B).
    log_v0_usd: float = float(np.log(2.8e10))
    log_v0_sigma: float = 1.0

    # --- Scale-dependent drift SHAPE (FIXED in default mode) ----------------------------------
    # mu(s) = mu_mature + (mu_young - mu_mature) * exp(-max(0, s - onset) / scale), s = log value.
    # mu_mature: the long-run asymptotic drift once the company is large. 0.008/mo = 10.0%/yr,
    # the S&P 100-year nominal CAGR -- the conservative "mature mega-cap grows with the market"
    # anchor. THIS is the key regularizer: it caps where the boom can extrapolate to. sigma=0.004
    # (~+-5%/yr at 1 sigma, range ~5-15%/yr) when the shape IS fit; ignored when fixed.
    mu_mature_mu: float = 0.008
    mu_mature_sigma: float = 0.004
    # mu_young_excess = mu_young - mu_mature >= 0: how much hotter a SMALL company runs. HalfNormal
    # scale 0.06 has mean ~0.048/mo (~78%/yr excess) and reaches ~0.12/mo (~290%/yr young drift)
    # in the tail -- wide enough to cover observed hypergrowth (~100-150%/yr) when the shape is
    # fit, without dragging the mature asymptote up with it. Fixed-mode value = the prior mean.
    mu_young_excess_sigma: float = 0.06
    # onset: enterprise value at which reversion young->mature begins. ~$20B = "low tens of $B",
    # roughly where hypergrowth startups hit large-cap dynamics. sigma=0.7 (~2x either way,
    # ~$10-40B). A company already far above onset (openai at ~$850B) is fully in the mature
    # regime, so its forward drift is essentially mu_mature regardless of the young excess.
    log_value_onset_mu: float = float(np.log(2.0e10))
    log_value_onset_sigma: float = 0.7
    # scale: e-folding length of the excess in log-value. 2.0 means the young excess decays by 1/e
    # per e^2 ~ 7.4x of value growth (~1.15 e-foldings per order of magnitude). sigma=0.5 LogNormal
    # (~1.65x either way) when fit.
    log_value_scale_mu: float = 2.0
    log_value_scale_sigma: float = 0.5

    # --- Company-value volatility (IDENTIFIABLE, but FORWARD-anchored) -------------------------
    # Informative LogNormal centered at 0.10/mo (~35%/yr). This is the de-smoothed late-stage-VC
    # figure (Anson ~38%/yr), comparable to a single large-cap tech name (NVDA ~36%/yr) and well
    # above a diversified index (~19%/yr). It is deliberately NOT the raw in-sample scatter of the
    # boom years: fit unconstrained, sigma_v runs to ~73%/yr and the 10-year mark p99 blows past
    # the sample_sanity 100x ceiling (~260x). Same forward-vs-in-sample logic as the drift, applied
    # to the second moment. log_sigma=0.10 keeps the prior tight (1 sigma ~ x[0.90,1.11] = ~31-38%/yr)
    # so the boom-era likelihood can only nudge the posterior to ~43%/yr -- which lands the openai
    # deploy inside ALL FOUR sample_sanity mark bands (m12 p50~1.0, m120 p50~0.45, both p1..p99 in
    # band). Loosen it (toward the in-sample ~50-70%/yr) only for an issuer you truly believe stays
    # that volatile forward.
    sigma_v_mu: float = 0.10
    sigma_v_log_sigma: float = 0.10

    # --- Share/unit count at window origin (IDENTIFIABLE; weak anchor) ------------------------
    # ~400M fully-diluted units, order-of-magnitude guess. sigma=0.5 (~1.65x). The price channel
    # (price = V / shares) pins this against the valuation channel, so the posterior moves freely
    # (openai lands ~190M).
    log_shares0: float = float(np.log(4.0e8))
    log_shares0_sigma: float = 0.5

    # --- Annual dilution rate (IDENTIFIABLE; the primary quantity of interest) ----------------
    # Prior on log(1+r) ~ Normal(log(1+0.20), 0.30). Center r=0.20 = a 20%/yr baseline unit mint
    # (new rounds + employee equity) for a high-growth private company. sigma=0.30 in log-space is
    # WIDE: 1 sigma spans r in ~[-11%, +62%], so the data dominates -- on the openai evidence the
    # posterior sharpens to r~0.27 with a small log-SD (~0.05). Wide on purpose: dilution is the
    # whole point of the fit, so we let the observations, not the prior, determine it.
    annual_dilution_rate_mu: float = 0.20
    log1p_r_sigma: float = 0.30


@dataclass(frozen=True)
class BayesianDilutionPrior:
    """Posterior summary of the M2.2-D fit, mapping onto the issuer config knobs.

    The drift fields populate a `ValuationDriftScaleReversion` submodel:
    `valuation_monthly_log_return_mu` is the mature asymptote `mu_mature`;
    `mu_young` / `log_value_onset_usd` / `log_value_scale` are the reversion shape.
    `annual_dilution_rate_log_sigma` is the posterior SD of `log(1 + r)` -- the per-rollout
    dispersion the M2.2-A sampler folds into each rollout -- not a posterior-of-a-parameter.
    """

    annual_dilution_rate: float
    annual_dilution_rate_log_sigma: float
    valuation_monthly_log_return_mu: float  # mu_mature (asymptote)
    valuation_drift_mu_young: float
    valuation_drift_log_value_onset_usd: float
    valuation_drift_log_value_scale: float
    valuation_monthly_log_return_sigma: float
    shares0: float
    n_price_observations: int
    n_valuation_observations: int
    num_divergences: int


def _generative(
    *,
    price_month_idx: jnp.ndarray,
    price_t: jnp.ndarray,
    price_y: jnp.ndarray,
    price_s: jnp.ndarray,
    val_month_idx: jnp.ndarray,
    val_y: jnp.ndarray,
    val_s: jnp.ndarray,
    n_months: int,
    priors: BayesianDilutionPriors,
    fit_shape: bool,
) -> None:
    """numpyro model: latent SCALE-reverting log-value SDE + deterministic log-share path.

    The latent `log_V` is integrated on a DENSE monthly grid `0..n_months` (an SDE because the
    drift `mu(s) = mu_mature + (mu_young - mu_mature) * exp(-max(0, s - onset)/scale)` depends on
    the realized level `s = log_V`). Observations index into the grid via `*_month_idx`
    (rounded month offsets). Mirrors the deployment sampler's scale-reverting V(t) integration.

    `fit_shape`: when True the four reversion-shape params (mu_mature, mu_young_excess,
    log_value_onset, log_value_scale) are sampled. When False they are FIXED at their prior
    centers and only the identifiable params (log_v0, sigma_v, log_shares0, dilution) are
    sampled -- the required mode for a single issuer whose data is all in the "large" regime and
    so cannot identify the shape (see module docstring / plans M2.2-D).
    """

    log_v0 = numpyro.sample("log_v0", dist.Normal(priors.log_v0_usd, priors.log_v0_sigma))
    if fit_shape:
        mu_mature = numpyro.sample("mu_mature", dist.Normal(priors.mu_mature_mu, priors.mu_mature_sigma))
        mu_young_excess = numpyro.sample("mu_young_excess", dist.HalfNormal(priors.mu_young_excess_sigma))
        log_value_onset = numpyro.sample(
            "log_value_onset", dist.Normal(priors.log_value_onset_mu, priors.log_value_onset_sigma)
        )
        log_value_scale = numpyro.sample(
            "log_value_scale", dist.LogNormal(jnp.log(priors.log_value_scale_mu), priors.log_value_scale_sigma)
        )
    else:
        # Fixed shape: deterministic at the prior centers (the young excess is the HalfNormal
        # mode-adjacent prior mean E[HalfNormal(s)] = s*sqrt(2/pi)). Recorded as deterministic so
        # the same posterior-summary extraction works in both modes.
        mu_mature = numpyro.deterministic("mu_mature", jnp.asarray(priors.mu_mature_mu))
        mu_young_excess = numpyro.deterministic(
            "mu_young_excess", jnp.asarray(priors.mu_young_excess_sigma * math.sqrt(2.0 / math.pi))
        )
        log_value_onset = numpyro.deterministic("log_value_onset", jnp.asarray(priors.log_value_onset_mu))
        log_value_scale = numpyro.deterministic("log_value_scale", jnp.asarray(priors.log_value_scale_mu))
    sigma_v = numpyro.sample("sigma_v", dist.LogNormal(jnp.log(priors.sigma_v_mu), priors.sigma_v_log_sigma))
    log_shares0 = numpyro.sample("log_shares0", dist.Normal(priors.log_shares0, priors.log_shares0_sigma))
    log1p_r = numpyro.sample(
        "log1p_r", dist.Normal(jnp.log(1.0 + priors.annual_dilution_rate_mu), priors.log1p_r_sigma)
    )
    numpyro.deterministic("annual_dilution_rate", jnp.exp(log1p_r) - 1.0)
    numpyro.deterministic("mu_young", mu_mature + mu_young_excess)

    # Per-month shocks (non-centered). Integrate the scale-reverting SDE on the dense grid via
    # scan: each step's drift is evaluated at the realized log-value at the step's start.
    z = numpyro.sample("z", dist.Normal(jnp.zeros(n_months), 1.0).to_event(1))
    shocks = sigma_v * z

    def _step(log_v_prev: jnp.ndarray, shock: jnp.ndarray) -> tuple[jnp.ndarray, jnp.ndarray]:
        over_onset = jnp.maximum(0.0, log_v_prev - log_value_onset)
        drift = mu_mature + mu_young_excess * jnp.exp(-over_onset / log_value_scale)
        log_v_next = log_v_prev + drift + shock
        return log_v_next, log_v_next

    _, log_v_path = jax.lax.scan(_step, log_v0, shocks)  # log_v_path[m] = log_V at month m+1
    log_v_grid = jnp.concatenate([log_v0[None], log_v_path])  # index 0 = month 0

    log_v_at_val = log_v_grid[val_month_idx]
    log_v_at_price = log_v_grid[price_month_idx]
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
    fit_scale_reversion_shape: bool = False,
    num_warmup: int = 1500,
    num_samples: int = 3000,
    num_chains: int = 2,
    seed: int = 0,
) -> BayesianDilutionPrior:
    """Fit the M2.2-D Bayesian dilution + scale-reversion prior via NUTS.

    `fit_scale_reversion_shape` defaults to False: the reversion SHAPE (mu_mature, mu_young,
    onset, scale) is FIXED at the prior centers and only the identifiable params (level,
    volatility, share count, dilution rate) are sampled. This is the required mode for a single
    issuer whose observations are all in the "large" regime -- it cannot identify the shape, and
    fitting it anyway diverges (see plans M2.2-D). Set True only when the data spans a wide size
    range (or for a future population fit). Either way the returned prior carries the full shape
    (fixed or fitted) so the deployment config is complete.

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
    # Dense monthly grid 0..n_months; each observation snaps to its nearest whole month. The SDE
    # is integrated on this grid (drift depends on the realized level), matching the sampler.
    n_months = round(float(max(price_t.max(), val_t.max())))
    price_month_idx = np.clip(np.round(price_t).astype(np.int64), 0, n_months)
    val_month_idx = np.clip(np.round(val_t).astype(np.int64), 0, n_months)

    mcmc = MCMC(
        NUTS(_generative, target_accept_prob=0.95),
        num_warmup=num_warmup,
        num_samples=num_samples,
        num_chains=num_chains,
        progress_bar=False,
    )
    mcmc.run(
        jax.random.PRNGKey(seed),
        price_month_idx=jnp.asarray(price_month_idx),
        price_t=jnp.asarray(price_t),
        price_y=jnp.asarray(price_y),
        price_s=jnp.asarray(price_s),
        val_month_idx=jnp.asarray(val_month_idx),
        val_y=jnp.asarray(val_y),
        val_s=jnp.asarray(val_s),
        n_months=n_months,
        priors=priors,
        fit_shape=fit_scale_reversion_shape,
        extra_fields=("diverging",),
    )
    samples = mcmc.get_samples()
    rate = np.asarray(samples["annual_dilution_rate"])
    extra = mcmc.get_extra_fields()
    num_divergences = int(np.sum(np.asarray(extra["diverging"]))) if "diverging" in extra else 0

    return BayesianDilutionPrior(
        annual_dilution_rate=float(np.mean(rate)),
        # Per-rollout dispersion = posterior SD of log(1+r): folds the epistemic uncertainty over
        # the dilution rate into the sampler's median-anchored LogNormal spread.
        annual_dilution_rate_log_sigma=float(np.std(np.log1p(rate))),
        valuation_monthly_log_return_mu=float(np.mean(np.asarray(samples["mu_mature"]))),
        valuation_drift_mu_young=float(np.mean(np.asarray(samples["mu_young"]))),
        valuation_drift_log_value_onset_usd=float(np.mean(np.asarray(samples["log_value_onset"]))),
        valuation_drift_log_value_scale=float(np.mean(np.asarray(samples["log_value_scale"]))),
        valuation_monthly_log_return_sigma=float(np.mean(np.asarray(samples["sigma_v"]))),
        shares0=float(np.exp(np.mean(np.asarray(samples["log_shares0"])))),
        n_price_observations=len(prices),
        n_valuation_observations=len(valuations),
        num_divergences=num_divergences,
    )

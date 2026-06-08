"""NUTS fit for the mint-streams private-equity model (M2.2-C, 2026-06).

Sibling of `bayes_dilution.py`: same scale-reverting V drift, but replaces the smooth
`(1+r)^(t/12)` dilution path with discrete primary-round event jumps + a continuous
employee-mint stream. The structural fix for the asymmetric-regularization defect
documented in `augur/plans/mint_streams_model.md`.

Key trick that makes this tractable: primary-round event *times* are observed (annotated
in the JSONL via `valuation_kind="primary"`). So:

- The **monthly hazard** decouples completely from the latent V/shares path — it's a
  closed-form Gamma-Poisson posterior over the realized event count over the observation
  window. No MCMC needed for hazard.
- The **cash/V_pre ratio distribution** is observed per round; the numpyro likelihood
  treats `log(cash_obs / V_pre_at_event)` as a noisy observation of the median + log_sigma
  pair. `V_pre_at_event` is the latent V at the event month (sampled by NUTS).
- **V(t)** is integrated as a scale-reverting Student-t SDE with deterministic jumps applied
  at observed primary event months (since cash is observed, V_post = V_pre + cash is
  mechanical with step_up = 1.0).
- **shares(t)** is a smooth employee-mint exponential between events with deterministic
  jumps at primary events (shares_post = shares_pre * (1 + cash/V_pre)).
- **Tender prices** anchor V/shares: `log P = log V(t_p) - log shares(t_p) + tender_discount`.
- **Secondary observations** are noisy V(t) observations (no jump, no share-count effect).

This means NUTS samples only the IDENTIFIABLE forward-relevant parameters:
- `log_v0`, `log_shares0`: anchors at the observation-window origin
- `sigma_v`: V random-walk vol
- `log_cash_over_v_pre_median`, `cash_over_v_pre_log_sigma`: primary-round size distribution
- `annual_mint_rate_mature`: continuous employee mint

The reversion SHAPE (`mu_mature`, `mu_young_excess`, `log_value_onset`, `log_value_scale`)
is FIXED at prior centers in the default mode, same as `bayes_dilution`'s fixed-shape mode
— a single issuer can't identify the shape from data all in one size regime. Step-up is
fixed at 1.0 (pure mechanical V_post = V_pre + cash) for v1.

Returns a `BayesianMintStreamsPosterior` populated with the params that drive a complete
`PrivateEquityRiskIssuerConfig.primary_round_config` + `employee_mint_config`. NUTS is not
run-to-run bit-reproducible, so persist the returned summary; don't re-fit at deploy time.
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

from finance.augur.fit.private_equity import PriceObservation, ValuationObservation

_DAYS_PER_MONTH = 365.2425 / 12.0
_MONTHS_PER_YEAR = 12.0


@dataclass(frozen=True)
class BayesianMintStreamsPriors:
    """Hyperparameters of the informative priors for the mint-streams fit.

    Same governing philosophy as `BayesianDilutionPriors`: every default is a FORWARD belief
    about a maturing private company, not an in-sample regression to the boom. See plans/
    mint_streams_model.md for derivations; the reversion-shape and V-vol defaults are reused
    verbatim from the dilution prior since the V process is unchanged.
    """

    # --- Level at the observation-window origin ----------------------------------------
    log_v0_usd: float = float(np.log(2.8e10))
    log_v0_sigma: float = 1.0

    # --- Scale-dependent drift SHAPE (FIXED in default mode, reused from dilution prior) -
    mu_mature_mu: float = 0.008
    mu_mature_sigma: float = 0.004
    mu_young_excess_sigma: float = 0.06
    log_value_onset_mu: float = float(np.log(2.0e10))
    log_value_onset_sigma: float = 0.7
    log_value_scale_mu: float = 2.0
    log_value_scale_sigma: float = 0.5

    # --- V vol -------------------------------------------------------------------------
    # Same as dilution prior. The mint-streams structure should let the posterior sharpen
    # further (no longer absorbing primary-round jumps as random-walk shocks), so a tighter
    # prior is reasonable but we keep it for cross-fit comparability.
    sigma_v_mu: float = 0.10
    sigma_v_log_sigma: float = 0.10

    # --- Share count at window origin --------------------------------------------------
    log_shares0: float = float(np.log(4.0e8))
    log_shares0_sigma: float = 0.5

    # --- Primary-round size: cash_over_v_pre ~ LogNormal(median, log_sigma) ------------
    # Empirical geometric mean across observed openai rounds: ~0.128 (6 rounds 2019-2026).
    # Prior centered at 0.13, wide log-sigma (0.5) lets the data speak.
    cash_over_v_pre_median_mu: float = 0.13
    log_cash_over_v_pre_sigma_prior: float = 0.5  # prior on log_median
    cash_over_v_pre_log_sigma_prior_scale: float = 0.6  # HalfNormal scale on dispersion

    # --- Employee mint rate ------------------------------------------------------------
    # Late-stage tech: 3-7%/yr is textbook. Prior centered at 0.06 with wide log-sigma.
    annual_mint_rate_mature_mu: float = 0.06
    annual_mint_rate_mature_log_sigma: float = 0.5

    # --- Tender price discount ---------------------------------------------------------
    # Observed in the deployment config as `tender_price_log_discount_mu = -0.02`. Used
    # in the price likelihood: log P_tender = log V - log shares + tender_discount + noise.
    tender_price_log_discount_mu: float = -0.02

    # --- Hazard prior (Gamma) ----------------------------------------------------------
    # Conjugate Gamma prior on monthly_hazard. Posterior is Gamma(alpha+n_events,
    # beta+total_months). Defaults: alpha=1.0 (weakly informative), beta=18 (prior mean
    # = 1/18 ≈ one round per 18 months).
    hazard_prior_alpha: float = 1.0
    hazard_prior_beta: float = 18.0


@dataclass(frozen=True)
class PrimaryRoundEvent:
    """A primary-round event derived from a `valuation_observation` with valuation_kind='primary'.

    `month_idx_from_origin` is the rounded month index from the fit window's origin.
    `log_v_post_obs` and `log_v_post_sigma` are the observed log post-money valuation and
    its uncertainty. `cash_raised_usd` is the cash injection used to compute V_pre = V_post -
    cash; cash/V_pre is the observed round-size ratio whose distribution the model fits.
    """

    month_idx_from_origin: int
    log_v_post_obs: float
    log_v_post_sigma: float
    cash_raised_usd: float


@dataclass(frozen=True)
class BayesianMintStreamsPosterior:
    """Posterior summary mapping onto the issuer config knobs.

    `monthly_hazard` and `monthly_hazard_log_sigma` come from a closed-form Gamma-Poisson
    posterior over the observed event count, not from NUTS. The rest is NUTS posterior means
    (and the SD-of-log for honest per-rollout dispersion).
    """

    # Primary-round event stream
    monthly_hazard: float
    monthly_hazard_posterior_alpha: float
    monthly_hazard_posterior_beta: float
    cash_over_v_pre_median: float
    cash_over_v_pre_log_sigma: float

    # Employee mint
    annual_mint_rate_mature: float
    annual_mint_rate_log_sigma: float

    # V drift + vol (reversion shape FIXED at prior centers in default mode; reported anyway
    # so the deploy config is complete).
    valuation_monthly_log_return_mu: float  # mu_mature
    valuation_drift_mu_young: float
    valuation_drift_log_value_onset_usd: float
    valuation_drift_log_value_scale: float
    valuation_monthly_log_return_sigma: float

    # Cap-table anchors
    shares0: float
    v0_usd: float

    # Counts and diagnostics
    n_primary_events: int
    n_secondary_observations: int
    n_price_observations: int
    observation_window_months: float
    num_divergences: int


def _generative(
    *,
    primary_event_month_idx: jnp.ndarray,
    primary_log_v_post_obs: jnp.ndarray,
    primary_log_v_post_sigma: jnp.ndarray,
    primary_cash_usd: jnp.ndarray,
    secondary_month_idx: jnp.ndarray,
    secondary_log_v_obs: jnp.ndarray,
    secondary_log_v_sigma: jnp.ndarray,
    price_month_idx: jnp.ndarray,
    price_log_obs: jnp.ndarray,
    price_log_sigma: jnp.ndarray,
    n_months: int,
    priors: BayesianMintStreamsPriors,
) -> None:
    """numpyro model: latent log V(t) + log shares(t) on a dense monthly grid with deterministic
    primary-round jumps applied at observed event months.
    """

    log_v0 = numpyro.sample("log_v0", dist.Normal(priors.log_v0_usd, priors.log_v0_sigma))
    # Fixed-shape mode (the only mode supported in v1 — see module docstring).
    mu_mature = numpyro.deterministic("mu_mature", jnp.asarray(priors.mu_mature_mu))
    mu_young_excess = numpyro.deterministic(
        "mu_young_excess", jnp.asarray(priors.mu_young_excess_sigma * math.sqrt(2.0 / math.pi))
    )
    log_value_onset = numpyro.deterministic("log_value_onset", jnp.asarray(priors.log_value_onset_mu))
    log_value_scale = numpyro.deterministic("log_value_scale", jnp.asarray(priors.log_value_scale_mu))
    sigma_v = numpyro.sample("sigma_v", dist.LogNormal(jnp.log(priors.sigma_v_mu), priors.sigma_v_log_sigma))
    log_shares0 = numpyro.sample("log_shares0", dist.Normal(priors.log_shares0, priors.log_shares0_sigma))

    log_cash_over_v_pre_median = numpyro.sample(
        "log_cash_over_v_pre_median",
        dist.Normal(jnp.log(priors.cash_over_v_pre_median_mu), priors.log_cash_over_v_pre_sigma_prior),
    )
    numpyro.deterministic("cash_over_v_pre_median", jnp.exp(log_cash_over_v_pre_median))
    cash_over_v_pre_log_sigma = numpyro.sample(
        "cash_over_v_pre_log_sigma", dist.HalfNormal(priors.cash_over_v_pre_log_sigma_prior_scale)
    )

    annual_mint_rate_mature = numpyro.sample(
        "annual_mint_rate_mature",
        dist.LogNormal(jnp.log(priors.annual_mint_rate_mature_mu), priors.annual_mint_rate_mature_log_sigma),
    )
    # Smooth per-month log share growth from continuous employee mint, dS/dt = m * S →
    # log shares grows by log(1 + m) / 12 per month between events.
    monthly_mint_log = jnp.log1p(annual_mint_rate_mature) / _MONTHS_PER_YEAR

    # Non-centered per-month V shocks.
    z = numpyro.sample("z", dist.Normal(jnp.zeros(n_months), 1.0).to_event(1))
    shocks = sigma_v * z

    # Build a boolean mask of primary-event months on the dense grid (length n_months+1
    # to align with the grid index, but the scan only iterates over the n_months transitions).
    is_primary_event = jnp.zeros(n_months + 1, dtype=jnp.bool_).at[primary_event_month_idx].set(True)
    # Cash deposit per month (zero where no event). cash[m] is applied AFTER the V-RW step
    # transitions log_v from month m-1 to month m, so the post-jump log_v at the event
    # month equals log(V_pre_at_event + cash_at_event).
    cash_per_month = jnp.zeros(n_months + 1).at[primary_event_month_idx].set(primary_cash_usd)

    def _step(carry, inputs):
        log_v_prev, log_shares_prev = carry
        shock, has_event, cash_at_month = inputs
        # V random walk between events: scale-reverting drift evaluated at log_v at start of step.
        over_onset = jnp.maximum(0.0, log_v_prev - log_value_onset)
        drift_v = mu_mature + mu_young_excess * jnp.exp(-over_onset / log_value_scale)
        log_v_pre = log_v_prev + drift_v + shock
        # Smooth mint between events.
        log_shares_pre = log_shares_prev + monthly_mint_log
        # If this month is a primary event, apply the V jump and the share jump.
        # V_post = V_pre + cash → log V_post = log(V_pre + cash). For numerical stability:
        # log(V_pre + cash) = log_v_pre + log1p(cash / V_pre) = log_v_pre + log1p(exp(log_cash - log_v_pre)).
        log_cash = jnp.log(jnp.maximum(cash_at_month, 1e-12))  # avoid log(0) when no event
        delta_log = jnp.log1p(jnp.exp(log_cash - log_v_pre))
        log_v_next = jnp.where(has_event, log_v_pre + delta_log, log_v_pre)
        # shares_post / shares_pre = 1 + cash/V_pre = exp(delta_log) (matching V jump factor).
        log_shares_next = jnp.where(has_event, log_shares_pre + delta_log, log_shares_pre)
        return (log_v_next, log_shares_next), (log_v_pre, log_v_next, log_shares_next)

    log_v0_arr = jnp.asarray(log_v0)
    log_shares0_arr = jnp.asarray(log_shares0)
    inputs = (shocks, is_primary_event[1:], cash_per_month[1:])
    _, (log_v_pre_path, log_v_path, log_shares_path) = jax.lax.scan(_step, (log_v0_arr, log_shares0_arr), inputs)
    # Dense grid: index 0 = month 0 (pre any step).
    log_v_grid = jnp.concatenate([log_v0_arr[None], log_v_path])
    log_shares_grid = jnp.concatenate([log_shares0_arr[None], log_shares_path])
    # Pre-jump log V at each step (i.e. the V_pre seen by the round at that month). At
    # non-event months this equals log_v_grid. We index this at event months below.
    log_v_pre_grid = jnp.concatenate([log_v0_arr[None], log_v_pre_path])

    # Likelihood 1 — primary-round post-money observations: log V_post_obs ~ Normal(log V_grid_at_event).
    log_v_at_primary = log_v_grid[primary_event_month_idx]
    numpyro.sample(
        "primary_v_post_obs", dist.Normal(log_v_at_primary, primary_log_v_post_sigma), obs=primary_log_v_post_obs
    )
    # Likelihood 2 — cash/V_pre ratio per primary round: log(cash/V_pre) ~ Normal(log_median, log_sigma).
    log_v_pre_at_primary = log_v_pre_grid[primary_event_month_idx]
    log_cash_over_v_pre_obs = jnp.log(primary_cash_usd) - log_v_pre_at_primary
    numpyro.sample(
        "cash_over_v_pre_obs",
        dist.Normal(log_cash_over_v_pre_median, cash_over_v_pre_log_sigma),
        obs=log_cash_over_v_pre_obs,
    )
    # Likelihood 3 — secondary observations are noisy V(t) (no jump).
    if secondary_month_idx.size > 0:
        log_v_at_secondary = log_v_grid[secondary_month_idx]
        numpyro.sample(
            "secondary_v_obs", dist.Normal(log_v_at_secondary, secondary_log_v_sigma), obs=secondary_log_v_obs
        )
    # Likelihood 4 — price observations: log P = log V - log shares + tender_discount + noise.
    if price_month_idx.size > 0:
        log_v_at_price = log_v_grid[price_month_idx]
        log_shares_at_price = log_shares_grid[price_month_idx]
        log_price_model = log_v_at_price - log_shares_at_price + priors.tender_price_log_discount_mu
        numpyro.sample("price_obs", dist.Normal(log_price_model, price_log_sigma), obs=price_log_obs)


def _months_since(observed_at: dt.date, origin: dt.date) -> float:
    return (observed_at - origin).days / _DAYS_PER_MONTH


def fit_bayesian_mint_streams_prior(
    prices: list[PriceObservation],
    valuations: list[ValuationObservation],
    *,
    priors: BayesianMintStreamsPriors | None = None,
    num_warmup: int = 1500,
    num_samples: int = 3000,
    num_chains: int = 2,
    seed: int = 0,
) -> BayesianMintStreamsPosterior:
    """Fit the mint-streams Bayesian model via NUTS.

    Splits valuation observations into `primary` (event jumps) and `secondary`/`implied`/`admin`
    (noisy V(t) without jumps). Hazard posterior is closed-form Gamma-Poisson over the realized
    primary-event count vs the observation window length, NOT MCMC-sampled. Returns a
    `BayesianMintStreamsPosterior` mapping onto the `primary_round_config` + `employee_mint_config`
    knobs of `PrivateEquityRiskIssuerConfig`.
    """

    priors = priors or BayesianMintStreamsPriors()
    primaries = [v for v in valuations if v.valuation_kind == "primary"]
    non_primary_valuations = [v for v in valuations if v.valuation_kind != "primary"]
    if len(primaries) < 2:
        raise ValueError(
            f"mint-streams fit needs >= 2 primary valuation_observations (round events) to "
            f"identify the V jump dynamics; got {len(primaries)}"
        )
    if len(prices) < 2:
        raise ValueError(f"mint-streams fit needs >= 2 price observations; got {len(prices)}")

    # Window origin = earliest observation date across all sources.
    all_dates = [obs.observed_at for obs in prices] + [obs.observed_at for obs in valuations]
    origin = min(all_dates)
    last_date = max(all_dates)
    observation_window_months = _months_since(last_date, origin)
    n_months = round(observation_window_months)

    primary_event_month_idx = np.array([round(_months_since(v.observed_at, origin)) for v in primaries], dtype=np.int64)
    primary_event_month_idx = np.clip(primary_event_month_idx, 1, n_months)  # avoid event-at-t=0
    primary_log_v_post_obs = np.array([math.log(v.valuation_usd) for v in primaries], dtype=np.float64)
    primary_log_v_post_sigma = np.array([v.uncertainty_log_sigma for v in primaries], dtype=np.float64)
    # cash_raised_usd is guaranteed set for primary observations by the model_validator.
    primary_cash_usd = np.array([v.cash_raised_usd for v in primaries], dtype=np.float64)

    secondary_month_idx = np.array(
        [round(_months_since(v.observed_at, origin)) for v in non_primary_valuations], dtype=np.int64
    )
    secondary_month_idx = np.clip(secondary_month_idx, 0, n_months)
    secondary_log_v_obs = np.array([math.log(v.valuation_usd) for v in non_primary_valuations], dtype=np.float64)
    secondary_log_v_sigma = np.array([v.uncertainty_log_sigma for v in non_primary_valuations], dtype=np.float64)

    price_month_idx = np.array([round(_months_since(p.observed_at, origin)) for p in prices], dtype=np.int64)
    price_month_idx = np.clip(price_month_idx, 0, n_months)
    price_log_obs = np.array([math.log(p.price_usd_per_share) for p in prices], dtype=np.float64)
    price_log_sigma = np.array([p.uncertainty_log_sigma for p in prices], dtype=np.float64)

    mcmc = MCMC(
        NUTS(_generative, target_accept_prob=0.95),
        num_warmup=num_warmup,
        num_samples=num_samples,
        num_chains=num_chains,
        progress_bar=False,
    )
    mcmc.run(
        jax.random.PRNGKey(seed),
        primary_event_month_idx=jnp.asarray(primary_event_month_idx),
        primary_log_v_post_obs=jnp.asarray(primary_log_v_post_obs),
        primary_log_v_post_sigma=jnp.asarray(primary_log_v_post_sigma),
        primary_cash_usd=jnp.asarray(primary_cash_usd),
        secondary_month_idx=jnp.asarray(secondary_month_idx),
        secondary_log_v_obs=jnp.asarray(secondary_log_v_obs),
        secondary_log_v_sigma=jnp.asarray(secondary_log_v_sigma),
        price_month_idx=jnp.asarray(price_month_idx),
        price_log_obs=jnp.asarray(price_log_obs),
        price_log_sigma=jnp.asarray(price_log_sigma),
        n_months=n_months,
        priors=priors,
        extra_fields=("diverging",),
    )
    samples = mcmc.get_samples()
    extra = mcmc.get_extra_fields()
    num_divergences = int(np.sum(np.asarray(extra["diverging"]))) if "diverging" in extra else 0

    # Hazard posterior: closed-form Gamma-Poisson. Posterior is Gamma(alpha + n_events,
    # beta + observation_window_months). Posterior mean = (alpha + n) / (beta + T).
    posterior_alpha = priors.hazard_prior_alpha + len(primaries)
    posterior_beta = priors.hazard_prior_beta + observation_window_months
    monthly_hazard_posterior_mean = posterior_alpha / posterior_beta

    cash_median_samples = np.asarray(samples["cash_over_v_pre_median"])
    cash_log_sigma_samples = np.asarray(samples["cash_over_v_pre_log_sigma"])
    mint_rate_samples = np.asarray(samples["annual_mint_rate_mature"])

    return BayesianMintStreamsPosterior(
        monthly_hazard=float(monthly_hazard_posterior_mean),
        monthly_hazard_posterior_alpha=float(posterior_alpha),
        monthly_hazard_posterior_beta=float(posterior_beta),
        cash_over_v_pre_median=float(np.mean(cash_median_samples)),
        cash_over_v_pre_log_sigma=float(np.mean(cash_log_sigma_samples)),
        annual_mint_rate_mature=float(np.mean(mint_rate_samples)),
        annual_mint_rate_log_sigma=float(np.std(np.log(mint_rate_samples))),
        valuation_monthly_log_return_mu=float(np.mean(np.asarray(samples["mu_mature"]))),
        valuation_drift_mu_young=float(
            np.mean(np.asarray(samples["mu_mature"]) + np.asarray(samples["mu_young_excess"]))
        ),
        valuation_drift_log_value_onset_usd=float(np.mean(np.asarray(samples["log_value_onset"]))),
        valuation_drift_log_value_scale=float(np.mean(np.asarray(samples["log_value_scale"]))),
        valuation_monthly_log_return_sigma=float(np.mean(np.asarray(samples["sigma_v"]))),
        shares0=float(np.exp(np.mean(np.asarray(samples["log_shares0"])))),
        v0_usd=float(np.exp(np.mean(np.asarray(samples["log_v0"])))),
        n_primary_events=len(primaries),
        n_secondary_observations=len(non_primary_valuations),
        n_price_observations=len(prices),
        observation_window_months=observation_window_months,
        num_divergences=num_divergences,
    )

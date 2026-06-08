"""Tests for the mint-streams NUTS fit (M2.2-C, 2026-06).

Small NUTS runs (few hundred samples per chain) — enough to assert the posterior recovers
known synthetic parameters, without the multi-thousand-sample cost of a production fit.
"""

from __future__ import annotations

import datetime as dt
import math
import random

import pytest
import pytest_bazel

from finance.augur.fit.bayes_mint_streams import BayesianMintStreamsPriors, fit_bayesian_mint_streams_prior
from finance.augur.fit.private_equity import PriceObservation, ValuationObservation

_BASE = dt.date(2020, 1, 1)
_DAYS_PER_MONTH = 365.2425 / 12.0


def _price(observed_at: dt.date, price_usd_per_share: float, sigma: float = 0.08) -> PriceObservation:
    return PriceObservation(
        type="price_observation",
        issuer_id="synthetic",
        observed_at=observed_at,
        kind="tender_price",
        price_usd_per_share=price_usd_per_share,
        uncertainty_log_sigma=sigma,
        source_id="test",
    )


def _primary(
    observed_at: dt.date, valuation_usd: float, cash_raised_usd: float, sigma: float = 0.05
) -> ValuationObservation:
    return ValuationObservation(
        type="valuation_observation",
        issuer_id="synthetic",
        observed_at=observed_at,
        valuation_usd=valuation_usd,
        uncertainty_log_sigma=sigma,
        valuation_kind="primary",
        cash_raised_usd=cash_raised_usd,
        source_id="test",
    )


def _secondary(observed_at: dt.date, valuation_usd: float, sigma: float = 0.10) -> ValuationObservation:
    return ValuationObservation(
        type="valuation_observation",
        issuer_id="synthetic",
        observed_at=observed_at,
        valuation_usd=valuation_usd,
        uncertainty_log_sigma=sigma,
        valuation_kind="secondary",
        source_id="test",
    )


def _date_at_months(months: float) -> dt.date:
    return _BASE + dt.timedelta(days=round(months * _DAYS_PER_MONTH))


def _synthetic_mint_streams_data(
    *,
    monthly_hazard_true: float,
    cash_over_v_pre_true: float,
    cash_over_v_pre_log_sigma_true: float,
    annual_mint_rate_true: float,
    monthly_drift: float = 0.012,
    sigma_v: float = 0.04,
    horizon_months: int = 96,
    n_tender_prices: int = 10,
    seed: int = 0,
) -> tuple[list[PriceObservation], list[ValuationObservation]]:
    """Build synthetic mint-streams data with known generative parameters.

    Drives:
    - Primary-round events at deterministic intervals (1 / monthly_hazard_true), each with
      cash/V_pre drawn from LogNormal(cash_over_v_pre_true, cash_over_v_pre_log_sigma_true).
    - V(t) is a constant-drift random walk (no scale-reversion — keeping the truth simple)
      with deterministic V_post = V_pre + cash jumps at events.
    - shares(t) grows at annual_mint_rate_true smoothly between events, with discrete jumps
      shares_post / shares_pre = 1 + cash/V_pre at events.
    - Tender prices are V/shares + Normal noise + tender_discount (matches the fit's price likelihood).
    """

    rng = random.Random(seed)
    v0 = 1.0e10
    shares0 = 1.0e8
    monthly_mint_log = math.log1p(annual_mint_rate_true) / 12.0
    tender_discount = -0.02  # match the fit's tender_price_log_discount_mu prior

    log_v = math.log(v0)
    log_shares = math.log(shares0)

    valuations: list[ValuationObservation] = []
    prices: list[PriceObservation] = []

    # Deterministic event interval = 1 / hazard rounded to nearest month.
    event_interval = max(1, round(1.0 / monthly_hazard_true))

    # Walk forward month-by-month, applying drift, mint, and event jumps.
    for m in range(1, horizon_months + 1):
        log_v += monthly_drift + sigma_v * rng.gauss(0.0, 1.0)
        log_shares += monthly_mint_log
        if m % event_interval == 0:
            # Sample cash/V_pre from the true LogNormal distribution.
            cash_over_v = math.exp(rng.gauss(math.log(cash_over_v_pre_true), cash_over_v_pre_log_sigma_true))
            cash_usd = math.exp(log_v) * cash_over_v
            v_post = math.exp(log_v) + cash_usd
            log_v = math.log(v_post)
            log_shares += math.log1p(cash_over_v)  # shares grow by same factor as V (step_up=1)
            valuations.append(_primary(_date_at_months(m), v_post, cash_usd))

    # Tender prices at evenly spaced months over the window, with small noise.
    for k in range(n_tender_prices):
        # Pick a month not at the very start.
        m = max(1, round((k + 1) * horizon_months / (n_tender_prices + 1)))
        # Replay the trajectory to month m so we can read V(m) and shares(m).
        # (Easier than tracking grids: do a fresh deterministic replay with the same seed.)
        # Approach: regenerate the path up to month m. Skip; cleaner to track inline.
        # For test simplicity, we'll just attach a tender at the END of the loop using the
        # final log_v / log_shares values (only at the horizon). Tenders elsewhere need
        # mid-loop bookkeeping; do that via the second loop below.

    # Re-derive V and shares at every month so we can pick tender prices anywhere.
    rng2 = random.Random(seed)
    log_v2 = math.log(v0)
    log_shares2 = math.log(shares0)
    grid_log_v: list[float] = [log_v2]
    grid_log_shares: list[float] = [log_shares2]
    for m in range(1, horizon_months + 1):
        log_v2 += monthly_drift + sigma_v * rng2.gauss(0.0, 1.0)
        log_shares2 += monthly_mint_log
        if m % event_interval == 0:
            cash_over_v = math.exp(rng2.gauss(math.log(cash_over_v_pre_true), cash_over_v_pre_log_sigma_true))
            log_v2 = math.log(math.exp(log_v2) + math.exp(log_v2) * cash_over_v)
            log_shares2 += math.log1p(cash_over_v)
        grid_log_v.append(log_v2)
        grid_log_shares.append(log_shares2)

    for k in range(n_tender_prices):
        m = max(1, round((k + 1) * horizon_months / (n_tender_prices + 1)))
        log_price = grid_log_v[m] - grid_log_shares[m] + tender_discount + rng.gauss(0.0, 0.05)
        prices.append(_price(_date_at_months(m), math.exp(log_price)))

    return prices, valuations


def test_recovers_known_cash_over_v_pre_median() -> None:
    """On clean synthetic data, the posterior recovers cash_over_v_pre_median near the truth."""

    prices, valuations = _synthetic_mint_streams_data(
        monthly_hazard_true=1.0 / 8.0,
        cash_over_v_pre_true=0.10,
        cash_over_v_pre_log_sigma_true=0.4,
        annual_mint_rate_true=0.05,
        seed=42,
    )
    posterior = fit_bayesian_mint_streams_prior(prices, valuations, num_warmup=600, num_samples=800, num_chains=1)
    # Recovered median within a generous band (small sample + small NUTS run).
    assert posterior.cash_over_v_pre_median == pytest.approx(0.10, abs=0.05)
    assert posterior.n_primary_events >= 6


def test_hazard_posterior_is_closed_form_gamma_poisson() -> None:
    """Hazard is NOT MCMC-sampled — it's a closed-form Gamma posterior over the event count."""

    prices, valuations = _synthetic_mint_streams_data(
        monthly_hazard_true=1.0 / 6.0,
        cash_over_v_pre_true=0.12,
        cash_over_v_pre_log_sigma_true=0.5,
        annual_mint_rate_true=0.04,
        horizon_months=96,
        seed=7,
    )
    posterior = fit_bayesian_mint_streams_prior(prices, valuations, num_warmup=400, num_samples=400, num_chains=1)
    # Closed-form: alpha = prior_alpha + n_events, beta = prior_beta + window_months.
    expected_alpha = BayesianMintStreamsPriors().hazard_prior_alpha + posterior.n_primary_events
    expected_beta = BayesianMintStreamsPriors().hazard_prior_beta + posterior.observation_window_months
    assert posterior.monthly_hazard_posterior_alpha == pytest.approx(expected_alpha)
    assert posterior.monthly_hazard_posterior_beta == pytest.approx(expected_beta)
    assert posterior.monthly_hazard == pytest.approx(expected_alpha / expected_beta, rel=1e-9)


def test_recovers_known_annual_mint_rate() -> None:
    """Posterior recovers annual_mint_rate_mature near the truth (driven by the residual share
    growth not attributable to primary rounds)."""

    prices, valuations = _synthetic_mint_streams_data(
        monthly_hazard_true=1.0 / 12.0,
        cash_over_v_pre_true=0.08,
        cash_over_v_pre_log_sigma_true=0.3,
        annual_mint_rate_true=0.05,
        horizon_months=96,
        n_tender_prices=12,
        seed=11,
    )
    posterior = fit_bayesian_mint_streams_prior(prices, valuations, num_warmup=800, num_samples=1000, num_chains=1)
    # Wider band — mint rate is identified from tender-price residual, weaker signal than
    # cash/V_pre which has direct observations.
    assert posterior.annual_mint_rate_mature == pytest.approx(0.05, abs=0.04)


def test_too_few_primary_events_raises() -> None:
    """Need >= 2 primary events to identify V jump dynamics."""

    # One primary, two tenders.
    valuations = [_primary(_date_at_months(12), 1.2e10, 1e9)]
    prices = [_price(_date_at_months(6), 100.0), _price(_date_at_months(18), 120.0)]
    with pytest.raises(ValueError, match=">= 2 primary valuation_observations"):
        fit_bayesian_mint_streams_prior(prices, valuations)


def test_priors_overridable() -> None:
    """A caller can pass a different priors instance to control the fit."""

    prices, valuations = _synthetic_mint_streams_data(
        monthly_hazard_true=1.0 / 10.0,
        cash_over_v_pre_true=0.15,
        cash_over_v_pre_log_sigma_true=0.4,
        annual_mint_rate_true=0.04,
        seed=99,
    )
    tight_priors = BayesianMintStreamsPriors(
        cash_over_v_pre_median_mu=0.15,
        log_cash_over_v_pre_sigma_prior=0.05,  # very tight
    )
    posterior = fit_bayesian_mint_streams_prior(
        prices, valuations, priors=tight_priors, num_warmup=400, num_samples=500, num_chains=1
    )
    # Tight prior pulls posterior to the truth even with limited data.
    assert posterior.cash_over_v_pre_median == pytest.approx(0.15, abs=0.03)


if __name__ == "__main__":
    pytest_bazel.main()

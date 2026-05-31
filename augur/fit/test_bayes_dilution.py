"""Tests for the M2.2-D Bayesian dilution + decaying-drift fit.

These are small NUTS runs (few hundred samples) -- enough to assert the posterior recovers a
known synthetic rate and respects the informative priors, without the multi-thousand-sample
cost of a production fit.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest
import pytest_bazel

from augur.fit.bayes_dilution import BayesianDilutionPriors, fit_bayesian_dilution_prior
from augur.fit.private_equity import PriceObservation, ValuationObservation

_BASE = dt.date(2020, 1, 1)


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


def _valuation(observed_at: dt.date, valuation_usd: float, sigma: float = 0.08) -> ValuationObservation:
    return ValuationObservation(
        type="valuation_observation",
        issuer_id="synthetic",
        observed_at=observed_at,
        valuation_usd=valuation_usd,
        uncertainty_log_sigma=sigma,
        source_id="test",
    )


def _synthetic(*, r_true: float, monthly_drift: float, n: int = 8, months_step: int = 6):
    """Paired observations on a clean exponential value path with known dilution.

    V(t) = V0 * exp(monthly_drift * t); shares(t) = shares0 * (1 + r_true) ** (t/12);
    price(t) = V(t) / shares(t). Both channels observed at every step so the fit is identified.
    """

    v0, shares0 = 2.8e10, 4.0e8
    prices, valuations = [], []
    for k in range(n):
        observed_at = _BASE + dt.timedelta(days=round(k * months_step * 365.2425 / 12.0))
        t = k * months_step
        v = v0 * math.exp(monthly_drift * t)
        shares = shares0 * (1.0 + r_true) ** (t / 12.0)
        valuations.append(_valuation(observed_at, v))
        prices.append(_price(observed_at, v / shares))
    return prices, valuations


def test_recovers_known_dilution_rate() -> None:
    """On clean synthetic data with a modest drift (inside the prior), the posterior-mean rate
    lands near the truth."""

    prices, valuations = _synthetic(r_true=0.25, monthly_drift=0.02, n=8)
    prior = fit_bayesian_dilution_prior(prices, valuations, num_warmup=600, num_samples=800, num_chains=1)
    assert prior.annual_dilution_rate == pytest.approx(0.25, abs=0.08)
    # A real posterior dispersion is reported (honest uncertainty), and it is finite/positive.
    assert prior.annual_dilution_rate_log_sigma > 0.0
    # Divergences are a soft sampler diagnostic; a small fraction (funnel geometry of the
    # non-centered RW) doesn't invalidate the fit, but a large fraction would. Allow < 5%.
    assert prior.num_divergences < 0.05 * 800


def test_decaying_drift_regularizes_a_hot_boom() -> None:
    """A boom hotter than the long-run prior is absorbed as NEAR-TERM excess (mu_0 > mu_inf),
    with mu_inf held near the informative prior -- the whole point of M2.2-D vs the OLS fit."""

    # ~8%/mo (~150%/yr) observed growth -- well above the mu_inf prior center of 0.02.
    prices, valuations = _synthetic(r_true=0.20, monthly_drift=0.08, n=8)
    prior = fit_bayesian_dilution_prior(prices, valuations, num_warmup=800, num_samples=1000, num_chains=1)
    # Long-run drift stays anchored near the prior (does NOT chase the boom).
    assert prior.valuation_monthly_log_return_mu < 0.06
    # Near-term drift is higher than long-run (the boom shows up as decaying excess).
    assert prior.valuation_monthly_log_return_mu_initial > prior.valuation_monthly_log_return_mu
    assert prior.valuation_drift_decay_halflife_months > 0.0


def test_priors_are_overridable() -> None:
    """A caller can pass tighter/looser priors (per-issuer reference class)."""

    prices, valuations = _synthetic(r_true=0.15, monthly_drift=0.02, n=6)
    tight = BayesianDilutionPriors(annual_dilution_rate_mu=0.15, log1p_r_sigma=0.05)
    prior = fit_bayesian_dilution_prior(prices, valuations, priors=tight, num_warmup=500, num_samples=600, num_chains=1)
    # With a tight prior centered at the truth, the posterior stays close to it.
    assert prior.annual_dilution_rate == pytest.approx(0.15, abs=0.05)


def test_too_few_observations_raises() -> None:
    one_price = [_price(_BASE, 10.0)]
    one_val = [_valuation(_BASE, 1.0e10)]
    with pytest.raises(ValueError, match="needs >= 2 price and >= 2 valuation"):
        fit_bayesian_dilution_prior(one_price, one_val)


if __name__ == "__main__":
    pytest_bazel.main()

"""Tests for the M2.2-A implied-shares dilution-prior fit."""

from __future__ import annotations

import datetime as dt
import math
import random

import pytest
import pytest_bazel

from finance.augur.fit.dilution_prior import fit_dilution_prior
from finance.augur.fit.private_equity import PriceObservation, ValuationObservation

_BASE_DATE = dt.date(2020, 1, 1)


def _price(observed_at: dt.date, price_usd_per_share: float) -> PriceObservation:
    return PriceObservation(
        type="price_observation",
        issuer_id="synthetic",
        observed_at=observed_at,
        kind="tender_price",
        price_usd_per_share=price_usd_per_share,
        uncertainty_log_sigma=0.1,
        source_id="test",
    )


def _valuation(observed_at: dt.date, valuation_usd: float) -> ValuationObservation:
    return ValuationObservation(
        type="valuation_observation",
        issuer_id="synthetic",
        observed_at=observed_at,
        valuation_usd=valuation_usd,
        uncertainty_log_sigma=0.15,
        valuation_kind="implied",
        source_id="test",
    )


def _synthetic_pairs(
    *, r_true: float, noise_log_sigma: float, n_points: int = 6, months_between: int = 8, seed: int = 0
) -> tuple[list[PriceObservation], list[ValuationObservation]]:
    """Build paired (price, valuation) observations whose implied shares follow

        shares(t) = shares0 * (1 + r_true) ** (t / 12)   (t in months)

    with multiplicative log-noise. Valuation is held constant at V0 so that
    `implied_shares = V0 / price_usd_per_share` reproduces shares(t); injecting noise on
    shares is equivalent to scatter on log(implied_shares) about the fit line.
    """

    rng = random.Random(seed)
    shares0 = 1_000_000.0
    v0 = 1_000_000_000.0
    prices: list[PriceObservation] = []
    valuations: list[ValuationObservation] = []
    for k in range(n_points):
        observed_at = _BASE_DATE + dt.timedelta(days=round(k * months_between * 365.25 / 12.0))
        # Grow shares over the SAME day-count the fit measures time on (whole-day date delta
        # over 365.25), not idealized integer months -- otherwise the day-rounding leaves
        # zero-noise points slightly off the OLS line and exact rate recovery fails.
        delta_years = (observed_at - _BASE_DATE).days / 365.25
        shares = shares0 * (1.0 + r_true) ** delta_years * math.exp(rng.gauss(0.0, noise_log_sigma))
        prices.append(_price(observed_at, v0 / shares))
        valuations.append(_valuation(observed_at, v0))
    return prices, valuations


def test_recovers_known_rate_without_noise() -> None:
    prices, valuations = _synthetic_pairs(r_true=0.18, noise_log_sigma=0.0)
    prior = fit_dilution_prior(prices, valuations)
    assert prior.annual_dilution_rate == pytest.approx(0.18, abs=1e-9)
    # Zero-noise synthetic data lies exactly on the line => no dispersion.
    assert prior.residual_log_std == pytest.approx(0.0, abs=1e-9)
    assert prior.annual_dilution_rate_log_sigma == pytest.approx(0.0, abs=1e-9)


def test_recovers_known_rate_with_noise() -> None:
    # Even with scatter the OLS slope recovers the true rate to within a few points.
    prices, valuations = _synthetic_pairs(r_true=0.25, noise_log_sigma=0.04, n_points=8, seed=3)
    prior = fit_dilution_prior(prices, valuations)
    assert prior.annual_dilution_rate == pytest.approx(0.25, abs=0.03)


def test_sigma_increases_with_injected_noise() -> None:
    """More scatter about the fit line => a wider per-rollout dispersion estimate."""

    sigmas = []
    rates = []
    for noise in (0.0, 0.05, 0.15):
        prices, valuations = _synthetic_pairs(r_true=0.20, noise_log_sigma=noise, n_points=8, seed=7)
        prior = fit_dilution_prior(prices, valuations)
        sigmas.append(prior.annual_dilution_rate_log_sigma)
        rates.append(prior.annual_dilution_rate)
    # Degenerate at zero noise, then monotonically increasing dispersion with injected noise.
    assert sigmas[0] == pytest.approx(0.0, abs=1e-9)
    assert sigmas[0] < sigmas[1] < sigmas[2]
    # The central rate stays near the truth regardless of noise.
    for rate in rates:
        assert rate == pytest.approx(0.20, abs=0.05)


def test_implied_share_points_recorded_for_transparency() -> None:
    prices, valuations = _synthetic_pairs(r_true=0.10, noise_log_sigma=0.0, n_points=5)
    prior = fit_dilution_prior(prices, valuations)
    assert len(prior.implied_share_points) == 5
    first = prior.implied_share_points[0]
    # implied_shares == valuation / price, and delta_years measured from the first paired date.
    assert first.implied_shares == pytest.approx(first.valuation_usd / first.price_usd_per_share)
    assert first.delta_years == pytest.approx(0.0)
    assert prior.implied_share_points[-1].delta_years > 0.0


def test_refreshes_valuation_drift_and_vol() -> None:
    """The optional valuation drift/vol refresh reads only the valuation series."""

    prices, valuations = _synthetic_pairs(r_true=0.12, noise_log_sigma=0.0, n_points=6)
    # Give the valuation series a real upward drift so mu is positive and sigma defined.
    valuations = [_valuation(obs.observed_at, 1_000_000_000.0 * (1.02**k)) for k, obs in enumerate(valuations)]
    prior = fit_dilution_prior(prices, valuations, refresh_valuation_drift=True)
    assert prior.valuation_monthly_log_return_mu is not None
    assert prior.valuation_monthly_log_return_mu > 0.0
    assert prior.valuation_monthly_log_return_sigma is not None


def test_drift_refresh_can_be_disabled() -> None:
    prices, valuations = _synthetic_pairs(r_true=0.12, noise_log_sigma=0.0, n_points=6)
    prior = fit_dilution_prior(prices, valuations, refresh_valuation_drift=False)
    assert prior.valuation_monthly_log_return_mu is None
    assert prior.valuation_monthly_log_return_sigma is None


def test_fewer_than_two_paired_points_raises() -> None:
    # A single in-window pair cannot identify a slope.
    prices = [_price(_BASE_DATE, 10.0)]
    valuations = [_valuation(_BASE_DATE, 1_000_000_000.0)]
    with pytest.raises(ValueError, match="need >= 2 paired"):
        fit_dilution_prior(prices, valuations)


def test_unpaired_observations_are_dropped() -> None:
    """A price with no valuation inside the tolerance window is excluded from the fit."""

    prices, valuations = _synthetic_pairs(r_true=0.15, noise_log_sigma=0.0, n_points=5)
    # Add a stray price far from any valuation date; it must not enter the fit.
    prices = [*prices, _price(dt.date(2030, 1, 1), 5.0)]
    prior = fit_dilution_prior(prices, valuations, pairing_tolerance_days=31)
    assert len(prior.implied_share_points) == 5
    assert all(point.date.year < 2030 for point in prior.implied_share_points)


if __name__ == "__main__":
    pytest_bazel.main()

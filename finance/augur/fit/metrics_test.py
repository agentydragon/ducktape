"""Metric battery exercised against synthetic, hand-checkable models.

The mock models implement `Scorable` (and stub `Sampler.sample` since
both protocols are required of the metric-battery argument type, even
though the test never invokes the sampling path). The closed-form
checks compare scorer output against direct numpy Gaussian log-density
calculations on the synthesised log-returns.
"""

from __future__ import annotations

import math

import jax.numpy as jnp
import numpy as np
import pytest
import pytest_bazel
from numpyro import distributions as dist

from finance.augur.fit.metrics import (
    ScoredHeldOutResult,
    ScoredMultiStepRow,
    ScoredRollingOriginResult,
    UnscoredHeldOutResult,
    UnscoredMultiStepRow,
    UnscoredRollingOriginResult,
    held_out_predictive_score,
    multi_step_predictive_score,
    rolling_origin_predictive_score,
)
from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle
from finance.augur.model.path_models.scenarios import HistoricalSeries, historical_log_returns
from finance.augur.model.series import (
    SP500_SYMBOL,
    HomeValueKey,
    InflationKey,
    IssuerId,
    LevelSeriesKey,
    LocationId,
    RentKey,
    SecurityKey,
    SecuritySymbol,
)

# Distinct synthetic level-series keys for the metric fixtures. Identities are
# arbitrary — these tests check numeric scoring, not series semantics — but they must be
# real typed keys now that factor identity is a LevelSeriesKey rather than an "f0"/"f1" string.
_SYNTHETIC_FACTOR_POOL: tuple[LevelSeriesKey, ...] = (
    SecurityKey(symbol=SP500_SYMBOL),
    InflationKey(),
    SecurityKey(symbol=SecuritySymbol("btc")),
    SecurityKey(symbol=SecuritySymbol("eth")),
    HomeValueKey(location_id=LocationId("san_francisco_ca")),
    RentKey(location_id=LocationId("san_francisco_ca")),
)


def _synthetic_factor_keys(n: int) -> tuple[LevelSeriesKey, ...]:
    if n > len(_SYNTHETIC_FACTOR_POOL):
        raise ValueError(f"metric fixtures support at most {len(_SYNTHETIC_FACTOR_POOL)} factors; got {n}")
    return _SYNTHETIC_FACTOR_POOL[:n]


def _gaussian_log_density(x: np.ndarray, mu: np.ndarray, sigma: np.ndarray) -> float:
    """Sum of independent Gaussian log-densities across factors."""
    return float(np.sum(-0.5 * ((x - mu) / sigma) ** 2 - np.log(sigma) - 0.5 * math.log(2 * math.pi)))


class _ConstantGaussianModel:
    """Synthetic model: each monthly log-return is iid Normal(mu, sigma) per
    factor, mu/sigma fixed at construction. `fit()` ignores its argument so
    the predictive is closed-form and independent of the train split —
    which makes it hand-checkable.

    Implements Fittable + Scorable + Sampler (Sampler.sample is a stub —
    the metric tests never invoke it)."""

    label = "constant_gaussian"

    def __init__(self, mu: np.ndarray, sigma: np.ndarray) -> None:
        self._mu = np.asarray(mu, dtype="float64")
        self._sigma = np.asarray(sigma, dtype="float64")
        self.factor_names: tuple[LevelSeriesKey, ...] = _synthetic_factor_keys(len(self._mu))

    def fit(self, historical: HistoricalSeries) -> None:
        del historical

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        return frozenset(self.factor_names)

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        return frozenset()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        raise NotImplementedError("metric-test fixture; sampling not exercised")

    def predictive(self, historical: HistoricalSeries, t: int, *, horizon: int = 1) -> dist.Distribution | None:
        # iid Δr ~ N(mu, σ²) → cumulative h-step is N(h·mu, h·σ²).
        del historical, t
        if horizon < 1:
            return None
        cum_mu = jnp.asarray(horizon * self._mu, dtype=jnp.float32)
        cum_var = horizon * self._sigma**2
        cov = jnp.diag(jnp.asarray(cum_var, dtype=jnp.float32))
        return dist.MultivariateNormal(cum_mu, covariance_matrix=cov)


class _UnscoredModel:
    """Model that returns None from predictive() — like a bootstrap."""

    label = "unscored"
    factor_names: tuple[LevelSeriesKey, ...] = (SecurityKey(symbol=SP500_SYMBOL),)

    def fit(self, historical: HistoricalSeries) -> None:
        del historical

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        return frozenset(self.factor_names)

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        return frozenset()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        raise NotImplementedError("metric-test fixture; sampling not exercised")

    def predictive(self, historical: HistoricalSeries, t: int, *, horizon: int = 1) -> dist.Distribution | None:
        del historical, t, horizon
        return None


def _toy_historical(n_steps: int, *, mu: np.ndarray, sigma: np.ndarray, seed: int) -> HistoricalSeries:
    rng = np.random.default_rng(seed)
    n_factors = len(mu)
    log_returns = rng.normal(loc=mu, scale=sigma, size=(n_steps, n_factors))
    levels = np.exp(np.concatenate([np.zeros((1, n_factors)), np.cumsum(log_returns, axis=0)], axis=0))
    months = tuple(f"2000-{i:02d}" for i in range(n_steps + 1))
    return HistoricalSeries(factor_names=_synthetic_factor_keys(n_factors), levels=levels, months=months)


class TestHeldOutPredictiveScore:
    def test_matches_closed_form_gaussian_on_held_out_window(self) -> None:
        mu = np.array([0.005, 0.003])
        sigma = np.array([0.04, 0.025])
        historical = _toy_historical(100, mu=mu, sigma=sigma, seed=1)
        model = _ConstantGaussianModel(mu=mu, sigma=sigma)

        result = held_out_predictive_score(model, historical, train_fraction=0.8)

        assert isinstance(result, ScoredHeldOutResult)
        assert result.model_label == "constant_gaussian"
        assert result.train_end == 80
        assert result.held_out_count == 20

        log_returns = historical_log_returns(historical)
        expected_total = float(sum(_gaussian_log_density(log_returns[t], mu, sigma) for t in range(80, 100)))
        # Scorer uses float32 in JAX so allow a tiny slack vs. closed-form float64.
        assert abs(result.joint_log_density_total - expected_total) < 1e-2
        assert abs(result.joint_log_density_per_month - expected_total / 20) < 1e-3

    def test_factor_marginals_sum_to_joint_for_diagonal_covariance(self) -> None:
        mu = np.array([0.005, -0.002, 0.001])
        sigma = np.array([0.04, 0.03, 0.015])
        historical = _toy_historical(60, mu=mu, sigma=sigma, seed=10)
        model = _ConstantGaussianModel(mu=mu, sigma=sigma)

        result = held_out_predictive_score(model, historical, train_fraction=0.5)

        assert isinstance(result, ScoredHeldOutResult)
        assert result.factor_breakdown is not None
        summed = sum(result.factor_breakdown.marginal_log_density_total.values())
        # Under diagonal covariance, joint = sum of marginals.
        assert abs(summed - result.joint_log_density_total) < 1e-2

        for name, total in result.factor_breakdown.marginal_log_density_total.items():
            per_month = result.factor_breakdown.marginal_log_density_per_month[name]
            assert abs(per_month - total / result.held_out_count) < 1e-6

    def test_unscored_model_records_reason_without_crashing(self) -> None:
        mu = np.array([0.0])
        sigma = np.array([0.01])
        historical = _toy_historical(20, mu=mu, sigma=sigma, seed=3)
        model = _UnscoredModel()

        result = held_out_predictive_score(model, historical, train_fraction=0.7)

        assert isinstance(result, UnscoredHeldOutResult)
        assert result.model_label == "unscored"
        assert "returned None" in result.unscored_reason

    def test_rejects_invalid_train_fraction(self) -> None:
        mu = np.array([0.0])
        sigma = np.array([0.01])
        historical = _toy_historical(20, mu=mu, sigma=sigma, seed=4)
        model = _ConstantGaussianModel(mu=mu, sigma=sigma)

        for bad in (0.0, 1.0, -0.1, 1.5):
            with pytest.raises(ValueError, match="train_fraction must be in"):
                held_out_predictive_score(model, historical, train_fraction=bad)

    def test_rejects_train_fraction_that_leaves_no_holdout(self) -> None:
        mu = np.array([0.0])
        sigma = np.array([0.01])
        historical = _toy_historical(3, mu=mu, sigma=sigma, seed=5)
        model = _ConstantGaussianModel(mu=mu, sigma=sigma)

        with pytest.raises(ValueError, match="no held-out months"):
            held_out_predictive_score(model, historical, train_fraction=0.99)


class TestRollingOriginPredictiveScore:
    def test_total_matches_closed_form_gaussian_over_all_origins(self) -> None:
        mu = np.array([0.005, 0.001])
        sigma = np.array([0.04, 0.02])
        historical = _toy_historical(50, mu=mu, sigma=sigma, seed=20)

        result = rolling_origin_predictive_score(
            lambda: _ConstantGaussianModel(mu=mu, sigma=sigma), historical, min_train=10, refit_every=1
        )

        log_returns = historical_log_returns(historical)
        expected_total = float(sum(_gaussian_log_density(log_returns[t], mu, sigma) for t in range(10, 50)))
        assert isinstance(result, ScoredRollingOriginResult)
        assert result.n_origins == 40
        assert abs(result.joint_log_density_total - expected_total) < 1e-2
        assert abs(result.joint_log_density_per_month - expected_total / 40) < 1e-3
        assert result.joint_log_density_mean_se > 0.0
        assert result.factor_breakdown is not None
        assert (
            abs(sum(result.factor_breakdown.marginal_log_density_total.values()) - result.joint_log_density_total)
            < 1e-2
        )

    def test_refit_every_skips_intermediate_fits(self) -> None:
        fit_calls = {"n": 0}

        def factory() -> _ConstantGaussianModel:
            class _CountingModel(_ConstantGaussianModel):
                def fit(self, historical: HistoricalSeries) -> None:
                    fit_calls["n"] += 1
                    super().fit(historical)

            return _CountingModel(mu=np.array([0.0]), sigma=np.array([0.01]))

        historical = _toy_historical(30, mu=np.array([0.0]), sigma=np.array([0.01]), seed=21)
        rolling_origin_predictive_score(factory, historical, min_train=10, refit_every=5)
        # Origins 10, 15, 20, 25 → 4 fits.
        assert fit_calls["n"] == 4

    def test_unscored_model_records_reason(self) -> None:
        historical = _toy_historical(20, mu=np.array([0.0]), sigma=np.array([0.01]), seed=22)
        result = rolling_origin_predictive_score(_UnscoredModel, historical, min_train=5, refit_every=1)
        assert isinstance(result, UnscoredRollingOriginResult)
        assert "returned None" in result.unscored_reason


class TestMultiStepPredictiveScore:
    def test_horizons_match_closed_form_for_iid_gaussian(self) -> None:
        mu = np.array([0.005, 0.001])
        sigma = np.array([0.04, 0.02])
        historical = _toy_historical(60, mu=mu, sigma=sigma, seed=30)
        model = _ConstantGaussianModel(mu=mu, sigma=sigma)

        result = multi_step_predictive_score(model, historical, horizons=(1, 6), train_fraction=0.5)

        assert result.horizons == (1, 6)
        log_returns = historical_log_returns(historical)
        train_end = round(60 * 0.5)
        for row in result.rows:
            assert isinstance(row, ScoredMultiStepRow)
            h = row.horizon_months
            n_origins_expected = (60 - h) - train_end + 1
            assert row.n_origins == n_origins_expected
            expected_total = 0.0
            for t in range(train_end, 60 - h + 1):
                observed = log_returns[t : t + h].sum(axis=0)
                cum_mu = h * mu
                cum_var = h * sigma**2
                diff = observed - cum_mu
                expected_total += float(np.sum(-0.5 * ((diff**2) / cum_var + np.log(cum_var) + math.log(2 * math.pi))))
            assert abs(row.joint_log_density_total - expected_total) < 1e-1

    def test_unscored_model_records_reason_per_horizon(self) -> None:
        historical = _toy_historical(20, mu=np.array([0.0]), sigma=np.array([0.01]), seed=31)
        result = multi_step_predictive_score(_UnscoredModel(), historical, horizons=(1, 3), train_fraction=0.5)
        for row in result.rows:
            assert isinstance(row, UnscoredMultiStepRow)


if __name__ == "__main__":
    pytest_bazel.main()

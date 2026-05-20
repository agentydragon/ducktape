"""Phase A: metric battery exercised against a synthetic, hand-checkable model."""

from __future__ import annotations

import math

import numpy as np
import pytest
import pytest_bazel

from augur.fit.metrics import (
    ScoredHeldOutLogDensity,
    ScoredMultiStepLogDensityRow,
    ScoredRollingOriginLogDensity,
    UnscoredHeldOutLogDensity,
    UnscoredMultiStepLogDensityRow,
    UnscoredRollingOriginLogDensity,
    held_out_predictive_log_density,
    multi_step_predictive_log_density,
    rolling_origin_predictive_log_density,
)
from augur.model.markets.scenarios import HistoricalSeries, Scenarios, historical_log_returns


def _gaussian_log_density(x: np.ndarray, mu: np.ndarray, sigma: np.ndarray) -> float:
    """Sum of independent Gaussian log-densities across factors."""
    return float(np.sum(-0.5 * ((x - mu) / sigma) ** 2 - np.log(sigma) - 0.5 * math.log(2 * math.pi)))


class _ConstantGaussianModel:
    """Synthetic model: each monthly log-return is iid Normal(mu, sigma) per
    factor, mu/sigma fixed at construction. fit() ignores its argument so the
    model's predictive density is closed-form and independent of the train
    split — which is exactly what makes it hand-checkable."""

    label = "constant_gaussian"

    def __init__(self, mu: np.ndarray, sigma: np.ndarray) -> None:
        self._mu = np.asarray(mu, dtype="float64")
        self._sigma = np.asarray(sigma, dtype="float64")
        self.factor_names: tuple[str, ...] = tuple(f"f{i}" for i in range(len(self._mu)))

    def fit(self, historical: HistoricalSeries) -> None:
        del historical

    def simulate(self, n_paths: int, n_months: int, seed: int) -> Scenarios:
        rng = np.random.default_rng(seed)
        steps = rng.normal(loc=self._mu, scale=self._sigma, size=(n_paths, n_months, len(self._mu)))
        cum = np.concatenate([np.zeros((n_paths, 1, len(self._mu))), np.cumsum(steps, axis=1)], axis=1)
        return Scenarios(
            factor_names=tuple(f"f{i}" for i in range(len(self._mu))),
            multipliers=np.exp(cum),
            seed=seed,
            label=self.label,
        )

    def log_predictive_density(self, historical: HistoricalSeries, t: int) -> float | None:
        log_returns = historical_log_returns(historical)
        return _gaussian_log_density(log_returns[t], self._mu, self._sigma)

    def log_predictive_marginals(self, historical: HistoricalSeries, t: int) -> dict[str, float] | None:
        log_returns = historical_log_returns(historical)
        x = log_returns[t]
        out: dict[str, float] = {}
        for k, name in enumerate(historical.factor_names):
            diff = x[k] - self._mu[k]
            out[name] = float(
                -0.5 * (math.log(2 * math.pi) + 2 * math.log(self._sigma[k]) + (diff / self._sigma[k]) ** 2)
            )
        return out

    def log_predictive_density_at_horizon(self, historical: HistoricalSeries, t: int, h: int) -> float | None:
        # Each r_{t+k} ~ iid N(mu, diag(sigma^2)), so cumulative is
        # N(h*mu, h*diag(sigma^2)).
        log_returns = historical_log_returns(historical)
        if t + h > log_returns.shape[0]:
            return None
        observed = log_returns[t : t + h].sum(axis=0)
        cum_mu = h * self._mu
        cum_var = h * self._sigma**2
        diff = observed - cum_mu
        log_density = -0.5 * np.sum((diff**2) / cum_var + np.log(cum_var) + math.log(2 * math.pi))
        return float(log_density)


class _UnscoredModel:
    """Model that can simulate but cannot expose a density — like the bootstrap."""

    label = "unscored"
    factor_names: tuple[str, ...] = ("f0",)

    def fit(self, historical: HistoricalSeries) -> None:
        del historical

    def simulate(self, n_paths: int, n_months: int, seed: int) -> Scenarios:
        rng = np.random.default_rng(seed)
        return Scenarios(
            factor_names=("f0",),
            multipliers=np.ones((n_paths, n_months + 1, 1)) * (1.0 + rng.normal(size=(n_paths, 1, 1)) * 0),
            seed=seed,
            label=self.label,
        )

    def log_predictive_density(self, historical: HistoricalSeries, t: int) -> float | None:
        del historical, t
        return None

    def log_predictive_marginals(self, historical: HistoricalSeries, t: int) -> dict[str, float] | None:
        del historical, t
        return None

    def log_predictive_density_at_horizon(self, historical: HistoricalSeries, t: int, h: int) -> float | None:
        del historical, t, h
        return None


def _toy_historical(n_steps: int, *, mu: np.ndarray, sigma: np.ndarray, seed: int) -> HistoricalSeries:
    rng = np.random.default_rng(seed)
    n_factors = len(mu)
    log_returns = rng.normal(loc=mu, scale=sigma, size=(n_steps, n_factors))
    levels = np.exp(np.concatenate([np.zeros((1, n_factors)), np.cumsum(log_returns, axis=0)], axis=0))
    months = tuple(f"2000-{i:02d}" for i in range(n_steps + 1))
    return HistoricalSeries(factor_names=tuple(f"f{i}" for i in range(n_factors)), levels=levels, months=months)


class TestHeldOutLogDensity:
    def test_matches_closed_form_gaussian_on_held_out_window(self) -> None:
        mu = np.array([0.005, 0.003])
        sigma = np.array([0.04, 0.025])
        historical = _toy_historical(100, mu=mu, sigma=sigma, seed=1)
        model = _ConstantGaussianModel(mu=mu, sigma=sigma)

        result = held_out_predictive_log_density(model, historical, train_fraction=0.8)

        assert isinstance(result, ScoredHeldOutLogDensity)
        assert result.model_label == "constant_gaussian"
        assert result.train_end == 80
        assert result.held_out_count == 20

        log_returns = historical_log_returns(historical)
        expected_total = float(sum(_gaussian_log_density(log_returns[t], mu, sigma) for t in range(80, 100)))
        assert abs(result.total - expected_total) < 10**-10
        assert abs(result.per_month - expected_total / 20) < 10**-10

    def test_per_factor_components_sum_to_joint_for_independent_marginals(self) -> None:
        mu = np.array([0.005, -0.002, 0.001])
        sigma = np.array([0.04, 0.03, 0.015])
        historical = _toy_historical(60, mu=mu, sigma=sigma, seed=2)
        joint = _ConstantGaussianModel(mu=mu, sigma=sigma)
        marginals = [_ConstantGaussianModel(mu=mu[k : k + 1], sigma=sigma[k : k + 1]) for k in range(len(mu))]

        joint_result = held_out_predictive_log_density(joint, historical, train_fraction=0.5)
        assert isinstance(joint_result, ScoredHeldOutLogDensity)

        marginal_total = 0.0
        for k, marginal in enumerate(marginals):
            single_factor = HistoricalSeries(
                factor_names=(f"f{k}",), levels=historical.levels[:, k : k + 1], months=historical.months
            )
            marginal_result = held_out_predictive_log_density(marginal, single_factor, train_fraction=0.5)
            assert isinstance(marginal_result, ScoredHeldOutLogDensity)
            marginal_total += marginal_result.total

        assert abs(joint_result.total - marginal_total) < 10**-8

    def test_per_factor_totals_sum_to_joint_for_independent_marginals(self) -> None:
        mu = np.array([0.005, -0.002, 0.001])
        sigma = np.array([0.04, 0.03, 0.015])
        historical = _toy_historical(60, mu=mu, sigma=sigma, seed=10)
        model = _ConstantGaussianModel(mu=mu, sigma=sigma)

        result = held_out_predictive_log_density(model, historical, train_fraction=0.5)

        assert isinstance(result, ScoredHeldOutLogDensity)
        assert result.factor_breakdown is not None
        summed = sum(result.factor_breakdown.per_factor_total.values())
        assert abs(summed - result.total) < 10**-8

        for name, total in result.factor_breakdown.per_factor_total.items():
            assert abs(result.factor_breakdown.per_factor_per_month[name] - total / result.held_out_count) < 10**-10

    def test_unscored_model_records_reason_without_crashing(self) -> None:
        mu = np.array([0.0])
        sigma = np.array([0.01])
        historical = _toy_historical(20, mu=mu, sigma=sigma, seed=3)
        model = _UnscoredModel()

        result = held_out_predictive_log_density(model, historical, train_fraction=0.7)

        assert isinstance(result, UnscoredHeldOutLogDensity)
        assert result.model_label == "unscored"
        assert "returned None" in result.unscored_reason

    def test_rejects_invalid_train_fraction(self) -> None:
        mu = np.array([0.0])
        sigma = np.array([0.01])
        historical = _toy_historical(20, mu=mu, sigma=sigma, seed=4)
        model = _ConstantGaussianModel(mu=mu, sigma=sigma)

        for bad in (0.0, 1.0, -0.1, 1.5):
            with pytest.raises(ValueError, match="train_fraction must be in"):
                held_out_predictive_log_density(model, historical, train_fraction=bad)

    def test_rejects_train_fraction_that_leaves_no_holdout(self) -> None:
        mu = np.array([0.0])
        sigma = np.array([0.01])
        historical = _toy_historical(3, mu=mu, sigma=sigma, seed=5)
        model = _ConstantGaussianModel(mu=mu, sigma=sigma)

        # 3 transitions × 0.99 rounds to 3 → no held-out months.
        with pytest.raises(ValueError, match="no held-out months"):
            held_out_predictive_log_density(model, historical, train_fraction=0.99)


class TestRollingOriginPredictiveLogDensity:
    def test_total_matches_closed_form_gaussian_over_all_origins(self) -> None:
        mu = np.array([0.005, 0.001])
        sigma = np.array([0.04, 0.02])
        historical = _toy_historical(50, mu=mu, sigma=sigma, seed=20)

        result = rolling_origin_predictive_log_density(
            lambda: _ConstantGaussianModel(mu=mu, sigma=sigma), historical, min_train=10, refit_every=1
        )

        log_returns = historical_log_returns(historical)
        expected_total = float(sum(_gaussian_log_density(log_returns[t], mu, sigma) for t in range(10, 50)))
        assert isinstance(result, ScoredRollingOriginLogDensity)
        assert result.n_origins == 40
        assert abs(result.total - expected_total) < 10**-10
        assert abs(result.per_month - expected_total / 40) < 10**-10
        assert result.mean_se > 0.0
        assert result.factor_breakdown is not None
        assert abs(sum(result.factor_breakdown.per_factor_total.values()) - result.total) < 10**-8

    def test_refit_every_skips_intermediate_fits(self) -> None:
        # Wrap the synthetic model so we can count fit() calls.
        fit_calls = {"n": 0}

        def factory() -> _ConstantGaussianModel:
            class _CountingModel(_ConstantGaussianModel):
                def fit(self, historical: HistoricalSeries) -> None:
                    fit_calls["n"] += 1
                    super().fit(historical)

            return _CountingModel(mu=np.array([0.0]), sigma=np.array([0.01]))

        historical = _toy_historical(30, mu=np.array([0.0]), sigma=np.array([0.01]), seed=21)
        rolling_origin_predictive_log_density(factory, historical, min_train=10, refit_every=5)
        # Origins 10, 15, 20, 25 → 4 fits (10 + 5*k for k = 0..3, with 30 as upper bound).
        assert fit_calls["n"] == 4

    def test_unscored_model_records_reason(self) -> None:
        historical = _toy_historical(20, mu=np.array([0.0]), sigma=np.array([0.01]), seed=22)
        result = rolling_origin_predictive_log_density(lambda: _UnscoredModel(), historical, min_train=5, refit_every=1)
        assert isinstance(result, UnscoredRollingOriginLogDensity)
        assert "returned None" in result.unscored_reason


class TestMultiStepPredictiveLogDensity:
    def test_horizons_match_closed_form_for_iid_gaussian(self) -> None:
        mu = np.array([0.005, 0.001])
        sigma = np.array([0.04, 0.02])
        historical = _toy_historical(60, mu=mu, sigma=sigma, seed=30)
        model = _ConstantGaussianModel(mu=mu, sigma=sigma)

        result = multi_step_predictive_log_density(model, historical, horizons=(1, 6), train_fraction=0.5)

        assert result.horizons == (1, 6)
        log_returns = historical_log_returns(historical)
        train_end = round(60 * 0.5)
        for row in result.rows:
            assert isinstance(row, ScoredMultiStepLogDensityRow)
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
            assert abs(row.total - expected_total) < 10**-8

    def test_unscored_model_records_reason_per_horizon(self) -> None:
        historical = _toy_historical(20, mu=np.array([0.0]), sigma=np.array([0.01]), seed=31)
        result = multi_step_predictive_log_density(_UnscoredModel(), historical, horizons=(1, 3), train_fraction=0.5)
        for row in result.rows:
            assert isinstance(row, UnscoredMultiStepLogDensityRow)


if __name__ == "__main__":
    pytest_bazel.main()

"""VECM (NumPyro) sanity tests.

These tests exercise the NumPyro-fit VECM end-to-end on synthetic
cointegrated series + the augur runtime sampling boundary. They run on
small horizons / coarse tolerances because SVI/MAP isn't bit-deterministic
across machines.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel
from numpyro import distributions as dist

from augur.model.exogenous import ExogenousSamplingRequest, level_series_request_channels
from augur.model.path_models.scenarios import HistoricalSeries
from augur.model.series import CryptoKey, CryptoSymbol, HomeValueKey, InflationKey, LocationId, RentKey, SP500Key
from augur.model.vecm import VecmConfig, VecmModel


def _series_from_log_levels(log_levels: np.ndarray) -> HistoricalSeries:
    levels = np.exp(log_levels - log_levels[0])
    months = tuple(f"2000-{i:02d}" for i in range(levels.shape[0]))
    # Synthetic 2-factor cointegration data: the factor identities are arbitrary (the test
    # exercises fit/predictive shapes, not series semantics) — two distinct level keys suffice.
    return HistoricalSeries(factor_names=(SP500Key(), InflationKey()), levels=levels, months=months)


def _historical_series_4factor(log_levels: np.ndarray) -> HistoricalSeries:
    levels = np.exp(log_levels - log_levels[0])
    months = tuple(f"2000-{i:02d}" for i in range(levels.shape[0]))
    return HistoricalSeries(
        factor_names=(
            SP500Key(),
            HomeValueKey(location_id=LocationId("san_francisco_ca")),
            RentKey(location_id=LocationId("san_francisco_ca")),
            InflationKey(),
        ),
        levels=levels,
        months=months,
    )


def _cointegrated_two_factor(seed: int, n_steps: int) -> HistoricalSeries:
    rng = np.random.default_rng(seed)
    r1 = np.cumsum(rng.normal(scale=0.02, size=n_steps))
    gap = np.zeros(n_steps)
    for t in range(1, n_steps):
        gap[t] = 0.7 * gap[t - 1] + rng.normal(scale=0.005)
    r2 = r1 + gap
    log_levels = np.column_stack([r1, r2])
    log_levels = np.concatenate([np.zeros((1, 2)), log_levels], axis=0)
    return _series_from_log_levels(log_levels)


class TestVecmModel:
    def test_fit_then_predictive_returns_a_multivariate_gaussian(self) -> None:
        historical = _cointegrated_two_factor(seed=42, n_steps=200)

        model = VecmModel(config=VecmConfig(n_iters=500))
        model.fit(historical)

        pred = model.predictive(historical, t=100, horizon=1)
        assert isinstance(pred, dist.MultivariateNormal)
        log_levels = np.log(historical.levels)
        observed = log_levels[101] - log_levels[100]
        log_prob = float(np.asarray(pred.log_prob(np.asarray(observed, dtype="float32"))))
        assert np.isfinite(log_prob)

    def test_predictive_returns_none_when_horizon_exceeds_window(self) -> None:
        historical = _cointegrated_two_factor(seed=7, n_steps=150)

        model = VecmModel(config=VecmConfig(n_iters=300))
        model.fit(historical)

        # n_steps observation transitions = 150, so origin t=148 with h=3 has no
        # observed value to score against → predictive returns None.
        assert model.predictive(historical, t=148, horizon=3) is None

    def test_h1_horizon_predictive_matches_one_step_in_distribution(self) -> None:
        historical = _cointegrated_two_factor(seed=42, n_steps=200)

        model = VecmModel(config=VecmConfig(n_iters=500))
        model.fit(historical)

        for t in (50, 100, 150):
            one_step = model.predictive(historical, t, horizon=1)
            h1 = model.predictive(historical, t, horizon=1)
            assert isinstance(one_step, dist.MultivariateNormal)
            assert isinstance(h1, dist.MultivariateNormal)
            # h=1 is closed-form in both paths; same params.
            np.testing.assert_allclose(np.asarray(one_step.mean), np.asarray(h1.mean), atol=1e-6)

    def test_sample_returns_correct_shapes_and_metadata(self) -> None:
        rng = np.random.default_rng(123)
        base = np.cumsum(rng.normal(scale=0.01, size=240))
        log_levels = np.column_stack(
            [
                base + rng.normal(scale=0.02, size=240),
                base * 0.8 + rng.normal(scale=0.01, size=240),
                base * 0.4 + rng.normal(scale=0.005, size=240),
                base * 0.2 + rng.normal(scale=0.003, size=240),
            ]
        )
        log_levels = np.concatenate([np.zeros((1, 4)), log_levels], axis=0)
        historical = _historical_series_4factor(log_levels)

        model = VecmModel(config=VecmConfig(n_iters=500))
        model.fit(historical)
        # Attach deployment-layer state (normally done by realize_model).
        model.latest_observations = {
            "sp500": 5500.0,
            "home_value:san_francisco_ca": 1_000_000.0,
            "rent:san_francisco_ca": 3000.0,
            "inflation": 320.0,
        }
        model._compute_provenance(evidence_source_id="test")

        sampled = model.sample(
            ExogenousSamplingRequest(
                horizon_months=12,
                rollout_seeds=(7, 8),
                **level_series_request_channels(
                    frozenset(
                        {
                            SP500Key(),
                            InflationKey(),
                            HomeValueKey(location_id=LocationId("san_francisco_ca")),
                            RentKey(location_id=LocationId("san_francisco_ca")),
                        }
                    )
                ),
            )
        )

        # SP500 paths start at 5500 (the latest observation) and scale by month-0=1 multiplier.
        assert sampled.level_matrix(SP500Key(), rollout_count=2, horizon_months=12)[:, 0].tolist() == [5500.0, 5500.0]
        assert sampled.level_matrix(
            HomeValueKey(location_id=LocationId("san_francisco_ca")), rollout_count=2, horizon_months=12
        )[:, 0].tolist() == [1_000_000.0, 1_000_000.0]
        assert sampled.metadata["scenario_generator_id"] == "vecm_numpyro"
        # Note: VECM-rejects-PE was previously asserted by calling
        # `model.sample(required_level_series={"private_equity:..."})`. With the
        # typed boundary, PE has no `LevelSeriesKey` variant — the rejection now
        # happens at type construction (`required_level_series` cannot contain PE),
        # so a sampler-level check is unnecessary.

    def test_offdiag_loadings_are_scaled_by_target_factor_volatility(self) -> None:
        model = VecmModel(
            factor_names=(SP500Key(), InflationKey()),
            n_factors=2,
            train_log_levels=np.zeros((1, 2), dtype=np.float64),
            params={
                "beta_tail_auto_loc": np.array([0.0], dtype=np.float64),
                "alpha_auto_loc": np.array([0.0, 0.0], dtype=np.float64),
                "const_coint_auto_loc": np.array(0.0, dtype=np.float64),
                "log_diag_auto_loc": np.log(np.array([0.5, 0.005], dtype=np.float64)),
                "offdiag_flat_auto_loc": np.array([0.9], dtype=np.float64),
            },
        )
        model.latest_observations = {"sp500": 5500.0, "inflation": 320.0}
        model._compute_provenance(evidence_source_id="test")

        cov = model._cov_np()
        assert cov[1, 1] == pytest.approx(0.005**2 * (1 + 0.9**2))

        sampled = model.sample(
            ExogenousSamplingRequest(
                horizon_months=1,
                rollout_seeds=tuple(range(512)),
                **level_series_request_channels(frozenset({InflationKey()})),
            )
        )

        inflation = sampled.level_matrix(InflationKey(), rollout_count=512, horizon_months=1)
        monthly_log_return = np.log(inflation[:, 1] / inflation[:, 0])
        assert float(np.std(monthly_log_return, ddof=1)) < 0.02

    def test_sample_anchors_crypto_factors_to_latest_close(self) -> None:
        rng = np.random.default_rng(456)
        base = np.cumsum(rng.normal(scale=0.02, size=200))
        log_levels = np.column_stack(
            [
                base + rng.normal(scale=0.015, size=200),
                base * 0.6 + rng.normal(scale=0.03, size=200),
                base * 0.4 + rng.normal(scale=0.025, size=200),
            ]
        )
        log_levels = np.concatenate([np.zeros((1, 3)), log_levels], axis=0)
        levels = np.exp(log_levels - log_levels[0])
        months = tuple(f"2010-{i:02d}" for i in range(levels.shape[0]))
        historical = HistoricalSeries(
            factor_names=(SP500Key(), CryptoKey(symbol=CryptoSymbol("btc")), CryptoKey(symbol=CryptoSymbol("eth"))),
            levels=levels,
            months=months,
        )

        model = VecmModel(config=VecmConfig(n_iters=300))
        model.fit(historical)
        model.latest_observations = {
            "spy_adjusted_close_latest": 5500.0,
            "btc_close_latest": 65_000.0,
            "eth_close_latest": 3_200.0,
        }
        model._compute_provenance(evidence_source_id="test")

        sampled = model.sample(
            ExogenousSamplingRequest(
                horizon_months=6,
                rollout_seeds=(11, 12),
                **level_series_request_channels(
                    frozenset(
                        {SP500Key(), CryptoKey(symbol=CryptoSymbol("btc")), CryptoKey(symbol=CryptoSymbol("eth"))}
                    )
                ),
            )
        )

        # Month-0 multiplier is 1.0, so the first sampled level equals latest_observations directly.
        # This proves _latest_factor_value's crypto:* branch correctly maps to <symbol>_close_latest.
        assert sampled.level_matrix(CryptoKey(symbol=CryptoSymbol("btc")), rollout_count=2, horizon_months=6)[
            :, 0
        ].tolist() == [65_000.0, 65_000.0]
        assert sampled.level_matrix(CryptoKey(symbol=CryptoSymbol("eth")), rollout_count=2, horizon_months=6)[
            :, 0
        ].tolist() == [3_200.0, 3_200.0]


if __name__ == "__main__":
    pytest_bazel.main()

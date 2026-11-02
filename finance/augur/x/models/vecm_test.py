"""VECM (NumPyro) sanity tests.

These tests exercise the NumPyro-fit VECM end-to-end on synthetic
cointegrated series + the augur runtime sampling boundary. They run on
small horizons / coarse tolerances because SVI/MAP isn't bit-deterministic
across machines.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest
import pytest_bazel
from numpyro import distributions as dist

from finance.augur.model.conditioning import ExogenousObservedPoint, ObservationTreatment, ObservationUnits
from finance.augur.model.evidence import EvidenceMetadata
from finance.augur.model.exogenous import ExogenousSamplingRequest, level_series_request_channels
from finance.augur.model.path_models.scenarios import DatedHistoricalSeries, HistoricalSeries
from finance.augur.model.series import SP500_SYMBOL, HomeValueKey, InflationKey, LocationId, RentKey, SecurityKey
from finance.augur.x.models.vecm import VecmConfig, VecmModel, VecmProviderConfig


def _observation(value: float, units: ObservationUnits) -> ExogenousObservedPoint:
    return ExogenousObservedPoint(
        value=value,
        units=units,
        observed_at=date(2026, 5, 1),
        source_id="test:synthetic",
        treatment=ObservationTreatment.HARD_START,
    )


def _month_labels(count: int) -> tuple[str, ...]:
    labels = []
    for index in range(count):
        year, zero_based_month = divmod(2000 * 12 + index, 12)
        labels.append(f"{year:04d}-{zero_based_month + 1:02d}")
    return tuple(labels)


def _series_from_log_levels(log_levels: np.ndarray) -> HistoricalSeries:
    levels = np.exp(log_levels - log_levels[0])
    months = _month_labels(levels.shape[0])
    # Synthetic 2-factor cointegration data: the factor identities are arbitrary (the test
    # exercises fit/predictive shapes, not series semantics) — two distinct level keys suffice.
    return HistoricalSeries(
        series_names=(SecurityKey(symbol=SP500_SYMBOL), InflationKey()), levels=levels, months=months
    )


def _historical_series_4factor(log_levels: np.ndarray) -> HistoricalSeries:
    levels = np.exp(log_levels - log_levels[0])
    months = _month_labels(levels.shape[0])
    return HistoricalSeries(
        series_names=(
            SecurityKey(symbol=SP500_SYMBOL),
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
    def test_fit_rejects_a_missing_calendar_month(self) -> None:
        continuous = _cointegrated_two_factor(seed=42, n_steps=3)
        historical = HistoricalSeries(
            series_names=continuous.series_names,
            levels=continuous.levels,
            months=("2000-01", "2000-02", "2000-04", "2000-05"),
        )

        with pytest.raises(ValueError, match="requires consecutive calendar months"):
            VecmModel(config=VecmConfig(n_iters=1)).fit(historical)

    def test_continuous_fit_matches_the_dated_fit_path(self) -> None:
        historical = _cointegrated_two_factor(seed=42, n_steps=30)
        dated = DatedHistoricalSeries(
            series_names=historical.series_names, levels=historical.levels, months=historical.months
        )
        regular_fit = VecmModel(config=VecmConfig(n_iters=5, seed=19))
        dated_fit = VecmModel(config=VecmConfig(n_iters=5, seed=19))

        regular_fit.fit(historical)
        dated_fit.fit_dated(dated)

        for name in regular_fit.params:
            np.testing.assert_array_equal(regular_fit.params[name], dated_fit.params[name])
        np.testing.assert_array_equal(regular_fit.train_log_levels, dated_fit.train_log_levels)
        assert regular_fit.train_months == dated_fit.train_months
        assert regular_fit.calendar_gaps == ()

    def test_dated_fit_uses_one_global_origin_and_omits_cross_gap_transition(self) -> None:
        continuous = _cointegrated_two_factor(seed=42, n_steps=50)
        scaled_levels = continuous.levels * np.array([5_500.0, 320.0])[None, :]
        keep = np.arange(len(continuous.months)) != 20
        dated = DatedHistoricalSeries(
            series_names=continuous.series_names,
            levels=scaled_levels[keep],
            months=tuple(month for index, month in enumerate(continuous.months) if keep[index]),
        )

        model = VecmModel(config=VecmConfig(n_iters=5, seed=19))
        model.fit_dated(dated)
        state = model.to_trained_state()

        assert len(dated.transition_pairs) == len(dated.months) - 2
        assert sum(segment.used_transition_count for segment in state.training_segments) == len(dated.transition_pairs)
        assert state.calendar_gaps[0].missing_months == (continuous.months[20],)
        assert model.as_of_month == dated.months[-1]
        np.testing.assert_allclose(model.log_level_origin, np.log(scaled_levels[0]))
        np.testing.assert_allclose(model.train_log_levels, np.log(dated.levels) - np.log(scaled_levels[0]))
        assert model.train_months == dated.months

    def test_dated_provider_requires_anchors_at_its_terminal_state(self) -> None:
        continuous = _cointegrated_two_factor(seed=7, n_steps=20)
        levels = continuous.levels * np.array([5_500.0, 320.0])[None, :]
        dated = DatedHistoricalSeries(series_names=continuous.series_names, levels=levels, months=continuous.months)
        model = VecmModel(config=VecmConfig(n_iters=2))
        model.fit_dated(dated)
        units = (ObservationUnits.USD_PER_UNIT, ObservationUnits.INDEX_POINTS)
        anchors = {
            factor.wire_id: ExogenousObservedPoint(
                value=float(dated.levels[-1, index]),
                units=units[index],
                observed_at=date.fromisoformat(f"{dated.months[-1]}-01"),
                source_id="test:aligned",
                treatment=ObservationTreatment.HARD_START,
            )
            for index, factor in enumerate(dated.series_names)
        }
        config = VecmProviderConfig(
            trained_state=model.to_trained_state(),
            latest_observations=anchors,
            evidence_metadata=EvidenceMetadata(),
            current_mortgage30_rate_pct=6.0,
        )
        restored = config.realize_model()
        assert restored.as_of_month == dated.months[-1]

        stale = config.model_dump(mode="python")
        stale["latest_observations"][dated.series_names[0].wire_id]["observed_at"] = date(2025, 1, 1)
        with pytest.raises(ValueError, match="share the fitted terminal as-of month"):
            VecmProviderConfig.model_validate(stale)

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
            "security:SPY": _observation(5500.0, ObservationUnits.USD_PER_UNIT),
            "home_value:san_francisco_ca": _observation(1_000_000.0, ObservationUnits.USD),
            "rent:san_francisco_ca": _observation(3000.0, ObservationUnits.USD_PER_MONTH),
            "inflation": _observation(320.0, ObservationUnits.INDEX_POINTS),
        }
        model._compute_provenance(evidence_source_id="test")

        sampled = model.sample(
            ExogenousSamplingRequest(
                horizon_months=12,
                rollout_seeds=(7, 8),
                **level_series_request_channels(
                    frozenset(
                        {
                            SecurityKey(symbol=SP500_SYMBOL),
                            InflationKey(),
                            HomeValueKey(location_id=LocationId("san_francisco_ca")),
                            RentKey(location_id=LocationId("san_francisco_ca")),
                        }
                    )
                ),
            )
        )

        # SP500 paths start at 5500 (the latest observation) and scale by month-0=1 multiplier.
        assert sampled.level_matrix(SecurityKey(symbol=SP500_SYMBOL), rollout_count=2, horizon_months=12)[
            :, 0
        ].tolist() == [5500.0, 5500.0]
        assert sampled.level_matrix(
            HomeValueKey(location_id=LocationId("san_francisco_ca")), rollout_count=2, horizon_months=12
        )[:, 0].tolist() == [1_000_000.0, 1_000_000.0]
        assert sampled.provenance["scenario_generator_id"] == "vecm_numpyro"
        # Note: VECM-rejects-PE was previously asserted by calling
        # `model.sample(required_level_series={"private_equity:..."})`. With the
        # typed boundary, PE has no `LevelSeriesKey` variant — the rejection now
        # happens at type construction (`required_level_series` cannot contain PE),
        # so a sampler-level check is unnecessary.

    def test_offdiag_loadings_are_scaled_by_target_factor_volatility(self) -> None:
        model = VecmModel(
            factor_names=(SecurityKey(symbol=SP500_SYMBOL), InflationKey()),
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
        model.latest_observations = {
            "security:SPY": _observation(5500.0, ObservationUnits.USD_PER_UNIT),
            "inflation": _observation(320.0, ObservationUnits.INDEX_POINTS),
        }
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


if __name__ == "__main__":
    pytest_bazel.main()

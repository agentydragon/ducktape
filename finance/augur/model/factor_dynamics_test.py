import math
from datetime import date

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.conditioning import ExogenousConditioningContext
from finance.augur.model.exogenous import ExogenousSamplingRequest
from finance.augur.model.factor_dynamics import (
    FactorDynamics,
    MeanReversionParams,
    dynamics_for_factor,
    to_innovation_space,
)
from finance.augur.model.series import InflationKey, MuniRatioKey, NominalYieldKey, SP500Key, TenorMonths
from finance.augur.model.state_space import StateSpaceModel, StateSpaceModelArtifact

TEN_YEAR = NominalYieldKey(tenor_months=TenorMonths(120))
MUNI_TEN_YEAR = MuniRatioKey(tenor_months=TenorMonths(120))


def test_rates_are_mean_reverting_and_levels_compound() -> None:
    assert dynamics_for_factor(TEN_YEAR) is FactorDynamics.MEAN_REVERTING_RATE
    assert dynamics_for_factor(MUNI_TEN_YEAR) is FactorDynamics.MEAN_REVERTING_RATE
    assert dynamics_for_factor(SP500Key()) is FactorDynamics.GEOMETRIC_RANDOM_WALK
    assert dynamics_for_factor(InflationKey()) is FactorDynamics.GEOMETRIC_RANDOM_WALK


def test_innovation_space_is_log_for_levels_and_identity_for_rates() -> None:
    assert to_innovation_space(100.0, FactorDynamics.GEOMETRIC_RANDOM_WALK) == pytest.approx(math.log(100.0))
    # A rate is passed through untouched, which is the only reason a negative one is representable.
    assert to_innovation_space(-0.004, FactorDynamics.MEAN_REVERTING_RATE) == pytest.approx(-0.004)


def _rate_artifact(
    *, start_rate: float, theta: float, kappa: float, rate_sigma: float = 1e-9, equity_rate_covariance: float = 0.0
) -> StateSpaceModelArtifact:
    """A two-factor artifact: sp500 (compounding) and the 10y nominal yield (mean-reverting)."""

    sp500 = SP500Key().wire_id
    rate = TEN_YEAR.wire_id
    factors = (sp500, rate)
    latest = {sp500: 100.0, rate: start_rate}
    cov = ((0.04**2, equity_rate_covariance), (equity_rate_covariance, rate_sigma**2))
    return StateSpaceModelArtifact(
        factor_names=factors,
        trained_through_month="2026-07",
        latest_level_by_factor=latest,
        # A rate's "log return" mu is read as the mean of its additive innovation: zero, because
        # the drift of a mean-reverting rate is carried entirely by theta.
        monthly_log_return_mu={sp500: 0.005, rate: 0.0},
        monthly_log_return_cov=cov,
        filtered_log_state_mean={sp500: math.log(100.0), rate: start_rate},
        filtered_log_state_cov=cov,
        mean_reversion_by_factor={rate: MeanReversionParams(kappa=kappa, theta=theta)},
        source_manifest={"source_ids": ("fixture:rates",)},
        prior_manifest={"kind": "fixture"},
    )


def _model(artifact: StateSpaceModelArtifact) -> StateSpaceModel:
    return StateSpaceModel(artifact=artifact, conditioning=ExogenousConditioningContext(start_at=date(2026, 8, 1)))


def _sample_rates(artifact: StateSpaceModelArtifact, *, horizon_months: int, rollouts: int = 1) -> np.ndarray:
    model = _model(artifact)
    bundle = model.sample(
        ExogenousSamplingRequest(
            horizon_months=horizon_months,
            rollout_seeds=tuple(range(rollouts)),
            required_discount_rates=frozenset({TEN_YEAR}),
            required_asset_prices=frozenset({SP500Key()}),
        )
    )
    frame = bundle.discount_rates.nominal_yield.sort(["rollout_index", "month_index"])
    values: np.ndarray = frame.get_column("value").to_numpy()
    return values.reshape(rollouts, horizon_months + 1)


def test_rate_path_decays_toward_theta_at_the_configured_half_life() -> None:
    # Deterministic (sigma ~ 0): the path is the pure OU decay, so it can be checked exactly.
    kappa = 0.05
    path = _sample_rates(_rate_artifact(start_rate=0.08, theta=0.03, kappa=kappa), horizon_months=48)[0]
    assert path[0] == pytest.approx(0.08)
    expected_month_12 = 0.03 + (0.08 - 0.03) * (1.0 - kappa) ** 12
    assert path[12] == pytest.approx(expected_month_12, abs=1e-6)
    # Monotone approach, and still short of theta after four years — not snapped to it.
    assert np.all(np.diff(path) < 0.0)
    assert 0.03 < path[-1] < path[12]


def test_a_rate_may_go_negative_where_a_level_never_could() -> None:
    path = _sample_rates(_rate_artifact(start_rate=0.01, theta=-0.005, kappa=0.2), horizon_months=36)[0]
    assert path[-1] < 0.0


def test_rate_and_equity_innovations_stay_correlated() -> None:
    # The 2022 state: rates rising while equities fall. A positive covariance between the
    # equity log return and the rate innovation must show up as a positive correlation
    # between realized equity returns and realized rate changes — if rates were sampled
    # from their own stream this would be ~0 and the model could not produce that state.
    artifact = _rate_artifact(
        start_rate=0.04, theta=0.04, kappa=0.02, rate_sigma=0.003, equity_rate_covariance=0.04 * 0.003 * 0.8
    )
    model = _model(artifact)
    bundle = model.sample(
        ExogenousSamplingRequest(
            horizon_months=1,
            rollout_seeds=tuple(range(400)),
            required_discount_rates=frozenset({TEN_YEAR}),
            required_asset_prices=frozenset({SP500Key()}),
        )
    )
    rates = (
        bundle.discount_rates.nominal_yield.sort(["rollout_index", "month_index"])
        .get_column("value")
        .to_numpy()
        .reshape(400, 2)
    )
    equities = (
        bundle.asset_prices.sp500.sort(["rollout_index", "month_index"]).get_column("value").to_numpy().reshape(400, 2)
    )
    correlation = np.corrcoef(np.log(equities[:, 1] / equities[:, 0]), rates[:, 1] - rates[:, 0])[0, 1]
    assert correlation > 0.6


def test_a_rate_series_round_trips_through_level_matrix() -> None:
    # The tenor sub-id column is Int64, not Utf8 like every other sub-id, so both the frame
    # build and the `pl.col(...) == subid` lookup have to carry the tenor as an integer.
    model = _model(_rate_artifact(start_rate=0.05, theta=0.03, kappa=0.1))
    bundle = model.sample(
        ExogenousSamplingRequest(
            horizon_months=6,
            rollout_seeds=(0, 1),
            required_discount_rates=frozenset({TEN_YEAR}),
            required_asset_prices=frozenset({SP500Key()}),
        )
    )
    matrix = bundle.level_matrix(TEN_YEAR, rollout_count=2, horizon_months=6)
    assert matrix.shape == (2, 7)
    assert matrix[0, 0] == pytest.approx(0.05)


def test_artifact_rejects_a_rate_factor_without_mean_reversion_parameters() -> None:
    with pytest.raises(ValueError, match="mean_reversion_by_factor must have an entry"):
        StateSpaceModelArtifact(
            factor_names=(SP500Key().wire_id, TEN_YEAR.wire_id),
            trained_through_month="2026-07",
            latest_level_by_factor={SP500Key().wire_id: 100.0, TEN_YEAR.wire_id: 0.04},
            monthly_log_return_mu={SP500Key().wire_id: 0.005, TEN_YEAR.wire_id: 0.0},
            monthly_log_return_cov=((0.0016, 0.0), (0.0, 1e-6)),
            filtered_log_state_mean={SP500Key().wire_id: math.log(100.0), TEN_YEAR.wire_id: 0.04},
            filtered_log_state_cov=((0.0016, 0.0), (0.0, 1e-6)),
        )


def test_artifact_rejects_mean_reversion_parameters_on_a_compounding_factor() -> None:
    with pytest.raises(ValueError, match="mean_reversion_by_factor must have an entry"):
        StateSpaceModelArtifact(
            factor_names=(SP500Key().wire_id,),
            trained_through_month="2026-07",
            latest_level_by_factor={SP500Key().wire_id: 100.0},
            monthly_log_return_mu={SP500Key().wire_id: 0.005},
            monthly_log_return_cov=((0.0016,),),
            filtered_log_state_mean={SP500Key().wire_id: math.log(100.0)},
            filtered_log_state_cov=((0.0016,),),
            mean_reversion_by_factor={SP500Key().wire_id: MeanReversionParams(kappa=0.1, theta=0.03)},
        )


def test_a_non_positive_start_is_allowed_only_for_a_rate() -> None:
    _rate_artifact(start_rate=-0.001, theta=0.02, kappa=0.1)  # a negative yield is a real state
    with pytest.raises(ValueError, match="must be positive for non-rate factors"):
        StateSpaceModelArtifact(
            factor_names=(SP500Key().wire_id,),
            trained_through_month="2026-07",
            latest_level_by_factor={SP500Key().wire_id: 0.0},
            monthly_log_return_mu={SP500Key().wire_id: 0.005},
            monthly_log_return_cov=((0.0016,),),
            filtered_log_state_mean={SP500Key().wire_id: 0.0},
            filtered_log_state_cov=((0.0016,),),
        )


if __name__ == "__main__":
    pytest_bazel.main()

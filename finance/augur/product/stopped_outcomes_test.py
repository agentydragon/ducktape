"""Public population summaries distinguish stopped books from horizon outcomes.

Synthetic cash-only control: both paths start with $2,000 and pay $500 at m0.
One stays at the opening CPI; the other jumps to 10 at m1 and cannot pay $5,000.
All-or-none settlement leaves its $1,500 cash intact and records $5,000 unpaid,
not the $3,500 additional funding that would have made that group payable.
"""

import numpy as np
import numpy.typing as npt
import pytest
import pytest_bazel

from finance.augur.api.portfolio import PortfolioConfig
from finance.augur.model.exogenous import ExogenousSamplingRequest
from finance.augur.model.series import InflationKey
from finance.augur.model.testing import ConstantFrameModel
from finance.augur.product.service import ProductService
from finance.augur.product.wire import (
    MetricName,
    ProductProjectionRequest,
    ProjectionSamplingRequest,
    RolloutRequest,
    ScenarioKey,
)
from finance.augur.sim.product_metrics import OutcomeBasis


def _product(*, future_cpi: float = 10) -> ProductService:
    def inflation(request: ExogenousSamplingRequest) -> npt.NDArray[np.float64]:
        values = np.ones((request.rollout_count, request.horizon_months + 1), dtype=np.float64)
        for row, seed in enumerate(request.rollout_seeds):
            if seed == 0:
                values[row, 1] = 10
                values[row, 2:] = future_cpi
        return values

    return ProductService(
        portfolio=PortfolioConfig(),
        initial_cash=2000,
        primary_agent_id="household",
        known_location_ids=frozenset(),
        locations={},
        properties_by_id={},
        models={"synthetic": ConstantFrameModel(levels={InflationKey(): inflation})},
        max_rollout_samples=2,
        max_horizon_months=3,
    )


def _request(*, metric: MetricName, horizon: int, count: int) -> ProjectionSamplingRequest:
    return ProjectionSamplingRequest(
        scenario=ScenarioKey(model_id="synthetic", horizon_months=horizon, monthly_spend=500, spend_index="inflation"),
        first_seed=0,
        rollout_count=count,
        metric=metric,
        percentiles=(0, 50, 100),
    )


@pytest.mark.parametrize("horizon", [2, 3])
def test_mixed_population_keeps_recorded_shortfall_but_not_stopped_terminal_wealth(horizon: int) -> None:
    # h=2 stops in the final event month: snapshot h still is not a completed horizon.
    product = _product()
    metrics: tuple[MetricName, ...] = ("net_worth", "shortfall")
    for metric in metrics:
        request = _request(metric=metric, horizon=horizon, count=2)
        fan = product.metric_fan(request)
        terminal = product.terminal_distribution(request)
        combined = product.projection_summary(
            ProductProjectionRequest(
                scenario=request.scenario,
                first_seed=request.first_seed,
                rollout_count=request.rollout_count,
                metric=request.metric,
                fan_percentiles=request.percentiles,
                terminal_percentiles=request.percentiles,
            )
        )
        assert combined.metric_fan == fan
        assert combined.terminal_distribution == terminal
        assert fan.failed_count == terminal.failed_count == 1
        assert fan.completed_count == terminal.completed_count == 1
        assert terminal.terminal_metric_samples["seed"] == [0, 1]
        assert terminal.terminal_metric_samples["failed"] == [True, False]

        if metric == "net_worth":
            assert fan.basis == terminal.basis == OutcomeBasis.COMPLETED_HORIZON
            assert fan.observation_count == terminal.observation_count == 1
            completed_wealth = str(200_000 - 50_000 * horizon)
            assert terminal.terminal_metric_samples["value_quanta"] == [None, completed_wealth]
            assert (
                fan.terminal_metric_percentiles
                == terminal.terminal_metric_percentiles
                == {"percentile": [0.0, 50.0, 100.0], "value_quanta": [completed_wealth] * 3}
            )
            counts = [2, 2] + [1] * (horizon - 1)
            values = [[str(200_000 - 50_000 * month)] * 3 for month in range(horizon + 1)]
        else:
            assert fan.basis == terminal.basis == OutcomeBasis.OBSERVED_THROUGH_STOP
            assert fan.observation_count == terminal.observation_count == 2
            assert terminal.terminal_metric_samples["value_quanta"] == ["500000", "0"]
            assert (
                fan.terminal_metric_percentiles
                == terminal.terminal_metric_percentiles
                == {"percentile": [0.0, 50.0, 100.0], "value_quanta": ["0", "250000", "500000"]}
            )
            counts = [2, 2, 2] + [1] * (horizon - 2)
            values = [["0"] * 3, ["0"] * 3, ["0", "250000", "500000"]] + [["0"] * 3] * (horizon - 2)
        assert fan.monthly_metric_fan["observed_count"] == [count for count in counts for _ in range(3)]
        assert fan.monthly_metric_fan["value_quanta"] == [value for row in values for value in row]


def test_all_stopped_has_no_terminal_wealth_but_has_observed_unpaid_demands() -> None:
    product = _product()
    wealth_request = _request(metric="net_worth", horizon=3, count=1)
    wealth = product.metric_fan(wealth_request)
    assert wealth.observation_count == wealth.completed_count == 0
    assert wealth.failed_count == 1
    assert wealth.terminal_metric_percentiles["value_quanta"] == [None] * 3
    assert wealth.monthly_metric_fan["observed_count"] == [1] * 6 + [0] * 6
    assert wealth.monthly_metric_fan["value_quanta"] == ["200000"] * 3 + ["150000"] * 3 + [None] * 6
    assert product.terminal_distribution(wealth_request).terminal_metric_samples["value_quanta"] == [None]

    shortfall_request = _request(metric="shortfall", horizon=3, count=1)
    shortfall = product.metric_fan(shortfall_request)
    assert shortfall.basis == OutcomeBasis.OBSERVED_THROUGH_STOP
    assert shortfall.observation_count == shortfall.failed_count == 1
    assert shortfall.completed_count == 0
    assert shortfall.terminal_metric_percentiles["value_quanta"] == ["500000"] * 3
    assert shortfall.monthly_metric_fan["observed_count"] == [1] * 9 + [0] * 3
    assert shortfall.monthly_metric_fan["value_quanta"] == ["0"] * 6 + ["500000"] * 3 + [None] * 3
    assert product.terminal_distribution(shortfall_request).terminal_metric_samples["value_quanta"] == ["500000"]

    selected = product.rollout(RolloutRequest(scenario=wealth_request.scenario, seed=0))
    assert selected.rollout.ending_metrics.snapshot_index == 2
    assert selected.rollout.ending_metrics.failed_month_index == 1
    assert selected.rollout.ending_metrics.cash_quanta == "150000"
    assert selected.rollout.ending_metrics.shortfall_quanta == "500000"
    assert selected.rollout.monthly_metrics["month_index"] == [0, 1, 2]
    different_future = _product(future_cpi=999)
    assert different_future.rollout(RolloutRequest(scenario=wealth_request.scenario, seed=0)) == selected
    assert different_future.metric_fan(wealth_request) == wealth
    assert different_future.metric_fan(shortfall_request) == shortfall


if __name__ == "__main__":
    pytest_bazel.main()

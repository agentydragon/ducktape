"""Product-shaped query surface. Owns the product simulation model.

Holds the slice of augur config the product surface needs (portfolio, primary
agent, initial cash); does not know about properties, locations, or bootstrap.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any, overload

import numpy as np

from finance.augur.api.config import SecurityDistributionConfig
from finance.augur.api.portfolio import PortfolioConfig
from finance.augur.api.schemas import Frame
from finance.augur.api.wire import Property
from finance.augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    anchor_sampled_series_levels,
    level_series_request_channels,
    validate_sample_satisfies_request,
)
from finance.augur.product.projection import project_product_rollout
from finance.augur.product.quantiles import currency_quantiles
from finance.augur.product.scenarios import (
    asset_label_by_series_id,
    build_scenario,
    initial_bonds_from_portfolio,
    initial_lots_from_portfolio,
    required_private_equity_issuers,
    security_distributions_from_portfolio,
)
from finance.augur.product.wire import (
    MetricFanResponse,
    ProductProjectionRequest,
    ProductProjectionResponse,
    ProjectionSamplingRequest,
    RolloutOutput,
    RolloutRequest,
    RolloutResponse,
    ScenarioKey,
    TerminalDistributionResponse,
    TerminalMetrics,
)
from finance.augur.sim.compiler.plan import CompiledSimulation, compile_simulation
from finance.augur.sim.compiler.series import scenario_level_series_keys
from finance.augur.sim.engine.jax_engine import run_jax_product_summaries, run_jax_product_summary
from finance.augur.sim.external_series import materialize_sampled_exogenous
from finance.augur.sim.locations import Location
from finance.augur.sim.output import DenseSimulationOutput
from finance.augur.sim.product_metrics import (
    ProductMetricArrays,
    ProductMetricFanSummary,
    ProductProjectionSummaries,
    ProductTerminalSummary,
)
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.scenario import HarvestPolicy, Scenario
from finance.augur.sim.simulate import simulate_with_external_series_and_product_metrics


class ProductService:
    def __init__(
        self,
        *,
        portfolio: PortfolioConfig,
        initial_cash: Decimal | int | str,
        primary_agent_id: str,
        security_distributions: tuple[SecurityDistributionConfig, ...] = (),
        harvest_policies: tuple[HarvestPolicy, ...] = (),
        known_location_ids: frozenset[str],
        locations: dict[str, Location],
        properties_by_id: dict[str, Property],
        models: dict[str, Sampler],
        max_rollout_samples: int,
        max_horizon_months: int,
    ) -> None:
        if max_horizon_months <= 0:
            raise ValueError("max_horizon_months must be positive")
        if not models:
            raise ValueError("models must contain at least one preset")
        self._portfolio = portfolio
        self._initial_cash = initial_cash if isinstance(initial_cash, Decimal) else Decimal(str(initial_cash))
        self._primary_agent_id = primary_agent_id
        self._known_location_ids = known_location_ids
        self._locations = locations
        self._properties_by_id = properties_by_id
        self._models = models
        self._max_rollout_samples = int(max_rollout_samples)
        self._max_horizon_months = int(max_horizon_months)
        self._initial_lots = initial_lots_from_portfolio(portfolio, primary_agent_id=primary_agent_id)
        self._initial_bonds = initial_bonds_from_portfolio(portfolio, primary_agent_id=primary_agent_id)
        self._security_distributions = security_distributions_from_portfolio(
            portfolio, security_distributions, primary_agent_id=primary_agent_id
        )
        self._harvest_policies = harvest_policies
        self._asset_label_by_id = asset_label_by_series_id(portfolio)
        # Keep one product projection in flight per API process. JAX/XLA batches are memory-heavy
        # enough that overlapping fan + terminal requests can exceed the production pod limit.
        self._projection_lock = threading.Lock()

    def metric_fan(self, request: ProjectionSamplingRequest) -> MetricFanResponse:
        if request.rollout_count > self._max_rollout_samples:
            raise ValueError(f"rollout count {request.rollout_count} exceeds max {self._max_rollout_samples}")
        percentiles = tuple(float(pct) for pct in request.percentiles)
        with self._projection_lock:
            summary, model_id = self._simulate_product_summary(
                request.scenario, request.rollout_seeds, metric=request.metric, percentiles=percentiles
            )
            return _metric_fan_response(summary, model_id=model_id, metric=request.metric)

    def terminal_distribution(self, request: ProjectionSamplingRequest) -> TerminalDistributionResponse:
        if request.rollout_count > self._max_rollout_samples:
            raise ValueError(f"rollout count {request.rollout_count} exceeds max {self._max_rollout_samples}")
        percentiles = tuple(float(pct) for pct in request.percentiles)
        with self._projection_lock:
            summary, model_id = self._simulate_product_summary(
                request.scenario, request.rollout_seeds, metric=request.metric, percentiles=None
            )
            return _terminal_distribution_response(
                summary, model_id=model_id, metric=request.metric, percentiles=percentiles, seeds=request.rollout_seeds
            )

    def projection_summary(self, request: ProductProjectionRequest) -> ProductProjectionResponse:
        """Return the fan and terminal distribution from one shared simulation."""
        if request.rollout_count > self._max_rollout_samples:
            raise ValueError(f"rollout count {request.rollout_count} exceeds max {self._max_rollout_samples}")
        fan_percentiles = tuple(float(pct) for pct in request.fan_percentiles)
        terminal_percentiles = tuple(float(pct) for pct in request.terminal_percentiles)
        with self._projection_lock:
            summaries, model_id = self._simulate_product_summaries(
                request.scenario, request.rollout_seeds, metric=request.metric, percentiles=fan_percentiles
            )
            fan = summaries.metric_fan
            terminal = summaries.terminal_distribution
            return ProductProjectionResponse(
                metric_fan=_metric_fan_response(fan, model_id=model_id, metric=request.metric),
                terminal_distribution=_terminal_distribution_response(
                    terminal,
                    model_id=model_id,
                    metric=request.metric,
                    percentiles=terminal_percentiles,
                    seeds=request.rollout_seeds,
                ),
            )

    def rollout(self, request: RolloutRequest) -> RolloutResponse:
        with self._projection_lock:
            return self._rollout_response(request.scenario, int(request.seed))

    def _rollout_response(self, scenario: ScenarioKey, seed: int) -> RolloutResponse:
        self._validate_scenario_key(scenario)
        plan, output, metrics, model_id = self._simulate_dense(scenario, (seed,))
        projection = project_product_rollout(
            plan,
            output,
            metrics,
            rollout_index=0,
            primary_agent_id=self._primary_agent_id,
            asset_label_by_id=self._asset_label_by_id,
        )
        monthly_arrays = projection.monthly_metric_arrays
        terminal = _terminal_metrics_from_arrays(monthly_arrays, failed_month_index=projection.failed_month_index)
        # `monthly_metrics` ships as `Frame = dict[str, list[...]]`; build directly from numpy
        # instead of round-tripping through polars.
        monthly_metrics_frame = {
            name: arr.tolist() if name == "month_index" else [_quanta(value) for value in arr]
            for name, arr in monthly_arrays.items()
        }
        return RolloutResponse(
            model_id=model_id,
            currency_code=projection.currency_code,
            currency_quantum=projection.currency_quantum,
            rollout=RolloutOutput(
                seed=seed,
                failed=terminal.failed_month_index is not None,
                monthly_metrics=monthly_metrics_frame,
                terminal_metrics=terminal,
                events=projection.events,
            ),
        )

    def _validate_scenario_key(self, scenario_key: ScenarioKey) -> None:
        if scenario_key.model_id not in self._models:
            raise ValueError(f"unknown model_id: {scenario_key.model_id!r} (known presets: {sorted(self._models)})")
        if (
            scenario_key.rental_location_id is not None
            and scenario_key.rental_location_id not in self._known_location_ids
        ):
            raise ValueError(f"unknown rental_location_id: {scenario_key.rental_location_id!r}")
        if (
            scenario_key.property_purchase is not None
            and scenario_key.property_purchase.property_id not in self._properties_by_id
        ):
            raise ValueError(f"unknown property_id: {scenario_key.property_purchase.property_id!r}")
        horizon_months = int(scenario_key.horizon_months)
        if horizon_months > self._max_horizon_months:
            raise ValueError(f"requested horizon {horizon_months} exceeds server max {self._max_horizon_months}")

    def _compile_product_plan(
        self, scenario_key: ScenarioKey, seeds: tuple[int, ...]
    ) -> tuple[CompiledSimulation, str]:
        self._validate_scenario_key(scenario_key)
        scenario, sampled, model_id = self._scenario_and_sample(scenario_key, seeds)
        plan = compile_simulation(
            scenario,
            rollout_count=len(seeds),
            external_series=materialize_sampled_exogenous(sampled),
            jurisdictions=load_jurisdictions_for(scenario),
            locations=self._locations,
        )
        return plan, model_id

    @overload
    def _simulate_product_summary(
        self, scenario_key: ScenarioKey, seeds: tuple[int, ...], *, metric: str, percentiles: tuple[float, ...]
    ) -> tuple[ProductMetricFanSummary, str]: ...

    @overload
    def _simulate_product_summary(
        self, scenario_key: ScenarioKey, seeds: tuple[int, ...], *, metric: str, percentiles: None
    ) -> tuple[ProductTerminalSummary, str]: ...

    def _simulate_product_summary(
        self, scenario_key: ScenarioKey, seeds: tuple[int, ...], *, metric: str, percentiles: tuple[float, ...] | None
    ) -> tuple[ProductMetricFanSummary | ProductTerminalSummary, str]:
        plan, model_id = self._compile_product_plan(scenario_key, seeds)
        summary = run_jax_product_summary(
            plan,
            primary_agent_id=self._primary_agent_id,
            metric=metric if metric.endswith("_quanta") else f"{metric}_quanta",
            percentiles=percentiles,
        )
        return summary, model_id

    def _simulate_product_summaries(
        self, scenario_key: ScenarioKey, seeds: tuple[int, ...], *, metric: str, percentiles: tuple[float, ...]
    ) -> tuple[ProductProjectionSummaries, str]:
        plan, model_id = self._compile_product_plan(scenario_key, seeds)
        summaries = run_jax_product_summaries(
            plan,
            primary_agent_id=self._primary_agent_id,
            metric=metric if metric.endswith("_quanta") else f"{metric}_quanta",
            percentiles=percentiles,
        )
        return summaries, model_id

    def _simulate_dense(
        self, scenario_key: ScenarioKey, seeds: tuple[int, ...]
    ) -> tuple[CompiledSimulation, DenseSimulationOutput, ProductMetricArrays, str]:
        scenario, sampled, model_id = self._scenario_and_sample(scenario_key, seeds)
        plan, output, metrics = simulate_with_external_series_and_product_metrics(
            scenario,
            rollout_count=len(seeds),
            external_series=materialize_sampled_exogenous(sampled),
            locations=self._locations,
            primary_agent_id=self._primary_agent_id,
        )
        return plan, output, metrics, model_id

    def _scenario_and_sample(
        self, scenario_key: ScenarioKey, seeds: tuple[int, ...]
    ) -> tuple[Scenario, SampledExogenousBundle, str]:
        scenario = build_scenario(
            scenario_key,
            primary_agent_id=self._primary_agent_id,
            initial_cash=self._initial_cash,
            initial_lots=self._initial_lots,
            properties_by_id=self._properties_by_id,
            initial_bonds=self._initial_bonds,
            security_distributions=self._security_distributions,
            harvest_policies=self._harvest_policies,
        )
        sampling_request = ExogenousSamplingRequest(
            horizon_months=int(scenario_key.horizon_months),
            rollout_seeds=seeds,
            # Derived from the scenario the simulator will actually compile, not re-derived
            # from the wire type — one answer to "what does this need", not two that must agree.
            **level_series_request_channels(scenario_level_series_keys(scenario)),
            required_private_equity_issuers=required_private_equity_issuers(self._initial_lots),
        )
        sampled = self._models[scenario_key.model_id].sample(sampling_request)
        validate_sample_satisfies_request(sampling_request, sampled)
        anchors = self._portfolio.level_anchors
        sampled = anchor_sampled_series_levels(
            sampled,
            level_series_anchors=anchors.level_series_anchors,
            private_equity_anchors=anchors.private_equity_anchors,
        )
        model_id = sampled.model_id or scenario_key.model_id
        return scenario, sampled, model_id


def _monthly_fan_frame(summary: ProductMetricFanSummary) -> Frame:
    month_indices = summary.month_index
    percentile_array = np.asarray(summary.percentiles, dtype=np.float64)
    return {
        "month_index": np.repeat(month_indices, percentile_array.size).tolist(),
        "percentile": np.tile(percentile_array, month_indices.size).tolist(),
        "value_quanta": [_quanta(value) for value in summary.monthly_percentiles.reshape(-1)],
    }


def _metric_fan_response(summary: ProductMetricFanSummary, *, model_id: str, metric: str) -> MetricFanResponse:
    return MetricFanResponse(
        model_id=model_id,
        currency_code=summary.currency_code,
        currency_quantum=summary.currency_quantum,
        metric=metric,
        monthly_metric_fan=_monthly_fan_frame(summary),
        terminal_metric_percentiles=_quantile_frame(summary.percentiles, summary.terminal_percentiles),
        failed_count=summary.failed_count,
    )


def _terminal_distribution_response(
    summary: ProductTerminalSummary,
    *,
    model_id: str,
    metric: str,
    percentiles: tuple[float, ...],
    seeds: tuple[int, ...],
) -> TerminalDistributionResponse:
    return TerminalDistributionResponse(
        model_id=model_id,
        currency_code=summary.currency_code,
        currency_quantum=summary.currency_quantum,
        metric=metric,
        terminal_metric_percentiles=_percentile_frame(summary.terminal_samples, percentiles),
        terminal_metric_samples=_terminal_samples_frame(seeds, summary),
        failed_count=_failed_count(summary.failed_month),
    )


def _percentile_frame(samples: np.ndarray, percentiles: tuple[float, ...]) -> Frame:
    return _quantile_frame(percentiles, np.asarray(currency_quantiles(samples, percentiles), dtype=np.int64))


def _quantile_frame(percentiles: tuple[float, ...], values: np.ndarray) -> Frame:
    return {"percentile": list(percentiles), "value_quanta": [_quanta(value) for value in values]}


def _terminal_samples_frame(seeds: tuple[int, ...], summary: ProductTerminalSummary) -> Frame:
    return {
        "seed": list(seeds),
        "value_quanta": [_quanta(value) for value in summary.terminal_samples],
        "failed": (summary.failed_month >= 0).tolist(),
    }


def _failed_count(failed_month: np.ndarray) -> int:
    return int((failed_month >= 0).sum())


def _quanta(value: int | np.integer[Any]) -> str:
    """Serialize an integer quantum count without a lossy JSON number."""

    return str(value)


def _terminal_metrics_from_arrays(arrays: dict[str, np.ndarray], *, failed_month_index: int | None) -> TerminalMetrics:
    """Build the rollout wire's terminal snapshot from JAX-emitted metric series."""

    return TerminalMetrics(
        cash_quanta=_quanta(arrays["cash_quanta"][-1]),
        holding_value_quanta=_quanta(arrays["holding_value_quanta"][-1]),
        private_equity_value_quanta=_quanta(arrays["private_equity_value_quanta"][-1]),
        property_value_quanta=_quanta(arrays["property_value_quanta"][-1]),
        mortgage_balance_quanta=_quanta(arrays["mortgage_balance_quanta"][-1]),
        bond_value_quanta=_quanta(arrays["bond_value_quanta"][-1]),
        home_equity_quanta=_quanta(arrays["home_equity_quanta"][-1]),
        liquid_net_worth_quanta=_quanta(arrays["liquid_net_worth_quanta"][-1]),
        net_worth_quanta=_quanta(arrays["net_worth_quanta"][-1]),
        shortfall_quanta=_quanta(arrays["shortfall_quanta"].sum()),
        failed_month_index=failed_month_index,
    )

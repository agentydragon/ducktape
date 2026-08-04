"""Product-shaped query surface. Owns the product simulation model.

Holds the slice of augur config the product surface needs (portfolio, primary
agent, initial cash); does not know about properties, locations, or bootstrap.
"""

from __future__ import annotations

import threading

import numpy as np

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
from finance.augur.product.decode import (
    failed_month_index_batch,
    monthly_metric_arrays,
    rollout_events_from,
    terminal_metrics_from_arrays,
)
from finance.augur.product.scenarios import (
    asset_label_by_series_id,
    build_scenario,
    initial_lots_from_portfolio,
    required_private_equity_issuers,
    sell_bucket_by_asset,
)
from finance.augur.product.wire import (
    MetricFanRequest,
    MetricFanResponse,
    RolloutOutput,
    RolloutRequest,
    RolloutResponse,
    ScenarioKey,
    TerminalDistributionRequest,
    TerminalDistributionResponse,
)
from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.compiler import compile_simulation
from finance.augur.sim.compiler.series import scenario_level_series_keys
from finance.augur.sim.engine.jax_engine import ProductSummary, run_jax_product_summary
from finance.augur.sim.external_series import materialize_sampled_exogenous
from finance.augur.sim.locations import Location
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.scenario import HarvestPolicy, Scenario
from finance.augur.sim.simulate import simulate_with_external_series


class ProductService:
    def __init__(
        self,
        *,
        portfolio: PortfolioConfig,
        initial_cash_usd: float,
        primary_agent_id: str,
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
        self._initial_cash_usd = float(initial_cash_usd)
        self._primary_agent_id = primary_agent_id
        self._known_location_ids = known_location_ids
        self._locations = locations
        self._properties_by_id = properties_by_id
        self._models = models
        self._max_rollout_samples = int(max_rollout_samples)
        self._max_horizon_months = int(max_horizon_months)
        self._initial_lots = initial_lots_from_portfolio(portfolio, primary_agent_id=primary_agent_id)
        self._harvest_policies = harvest_policies
        self._asset_label_by_id = asset_label_by_series_id(portfolio)
        # Keep one product projection in flight per API process. JAX/XLA batches are memory-heavy
        # enough that overlapping fan + terminal requests can exceed the production pod limit.
        self._projection_lock = threading.Lock()

    def metric_fan(self, request: MetricFanRequest) -> MetricFanResponse:
        if request.rollout_count > self._max_rollout_samples:
            raise ValueError(f"rollout count {request.rollout_count} exceeds max {self._max_rollout_samples}")
        percentiles = tuple(float(pct) for pct in request.percentiles)
        with self._projection_lock:
            summary, model_id = self._simulate_product_summary(
                request.scenario, request.rollout_seeds, metric=request.metric, percentiles=percentiles
            )
            return MetricFanResponse(
                model_id=model_id,
                metric=request.metric,
                monthly_metric_fan=_monthly_fan_frame(summary, percentiles),
                terminal_metric_percentiles=_percentile_frame(summary.terminal_samples, percentiles),
                failed_count=_failed_count(summary),
            )

    def terminal_distribution(self, request: TerminalDistributionRequest) -> TerminalDistributionResponse:
        if request.rollout_count > self._max_rollout_samples:
            raise ValueError(f"rollout count {request.rollout_count} exceeds max {self._max_rollout_samples}")
        percentiles = tuple(float(pct) for pct in request.percentiles)
        with self._projection_lock:
            summary, model_id = self._simulate_product_summary(
                request.scenario, request.rollout_seeds, metric=request.metric, percentiles=None
            )
            return TerminalDistributionResponse(
                model_id=model_id,
                metric=request.metric,
                terminal_metric_percentiles=_percentile_frame(summary.terminal_samples, percentiles),
                terminal_metric_samples=_terminal_samples_frame(request.rollout_seeds, summary),
                failed_count=_failed_count(summary),
            )

    def rollout(self, request: RolloutRequest) -> RolloutResponse:
        with self._projection_lock:
            return self._rollout_response(request.scenario, int(request.seed))

    def _rollout_response(self, scenario: ScenarioKey, seed: int) -> RolloutResponse:
        self._validate_scenario_key(scenario)
        horizon_months = int(scenario.horizon_months)
        dense, model_id = self._simulate_dense(scenario, (seed,))
        monthly_arrays = monthly_metric_arrays(dense, primary_agent_id=self._primary_agent_id)
        failed_month = int(failed_month_index_batch(dense)[0])
        terminal = terminal_metrics_from_arrays(
            monthly_arrays, failed_month_index=None if failed_month < 0 else failed_month
        )
        events = tuple(
            event
            for event in rollout_events_from(
                dense, primary_agent_id=self._primary_agent_id, asset_label_by_id=self._asset_label_by_id
            )
            if event.month_index < horizon_months
        )
        # `monthly_metrics` ships as `Frame = dict[str, list[...]]`; build directly from numpy
        # instead of round-tripping through polars.
        monthly_metrics_frame = {name: arr.tolist() for name, arr in monthly_arrays.items()}
        return RolloutResponse(
            model_id=model_id,
            rollout=RolloutOutput(
                seed=seed,
                failed=terminal.failed_month_index is not None,
                monthly_metrics=monthly_metrics_frame,
                terminal_metrics=terminal,
                events=events,
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

    def _simulate_product_summary(
        self, scenario_key: ScenarioKey, seeds: tuple[int, ...], *, metric: str, percentiles: tuple[float, ...] | None
    ) -> tuple[ProductSummary, str]:
        self._validate_scenario_key(scenario_key)
        scenario, sampled, model_id = self._scenario_and_sample(scenario_key, seeds)
        plan = compile_simulation(
            scenario,
            rollout_count=len(seeds),
            external_series=materialize_sampled_exogenous(sampled),
            jurisdictions=load_jurisdictions_for(scenario),
            locations=self._locations,
        )
        summary = run_jax_product_summary(
            plan, primary_agent_id=self._primary_agent_id, metric=metric, percentiles=percentiles
        )
        return summary, model_id

    def _simulate_dense(self, scenario_key: ScenarioKey, seeds: tuple[int, ...]) -> tuple[SimulationRun, str]:
        scenario, sampled, model_id = self._scenario_and_sample(scenario_key, seeds)
        dense = simulate_with_external_series(
            scenario,
            rollout_count=len(seeds),
            external_series=materialize_sampled_exogenous(sampled),
            locations=self._locations,
        )
        return dense, model_id

    def _scenario_and_sample(
        self, scenario_key: ScenarioKey, seeds: tuple[int, ...]
    ) -> tuple[Scenario, SampledExogenousBundle, str]:
        scenario = build_scenario(
            scenario_key,
            primary_agent_id=self._primary_agent_id,
            initial_cash_usd=self._initial_cash_usd,
            initial_lots=self._initial_lots,
            sell_buckets=sell_bucket_by_asset(self._portfolio),
            properties_by_id=self._properties_by_id,
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
        model_id = str(sampled.metadata.get("model_id") or scenario_key.model_id)
        return scenario, sampled, model_id


def _monthly_fan_frame(summary: ProductSummary, percentiles: tuple[float, ...]) -> Frame:
    month_indices = summary.month_index
    bands = summary.monthly_bands  # (n_percentiles, H+1)
    assert bands is not None, "monthly fan requires percentiles"
    percentile_array = np.asarray(percentiles, dtype=np.float64)
    return {
        "month_index": np.repeat(month_indices, percentile_array.size).tolist(),
        "percentile": np.tile(percentile_array, month_indices.size).tolist(),
        "value": bands.T.reshape(-1).tolist(),
    }


def _percentile_frame(samples: np.ndarray, percentiles: tuple[float, ...]) -> Frame:
    percentile_array = np.asarray(percentiles, dtype=np.float64)
    values = np.percentile(samples, percentile_array, method="linear")
    return {"percentile": percentile_array.tolist(), "value": np.asarray(values, dtype=np.float64).tolist()}


def _terminal_samples_frame(seeds: tuple[int, ...], summary: ProductSummary) -> Frame:
    return {
        "seed": list(seeds),
        "value": summary.terminal_samples.tolist(),
        "failed": (summary.failed_month >= 0).tolist(),
    }


def _failed_count(summary: ProductSummary) -> int:
    return int((summary.failed_month >= 0).sum())

"""Product-shaped query surface. Owns the rollout LRU cache and the model.

Holds the slice of augur config the product surface needs (portfolio, primary
agent, initial cash); does not know about properties, locations, or bootstrap.
The cache stores per-rollout R=1 DenseSimulationResult primitives keyed by
``(ScenarioKey, seed)``, where the key's ``horizon_months`` is normalized to the
server max horizon: every rollout is simulated once to that max and truncated to
the per-request horizon, so changing the requested horizon never re-simulates.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import cast

import numpy as np

from augur.api.portfolio import PortfolioConfig
from augur.api.schemas import Frame
from augur.api.wire import Property
from augur.model.exogenous import (
    ExogenousSamplingRequest,
    Sampler,
    anchor_sampled_series_levels,
    validate_sample_satisfies_request,
)
from augur.product.decode import (
    failed_month_index_for_rollout,
    monthly_metric_arrays,
    rollout_events_from,
    terminal_metrics_from_arrays,
)
from augur.product.scenarios import (
    asset_label_by_series_id,
    build_scenario,
    initial_lots_from_portfolio,
    required_level_series,
    required_private_equity_issuers,
)
from augur.product.wire import (
    MetricFanRequest,
    MetricFanResponse,
    MetricName,
    RolloutOutput,
    RolloutRequest,
    RolloutResponse,
    RolloutSummary,
    ScenarioKey,
    TerminalMetrics,
)
from augur.sim.codec.plan import DenseSimulationResult
from augur.sim.external_series import materialize_sampled_exogenous
from augur.sim.locations import Location
from augur.sim.simulate import simulate_dense_with_external_series
from augur.sim.slice import slice_dense_result

DEFAULT_MAX_CACHE_ROLLOUTS = 25_000


@dataclass(frozen=True)
class _CachedRollout:
    # One simulated batch is shared by every seed it was sampled with; each seed reads its own
    # column out of `batch` (the metric reductions take a `rollout_index`), so no per-rollout
    # dense slice is materialized for the fan path. The batch stays alive while any of its seeds
    # remains cached; `rollout()` slices a single seed out of it on demand for the detail view.
    batch: DenseSimulationResult
    column_index: int
    model_id: str


@dataclass(frozen=True)
class _DecodedRollout:
    seed: int
    monthly_metric_arrays: dict[str, np.ndarray]
    terminal_metrics: TerminalMetrics
    cached: _CachedRollout

    @property
    def failed(self) -> bool:
        return self.terminal_metrics.failed_month_index is not None


class ProductService:
    def __init__(
        self,
        *,
        portfolio: PortfolioConfig,
        initial_cash_usd: float,
        primary_agent_id: str,
        known_location_ids: frozenset[str],
        locations: dict[str, Location],
        properties_by_id: dict[str, Property],
        models: dict[str, Sampler],
        max_rollout_samples: int,
        max_horizon_months: int,
        max_cache_rollouts: int = DEFAULT_MAX_CACHE_ROLLOUTS,
    ) -> None:
        if max_cache_rollouts <= 0:
            raise ValueError("max_cache_rollouts must be positive")
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
        self._max_cache_rollouts = int(max_cache_rollouts)
        self._initial_lots = initial_lots_from_portfolio(portfolio, primary_agent_id=primary_agent_id)
        self._asset_label_by_id = asset_label_by_series_id(portfolio)
        self._cache: OrderedDict[tuple[ScenarioKey, int], _CachedRollout] = OrderedDict()
        # FastAPI + uvicorn dispatches request handlers concurrently. The cache's get→miss→
        # simulate→put sequence is not atomic; without a lock two simultaneous metric-fan
        # requests on the same scenario+seed can both run the simulation. The lock is held
        # only across the OrderedDict ops (which are O(1) plus the move_to_end / popitem
        # bookkeeping) — `_simulate_missing` runs outside the lock so we don't serialize
        # CPU-bound simulations.
        self._cache_lock = threading.Lock()

    def metric_fan(self, request: MetricFanRequest) -> MetricFanResponse:
        if request.rollout_count > self._max_rollout_samples:
            raise ValueError(f"rollout count {request.rollout_count} exceeds max {self._max_rollout_samples}")
        decoded = self._decoded_rollouts(request.scenario, tuple(int(seed) for seed in request.rollout_seeds))
        model_id = decoded[0].cached.model_id if decoded else request.scenario.model_id
        percentiles = tuple(float(pct) for pct in request.percentiles)
        return MetricFanResponse(
            model_id=model_id,
            metric=request.metric,
            monthly_metric_fan=_monthly_metric_fan(decoded, metric=request.metric, percentiles=percentiles),
            terminal_metric_percentiles=_terminal_metric_percentiles(
                decoded, metric=request.metric, percentiles=percentiles
            ),
            rollout_summaries=_rollout_summaries(decoded),
            failed_count=sum(1 for rollout in decoded if rollout.failed),
        )

    def rollout(self, request: RolloutRequest) -> RolloutResponse:
        horizon_months = int(request.scenario.horizon_months)
        [decoded] = self._decoded_rollouts(request.scenario, (int(request.seed),))
        # The detail view needs this one rollout's event log: slice just its column out of the
        # shared batch (one slice, not R) and decode it. The cached batch spans the server max
        # horizon; keep only events within the requested window so the detail matches the
        # truncated monthly metrics.
        single = slice_dense_result(decoded.cached.batch, rollout_index=decoded.cached.column_index)
        events = tuple(
            event
            for event in rollout_events_from(
                single.decode(),
                primary_agent_id=self._primary_agent_id,
                asset_label_by_id=self._asset_label_by_id,
            )
            if event.month_index < horizon_months
        )
        # `monthly_metrics` ships as `Frame = dict[str, list[...]]`; build directly from numpy
        # instead of round-tripping through polars.
        monthly_metrics_frame = {name: arr.tolist() for name, arr in decoded.monthly_metric_arrays.items()}
        return RolloutResponse(
            model_id=decoded.cached.model_id,
            rollout=RolloutOutput(
                seed=decoded.seed,
                failed=decoded.failed,
                monthly_metrics=monthly_metrics_frame,
                terminal_metrics=decoded.terminal_metrics,
                events=events,
            ),
        )

    def _decoded_rollouts(self, scenario_key: ScenarioKey, seeds: tuple[int, ...]) -> tuple[_DecodedRollout, ...]:
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
        # Every scenario+seed is simulated once at the server max horizon, cached under a horizon-
        # normalized key, then truncated to the requested horizon below. So scrolling the requested
        # horizon reuses the cached max-length rollout instead of re-simulating.
        cache_key = scenario_key.model_copy(update={"horizon_months": self._max_horizon_months})
        cached_by_seed: dict[int, _CachedRollout] = {}
        missing: list[int] = []
        for seed in seeds:
            entry = self._cache_get(cache_key, seed)
            if entry is None:
                missing.append(seed)
            else:
                cached_by_seed[seed] = entry
        if missing:
            fresh = self._simulate_missing(cache_key, tuple(missing))
            for seed, entry in fresh.items():
                cached_by_seed[seed] = entry
                self._cache_put(cache_key, seed, entry)
        decoded: list[_DecodedRollout] = []
        for seed in seeds:
            cached = cached_by_seed[seed]
            full_arrays = monthly_metric_arrays(
                cached.batch, primary_agent_id=self._primary_agent_id, rollout_index=cached.column_index
            )
            # `month_index` runs 0..H_max; keep months 0..horizon (i.e. the first horizon+1 snapshots).
            arrays = {name: array[: horizon_months + 1] for name, array in full_arrays.items()}
            full_failed_month = failed_month_index_for_rollout(cached.batch, rollout_index=cached.column_index)
            # Months are 0-based (0..horizon-1); a failure at/after the requested horizon is outside it.
            in_window = full_failed_month is not None and full_failed_month < horizon_months
            terminal = terminal_metrics_from_arrays(arrays, failed_month_index=full_failed_month if in_window else None)
            decoded.append(
                _DecodedRollout(seed=seed, monthly_metric_arrays=arrays, terminal_metrics=terminal, cached=cached)
            )
        return tuple(decoded)

    def _simulate_missing(self, scenario_key: ScenarioKey, seeds: tuple[int, ...]) -> dict[int, _CachedRollout]:
        scenario = build_scenario(
            scenario_key,
            primary_agent_id=self._primary_agent_id,
            initial_cash_usd=self._initial_cash_usd,
            initial_lots=self._initial_lots,
            properties_by_id=self._properties_by_id,
        )
        sampling_request = ExogenousSamplingRequest(
            horizon_months=int(scenario_key.horizon_months),
            rollout_seeds=seeds,
            **required_level_series(
                scenario_key, initial_lots=self._initial_lots, properties_by_id=self._properties_by_id
            ),
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
        dense = simulate_dense_with_external_series(
            scenario,
            rollout_count=len(seeds),
            external_series=materialize_sampled_exogenous(sampled),
            locations=self._locations,
        )
        model_id = str(sampled.metadata.get("model_id") or scenario_key.model_id)
        # Cache the batch once, shared by every seed; each seed records only its column. The fan
        # path reduces a column per seed straight from this batch (no per-rollout slice), and the
        # detail view slices a single seed out of it on demand.
        return {
            seed: _CachedRollout(batch=dense, column_index=batch_index, model_id=model_id)
            for batch_index, seed in enumerate(seeds)
        }

    def _cache_get(self, scenario_key: ScenarioKey, seed: int) -> _CachedRollout | None:
        key = (scenario_key, seed)
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            self._cache.move_to_end(key)
            return entry

    def _cache_put(self, scenario_key: ScenarioKey, seed: int, entry: _CachedRollout) -> None:
        key = (scenario_key, seed)
        with self._cache_lock:
            self._cache[key] = entry
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_cache_rollouts:
                self._cache.popitem(last=False)


def _monthly_metric_fan(
    rollouts: tuple[_DecodedRollout, ...], *, metric: MetricName, percentiles: tuple[float, ...]
) -> Frame:
    matrix = _metric_matrix(rollouts, metric=metric)
    if matrix is None:
        return {"month_index": [], "percentile": [], "value": []}
    month_indices, values = matrix
    percentile_values = _percentile(values, percentiles, axis=0)
    percentile_array = np.asarray(percentiles, dtype=np.float64)
    return {
        "month_index": np.repeat(month_indices, percentile_array.size).tolist(),
        "percentile": np.tile(percentile_array, month_indices.size).tolist(),
        "value": percentile_values.T.reshape(-1).tolist(),
    }


def _terminal_metric_percentiles(
    rollouts: tuple[_DecodedRollout, ...], *, metric: MetricName, percentiles: tuple[float, ...]
) -> Frame:
    values = np.asarray(
        [_terminal_metric_value(rollout.terminal_metrics, metric) for rollout in rollouts], dtype=np.float64
    )
    if values.size == 0:
        return {"percentile": [], "value": []}
    percentile_array = np.asarray(percentiles, dtype=np.float64)
    percentile_values = _percentile(values, percentiles, axis=0)
    return {"percentile": percentile_array.tolist(), "value": percentile_values.tolist()}


def _metric_matrix(
    rollouts: tuple[_DecodedRollout, ...], *, metric: MetricName
) -> tuple[np.ndarray, np.ndarray] | None:
    if not rollouts:
        return None
    month_indices = rollouts[0].monthly_metric_arrays["month_index"]
    values = np.empty((len(rollouts), month_indices.size), dtype=np.float64)
    for rollout_index, rollout in enumerate(rollouts):
        rollout_months = rollout.monthly_metric_arrays["month_index"]
        if rollout_months.shape != month_indices.shape or not np.array_equal(rollout_months, month_indices):
            raise ValueError("metric fan rollouts have inconsistent month indices")
        values[rollout_index] = rollout.monthly_metric_arrays[metric].astype(np.float64, copy=False)
    return month_indices, values


def _percentile(values: np.ndarray, percentiles: tuple[float, ...], *, axis: int) -> np.ndarray:
    return cast(
        np.ndarray, np.percentile(values, np.asarray(percentiles, dtype=np.float64), axis=axis, method="linear")
    )


def _terminal_metric_value(terminal: TerminalMetrics, metric: MetricName) -> float:
    match metric:
        case "cash_usd":
            return terminal.cash_usd
        case "holding_value_usd":
            return terminal.holding_value_usd
        case "private_equity_value_usd":
            return terminal.private_equity_value_usd
        case "property_value_usd":
            return terminal.property_value_usd
        case "mortgage_balance_usd":
            return terminal.mortgage_balance_usd
        case "home_equity_usd":
            return terminal.home_equity_usd
        case "liquid_net_worth_usd":
            return terminal.liquid_net_worth_usd
        case "net_worth_usd":
            return terminal.net_worth_usd
        case "shortfall_usd":
            return terminal.shortfall_usd


def _rollout_summaries(rollouts: tuple[_DecodedRollout, ...]) -> tuple[RolloutSummary, ...]:
    sorted_rollouts = sorted(rollouts, key=_rollout_sort_key)
    count = len(sorted_rollouts)
    return tuple(
        RolloutSummary(
            seed=rollout.seed,
            failed=rollout.failed,
            terminal_metrics=rollout.terminal_metrics,
            sort_rank=rank,
            rank_percentile=((rank + 0.5) / count * 100) if count else 50.0,
        )
        for rank, rollout in enumerate(sorted_rollouts)
    )


def _rollout_sort_key(rollout: _DecodedRollout) -> tuple[bool, int, float, int]:
    terminal = rollout.terminal_metrics
    failed_month = terminal.failed_month_index if terminal.failed_month_index is not None else 10**9
    return (not rollout.failed, failed_month, terminal.net_worth_usd, rollout.seed)

"""External-series wrangling: collect referenced series IDs from a scenario, and
build the dense `(series, rollout, month)` cubes the engine reads at runtime.

Separated from the orchestrator so the compile_simulation function in
`compiler/plan.py` reads as pure scaffolding and the per-domain compilers can
import these helpers directly when they need to encode `SeriesIndexedAmount`
fields."""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from finance.augur.model.series import HomeValueKey, LevelSeriesKey, LocationId, try_parse_level_series_key
from finance.augur.product.asset_key import asset_price_key, asset_price_key_or_none
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.scenario import Scenario, SeriesIndexedAmount


def scenario_level_series_keys(scenario: Scenario) -> tuple[LevelSeriesKey, ...]:
    """Every level series the scenario REFERENCES — its exogenous demand.

    Derivable before anything is sampled, which is the point: it lets the caller ask the
    exogenous model for exactly this set instead of re-deriving the same fact from the
    product wire type in a second, drifting implementation.

    Must stay exhaustive over the compiler's series lookups. Each entry below corresponds to
    a `series_index_by_id[...]` in `compiler/`; a demand missing here surfaces as a `NO_CODE`
    series index, which the engine rejects for holdings and which
    `_reject_missing_property_sale_home_values` rejects for property sales.
    """

    keys: list[LevelSeriesKey] = []
    seen: set[LevelSeriesKey] = set()

    def add(key: LevelSeriesKey | None) -> None:
        if key is not None and key not in seen:
            seen.add(key)
            keys.append(key)

    # Holdings are marked every month off their asset-price series (`plan.lot_asset_series_index`).
    for lot in scenario.initial_lots:
        add(asset_price_key_or_none(lot.asset))
    for scheduled_transfer in scenario.scheduled_transfers:
        _add_amount_series_key(scheduled_transfer.amount_usd, add)
    for recurring_transfer in scenario.recurring_transfers:
        _add_amount_series_key(recurring_transfer.amount_usd, add)
    for scheduled_cashflow in scenario.scheduled_property_cashflows:
        _add_amount_series_key(scheduled_cashflow.amount_usd, add)
    for recurring_cashflow in scenario.recurring_property_cashflows:
        _add_amount_series_key(recurring_cashflow.amount_usd, add)
    for scheduled_obligation in scenario.scheduled_obligations:
        _add_amount_series_key(scheduled_obligation.amount_due_usd, add)
    for recurring_obligation in scenario.recurring_obligations:
        _add_amount_series_key(recurring_obligation.amount_due_usd, add)
    for sale in scenario.scheduled_asset_sales:
        if sale.price_per_unit_usd is None:
            add(asset_price_key(sale.asset))
    for policy in scenario.liquidity_policies:
        for asset in policy.asset_preference_chain:
            add(asset_price_key_or_none(asset))
        _add_amount_series_key(policy.cash_buffer_trigger_below_usd, add)
        _add_amount_series_key(policy.cash_buffer_sale_usd, add)
    for pe_policy in scenario.private_equity_tender_policies:
        _add_amount_series_key(pe_policy.liquid_net_worth_floor, add)
    # A property is valued at sale off its location's home-value series.
    for purchase in scenario.scheduled_property_purchases:
        add(HomeValueKey(location_id=LocationId(purchase.location_id)))
    return tuple(keys)


def collect_level_series_keys(scenario: Scenario, external_series: ExternalSeriesContext) -> tuple[LevelSeriesKey, ...]:
    """Distinct typed level-series keys the compiled cube carries a row for.

    Deliberately NOT `scenario_level_series_keys`: that is the scenario's *demand*, this is
    what the cube can actually serve. A demanded series nobody sampled must stay absent here
    so it resolves to `NO_CODE` and fails as "no modeled price series" — naming the real
    problem — rather than getting an all-NaN row and failing later as a non-finite price.

    The scenario walk below is only for lookups the compiler does with `[]` rather than
    `.get(..., NO_CODE)`, which would otherwise raise `KeyError`.
    """

    keys: list[LevelSeriesKey] = []
    seen: set[LevelSeriesKey] = set()

    def add(key: LevelSeriesKey | None) -> None:
        if key is not None and key not in seen:
            seen.add(key)
            keys.append(key)

    # `.sort()` after `.unique()`: polars `unique` returns rows in non-deterministic hash order, which
    # would assign series row-indices differently per compile. Those indices are baked into the jitted
    # program's STATIC structure (e.g. `_FoldedPE.floor_series`), so a non-deterministic order busts the
    # native `jax.jit` compile cache (every other compile re-traces). Sorting makes the index assignment
    # deterministic so identical scenarios produce an identical structure → one compile, then cache hits.
    for series_id in (
        external_series.series_values.select("series_id").unique().get_column("series_id").sort().to_list()
    ):
        # Boundary parse: the external frame is still series_id-string keyed (typed in the frame
        # pass). Every row is a non-PE level series, so the parse must succeed.
        add(_require_level_series_key(str(series_id)))
    for scheduled_transfer in scenario.scheduled_transfers:
        _add_amount_series_key(scheduled_transfer.amount_usd, add)
    for recurring_transfer in scenario.recurring_transfers:
        _add_amount_series_key(recurring_transfer.amount_usd, add)
    for scheduled_cashflow in scenario.scheduled_property_cashflows:
        _add_amount_series_key(scheduled_cashflow.amount_usd, add)
    for recurring_cashflow in scenario.recurring_property_cashflows:
        _add_amount_series_key(recurring_cashflow.amount_usd, add)
    for scheduled_obligation in scenario.scheduled_obligations:
        _add_amount_series_key(scheduled_obligation.amount_due_usd, add)
    for recurring_obligation in scenario.recurring_obligations:
        _add_amount_series_key(recurring_obligation.amount_due_usd, add)
    for sale in scenario.scheduled_asset_sales:
        if sale.price_per_unit_usd is None:
            add(asset_price_key(sale.asset))
    for policy in scenario.liquidity_policies:
        for asset in policy.asset_preference_chain:
            add(asset_price_key_or_none(asset))
    return tuple(keys)


def _require_level_series_key(series_id: str) -> LevelSeriesKey:
    key = try_parse_level_series_key(series_id)
    if key is None:
        raise ValueError(f"external series frame carries non-level-series id {series_id!r}")
    return key


def _add_amount_series_key(amount: Any, add: Any) -> None:
    if isinstance(amount, SeriesIndexedAmount):
        add(amount.series)


def external_values_cube(
    external_series: ExternalSeriesContext,
    *,
    series_index_by_id: dict[LevelSeriesKey, int],
    rollout_count: int,
    horizon_months: int,
) -> np.ndarray:
    values = np.full((len(series_index_by_id), rollout_count, horizon_months + 1), np.nan, dtype=np.float64)
    frame = external_series.series_values
    if frame.is_empty():
        return values
    # The external frame is still keyed by series_id wire strings; bridge to the typed index
    # once via wire_id (removed when the frame itself goes typed). Vectorized scatter: map
    # series_id → compiled index columnwise, then a single fancy-index assignment, instead of
    # a Python loop over every (rollout, month, series) row (millions at a 100-year horizon).
    index_by_wire_id = {key.wire_id: index for key, index in series_index_by_id.items()}
    series_index = (
        frame.get_column("series_id").replace_strict(index_by_wire_id, default=-1, return_dtype=pl.Int64).to_numpy()
    )
    rollout_index = frame.get_column("rollout_index").to_numpy()
    month_index = frame.get_column("month_index").to_numpy()
    value = frame.get_column("value").to_numpy()
    keep = (
        (series_index >= 0)
        & (rollout_index >= 0)
        & (rollout_index < rollout_count)
        & (month_index >= 0)
        & (month_index <= horizon_months)
    )
    values[series_index[keep], rollout_index[keep], month_index[keep]] = value[keep]
    return values

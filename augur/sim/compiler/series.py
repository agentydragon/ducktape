"""External-series wrangling: collect referenced series IDs from a scenario, and
build the dense `(series, rollout, month)` cubes the engine reads at runtime.

Separated from the orchestrator so the compile_simulation function in
`compiler/plan.py` reads as pure scaffolding and the per-domain compilers can
import these helpers directly when they need to encode `SeriesIndexedAmount`
fields."""

from __future__ import annotations

from typing import Any

import numpy as np

from augur.model.series import LevelSeriesKey, try_parse_level_series_key
from augur.product.asset_key import asset_price_key, asset_price_key_or_none
from augur.sim.external_series import ExternalSeriesContext
from augur.sim.scenario import Scenario, SeriesIndexedAmount


def collect_level_series_keys(scenario: Scenario, external_series: ExternalSeriesContext) -> tuple[LevelSeriesKey, ...]:
    """Distinct typed level-series keys the cube must carry: every series materialized in the
    external frame, plus the typed key each scenario reference resolves to (amount indices, sale
    /liquidity asset prices). PE assets resolve to `None` (priced off-series) and are skipped."""

    keys: list[LevelSeriesKey] = []
    seen: set[LevelSeriesKey] = set()

    def add(key: LevelSeriesKey | None) -> None:
        if key is not None and key not in seen:
            seen.add(key)
            keys.append(key)

    for series_id in external_series.series_values.select("series_id").unique().get_column("series_id").to_list():
        # Boundary parse: the external frame is still series_id-string keyed (typed in the frame
        # pass). Every row is a non-PE level series, so the parse must succeed.
        add(_require_level_series_key(str(series_id)))
    for scheduled_transfer in scenario.scheduled_transfers:
        _add_amount_series_key(scheduled_transfer.amount_usd, add)
    for recurring_transfer in scenario.recurring_transfers:
        _add_amount_series_key(recurring_transfer.amount_usd, add)
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
    if external_series.series_values.is_empty():
        return values
    # The external frame is still keyed by series_id wire strings; bridge to the typed index
    # once via wire_id (removed when the frame itself goes typed).
    index_by_wire_id = {key.wire_id: index for key, index in series_index_by_id.items()}
    for row in external_series.series_values.iter_rows(named=True):
        series_index = index_by_wire_id.get(str(row["series_id"]))
        if series_index is None:
            continue
        rollout_index = int(row["rollout_index"])
        month_index = int(row["month_index"])
        if 0 <= rollout_index < rollout_count and 0 <= month_index <= horizon_months:
            values[series_index, rollout_index, month_index] = float(row["value"])
    return values

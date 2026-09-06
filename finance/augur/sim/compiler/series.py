"""External-series wrangling: collect referenced series IDs from a scenario, and
build the dense `(series, rollout, month)` cubes the engine reads at runtime.

Separated from the orchestrator so the compile_simulation function in
`compiler/plan.py` reads as pure scaffolding and the per-domain compilers can
import these helpers directly when they need to encode `SeriesIndexedAmount`
fields."""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from typing import Any, NamedTuple

import numpy as np
from jaxtyping import Bool, Float64, Int64

from finance.augur.model.series import (
    HomeValueKey,
    InflationKey,
    LevelSeriesKey,
    LocationId,
    SecurityDistributionKey,
    SecurityKey,
)
from finance.augur.product.asset_key import asset_price_key, asset_price_key_or_none
from finance.augur.sim.fixed_point import sampled_array_to_quanta
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
    # A TIPS' principal rides CPI, so an inflation-indexed bond DEMANDS inflation even when
    # nothing else in the scenario does. Without this, `compile_bonds._cpi_series_row` raises
    # ("carry no inflation path") for any scenario that does not happen to want CPI for another
    # reason — a CPI-indexed spend, cash band, tender floor, or property obligation.
    #
    # Demand side only, deliberately: the supply-side twin must NOT add this. Inflation reaches
    # the cube by having been SAMPLED; adding the key there when nobody sampled it would give
    # the TIPS an all-NaN price row instead of the loud raise, which is strictly worse.
    if any(bond.inflation_indexed for bond in scenario.initial_bonds):
        add(InflationKey())
    # A distributing security demands TWO series: its price (already demanded by the lots that
    # hold it) and its dollars-per-unit payout, which nothing else references.
    for distribution in scenario.security_distributions:
        add(SecurityDistributionKey(symbol=asset_price_key(distribution.asset).symbol))
    for scheduled_transfer in scenario.scheduled_transfers:
        _add_amount_series_key(scheduled_transfer.amount, add)
    for recurring_transfer in scenario.recurring_transfers:
        _add_amount_series_key(recurring_transfer.amount, add)
    for scheduled_cashflow in scenario.scheduled_property_cashflows:
        _add_amount_series_key(scheduled_cashflow.amount, add)
    for recurring_cashflow in scenario.recurring_property_cashflows:
        _add_amount_series_key(recurring_cashflow.amount, add)
    for scheduled_obligation in scenario.scheduled_obligations:
        _add_amount_series_key(scheduled_obligation.amount_due, add)
    for recurring_obligation in scenario.recurring_obligations:
        _add_amount_series_key(recurring_obligation.amount_due, add)
    for sale in scenario.scheduled_asset_sales:
        add(asset_price_key(sale.asset))
    for policy in scenario.target_allocation_policies:
        for sleeve in policy.sleeves:
            add(asset_price_key_or_none(sleeve.asset))
        # Both band bounds, not just the floor: the ceiling is the refill TARGET, so a raise
        # cannot be sized without it, and an indexed ceiling needs its series sampled.
        _add_amount_series_key(policy.cash_floor, add)
        _add_amount_series_key(policy.cash_ceiling, add)
    for pe_policy in scenario.private_equity_tender_policies:
        _add_amount_series_key(pe_policy.liquid_net_worth_floor, add)
    # A property is valued at sale off its location's home-value series.
    for purchase in scenario.scheduled_property_purchases:
        add(HomeValueKey(location_id=LocationId(purchase.location_id)))
    return tuple(keys)


class MaterializedLevelRows(NamedTuple):
    key: LevelSeriesKey
    rollout_index: Int64[np.ndarray, " observation"]
    month_index: Int64[np.ndarray, " observation"]
    values: Float64[np.ndarray, " observation"]
    present: Bool[np.ndarray, " observation"]
    in_bounds: Bool[np.ndarray, " observation"]


def materialize_level_rows(
    value_rows: tuple[tuple[LevelSeriesKey, Any], ...], *, rollout_count: int, horizon_months: int
) -> tuple[MaterializedLevelRows, ...]:
    """Read every split series frame into indexed NumPy columns once."""

    rows: list[MaterializedLevelRows] = []
    for key, frame in value_rows:
        rollout_index, month_index, values, in_bounds = _frame_values(frame, rollout_count, horizon_months)
        rows.append(
            MaterializedLevelRows(
                key=key,
                rollout_index=rollout_index,
                month_index=month_index,
                values=values,
                present=frame.get_column("value").is_not_null().to_numpy(),
                in_bounds=in_bounds,
            )
        )
    return tuple(rows)


def collect_level_series_keys(
    scenario: Scenario, level_rows: tuple[MaterializedLevelRows, ...]
) -> tuple[LevelSeriesKey, ...]:
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

    # `value_rows()` is ordered by wire id. Series row-indices are assigned from that order and
    # baked into the jitted program's STATIC structure (e.g. `_FoldedPE.floor_series`), so a
    # content-independent order would bust the native `jax.jit` compile cache (every other compile
    # re-traces); a deterministic one gives identical scenarios one compile, then cache hits.
    for rows in level_rows:
        add(rows.key)
    for scheduled_transfer in scenario.scheduled_transfers:
        _add_amount_series_key(scheduled_transfer.amount, add)
    for recurring_transfer in scenario.recurring_transfers:
        _add_amount_series_key(recurring_transfer.amount, add)
    for scheduled_cashflow in scenario.scheduled_property_cashflows:
        _add_amount_series_key(scheduled_cashflow.amount, add)
    for recurring_cashflow in scenario.recurring_property_cashflows:
        _add_amount_series_key(recurring_cashflow.amount, add)
    for scheduled_obligation in scenario.scheduled_obligations:
        _add_amount_series_key(scheduled_obligation.amount_due, add)
    for recurring_obligation in scenario.recurring_obligations:
        _add_amount_series_key(recurring_obligation.amount_due, add)
    for sale in scenario.scheduled_asset_sales:
        add(asset_price_key(sale.asset))
    for policy in scenario.target_allocation_policies:
        for sleeve in policy.sleeves:
            add(asset_price_key_or_none(sleeve.asset))
    return tuple(keys)


def _add_amount_series_key(amount: Any, add: Any) -> None:
    if isinstance(amount, SeriesIndexedAmount):
        add(amount.series)


def _frame_values(
    frame: Any, rollout_count: int, horizon_months: int
) -> tuple[
    Int64[np.ndarray, " observation"],
    Int64[np.ndarray, " observation"],
    Float64[np.ndarray, " observation"],
    Bool[np.ndarray, " observation"],
]:
    rollout_index = frame.get_column("rollout_index").to_numpy()
    month_index = frame.get_column("month_index").to_numpy()
    raw_values = frame.get_column("value").to_numpy()
    in_bounds = (
        (rollout_index >= 0) & (rollout_index < rollout_count) & (month_index >= 0) & (month_index <= horizon_months)
    )
    return rollout_index, month_index, raw_values, in_bounds


def external_series_cubes(
    level_rows: tuple[MaterializedLevelRows, ...],
    *,
    series_index_by_id: dict[LevelSeriesKey, int],
    rollout_count: int,
    horizon_months: int,
    currency_quantum: object,
) -> tuple[Float64[np.ndarray, " series rollout snapshot"], Int64[np.ndarray, " series rollout snapshot"]]:
    """Materialize heterogeneous and money values together.

    Each sampled series is split and indexed once. The float cube carries rates and index ratios;
    the integer cube carries price-like values.
    """

    shape = (len(series_index_by_id), rollout_count, horizon_months + 1)
    values = np.full(shape, np.nan, dtype=np.float64)
    money_values = np.zeros(shape, dtype=np.int64)
    money_keys = (SecurityKey, SecurityDistributionKey, HomeValueKey)
    for rows in level_rows:
        index = series_index_by_id.get(rows.key)
        if index is None:
            continue
        keep = rows.in_bounds
        values[index, rows.rollout_index[keep], rows.month_index[keep]] = rows.values[keep]
        if not isinstance(rows.key, money_keys):
            continue
        keep = rows.in_bounds & np.isfinite(rows.values)
        if keep.any():
            money_values[index, rows.rollout_index[keep], rows.month_index[keep]] = sampled_array_to_quanta(
                rows.values[keep], quantum=currency_quantum
            )
    return values, money_values


def validate_series_indexed_amounts(
    scenario: Scenario, *, rollout_count: int, rows_by_key: dict[LevelSeriesKey, MaterializedLevelRows]
) -> None:
    """Validate path-indexed amount schedules against their materialized cube rows."""

    for label, amount, months in _series_indexed_amount_uses(scenario):
        if not isinstance(amount, SeriesIndexedAmount) or not months:
            continue
        before_base = [month for month in months if month < amount.base_month_index]
        if before_base:
            raise ValueError(
                f"series-indexed amount {label} is active at month {before_base[0]} "
                f"before base month {amount.base_month_index}"
            )
        base_month = int(amount.base_month_index)
        rows = rows_by_key.get(amount.series)
        required_months = sorted({base_month, *(amount._reset_month(month) for month in months)})
        for month in required_months:
            present_rollouts = (
                np.empty(0, dtype=np.int64)
                if rows is None
                else np.unique(rows.rollout_index[(rows.month_index == month) & rows.present])
            )
            if present_rollouts.size < rollout_count:
                present_set = set(present_rollouts.tolist())
                missing_rollouts = [rollout for rollout in range(rollout_count) if rollout not in present_set]
                raise KeyError(
                    f"series-indexed amount {label} references external series {amount.series.wire_id!r} "
                    f"at month {month}, but it is missing rollout(s): {_format_rollout_sample(missing_rollouts)}"
                )
        zero_base_rollouts = (
            []
            if rows is None
            else sorted(rows.rollout_index[(rows.month_index == base_month) & (rows.values == 0.0)].tolist())
        )
        if zero_base_rollouts:
            raise ValueError(
                f"external series {amount.series.wire_id!r} has zero base level at month "
                f"{amount.base_month_index} for rollout(s): {_format_rollout_sample(zero_base_rollouts)}"
            )


def _series_indexed_amount_uses(scenario: Scenario) -> list[tuple[str, object, tuple[int, ...]]]:
    horizon = int(scenario.horizon_months)
    uses: list[tuple[str, object, tuple[int, ...]]] = []
    months: tuple[int, ...]
    for scheduled_transfer in scenario.scheduled_transfers:
        months = (scheduled_transfer.month,) if 0 <= scheduled_transfer.month < horizon else ()
        uses.append((f"scheduled transfer {scheduled_transfer.cause_id!r}", scheduled_transfer.amount, months))
    for recurring_transfer in scenario.recurring_transfers:
        months = tuple(month for month in range(horizon) if recurring_transfer.is_active_at(month))
        uses.append((f"recurring transfer {recurring_transfer.cause_id!r}", recurring_transfer.amount, months))
    for scheduled_cashflow in scenario.scheduled_property_cashflows:
        months = (scheduled_cashflow.month,) if 0 <= scheduled_cashflow.month < horizon else ()
        uses.append((f"scheduled property cashflow {scheduled_cashflow.cause_id!r}", scheduled_cashflow.amount, months))
    for recurring_cashflow in scenario.recurring_property_cashflows:
        months = tuple(month for month in range(horizon) if recurring_cashflow.is_active_at(month))
        uses.append((f"recurring property cashflow {recurring_cashflow.cause_id!r}", recurring_cashflow.amount, months))
    for scheduled_obligation in scenario.scheduled_obligations:
        months = (scheduled_obligation.month,) if 0 <= scheduled_obligation.month < horizon else ()
        uses.append(
            (f"scheduled obligation {scheduled_obligation.obligation_id!r}", scheduled_obligation.amount_due, months)
        )
    for recurring_obligation in scenario.recurring_obligations:
        months = tuple(month for month in range(horizon) if recurring_obligation.is_active_at(month))
        uses.append(
            (f"recurring obligation {recurring_obligation.obligation_id!r}", recurring_obligation.amount_due, months)
        )
    return uses


def _format_rollout_sample(rollout_indices: list[int]) -> str:
    sample = ", ".join(str(index) for index in rollout_indices[:5])
    if len(rollout_indices) > 5:
        sample += ", ..."
    return sample

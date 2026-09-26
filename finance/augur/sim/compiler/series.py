"""Collect declarations' path requirements, validate supplied series and quantize them.

Demand discovery is available before sampling. Materialization supplies the integer
paths a composed world reads, without allocating financial-state slots.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple

import numpy as np
from jaxtyping import Bool, Float64, Int64

from finance.augur.model.asset_key import asset_price_key, asset_price_key_or_none
from finance.augur.model.series import (
    HomeValueKey,
    InflationKey,
    LevelSeriesKey,
    LocationId,
    SecurityDistributionKey,
    SecurityKey,
)
from finance.augur.sim.fixed_point import sampled_array_to_per_unit_rate, sampled_array_to_quanta
from finance.augur.sim.scenario import (
    AmountSpec,
    BondHolding,
    InitialLot,
    PrivateEquityTenderPolicy,
    ScheduledPropertyPurchase,
    SecurityDistribution,
    SeriesIndexedAmount,
    TargetAllocationPolicy,
    TlhPortfolioSpec,
)


def level_series_demand(
    *,
    lots: Iterable[InitialLot],
    tlh_portfolios: Iterable[TlhPortfolioSpec],
    bonds: Iterable[BondHolding],
    distributions: Iterable[SecurityDistribution],
    amounts: Iterable[AmountSpec],
    policies: Iterable[TargetAllocationPolicy],
    tender_policies: Iterable[PrivateEquityTenderPolicy],
    purchases: Iterable[ScheduledPropertyPurchase],
) -> tuple[LevelSeriesKey, ...]:
    """Every level series these declarations REFERENCE — their exogenous demand.

    `amounts` are the cashflows' and obligations' amounts. Derivable before anything is
    sampled, which is the point: it lets the caller ask the exogenous model for exactly this
    set instead of re-deriving the same fact from the product wire type in a second, drifting
    implementation.

    Must stay exhaustive over the series the declarations read: a demand missing here is a
    series the path does not carry, which the declaration that reads it refuses.
    """

    keys: list[LevelSeriesKey] = []
    seen: set[LevelSeriesKey] = set()

    def add(key: LevelSeriesKey | None) -> None:
        if key is not None and key not in seen:
            seen.add(key)
            keys.append(key)

    # Holdings are marked every month off their asset-price series.
    for lot in lots:
        add(asset_price_key_or_none(lot.asset))
    for portfolio in tlh_portfolios:
        add(asset_price_key_or_none(portfolio.asset))
    # A TIPS' principal rides CPI, so an inflation-indexed bond DEMANDS inflation even when
    # nothing else does. Without this, the declaration rejects a missing inflation path for
    # any holding that does not happen to want CPI for another reason — a CPI-indexed spend,
    # cash band, tender floor, or property obligation.
    #
    # Demand side only: `compile_series` carries only what was SAMPLED, so a TIPS whose inflation
    # nobody sampled is refused where it is held rather than priced off an all-NaN row.
    if any(bond.inflation_indexed for bond in bonds):
        add(InflationKey())
    # A distributing security demands TWO series: its price (already demanded by the lots that
    # hold it) and its dollars-per-unit payout, which nothing else references.
    for distribution in distributions:
        add(SecurityDistributionKey(symbol=asset_price_key(distribution.asset).symbol))
    for amount in amounts:
        _add_amount_series_key(amount, add)
    for policy in policies:
        for sleeve in policy.sleeves:
            add(asset_price_key_or_none(sleeve.asset))
        # Both band bounds, not just the floor: the ceiling is the refill TARGET, so a raise
        # cannot be sized without it, and an indexed ceiling needs its series sampled.
        _add_amount_series_key(policy.cash_floor, add)
        _add_amount_series_key(policy.cash_ceiling, add)
    for pe_policy in tender_policies:
        _add_amount_series_key(pe_policy.liquid_net_worth_floor, add)
    # A property is valued at sale off its location's home-value series.
    for purchase in purchases:
        add(HomeValueKey(location_id=LocationId(purchase.location_id)))
    return tuple(keys)


class MaterializedLevelRows(NamedTuple):
    """Coordinates in the prepared input tensors, not selected result columns."""

    key: LevelSeriesKey
    rollout_position: Int64[np.ndarray, " observation"]
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
        rollout_position, month_index, values, in_bounds = _frame_values(frame, rollout_count, horizon_months)
        rows.append(
            MaterializedLevelRows(
                key=key,
                rollout_position=rollout_position,
                month_index=month_index,
                values=values,
                present=frame.get_column("value").is_not_null().to_numpy(),
                in_bounds=in_bounds,
            )
        )
    return tuple(rows)


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
    rollout_position = frame.get_column("rollout_index").to_numpy()
    month_index = frame.get_column("month_index").to_numpy()
    raw_values = frame.get_column("value").to_numpy()
    in_bounds = (
        (rollout_position >= 0)
        & (rollout_position < rollout_count)
        & (month_index >= 0)
        & (month_index <= horizon_months)
    )
    return rollout_position, month_index, raw_values, in_bounds


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
    for rows in level_rows:
        index = series_index_by_id.get(rows.key)
        if index is None:
            continue
        keep = rows.in_bounds
        values[index, rows.rollout_position[keep], rows.month_index[keep]] = rows.values[keep]
        quantize = _money_quantizer(rows.key)
        if quantize is None:
            continue
        keep = rows.in_bounds & np.isfinite(rows.values)
        if keep.any():
            money_values[index, rows.rollout_position[keep], rows.month_index[keep]] = quantize(
                rows.values[keep], quantum=currency_quantum
            )
    return values, money_values


def _money_quantizer(key: LevelSeriesKey) -> Callable[..., Int64[np.ndarray, " ..."]] | None:
    """How this series' levels cross into integer money, or `None` if they do not.

    Both quantizers take `(values, *, quantum)`, which `Callable` cannot spell.

    A per-unit RATE and a per-unit PRICE are not the same unit, and the difference is the
    whole of #5832: the engine multiplies a distribution by a whole position before anything is
    owed, so rounding it to the currency quantum first both loses precision proportional to
    units held and sends a sub-quantum payout to a literal zero.
    A price is large enough per unit that the quantum is the right grid for it, and it is
    already the grid every price, basis and order on the wire agrees on.
    """

    match key:
        case SecurityDistributionKey():
            return sampled_array_to_per_unit_rate
        case SecurityKey() | HomeValueKey():
            return sampled_array_to_quanta
        case _:
            return None

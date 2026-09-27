"""Consumer-side external-series context for the simulator.

Production evidence ingestion, model fitting, stochastic sampling, and
provenance belong in `augur/model`; `augur/sim` is a deterministic path
evaluator once it receives those trajectories.

The handoff is the model's own typed `LevelFrames` (one frame per
`LevelSeriesKind`, keyed by a sub-id column, never by a magic-prefix
`series_id` string) plus the typed `PrivateEquityBundle`. The compiler looks
paths up by `LevelSeriesKey`, so nothing on this path re-parses a wire string
that the layer above already had typed. Wire strings reappear only in the
decoded read model (`sim/codec/series.py`), which is a serialization boundary.

Declarations' path requirements are discoverable before sampling (`level_series_demand`);
supplied series are validated and quantized into the integer paths a composed world
reads (`compile_series`), without allocating financial-state slots.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import numpy as np
from jaxtyping import Bool, Float64, Int64

from finance.augur.model.asset_key import asset_price_key, asset_price_key_or_none
from finance.augur.model.exogenous import LevelFrames, SampledExogenousBundle, assemble_level_frames
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    HomeValueKey,
    InflationKey,
    LevelSeriesKey,
    LocationId,
    RentKey,
    SecurityDistributionKey,
    SecurityKey,
    parse_level_series_key,
)
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.sim.fixed_point import round_ppb, sampled_array_to_per_unit_rate, sampled_array_to_quanta
from finance.augur.sim.holdings import asset_key
from finance.augur.sim.money import Currency
from finance.augur.sim.prepared import (
    PreparedAmount,
    PreparedBond,
    PreparedDistribution,
    PreparedIndexedAmount,
    PreparedIndexedCoupon,
    PreparedLot,
    PreparedSeries,
    PreparedTlhPortfolio,
    _PropertyPurchase,
    _TenderPolicy,
)

_MONEY_SERIES_KINDS = (SecurityKey, SecurityDistributionKey, HomeValueKey)
INDEX_SERIES_KINDS = (InflationKey, RentKey)


class UnsupportedScenarioError(ValueError):
    """An authored input the prepared records have no representation for.

    Raised rather than dropped: dropping a feature changes the answer without changing its shape.
    """


@dataclass(frozen=True)
class ExternalSeriesContext:
    """The materialized external-series context.

    `levels` carries non-PE level series (asset prices, CPI levels, rent
    levels). `private_equity` carries the typed PE protocol bundle — mark,
    regime, event-kind, fractions, blocked, recovery — per issuer; the sim
    compiler reads it directly by issuer index. PE tender events live on the
    `private_equity.sale_opportunity_active` channel — there is no separate
    exogenous-event frame.
    """

    levels: LevelFrames = field(default_factory=LevelFrames.empty)
    private_equity: PrivateEquityBundle = field(default_factory=PrivateEquityBundle.empty)

    @classmethod
    def from_level_blocks(
        cls,
        blocks: list[tuple[LevelSeriesKey, Float64[np.ndarray, " rollout snapshot"]]],
        *,
        rollout_count: int,
        horizon_months: int,
        private_equity: PrivateEquityBundle | None = None,
    ) -> ExternalSeriesContext:
        """Build a context from `(key, (rollout, month) matrix)` blocks."""

        return cls(
            levels=assemble_level_frames(blocks, rollout_count=rollout_count, horizon_months=horizon_months),
            private_equity=private_equity if private_equity is not None else PrivateEquityBundle.empty(),
        )


def materialize_external_series(
    bundle: SeriesModelBundle, *, rollout_seeds: tuple[int, ...], horizon_months: int
) -> ExternalSeriesContext:
    """Sample a scenario's own series-model bundle into the simulator's series context."""

    return materialize_sampled_exogenous(bundle.sample(rollout_seeds=rollout_seeds, horizon_months=horizon_months))


def materialize_sampled_exogenous(bundle: SampledExogenousBundle) -> ExternalSeriesContext:
    """Adapt a model-owned sampled bundle into the simulator's series context."""

    return ExternalSeriesContext(levels=bundle.levels, private_equity=bundle.private_equity)


def level_series_demand(
    *,
    lots: Iterable[PreparedLot],
    tlh_portfolios: Iterable[PreparedTlhPortfolio],
    bonds: Iterable[PreparedBond],
    distributions: Iterable[PreparedDistribution],
    amounts: Iterable[PreparedAmount],
    tender_policies: Iterable[_TenderPolicy],
    purchases: Iterable[_PropertyPurchase],
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
        add(asset_price_key_or_none(asset_key(lot.asset_id)))
    for portfolio in tlh_portfolios:
        add(asset_price_key_or_none(asset_key(portfolio.asset_id)))
    # A TIPS' principal rides CPI, so an inflation-indexed bond DEMANDS inflation even when
    # nothing else does. Without this, the declaration rejects a missing inflation path for
    # any holding that does not happen to want CPI for another reason — a CPI-indexed spend,
    # cash band, tender floor, or property obligation.
    #
    # Demand side only: `compile_series` carries only what was SAMPLED, so a TIPS whose inflation
    # nobody sampled is refused where it is held rather than priced off an all-NaN row.
    if any(isinstance(bond.coupon, PreparedIndexedCoupon) for bond in bonds):
        add(InflationKey())
    # A distributing security demands TWO series: its price (already demanded by the lots that
    # hold it) and its dollars-per-unit payout, which nothing else references.
    for distribution in distributions:
        add(SecurityDistributionKey(symbol=asset_price_key(asset_key(distribution.asset_id)).symbol))
    for amount in amounts:
        _add_amount_series_key(amount, add)
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


def _add_amount_series_key(amount: PreparedAmount, add: Callable[[LevelSeriesKey], None]) -> None:
    if isinstance(amount, PreparedIndexedAmount):
        add(parse_level_series_key(amount.series_id))


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
    currency: Currency,
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
                rows.values[keep], quantum=currency.quantum
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


def _series_values(
    key: LevelSeriesKey, levels: Float64[np.ndarray, " rollout snapshot"], money: Int64[np.ndarray, " rollout snapshot"]
) -> Int64[np.ndarray, " rollout snapshot"]:
    if not np.isfinite(levels).all():
        rollout, month = np.argwhere(~np.isfinite(levels))[0]
        raise ValueError(
            f"series {key.wire_id!r} has no finite level at rollout {rollout}, month {month}; "
            "the execution input's series are dense over every rollout and snapshot"
        )
    if isinstance(key, SecurityDistributionKey) and np.any(levels < 0):
        rollout, month = np.argwhere(levels < 0)[0]
        raise ValueError(
            f"distribution series {key.wire_id!r} has a negative payout at rollout {rollout}, month {month}"
        )
    if isinstance(key, _MONEY_SERIES_KINDS):
        return money
    if isinstance(key, INDEX_SERIES_KINDS):
        return round_ppb(levels)
    raise UnsupportedScenarioError(f"level series {key.wire_id!r} has no execution input representation")


def _level_series(
    keys: tuple[LevelSeriesKey, ...],
    levels: Float64[np.ndarray, " series rollout snapshot"],
    money: Int64[np.ndarray, " series rollout snapshot"],
) -> tuple[PreparedSeries, ...]:
    return tuple(
        PreparedSeries(
            series_id=key.wire_id,
            snapshots=levels.shape[2],
            values=tuple(int(value) for value in _series_values(key, levels[row], money[row]).reshape(-1)),
        )
        for row, key in enumerate(keys)
    )


def compile_series(
    external_series: ExternalSeriesContext, *, rollout_count: int, horizon_months: int, currency: Currency
) -> tuple[PreparedSeries, ...]:
    """The sampled level series as integer paths.

    Only sampled keys are carried; a composed world checks at `declare_pool` that the
    series a pool needs is present.
    """
    rows = materialize_level_rows(
        tuple(external_series.levels.value_rows()), rollout_count=rollout_count, horizon_months=horizon_months
    )
    keys = tuple(row.key for row in rows)
    levels, money = external_series_cubes(
        rows,
        series_index_by_id={key: index for index, key in enumerate(keys)},
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        currency=currency,
    )
    return _level_series(keys, levels, money)

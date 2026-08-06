"""External-series wrangling: collect referenced series IDs from a scenario, and
build the dense `(series, rollout, month)` cubes the engine reads at runtime.

Separated from the orchestrator so the compile_simulation function in
`compiler/plan.py` reads as pure scaffolding and the per-domain compilers can
import these helpers directly when they need to encode `SeriesIndexedAmount`
fields."""

from __future__ import annotations

from typing import Any

import numpy as np

from finance.augur.model.series import HomeValueKey, InflationKey, LevelSeriesKey, LocationId, SecurityDistributionKey
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
    # Unconditional, unlike a sale: a purchase leaves a LOT BEHIND, and that lot has to be
    # markable for the rest of the horizon. A fixed `price_per_unit_usd` sets the execution
    # price only — without the series the lot's `lot_asset_series_index` is NO_CODE and it
    # cannot be valued afterwards. A sale leaves nothing behind, so it demands a series only
    # when it needs one to price the sale itself.
    for asset_purchase in scenario.scheduled_asset_purchases:
        add(asset_price_key(asset_purchase.asset))
    for policy in scenario.target_allocation_policies:
        for sleeve in policy.sleeves:
            add(asset_price_key_or_none(sleeve.asset))
        # Both band bounds, not just the floor: the ceiling is the refill TARGET, so a raise
        # cannot be sized without it, and an indexed ceiling needs its series sampled.
        _add_amount_series_key(policy.cash_floor_usd, add)
        _add_amount_series_key(policy.cash_ceiling_usd, add)
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

    # `value_rows()` is ordered by wire id. Series row-indices are assigned from that order and
    # baked into the jitted program's STATIC structure (e.g. `_FoldedPE.floor_series`), so a
    # content-independent order would bust the native `jax.jit` compile cache (every other compile
    # re-traces); a deterministic one gives identical scenarios one compile, then cache hits.
    for key, _ in external_series.levels.value_rows():
        add(key)
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
    # Conditional HERE even though the demand list above is unconditional, and the asymmetry is
    # the point of this function: this guards `compile_purchases`' `[]` lookup, which only runs
    # for a series-priced purchase. Adding a key nobody sampled would manufacture an all-NaN
    # cube row; leaving it absent resolves to NO_CODE and reports "no modeled price series",
    # which is the real problem.
    for asset_purchase in scenario.scheduled_asset_purchases:
        if asset_purchase.price_per_unit_usd is None:
            add(asset_price_key(asset_purchase.asset))
    for policy in scenario.target_allocation_policies:
        for sleeve in policy.sleeves:
            add(asset_price_key_or_none(sleeve.asset))
    return tuple(keys)


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
    # Loop over series (tens), scatter each one's rows in a single fancy-index assignment —
    # never a Python loop over the (rollout, month, series) rows themselves, which number in
    # the millions at a 100-year horizon. Series the cube has no row for are skipped; a series
    # whose rows do not cover every (rollout, month) keeps NaN there, which the engine rejects
    # at the point of use.
    for key, frame in external_series.levels.value_rows():
        index = series_index_by_id.get(key)
        if index is None:
            continue
        rollout_index = frame.get_column("rollout_index").to_numpy()
        month_index = frame.get_column("month_index").to_numpy()
        keep = (
            (rollout_index >= 0)
            & (rollout_index < rollout_count)
            & (month_index >= 0)
            & (month_index <= horizon_months)
        )
        values[index, rollout_index[keep], month_index[keep]] = frame.get_column("value").to_numpy()[keep]
    return values

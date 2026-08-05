"""Asset acquisition/disposition compile output. Pairs with `codec/assets.py`."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.model.series import LevelSeriesKey
from finance.augur.product.asset_key import asset_price_key
from finance.augur.sim.compiler.helpers import NO_CODE, AccountSlots, AssetTable, StringTable
from finance.augur.sim.fixed_point import quantity_scale_for_asset, quantity_to_quanta, usd_to_cents
from finance.augur.sim.scenario import Scenario


@dataclass(frozen=True)
class SaleCompileOutput:
    """Scheduled asset-sale plumbing. One row per scheduled sale. `price_fixed[i]` is
    NaN when the sale price comes from a sampled series — `price_series[i]` is that
    series index, NO_CODE otherwise."""

    cause: NDArray[np.int64]
    month: NDArray[np.int64]
    agent: NDArray[np.int64]
    source_account: NDArray[np.int64]
    asset: NDArray[np.int64]
    quantity: NDArray[np.int64]
    quantity_scale: NDArray[np.int64]
    proceeds_account: NDArray[np.int64]
    proceeds_slot: NDArray[np.int64]
    price_fixed: NDArray[np.int64]
    price_series: NDArray[np.int64]


@dataclass(frozen=True)
class PurchaseCompileOutput:
    """Scheduled asset-purchase plumbing. One row per scheduled purchase, plus the lot slot
    each one fills.

    `lot_slot[i]` indexes the lot axis. The slot is allocated here, at compile time, but
    stays empty (`lot_remaining == 0`) until `month[i]`, so FIFO passes over it for free —
    which is why the slot can carry its real `purchase_month` and holding-period
    classification needs no runtime month.

    `amount_cents` is what the purchase asks for; what it actually spends is per-rollout,
    because whole-quantum rounding and available cash are both path-dependent."""

    cause: NDArray[np.int64]
    month: NDArray[np.int64]
    agent: NDArray[np.int64]
    from_slot: NDArray[np.int64]
    asset: NDArray[np.int64]
    amount_cents: NDArray[np.int64]
    quantity_scale: NDArray[np.int64]
    price_fixed: NDArray[np.int64]
    price_series: NDArray[np.int64]
    lot_slot: NDArray[np.int64]


def compile_purchases(
    scenario: Scenario,
    strings: StringTable,
    assets: AssetTable,
    account_slot_by_key: AccountSlots,
    series_index_by_id: dict[LevelSeriesKey, int],
    *,
    first_lot_slot: int,
) -> PurchaseCompileOutput:
    count = len(scenario.scheduled_asset_purchases)
    slots = max(1, count)
    cause = np.full((int(scenario.horizon_months), slots), NO_CODE, dtype=np.int64)
    month = np.full(slots, NO_CODE, dtype=np.int64)
    agent = np.zeros(slots, dtype=np.int64)
    from_slot = np.full(slots, NO_CODE, dtype=np.int64)
    asset = np.zeros(slots, dtype=np.int64)
    amount_cents = np.zeros(slots, dtype=np.int64)
    quantity_scale = np.ones(slots, dtype=np.int64)
    price_fixed = np.zeros(slots, dtype=np.int64)
    price_series = np.full(slots, NO_CODE, dtype=np.int64)
    lot_slot = np.full(slots, NO_CODE, dtype=np.int64)
    for idx, purchase in enumerate(scenario.scheduled_asset_purchases):
        cause[purchase.month, idx] = strings.require(purchase.cause_id)
        month[idx] = int(purchase.month)
        agent[idx] = strings.require(purchase.agent_id)
        from_slot[idx] = account_slot_by_key.require(
            purchase.agent_id, purchase.from_account_id, owner=f"scheduled asset purchase {purchase.cause_id!r}"
        )
        asset[idx] = assets.require(purchase.asset)
        amount_cents[idx] = usd_to_cents(purchase.amount_usd)
        quantity_scale[idx] = quantity_scale_for_asset(purchase.asset)
        if purchase.price_per_unit_usd is not None:
            price_fixed[idx] = usd_to_cents(purchase.price_per_unit_usd)
        else:
            price_series[idx] = series_index_by_id[asset_price_key(purchase.asset)]
        lot_slot[idx] = first_lot_slot + idx
    return PurchaseCompileOutput(
        cause=cause,
        month=month,
        agent=agent,
        from_slot=from_slot,
        asset=asset,
        amount_cents=amount_cents,
        quantity_scale=quantity_scale,
        price_fixed=price_fixed,
        price_series=price_series,
        lot_slot=lot_slot,
    )


def compile_sales(
    scenario: Scenario,
    strings: StringTable,
    assets: AssetTable,
    account_slot_by_key: AccountSlots,
    series_index_by_id: dict[LevelSeriesKey, int],
) -> SaleCompileOutput:
    count = len(scenario.scheduled_asset_sales)
    cause = np.full((int(scenario.horizon_months), max(1, count)), NO_CODE, dtype=np.int64)
    month = np.full(max(1, count), NO_CODE, dtype=np.int64)
    agent = np.zeros(max(1, count), dtype=np.int64)
    source_account = np.zeros(max(1, count), dtype=np.int64)
    asset = np.zeros(max(1, count), dtype=np.int64)
    quantity = np.zeros(max(1, count), dtype=np.int64)
    quantity_scale = np.ones(max(1, count), dtype=np.int64)
    proceeds_account = np.zeros(max(1, count), dtype=np.int64)
    proceeds_slot = np.full(max(1, count), NO_CODE, dtype=np.int64)
    price_fixed = np.zeros(max(1, count), dtype=np.int64)
    price_series = np.full(max(1, count), NO_CODE, dtype=np.int64)
    for idx, sale in enumerate(scenario.scheduled_asset_sales):
        cause[sale.month, idx] = strings.require(sale.cause_id)
        month[idx] = int(sale.month)
        agent[idx] = strings.require(sale.agent_id)
        source_account[idx] = strings.require(sale.source_account_id)
        asset[idx] = assets.require(sale.asset)
        quantity_scale[idx] = quantity_scale_for_asset(sale.asset)
        quantity[idx] = quantity_to_quanta(sale.quantity, scale=int(quantity_scale[idx]))
        proceeds_account[idx] = strings.require(sale.proceeds_account_id)
        proceeds_slot[idx] = account_slot_by_key.resolve(sale.agent_id, sale.proceeds_account_id)
        if sale.price_per_unit_usd is not None:
            price_fixed[idx] = usd_to_cents(sale.price_per_unit_usd)
        else:
            price_series[idx] = series_index_by_id[asset_price_key(sale.asset)]
    return SaleCompileOutput(
        cause=cause,
        month=month,
        agent=agent,
        source_account=source_account,
        asset=asset,
        quantity=quantity,
        quantity_scale=quantity_scale,
        proceeds_account=proceeds_account,
        proceeds_slot=proceeds_slot,
        price_fixed=price_fixed,
        price_series=price_series,
    )

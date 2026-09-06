"""Asset acquisition/disposition compile output. Pairs with `codec/assets.py`."""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass

import numpy as np
from jaxtyping import Int64

from finance.augur.model.series import LevelSeriesKey
from finance.augur.product.asset_key import asset_price_key
from finance.augur.sim.compiler.helpers import NO_CODE, AccountSlots, AssetTable, StringTable
from finance.augur.sim.fixed_point import quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.scenario import Scenario


@dataclass(frozen=True)
class SaleCompileOutput:
    """Scheduled asset-sale plumbing. One row per scheduled sale. `price_series[i]` is
    the index of the level series the sale is priced off."""

    cause: Int64[np.ndarray, " month scheduled_sale"]
    month: Int64[np.ndarray, " scheduled_sale"]
    agent: Int64[np.ndarray, " scheduled_sale"]
    source_account: Int64[np.ndarray, " scheduled_sale"]
    asset: Int64[np.ndarray, " scheduled_sale"]
    quantity: Int64[np.ndarray, " scheduled_sale"]
    quantity_scale: Int64[np.ndarray, " scheduled_sale"]
    proceeds_account: Int64[np.ndarray, " scheduled_sale"]
    proceeds_slot: Int64[np.ndarray, " scheduled_sale"]
    price_series: Int64[np.ndarray, " scheduled_sale"]


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
        price_series=price_series,
    )

"""Asset-sale (scheduled disposition) compile output. Pairs with `codec/assets.py`."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from augur.model.series import LevelSeriesKey
from augur.product.asset_key import asset_price_key
from augur.sim.compiler.helpers import NO_CODE, AssetTable, StringTable, slot
from augur.sim.scenario import Scenario


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
    quantity: NDArray[np.float64]
    proceeds_account: NDArray[np.int64]
    proceeds_slot: NDArray[np.int64]
    price_fixed: NDArray[np.float64]
    price_series: NDArray[np.int64]


def compile_sales(
    scenario: Scenario,
    strings: StringTable,
    assets: AssetTable,
    account_slot_by_key: dict[tuple[str, str], int],
    series_index_by_id: dict[LevelSeriesKey, int],
) -> SaleCompileOutput:
    count = len(scenario.scheduled_asset_sales)
    cause = np.full((int(scenario.horizon_months), max(1, count)), NO_CODE, dtype=np.int64)
    month = np.full(max(1, count), NO_CODE, dtype=np.int64)
    agent = np.zeros(max(1, count), dtype=np.int64)
    source_account = np.zeros(max(1, count), dtype=np.int64)
    asset = np.zeros(max(1, count), dtype=np.int64)
    quantity = np.zeros(max(1, count), dtype=np.float64)
    proceeds_account = np.zeros(max(1, count), dtype=np.int64)
    proceeds_slot = np.full(max(1, count), NO_CODE, dtype=np.int64)
    price_fixed = np.full(max(1, count), np.nan, dtype=np.float64)
    price_series = np.full(max(1, count), NO_CODE, dtype=np.int64)
    for idx, sale in enumerate(scenario.scheduled_asset_sales):
        cause[sale.month, idx] = strings.require(sale.cause_id)
        month[idx] = int(sale.month)
        agent[idx] = strings.require(sale.agent_id)
        source_account[idx] = strings.require(sale.source_account_id)
        asset[idx] = assets.require(sale.asset)
        quantity[idx] = float(sale.quantity)
        proceeds_account[idx] = strings.require(sale.proceeds_account_id)
        proceeds_slot[idx] = slot(account_slot_by_key, sale.agent_id, sale.proceeds_account_id)
        if sale.price_per_unit_usd is not None:
            price_fixed[idx] = float(sale.price_per_unit_usd)
        else:
            price_series[idx] = series_index_by_id[asset_price_key(sale.asset)]
    return SaleCompileOutput(
        cause=cause,
        month=month,
        agent=agent,
        source_account=source_account,
        asset=asset,
        quantity=quantity,
        proceeds_account=proceeds_account,
        proceeds_slot=proceeds_slot,
        price_fixed=price_fixed,
        price_series=price_series,
    )

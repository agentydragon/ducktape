"""Liquidity-policy compile output. The engine consumes this in `_apply_liquidity_policy_sales`
to fire cash-buffer-triggered asset sales. Decode side: see lot-disposition events in
`codec/assets.py` (liquidity dispositions share the same dispatch as scheduled sales)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from augur.model.series import LevelSeriesKey
from augur.product.asset_key import asset_price_key_or_none
from augur.sim.compiler.helpers import AMOUNT_FIXED, NO_CODE, AssetTable, StringTable, amount_arrays, slot
from augur.sim.scenario import Scenario


@dataclass(frozen=True)
class LiquidityPolicyCompileOutput:
    """Per-policy + (policy × asset-preference) arrays for the cash-buffer / asset-sale
    LiquidityPolicy. Trigger fires when cash drops below the indexed `trigger_*` schedule;
    `sale_*` is the indexed sale amount; `assets` is the per-policy asset-preference
    chain (NO_CODE for empty slots). `cause_id_prefixes` is one string per policy used
    to format cause_id strings at sale time."""

    agent: NDArray[np.int64]
    account: NDArray[np.int64]
    cash_slot: NDArray[np.int64]
    source_accounts: NDArray[np.int64]
    trigger_kind: NDArray[np.int64]
    trigger_fixed: NDArray[np.float64]
    trigger_base: NDArray[np.float64]
    trigger_series: NDArray[np.int64]
    trigger_base_month: NDArray[np.int64]
    trigger_period: NDArray[np.int64]
    sale_kind: NDArray[np.int64]
    sale_fixed: NDArray[np.float64]
    sale_base: NDArray[np.float64]
    sale_series: NDArray[np.int64]
    sale_base_month: NDArray[np.int64]
    sale_period: NDArray[np.int64]
    assets: NDArray[np.int64]
    asset_series: NDArray[np.int64]
    cause_id_prefixes: tuple[str, ...]


def compile_liquidity_policies(
    scenario: Scenario,
    strings: StringTable,
    asset_table: AssetTable,
    account_slot_by_key: dict[tuple[str, str], int],
    series_index_by_id: dict[LevelSeriesKey, int],
) -> LiquidityPolicyCompileOutput:
    policy_count = len(scenario.liquidity_policies)
    slot_count = max(1, policy_count)
    max_assets = max(1, max((len(policy.asset_preference_chain) for policy in scenario.liquidity_policies), default=0))
    max_source_accounts = max(
        1, max((len(policy.source_account_ids) or 1 for policy in scenario.liquidity_policies), default=0)
    )
    agent = np.zeros(slot_count, dtype=np.int64)
    account = np.zeros(slot_count, dtype=np.int64)
    cash_slot = np.full(slot_count, NO_CODE, dtype=np.int64)
    source_accounts = np.full((slot_count, max_source_accounts), NO_CODE, dtype=np.int64)
    trigger_kind = np.full(slot_count, AMOUNT_FIXED, dtype=np.int64)
    trigger_fixed = np.zeros(slot_count, dtype=np.float64)
    trigger_base = np.zeros(slot_count, dtype=np.float64)
    trigger_series_index = np.full(slot_count, NO_CODE, dtype=np.int64)
    trigger_base_month = np.zeros(slot_count, dtype=np.int64)
    trigger_adjustment_period = np.ones(slot_count, dtype=np.int64)
    sale_kind = np.full(slot_count, AMOUNT_FIXED, dtype=np.int64)
    sale_fixed = np.zeros(slot_count, dtype=np.float64)
    sale_base = np.zeros(slot_count, dtype=np.float64)
    sale_series_index = np.full(slot_count, NO_CODE, dtype=np.int64)
    sale_base_month = np.zeros(slot_count, dtype=np.int64)
    sale_adjustment_period = np.ones(slot_count, dtype=np.int64)
    assets = np.full((slot_count, max_assets), NO_CODE, dtype=np.int64)
    asset_series = np.full((slot_count, max_assets), NO_CODE, dtype=np.int64)
    prefixes: list[str] = []
    for idx, policy in enumerate(scenario.liquidity_policies):
        agent[idx] = strings.require(policy.agent_id)
        account[idx] = strings.require(policy.account_id)
        cash_slot[idx] = slot(account_slot_by_key, policy.agent_id, policy.account_id)
        for source_idx, source_account_id in enumerate(policy.source_account_ids or (policy.account_id,)):
            source_accounts[idx, source_idx] = strings.require(source_account_id)
        (
            trigger_kind[idx],
            trigger_fixed[idx],
            trigger_base[idx],
            trigger_series_index[idx],
            trigger_base_month[idx],
            trigger_adjustment_period[idx],
        ) = amount_arrays(policy.cash_buffer_trigger_below_usd, series_index_by_id)
        (
            sale_kind[idx],
            sale_fixed[idx],
            sale_base[idx],
            sale_series_index[idx],
            sale_base_month[idx],
            sale_adjustment_period[idx],
        ) = amount_arrays(policy.cash_buffer_sale_usd, series_index_by_id)
        prefixes.append(policy.cause_id_prefix)
        # PE assets are valid chain members for decode/labeling but price off-series → NO_CODE.
        for asset_idx, asset in enumerate(policy.asset_preference_chain):
            assets[idx, asset_idx] = asset_table.require(asset)
            price_key = asset_price_key_or_none(asset)
            asset_series[idx, asset_idx] = NO_CODE if price_key is None else series_index_by_id.get(price_key, NO_CODE)
    return LiquidityPolicyCompileOutput(
        agent=agent,
        account=account,
        cash_slot=cash_slot,
        source_accounts=source_accounts,
        trigger_kind=trigger_kind,
        trigger_fixed=trigger_fixed,
        trigger_base=trigger_base,
        trigger_series=trigger_series_index,
        trigger_base_month=trigger_base_month,
        trigger_period=trigger_adjustment_period,
        sale_kind=sale_kind,
        sale_fixed=sale_fixed,
        sale_base=sale_base,
        sale_series=sale_series_index,
        sale_base_month=sale_base_month,
        sale_period=sale_adjustment_period,
        assets=assets,
        asset_series=asset_series,
        cause_id_prefixes=tuple(prefixes),
    )

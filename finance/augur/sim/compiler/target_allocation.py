"""Target-allocation policy compile output. The engine consumes this to run the cash band.

Mirrors `liquidity.py`'s shape — one dense row per policy, ragged lists padded with
`NO_CODE` — with two differences that matter. Sleeves carry an integer WEIGHT, because the
policy sells toward a target rather than down a preference list. And the amount pair is a
band (floor, ceiling) rather than (trigger, sale amount): crossing the floor refills to the
ceiling, so the second amount is a destination rather than a size.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.model.series import LevelSeriesKey
from finance.augur.product.asset_key import asset_price_key_or_none
from finance.augur.sim.compiler.helpers import (
    AMOUNT_FIXED,
    NO_CODE,
    AccountSlots,
    AssetTable,
    StringTable,
    amount_arrays_cents,
)
from finance.augur.sim.scenario import Scenario


@dataclass(frozen=True)
class TargetAllocationCompileOutput:
    """Per-policy and (policy × sleeve) arrays for the target-allocation cash band.

    `weights[i, s]` is sleeve `s`'s relative weight, 0 in padded columns — a padded sleeve
    must contribute nothing to the water level, and weight 0 with no lots does exactly that.
    `sleeve_series[i, s]` is NO_CODE for an asset with no modeled price series, which makes
    it unsellable rather than free: the engine cannot mark it, so it never contributes value
    and is never sold.
    """

    agent: NDArray[np.int64]
    account: NDArray[np.int64]
    cash_slot: NDArray[np.int64]
    source_accounts: NDArray[np.int64]
    floor_kind: NDArray[np.int64]
    floor_fixed: NDArray[np.int64]
    floor_base: NDArray[np.int64]
    floor_series: NDArray[np.int64]
    floor_base_month: NDArray[np.int64]
    floor_period: NDArray[np.int64]
    ceiling_kind: NDArray[np.int64]
    ceiling_fixed: NDArray[np.int64]
    ceiling_base: NDArray[np.int64]
    ceiling_series: NDArray[np.int64]
    ceiling_base_month: NDArray[np.int64]
    ceiling_period: NDArray[np.int64]
    sleeve_assets: NDArray[np.int64]
    sleeve_series: NDArray[np.int64]
    weights: NDArray[np.int64]
    cause_id_prefixes: tuple[str, ...]


def compile_target_allocation_policies(
    scenario: Scenario,
    strings: StringTable,
    asset_table: AssetTable,
    account_slot_by_key: AccountSlots,
    series_index_by_id: dict[LevelSeriesKey, int],
) -> TargetAllocationCompileOutput:
    policies = scenario.target_allocation_policies
    slot_count = max(1, len(policies))
    max_sleeves = max(1, max((len(policy.sleeves) for policy in policies), default=0))
    max_source_accounts = max(1, max((len(policy.source_account_ids) or 1 for policy in policies), default=0))

    agent = np.zeros(slot_count, dtype=np.int64)
    account = np.zeros(slot_count, dtype=np.int64)
    cash_slot = np.full(slot_count, NO_CODE, dtype=np.int64)
    source_accounts = np.full((slot_count, max_source_accounts), NO_CODE, dtype=np.int64)
    floor_kind = np.full(slot_count, AMOUNT_FIXED, dtype=np.int64)
    floor_fixed = np.zeros(slot_count, dtype=np.int64)
    floor_base = np.zeros(slot_count, dtype=np.int64)
    floor_series = np.full(slot_count, NO_CODE, dtype=np.int64)
    floor_base_month = np.zeros(slot_count, dtype=np.int64)
    floor_period = np.ones(slot_count, dtype=np.int64)
    ceiling_kind = np.full(slot_count, AMOUNT_FIXED, dtype=np.int64)
    ceiling_fixed = np.zeros(slot_count, dtype=np.int64)
    ceiling_base = np.zeros(slot_count, dtype=np.int64)
    ceiling_series = np.full(slot_count, NO_CODE, dtype=np.int64)
    ceiling_base_month = np.zeros(slot_count, dtype=np.int64)
    ceiling_period = np.ones(slot_count, dtype=np.int64)
    sleeve_assets = np.full((slot_count, max_sleeves), NO_CODE, dtype=np.int64)
    sleeve_series = np.full((slot_count, max_sleeves), NO_CODE, dtype=np.int64)
    weights = np.zeros((slot_count, max_sleeves), dtype=np.int64)
    prefixes: list[str] = []

    for idx, policy in enumerate(policies):
        agent[idx] = strings.require(policy.agent_id)
        account[idx] = strings.require(policy.account_id)
        # `require`, not `resolve`: the funding account is a position held BY a modeled agent,
        # so "outside the model" is not a possible answer — an unknown one is a typo, and
        # settling its proceeds against the rest of the world would hand them to nobody.
        cash_slot[idx] = account_slot_by_key.require(
            policy.agent_id, policy.account_id, owner=f"target-allocation policy {policy.cause_id_prefix!r}"
        )
        for source_idx, source_account_id in enumerate(policy.source_account_ids or (policy.account_id,)):
            source_accounts[idx, source_idx] = strings.require(source_account_id)
        (
            floor_kind[idx],
            floor_fixed[idx],
            floor_base[idx],
            floor_series[idx],
            floor_base_month[idx],
            floor_period[idx],
        ) = amount_arrays_cents(policy.cash_floor_usd, series_index_by_id)
        (
            ceiling_kind[idx],
            ceiling_fixed[idx],
            ceiling_base[idx],
            ceiling_series[idx],
            ceiling_base_month[idx],
            ceiling_period[idx],
        ) = amount_arrays_cents(policy.cash_ceiling_usd, series_index_by_id)
        prefixes.append(policy.cause_id_prefix)
        for sleeve_idx, sleeve in enumerate(policy.sleeves):
            sleeve_assets[idx, sleeve_idx] = asset_table.require(sleeve.asset)
            weights[idx, sleeve_idx] = sleeve.weight
            price_key = asset_price_key_or_none(sleeve.asset)
            sleeve_series[idx, sleeve_idx] = (
                NO_CODE if price_key is None else series_index_by_id.get(price_key, NO_CODE)
            )

    return TargetAllocationCompileOutput(
        agent=agent,
        account=account,
        cash_slot=cash_slot,
        source_accounts=source_accounts,
        floor_kind=floor_kind,
        floor_fixed=floor_fixed,
        floor_base=floor_base,
        floor_series=floor_series,
        floor_base_month=floor_base_month,
        floor_period=floor_period,
        ceiling_kind=ceiling_kind,
        ceiling_fixed=ceiling_fixed,
        ceiling_base=ceiling_base,
        ceiling_series=ceiling_series,
        ceiling_base_month=ceiling_base_month,
        ceiling_period=ceiling_period,
        sleeve_assets=sleeve_assets,
        sleeve_series=sleeve_series,
        weights=weights,
        cause_id_prefixes=tuple(prefixes),
    )

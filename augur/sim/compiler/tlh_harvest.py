"""Compile-side table for the reduced-form tax-loss-harvesting (TLH) process — Piece 2b.

LIMITED / DELIBERATELY-APPROXIMATE MODEL. This table drives `_apply_tlh_harvest` in the
engine. It does NOT model the direct-indexing sleeve's individual constituent stocks: the
harvested loss is a *calibrated* function of the index path (`augur/sim/tlh_harvest.py`), and
the basis give-back is a single scalar per (policy, rollout). See `HarvestPolicy` and the
engine phase for the full "this is fake on purpose" rationale, and
`augur/plans/tax_loss_harvesting.md` for the upgrade path (options #3/#4).

Mirrors the per-policy + lot-mask shape of `private_equity.py`. One row per `HarvestPolicy`:
its yield-curve params, the index level-series index driving the period return, the owner's
capital-gain agent index (where harvested losses + the give-back land), the short-term share,
and a `(policy, lot)` mask flagging the lots the policy harvests + gives back against.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from augur.product.asset_key import asset_price_key
from augur.sim.compiler.helpers import NO_CODE
from augur.sim.scenario import Scenario
from augur.sim.tlh_harvest import HarvestYieldParams


@dataclass(frozen=True)
class HarvestPolicyCompileOutput:
    """Per-`HarvestPolicy` arrays (one row per policy). `lot_mask[p, l]` flags which lots the
    policy harvests against (and gives basis back on at sale). `gain_profile_index[p]` is the
    owner's capital-gain agent index (NO_CODE if the owner has no capital-gain profile, in which
    case the engine skips the policy). `series_index[p]` indexes `external_values` for the index
    price path. `params` keeps the typed yield curve per policy (small list; no array needed)."""

    gain_profile_index: NDArray[np.int64]
    series_index: NDArray[np.int64]
    short_term_fraction: NDArray[np.float64]
    lot_mask: NDArray[np.bool_]
    params: tuple[HarvestYieldParams, ...]


def compile_harvest_policies(
    scenario: Scenario,
    *,
    series_index_by_id: dict,
    lot_agent_codes: np.ndarray,
    lot_account_codes: np.ndarray,
    lot_asset_codes: np.ndarray,
    capital_gain_agent_codes: np.ndarray,
    string_code_of,
    asset_code_of,
) -> HarvestPolicyCompileOutput:
    """Compile per-policy harvest arrays.

    `string_code_of(value)` / `asset_code_of(asset)` are the (already-populated) intern lookups
    so this matches `lot_agent_codes` / `lot_account_codes` / `lot_asset_codes` exactly the way
    the lot table was built. A policy's lot mask is the lots matching its (owner, account, asset).
    """

    policies = scenario.harvest_policies
    policy_count = max(1, len(policies))
    lot_count = lot_agent_codes.shape[0]

    gain_profile_index = np.full(policy_count, NO_CODE, dtype=np.int64)
    series_index = np.full(policy_count, NO_CODE, dtype=np.int64)
    short_term_fraction = np.ones(policy_count, dtype=np.float64)
    lot_mask = np.zeros((policy_count, max(1, lot_count)), dtype=np.bool_)
    params: list[HarvestYieldParams] = []

    # Pad `params` to `policy_count` (the min-1 sentinel row carries a throwaway curve the engine
    # never reads, since its lot_mask is empty / gain_profile_index is NO_CODE).
    sentinel_params = HarvestYieldParams(
        peak_annual_yield=0.01, floor_annual_yield=0.0, maturity_decay_exponent=1.0, drawdown_sensitivity=0.0
    )
    if not policies:
        return HarvestPolicyCompileOutput(
            gain_profile_index=gain_profile_index,
            series_index=series_index,
            short_term_fraction=short_term_fraction,
            lot_mask=lot_mask,
            params=(sentinel_params,),
        )

    gain_index_by_agent_code = {int(code): idx for idx, code in enumerate(capital_gain_agent_codes)}
    for policy_idx, policy in enumerate(policies):
        owner_code = string_code_of(policy.owner_agent_id)
        account_code = string_code_of(policy.account_id)
        asset_code = asset_code_of(policy.asset)
        gain_profile_index[policy_idx] = gain_index_by_agent_code.get(int(owner_code), NO_CODE)
        # The harvested loss is shaped by the *index* path; PE assets have no asset-price series
        # and are never a valid harvest target, so `asset_price_key` raising here is correct.
        series_index[policy_idx] = series_index_by_id[asset_price_key(policy.asset)]
        short_term_fraction[policy_idx] = float(policy.short_term_fraction)
        params.append(policy.yield_params)
        if lot_count > 0:
            lot_mask[policy_idx, :lot_count] = (
                (lot_agent_codes == owner_code) & (lot_account_codes == account_code) & (lot_asset_codes == asset_code)
            )

    return HarvestPolicyCompileOutput(
        gain_profile_index=gain_profile_index,
        series_index=series_index,
        short_term_fraction=short_term_fraction,
        lot_mask=lot_mask,
        params=tuple(params),
    )

"""Private-equity tender-policy compile output. Per-issuer + per-policy arrays that
drive the engine's `_apply_pe_tenders` phase: at each tender event the policy's
liquid-net-worth floor governs whether (and how much) of the issuer's lots gets sold."""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
from jaxtyping import Bool, Int64

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import IssuerId, LevelSeriesKey, PrivateEquityEventKindCode
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.compiler.helpers import AMOUNT_FIXED, NO_CODE, AssetTable, StringTable, amount_arrays_quanta
from finance.augur.sim.fixed_point import sampled_array_to_quanta
from finance.augur.sim.scenario import Scenario


@dataclass(frozen=True)
class PEIssuerCompileOutput:
    """Per-issuer arrays (one row per distinct `private_equity:<issuer>` asset). An issuer
    is `policy_index = NO_CODE` if no PrivateEquityTenderPolicy applies (issuer never
    tenders within horizon); the engine skips it. `lot_mask[i, l]` flags which lots
    belong to issuer `i`."""

    codes: Int64[np.ndarray, " issuer"]
    issuer_ids: tuple[str, ...]
    policy_index: Int64[np.ndarray, " issuer"]
    lot_mask: Bool[np.ndarray, " issuer lot"]


class PEExecutionChannels[ArrayT](NamedTuple):
    """Per-issuer channel arrays consumed by the simulation engine.

    Shape: `(issuer, rollout, month + 1)` for each channel. Built from the
    typed `PrivateEquityBundle` at compile time so the engine reads PE state
    by field access instead of going through `external_values[series_index]`.
    """

    mark_quanta: ArrayT
    regime_codes: ArrayT
    sale_opportunity_active: ArrayT
    sale_capacity_fractions: ArrayT
    eligible_fractions: ArrayT
    forced_sale_fractions: ArrayT
    liquidity_blocked: ArrayT
    forced_recovery_cashout_quanta: ArrayT


@dataclass(frozen=True)
class PEChannels:
    """Engine channels plus the event-kind channel used only by host-side decoders."""

    execution: PEExecutionChannels[np.ndarray]
    event_kind_codes: Int64[np.ndarray, " issuer rollout snapshot"]


@dataclass(frozen=True)
class PEPolicyCompileOutput:
    """Per-policy arrays (one row per PrivateEquityTenderPolicy). `floor_*` is the
    indexed-amount schedule for the liquid-net-worth floor (CPI-indexable). `owner_cash_mask`
    + `owner_non_pe_lot_mask` are (policy × slot) masks the engine uses to compute LNW
    from the owner's non-PE liquid assets."""

    owner_agent: Int64[np.ndarray, " policy"]
    proceeds_cash_slot: Int64[np.ndarray, " policy"]
    floor_kind: Int64[np.ndarray, " policy"]
    floor_fixed: Int64[np.ndarray, " policy"]
    floor_base: Int64[np.ndarray, " policy"]
    floor_series: Int64[np.ndarray, " policy"]
    floor_base_month: Int64[np.ndarray, " policy"]
    floor_period: Int64[np.ndarray, " policy"]
    owner_cash_mask: Bool[np.ndarray, " policy cash"]
    owner_non_pe_lot_mask: Bool[np.ndarray, " policy lot"]


def compile_private_equity_tenders(
    scenario: Scenario,
    strings: StringTable,
    *,
    asset_table: AssetTable,
    series_index_by_id: dict[LevelSeriesKey, int],
    lot_agent_codes: Int64[np.ndarray, " lot"],
    lot_asset_codes: Int64[np.ndarray, " lot"],
    cash_agent_codes: Int64[np.ndarray, " cash"],
    cash_account_codes: Int64[np.ndarray, " cash"],
) -> tuple[PEIssuerCompileOutput, PEPolicyCompileOutput]:
    """Compile per-(issuer, policy) arrays driving the PE tender-sale path.

    Issuer set is derived from `initial_lots` (any `private_equity:<issuer>` asset_id);
    the policy set is `scenario.private_equity_tender_policies` (per-owner). Each issuer
    maps to a policy by matching the lot's owner_agent_id to the policy's owner. The
    engine uses these arrays to fire LNW-floor-driven sales when a tender event activates.
    """

    issuer_to_lots: dict[str, list[int]] = {}
    for lot_index, lot in enumerate(scenario.initial_lots):
        if isinstance(lot.asset, PrivateEquityAssetKey):
            issuer_to_lots.setdefault(str(lot.asset.issuer_id), []).append(lot_index)
    issuer_ids = tuple(sorted(issuer_to_lots))

    policies = scenario.private_equity_tender_policies
    policy_count = max(1, len(policies))
    lot_count = lot_agent_codes.shape[0]
    cash_count = cash_agent_codes.shape[0]
    issuer_count = max(1, len(issuer_ids))

    pe_issuer_codes = np.full(issuer_count, NO_CODE, dtype=np.int64)
    pe_issuer_policy_index = np.full(issuer_count, NO_CODE, dtype=np.int64)
    pe_issuer_lot_mask = np.zeros((issuer_count, max(1, lot_count)), dtype=np.bool_)

    pe_policy_owner_agent_codes = np.full(policy_count, NO_CODE, dtype=np.int64)
    pe_policy_proceeds_cash_slot = np.full(policy_count, NO_CODE, dtype=np.int64)
    pe_policy_floor_kind = np.full(policy_count, AMOUNT_FIXED, dtype=np.int64)
    pe_policy_floor_fixed = np.zeros(policy_count, dtype=np.int64)
    pe_policy_floor_base = np.zeros(policy_count, dtype=np.int64)
    pe_policy_floor_series_index = np.full(policy_count, NO_CODE, dtype=np.int64)
    pe_policy_floor_base_month = np.zeros(policy_count, dtype=np.int64)
    pe_policy_floor_adjustment_period = np.ones(policy_count, dtype=np.int64)
    pe_policy_owner_cash_mask = np.zeros((policy_count, max(1, cash_count)), dtype=np.bool_)
    pe_policy_owner_non_pe_lot_mask = np.zeros((policy_count, max(1, lot_count)), dtype=np.bool_)

    issuers = PEIssuerCompileOutput(
        codes=pe_issuer_codes, issuer_ids=issuer_ids, policy_index=pe_issuer_policy_index, lot_mask=pe_issuer_lot_mask
    )
    pe_policies = PEPolicyCompileOutput(
        owner_agent=pe_policy_owner_agent_codes,
        proceeds_cash_slot=pe_policy_proceeds_cash_slot,
        floor_kind=pe_policy_floor_kind,
        floor_fixed=pe_policy_floor_fixed,
        floor_base=pe_policy_floor_base,
        floor_series=pe_policy_floor_series_index,
        floor_base_month=pe_policy_floor_base_month,
        floor_period=pe_policy_floor_adjustment_period,
        owner_cash_mask=pe_policy_owner_cash_mask,
        owner_non_pe_lot_mask=pe_policy_owner_non_pe_lot_mask,
    )
    if not issuer_ids and not policies:
        return issuers, pe_policies

    # Per-policy arrays.
    for policy_idx, policy in enumerate(policies):
        owner_code = strings.require(policy.owner_agent_id)
        pe_policy_owner_agent_codes[policy_idx] = owner_code
        # Proceeds cash slot: the (owner_agent, proceeds_account_id) pair.
        proceeds_account_code = strings.require(policy.proceeds_account_id)
        proceeds_cash_slots = np.flatnonzero(
            (cash_agent_codes == owner_code) & (cash_account_codes == proceeds_account_code)
        )
        if proceeds_cash_slots.size != 1:
            raise ValueError(
                "private-equity tender policy must resolve exactly one proceeds account "
                f"for {policy.owner_agent_id}/{policy.proceeds_account_id}; found {proceeds_cash_slots.size}"
            )
        pe_policy_proceeds_cash_slot[policy_idx] = int(proceeds_cash_slots[0])
        kind, fixed, base, series, base_month, period = amount_arrays_quanta(
            policy.liquid_net_worth_floor, series_index_by_id, currency_quantum=scenario.currency.quantum
        )
        pe_policy_floor_kind[policy_idx] = kind
        pe_policy_floor_fixed[policy_idx] = fixed
        pe_policy_floor_base[policy_idx] = base
        pe_policy_floor_series_index[policy_idx] = series
        pe_policy_floor_base_month[policy_idx] = base_month
        pe_policy_floor_adjustment_period[policy_idx] = period
        if cash_count > 0:
            pe_policy_owner_cash_mask[policy_idx, :cash_count] = cash_agent_codes == owner_code
        if lot_count > 0:
            owner_lots = lot_agent_codes == owner_code
            pe_codes = {
                asset_table.require(PrivateEquityAssetKey(issuer_id=IssuerId(issuer))) for issuer in issuer_to_lots
            }  # AssetTable codes for this issuer's PE lots (match `lot_asset_codes`)
            non_pe_lot = ~np.isin(lot_asset_codes, list(pe_codes)) if pe_codes else np.ones(lot_count, dtype=np.bool_)
            pe_policy_owner_non_pe_lot_mask[policy_idx, :lot_count] = owner_lots & non_pe_lot

    # Per-issuer arrays.
    policy_index_by_owner = {int(pe_policy_owner_agent_codes[idx]): idx for idx in range(len(policies))}
    for issuer_idx, issuer in enumerate(issuer_ids):
        pe_issuer_codes[issuer_idx] = strings.require(issuer)
        # Lot indices owned by this issuer.
        lots = issuer_to_lots[issuer]
        for lot_index in lots:
            pe_issuer_lot_mask[issuer_idx, lot_index] = True
        # Resolve policy by owner-agent match. All lots for a given issuer in v1 are owned by
        # the same agent (single-actor scenarios); use the first lot's owner.
        owner_code = int(lot_agent_codes[lots[0]])
        if owner_code in policy_index_by_owner:
            pe_issuer_policy_index[issuer_idx] = policy_index_by_owner[owner_code]

    return issuers, pe_policies


def compile_pe_channels(
    issuers: PEIssuerCompileOutput,
    *,
    private_equity: PrivateEquityBundle,
    rollout_count: int,
    horizon_months: int,
    currency_quantum: object,
) -> PEChannels:
    """Materialize per-issuer PE channel arrays from the typed `PrivateEquityBundle`.

    Returns shape `(issuer, rollout, month + 1)` for each channel. The bundle's
    `from_issuer_arrays` already validates ranges, dtypes, and known code values;
    this just slices the typed columns per issuer into dense ndarrays for the
    engine to read by field access.
    """

    issuer_count = issuers.codes.shape[0]
    snapshot_months = horizon_months + 1
    mark_quanta = np.zeros((issuer_count, rollout_count, snapshot_months), dtype=np.int64)
    regime_codes = np.full((issuer_count, rollout_count, snapshot_months), NO_CODE, dtype=np.int64)
    event_kind_codes = np.full(
        (issuer_count, rollout_count, snapshot_months), int(PrivateEquityEventKindCode.NONE), dtype=np.int64
    )
    sale_opportunity_active = np.zeros((issuer_count, rollout_count, snapshot_months), dtype=np.bool_)
    sale_capacity_fractions = np.ones((issuer_count, rollout_count, snapshot_months), dtype=np.float64)
    eligible_fractions = np.ones((issuer_count, rollout_count, snapshot_months), dtype=np.float64)
    forced_sale_fractions = np.zeros((issuer_count, rollout_count, snapshot_months), dtype=np.float64)
    liquidity_blocked = np.zeros((issuer_count, rollout_count, snapshot_months), dtype=np.bool_)
    forced_recovery_cashout_quanta = np.zeros((issuer_count, rollout_count, snapshot_months), dtype=np.int64)
    for issuer_idx, issuer_code in enumerate(issuers.codes):
        if int(issuer_code) < 0:
            continue
        issuer_id = issuers.issuer_ids[issuer_idx]
        if issuer_id not in private_equity.issuer_ids():
            raise ValueError(f"private-equity bundle missing required issuer {issuer_id!r}")
        mark_values = private_equity.issuer_float_matrix(
            issuer_id, "mark_usd_per_unit", rollout_count=rollout_count, horizon_months=horizon_months
        )
        # A terminal snapshot is informative only; the scan executes months
        # 0..H-1. Preserve that established validation boundary before
        # quantization removes the float NaN sentinel.
        executable_marks = mark_values[:, :horizon_months]
        if executable_marks.size and (not np.isfinite(executable_marks).all() or (executable_marks < 0.0).any()):
            raise ValueError(
                f"private-equity mark series for issuer {issuer_id!r} produced a negative or non-finite value"
            )
        mark_quanta[issuer_idx] = sampled_array_to_quanta(mark_values, quantum=currency_quantum)
        regime_codes[issuer_idx] = private_equity.issuer_int_matrix(
            issuer_id, "regime_code", rollout_count=rollout_count, horizon_months=horizon_months
        )
        event_kind_codes[issuer_idx] = private_equity.issuer_int_matrix(
            issuer_id, "event_kind_code", rollout_count=rollout_count, horizon_months=horizon_months
        )
        sale_opportunity_active[issuer_idx] = private_equity.issuer_bool_matrix(
            issuer_id, "sale_opportunity_active", rollout_count=rollout_count, horizon_months=horizon_months
        )
        sale_capacity_fractions[issuer_idx] = private_equity.issuer_float_matrix(
            issuer_id, "sale_capacity_fraction", rollout_count=rollout_count, horizon_months=horizon_months
        )
        eligible_fractions[issuer_idx] = private_equity.issuer_float_matrix(
            issuer_id, "eligible_fraction", rollout_count=rollout_count, horizon_months=horizon_months
        )
        forced_sale_fractions[issuer_idx] = private_equity.issuer_float_matrix(
            issuer_id, "forced_sale_fraction", rollout_count=rollout_count, horizon_months=horizon_months
        )
        liquidity_blocked[issuer_idx] = private_equity.issuer_bool_matrix(
            issuer_id, "liquidity_blocked", rollout_count=rollout_count, horizon_months=horizon_months
        )
        forced_recovery_values = private_equity.issuer_float_matrix(
            issuer_id, "forced_recovery_cashout_usd", rollout_count=rollout_count, horizon_months=horizon_months
        )
        executable_recovery = forced_recovery_values[:, :horizon_months]
        if executable_recovery.size and (executable_recovery < 0.0).any():
            raise ValueError("private-equity forced-recovery cashout series produced a negative value")
        forced_recovery_cashout_quanta[issuer_idx] = sampled_array_to_quanta(
            forced_recovery_values, quantum=currency_quantum
        )
    return PEChannels(
        execution=PEExecutionChannels(
            mark_quanta=mark_quanta,
            regime_codes=regime_codes,
            sale_opportunity_active=sale_opportunity_active,
            sale_capacity_fractions=sale_capacity_fractions,
            eligible_fractions=eligible_fractions,
            forced_sale_fractions=forced_sale_fractions,
            liquidity_blocked=liquidity_blocked,
            forced_recovery_cashout_quanta=forced_recovery_cashout_quanta,
        ),
        event_kind_codes=event_kind_codes,
    )

"""Asset domain decoders: cash balances, lot inventory, and lot dispositions.
The compile-side twins live in `_compile_lots`, `_compile_cash`, `_compile_sales`,
and `compile_target_allocation_policies` in `augur.sim.compiler`."""

from __future__ import annotations

import numpy as np
import polars as pl

from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.codec.helpers import (
    codes_to_asset_wire_ids,
    codes_to_strings,
    currency_quanta_column,
    frame_from_columns,
    lot_quantity_column,
    quantity_column,
)
from finance.augur.sim.compiler.plan import CompiledSimulation
from finance.augur.sim.enums import PrivateEquityDispositionKind, PrivateEquityOpportunityOutcome
from finance.augur.sim.events import EVENT_FRAMES
from finance.augur.sim.output import DenseSimulationOutput


def decode_sched_dispositions(plan: CompiledSimulation, output: DenseSimulationOutput) -> pl.DataFrame:
    active = output.scheduled_dispositions.active  # (sale, lot, R) — horizon collapsed
    if active.any():
        sales, lots, rollouts = np.argwhere(active).T
    else:
        sales = lots = rollouts = np.array([], dtype=np.int64)
    # Each sale fires once, at its static month; recover it from the plan rather than a stored axis.
    months = plan.sales.month[sales]
    cause_ids = codes_to_strings(plan, plan.sales.cause)[months, sales]  # `cause` is (month, sale)
    return _lot_disposition_frame(
        plan=plan,
        rollouts=rollouts,
        months=months,
        cause_ids=cause_ids,
        agent_codes=plan.sales.agent[sales],
        source_account_codes=plan.sales.source_account[sales],
        asset_codes=plan.sales.asset[sales],
        lots=lots,
        purchase_months=output.state.lot_purchase_month[lots, rollouts],
        units=output.scheduled_dispositions.units[sales, lots, rollouts],
        basis=output.scheduled_dispositions.basis[sales, lots, rollouts],
        proceeds=output.scheduled_dispositions.proceeds[sales, lots, rollouts],
        proceeds_account_codes=plan.sales.proceeds_account[sales],
    )


def decode_target_allocation_dispositions(plan: CompiledSimulation, output: DenseSimulationOutput) -> pl.DataFrame:
    """Lot dispositions from the target-allocation policy: one row per (month, policy, sleeve,
    lot, rollout) the engine actually sold from."""

    active = output.target_allocation.dispositions.active  # (M, policy, sleeve, lot, R)
    # A padded sleeve column carries asset_code < 0 and can never have sold anything; masking it
    # here keeps the argwhere from having to be trusted to agree.
    sleeve_valid = plan.target_allocation_policies.sleeve_assets >= 0  # (policy, sleeve)
    active = active & sleeve_valid[None, :, :, None, None]
    if active.any():
        months, policies, sleeve_idxs, lots, rollouts = np.argwhere(active).T
    else:
        months = policies = sleeve_idxs = lots = rollouts = np.array([], dtype=np.int64)
    asset_codes = plan.target_allocation_policies.sleeve_assets[policies, sleeve_idxs]
    asset_names = codes_to_asset_wire_ids(plan, plan.target_allocation_policies.sleeve_assets)[policies, sleeve_idxs]
    prefixes_per_event = np.array(plan.target_allocation_policies.cause_id_prefixes, dtype=object)[policies]
    cause_ids = np.array(
        [f"{p}_m{m}_{a}" for p, m, a in zip(prefixes_per_event, months, asset_names, strict=True)], dtype=object
    )
    return _lot_disposition_frame(
        plan=plan,
        rollouts=rollouts,
        months=months,
        cause_ids=cause_ids,
        agent_codes=plan.target_allocation_policies.agent[policies],
        source_account_codes=plan.lot_account_codes[lots],
        asset_codes=asset_codes,
        lots=lots,
        purchase_months=output.state.lot_purchase_month[lots, rollouts],
        units=output.target_allocation.dispositions.units[months, policies, sleeve_idxs, lots, rollouts],
        basis=output.target_allocation.dispositions.basis[months, policies, sleeve_idxs, lots, rollouts],
        proceeds=output.target_allocation.dispositions.proceeds[months, policies, sleeve_idxs, lots, rollouts],
        proceeds_account_codes=plan.target_allocation_policies.account[policies],
    )


def decode_pe_dispositions(plan: CompiledSimulation, output: DenseSimulationOutput) -> pl.DataFrame:
    active = output.private_equity.dispositions.active  # (M, issuer, kind, lot, R)
    if active.any():
        months, issuers, kinds, lots, rollouts = np.argwhere(active).T
    else:
        months = issuers = kinds = lots = rollouts = np.array([], dtype=np.int64)
    # pe_issuers.codes are issuer names (e.g. "acme"), not full asset_ids.
    # Use lot_asset_codes to get the correct "private_equity:<issuer>" asset_id.
    asset_codes = plan.lot_asset_codes[lots]
    issuer_names = codes_to_strings(plan, plan.pe_issuers.codes)[issuers]
    cause_prefixes = np.array([_pe_disposition_cause_prefix(PrivateEquityDispositionKind(int(k))) for k in kinds])
    cause_ids = np.array(
        [f"{p}_m{m}_{n}" for p, m, n in zip(cause_prefixes, months, issuer_names, strict=True)], dtype=object
    )
    policy_idxs = plan.pe_issuers.policy_index[issuers]
    return _lot_disposition_frame(
        plan=plan,
        rollouts=rollouts,
        months=months,
        cause_ids=cause_ids,
        agent_codes=plan.pe_policies.owner_agent[policy_idxs],
        # PE compatibility rows use the owner id as their synthetic source-account id;
        # proceeds_cash_slot is a CASH AXIS index, not a string-table code.
        source_account_codes=plan.pe_policies.owner_agent[policy_idxs],
        asset_codes=asset_codes,
        lots=lots,
        purchase_months=output.state.lot_purchase_month[lots, rollouts],
        units=output.private_equity.dispositions.units[months, issuers, kinds, lots, rollouts],
        basis=output.private_equity.dispositions.basis[months, issuers, kinds, lots, rollouts],
        proceeds=output.private_equity.dispositions.proceeds[months, issuers, kinds, lots, rollouts],
        proceeds_account_codes=plan.cash_account_codes[plan.pe_policies.proceeds_cash_slot[policy_idxs]],
    )


def decode_pe_protocol_events(plan: CompiledSimulation) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    channels = plan.pe_channels.execution
    for issuer_idx, issuer_code in enumerate(plan.pe_issuers.codes):
        if int(issuer_code) < 0:
            continue
        issuer_id = str(codes_to_strings(plan, np.array([issuer_code], dtype=np.int64))[0])
        event_codes = plan.pe_channels.event_kind_codes[issuer_idx]
        regime_codes = channels.regime_codes[issuer_idx]
        event_window = event_codes[:, : plan.horizon_months]
        active = event_window != int(PrivateEquityEventKindCode.NONE)
        if not active.any():
            continue
        rollouts, months = np.argwhere(active).T
        for rollout, month in zip(rollouts, months, strict=True):
            event_code = PrivateEquityEventKindCode(int(event_codes[rollout, month]))
            regime_code = PrivateEquityRegimeCode(int(regime_codes[rollout, month]))
            rows.append(
                {
                    "rollout_index": int(rollout),
                    "month_index": int(month),
                    "issuer_id": issuer_id,
                    "asset_id": PrivateEquityAssetKey(issuer_id=IssuerId(issuer_id)).wire_id,
                    "event_kind": event_code.name.lower(),
                    "regime": regime_code.name.lower(),
                    "mark_quanta": int(channels.mark_quanta[issuer_idx, rollout, month]),
                    "sale_capacity_fraction": float(channels.sale_capacity_fractions[issuer_idx, rollout, month]),
                    "eligible_fraction": float(channels.eligible_fractions[issuer_idx, rollout, month]),
                    "forced_sale_fraction": float(channels.forced_sale_fractions[issuer_idx, rollout, month]),
                    "liquidity_blocked": bool(channels.liquidity_blocked[issuer_idx, rollout, month]),
                    "forced_recovery_cashout_quanta": int(
                        channels.forced_recovery_cashout_quanta[issuer_idx, rollout, month]
                    ),
                }
            )
    if not rows:
        return EVENT_FRAMES.private_equity_events.empty()
    return EVENT_FRAMES.private_equity_events.normalize(pl.DataFrame(rows))


def decode_pe_opportunity_events(plan: CompiledSimulation, output: DenseSimulationOutput) -> pl.DataFrame:
    active = output.private_equity.opportunities.active
    if not active.any():
        return EVENT_FRAMES.private_equity_opportunities.empty()
    months, issuers, rollouts = np.argwhere(active).T
    issuer_ids = codes_to_strings(plan, plan.pe_issuers.codes)
    channels = plan.pe_channels.execution
    rows: list[dict[str, object]] = []
    for month, issuer_idx, rollout in zip(months, issuers, rollouts, strict=True):
        issuer_id = str(issuer_ids[issuer_idx])
        event_code = PrivateEquityEventKindCode(int(plan.pe_channels.event_kind_codes[issuer_idx, rollout, month]))
        regime_code = PrivateEquityRegimeCode(int(channels.regime_codes[issuer_idx, rollout, month]))
        outcome = PrivateEquityOpportunityOutcome(
            int(output.private_equity.opportunities.outcome[month, issuer_idx, rollout])
        )
        rows.append(
            {
                "rollout_index": int(rollout),
                "month_index": int(month),
                "cause_id": f"pe_opportunity_m{int(month)}_{issuer_id}",
                "issuer_id": issuer_id,
                "asset_id": PrivateEquityAssetKey(issuer_id=IssuerId(issuer_id)).wire_id,
                "event_kind": event_code.name.lower(),
                "regime": regime_code.name.lower(),
                "outcome": outcome.name.lower(),
                "mark_quanta": int(channels.mark_quanta[issuer_idx, rollout, month]),
                "sale_capacity_fraction": float(channels.sale_capacity_fractions[issuer_idx, rollout, month]),
                "eligible_fraction": float(channels.eligible_fractions[issuer_idx, rollout, month]),
                "liquidity_blocked": bool(channels.liquidity_blocked[issuer_idx, rollout, month]),
                "floor_quanta": int(output.private_equity.opportunities.floor[month, issuer_idx, rollout]),
                "liquid_net_worth_quanta": int(
                    output.private_equity.opportunities.liquid_net_worth[month, issuer_idx, rollout]
                ),
                "shortfall_quanta": int(output.private_equity.opportunities.shortfall[month, issuer_idx, rollout]),
                "units_held": float(
                    quantity_column(
                        output.private_equity.opportunities.units_held[month, issuer_idx, rollout],
                        _pe_issuer_scale(plan, issuer_idx),
                    )
                ),
                "sellable_units": float(
                    quantity_column(
                        output.private_equity.opportunities.sellable_units[month, issuer_idx, rollout],
                        _pe_issuer_scale(plan, issuer_idx),
                    )
                ),
                "target_units": float(
                    quantity_column(
                        output.private_equity.opportunities.target_units[month, issuer_idx, rollout],
                        _pe_issuer_scale(plan, issuer_idx),
                    )
                ),
                "proceeds_quanta": int(output.private_equity.opportunities.proceeds[month, issuer_idx, rollout]),
            }
        )
    return EVENT_FRAMES.private_equity_opportunities.normalize(pl.DataFrame(rows))


def _pe_disposition_cause_prefix(kind: PrivateEquityDispositionKind) -> str:
    return f"pe_{kind.name.lower()}"


def _pe_issuer_scale(plan: CompiledSimulation, issuer_idx: int) -> int:
    lots = np.flatnonzero(plan.pe_issuers.lot_mask[issuer_idx])
    return int(plan.lot_quantity_scale[int(lots[0])]) if lots.size else 1


def _lot_disposition_frame(
    *,
    plan: CompiledSimulation,
    rollouts: np.ndarray,
    months: np.ndarray,
    cause_ids: np.ndarray,
    agent_codes: np.ndarray,
    source_account_codes: np.ndarray,
    asset_codes: np.ndarray,
    lots: np.ndarray,
    purchase_months: np.ndarray,
    units: np.ndarray,
    basis: np.ndarray,
    proceeds: np.ndarray,
    proceeds_account_codes: np.ndarray,
) -> pl.DataFrame:
    return frame_from_columns(
        EVENT_FRAMES.lot_dispositions,
        rollout_index=rollouts,
        month_index=months,
        cause_id=cause_ids,
        agent_id=codes_to_strings(plan, agent_codes),
        source_account_id=codes_to_strings(plan, source_account_codes),
        asset_id=codes_to_asset_wire_ids(plan, asset_codes),
        lot_id=codes_to_strings(plan, plan.lot_id_codes)[lots],
        # Purchase slots start with a static placeholder in the plan and are filled by
        # target-allocation buys at runtime. Slots are single-use, so the final runtime
        # purchase month is also the purchase month for every disposition from that slot.
        purchase_month_index=purchase_months.astype(np.int64),
        units_sold=lot_quantity_column(plan, lots, units),
        cost_basis_consumed_quanta=currency_quanta_column(basis),
        proceeds_quanta=currency_quanta_column(proceeds),
        proceeds_account_id=codes_to_strings(plan, proceeds_account_codes),
    )

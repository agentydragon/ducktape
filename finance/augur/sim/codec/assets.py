"""Asset domain decoders: cash balances, lot inventory, and lot dispositions.
The compile-side twins live in `_compile_lots`, `_compile_cash`, `_compile_sales`,
and `_compile_liquidity_policies` in `augur.sim.compiler`."""

from __future__ import annotations

import numpy as np
import polars as pl

from finance.augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.codec.helpers import (
    asset_code_column,
    code_column,
    codes_to_asset_wire_ids,
    codes_to_strings,
    frame_from_columns,
    lot_quantity_column,
    quantity_column,
    r_first_view,
    state_axes,
    state_history_frame_from_columns,
    usd_column,
)
from finance.augur.sim.compiler import CompiledSimulation
from finance.augur.sim.enums import PrivateEquityDispositionKind, PrivateEquityOpportunityOutcome
from finance.augur.sim.events import EVENT_FRAMES
from finance.augur.sim.state import ASSET_LOT_FRAME, CASH_BALANCES_FRAME


def decode_cash(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    """Cash held by the scenario's AGENTS, month by month.

    The engine's cash array has one more row than this frame: the external account every
    unmodeled counterparty settles against. It is an accounting device, not somebody's bank
    account, and surfacing it here would put a fictitious agent in every consumer of this
    frame — including the UI. Callers that want it (a conservation check, "where did the
    money go") read `buffers.state.cash_state` directly, where it is row
    `plan.external_cash_slot`.
    """

    state = r_first_view(buffers.state.cash_state)[:, :, : plan.external_cash_slot]  # (H+1, r, s)
    h1, r, s = state.shape
    months, rollouts, slots = state_axes(h1, r, s)
    return state_history_frame_from_columns(
        {
            "rollout_index": rollouts,
            "month_index": months,
            "agent_id": code_column(plan, plan.cash_agent_codes[slots]),
            "account_id": code_column(plan, plan.cash_account_codes[slots]),
            "balance_usd": usd_column(state.reshape(-1)),
        },
        CASH_BALANCES_FRAME,
    )


def decode_asset_lots(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    state = r_first_view(buffers.state.lot_state)  # (H+1, r, s)
    # Basis is per-rollout, not a plan column: a lot bought mid-horizon carries the price its
    # rollout paid, and reading the compile-time column would report 0 for every purchased lot.
    basis = r_first_view(buffers.state.lot_cost_basis_state)  # (H+1, r, s)
    h1, r, s = state.shape
    months, rollouts, slots = state_axes(h1, r, s)
    return state_history_frame_from_columns(
        {
            "rollout_index": rollouts,
            "month_index": months,
            "lot_id": code_column(plan, plan.lot_id_codes[slots]),
            "agent_id": code_column(plan, plan.lot_agent_codes[slots]),
            "account_id": code_column(plan, plan.lot_account_codes[slots]),
            "asset_id": asset_code_column(plan, plan.lot_asset_codes[slots]),
            "purchase_month_index": plan.lot_purchase_month.astype(np.int64)[slots],
            "cost_basis_per_unit_usd": usd_column(basis.reshape(-1)),
            "remaining_quantity": lot_quantity_column(plan, slots, state.reshape(-1)),
        },
        ASSET_LOT_FRAME,
    )


def decode_sched_dispositions(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = buffers.lot_dispositions.scheduled.active  # (sale, lot, R) — horizon collapsed
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
        units=buffers.lot_dispositions.scheduled.units[sales, lots, rollouts],
        basis=buffers.lot_dispositions.scheduled.basis[sales, lots, rollouts],
        proceeds=buffers.lot_dispositions.scheduled.proceeds[sales, lots, rollouts],
        proceeds_account_codes=plan.sales.proceeds_account[sales],
    )


def decode_liquidity_dispositions(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = buffers.lot_dispositions.liquidity.active  # (M, policy, asset_idx, lot, R)
    # Pre-filter inactive asset slots (asset_code < 0). The plan's liquidity_policy_asset_codes
    # is (policy, asset_idx); a negative entry means that asset slot isn't used by the policy.
    asset_valid = plan.liquidity_policies.assets >= 0  # (policy, asset_idx)
    # Broadcast valid mask to active's shape and AND it in.
    valid_full = asset_valid[None, :, :, None, None]  # (1, policy, asset_idx, 1, 1)
    active = active & valid_full
    if active.any():
        months, policies, asset_idxs, lots, rollouts = np.argwhere(active).T
    else:
        months = policies = asset_idxs = lots = rollouts = np.array([], dtype=np.int64)
    asset_codes = plan.liquidity_policies.assets[policies, asset_idxs]
    # Per-event cause_id is "{policy_prefix}_m{month}_{asset_name}". O(N) Python comp over
    # the gathered events, not the dense iteration space.
    asset_names = codes_to_asset_wire_ids(plan, plan.liquidity_policies.assets)[policies, asset_idxs]
    prefixes_per_event = np.array(plan.liquidity_policies.cause_id_prefixes, dtype=object)[policies]
    cause_ids = np.array(
        [f"{p}_m{m}_{a}" for p, m, a in zip(prefixes_per_event, months, asset_names, strict=True)], dtype=object
    )
    return _lot_disposition_frame(
        plan=plan,
        rollouts=rollouts,
        months=months,
        cause_ids=cause_ids,
        agent_codes=plan.liquidity_policies.agent[policies],
        source_account_codes=plan.lot_account_codes[lots],
        asset_codes=asset_codes,
        lots=lots,
        units=buffers.lot_dispositions.liquidity.units[months, policies, asset_idxs, lots, rollouts],
        basis=buffers.lot_dispositions.liquidity.basis[months, policies, asset_idxs, lots, rollouts],
        proceeds=buffers.lot_dispositions.liquidity.proceeds[months, policies, asset_idxs, lots, rollouts],
        proceeds_account_codes=plan.liquidity_policies.account[policies],
    )


def decode_pe_dispositions(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = buffers.lot_dispositions.pe.active  # (M, issuer, kind, lot, R)
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
        source_account_codes=plan.pe_policies.proceeds_cash_slot[policy_idxs],
        asset_codes=asset_codes,
        lots=lots,
        units=buffers.lot_dispositions.pe.units[months, issuers, kinds, lots, rollouts],
        basis=buffers.lot_dispositions.pe.basis[months, issuers, kinds, lots, rollouts],
        proceeds=buffers.lot_dispositions.pe.proceeds[months, issuers, kinds, lots, rollouts],
        proceeds_account_codes=plan.pe_policies.proceeds_cash_slot[policy_idxs],
    )


def decode_pe_protocol_events(plan: CompiledSimulation) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    channels = plan.pe_channels
    for issuer_idx, issuer_code in enumerate(plan.pe_issuers.codes):
        if int(issuer_code) < 0:
            continue
        issuer_id = str(codes_to_strings(plan, np.array([issuer_code], dtype=np.int64))[0])
        event_codes = channels.event_kind_codes[issuer_idx]
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
                    "mark_usd": float(channels.marks[issuer_idx, rollout, month]),
                    "sale_capacity_fraction": float(channels.sale_capacity_fractions[issuer_idx, rollout, month]),
                    "eligible_fraction": float(channels.eligible_fractions[issuer_idx, rollout, month]),
                    "forced_sale_fraction": float(channels.forced_sale_fractions[issuer_idx, rollout, month]),
                    "liquidity_blocked": bool(channels.liquidity_blocked[issuer_idx, rollout, month]),
                    "forced_recovery_cashout_usd": float(
                        usd_column(channels.forced_recovery_cashout_cents[issuer_idx, rollout, month])
                    ),
                }
            )
    if not rows:
        return EVENT_FRAMES.private_equity_events.empty()
    return EVENT_FRAMES.private_equity_events.normalize(pl.DataFrame(rows))


def decode_pe_opportunity_events(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = buffers.private_equity_opportunities.active
    if not active.any():
        return EVENT_FRAMES.private_equity_opportunities.empty()
    months, issuers, rollouts = np.argwhere(active).T
    issuer_ids = codes_to_strings(plan, plan.pe_issuers.codes)
    channels = plan.pe_channels
    rows: list[dict[str, object]] = []
    for month, issuer_idx, rollout in zip(months, issuers, rollouts, strict=True):
        issuer_id = str(issuer_ids[issuer_idx])
        event_code = PrivateEquityEventKindCode(int(channels.event_kind_codes[issuer_idx, rollout, month]))
        regime_code = PrivateEquityRegimeCode(int(channels.regime_codes[issuer_idx, rollout, month]))
        outcome = PrivateEquityOpportunityOutcome(
            int(buffers.private_equity_opportunities.outcome[month, issuer_idx, rollout])
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
                "mark_usd": float(channels.marks[issuer_idx, rollout, month]),
                "sale_capacity_fraction": float(channels.sale_capacity_fractions[issuer_idx, rollout, month]),
                "eligible_fraction": float(channels.eligible_fractions[issuer_idx, rollout, month]),
                "liquidity_blocked": bool(channels.liquidity_blocked[issuer_idx, rollout, month]),
                "floor_usd": float(usd_column(buffers.private_equity_opportunities.floor[month, issuer_idx, rollout])),
                "liquid_net_worth_usd": float(
                    usd_column(buffers.private_equity_opportunities.liquid_net_worth[month, issuer_idx, rollout])
                ),
                "shortfall_usd": float(
                    usd_column(buffers.private_equity_opportunities.shortfall[month, issuer_idx, rollout])
                ),
                "units_held": float(
                    quantity_column(
                        buffers.private_equity_opportunities.units_held[month, issuer_idx, rollout],
                        _pe_issuer_scale(plan, issuer_idx),
                    )
                ),
                "sellable_units": float(
                    quantity_column(
                        buffers.private_equity_opportunities.sellable_units[month, issuer_idx, rollout],
                        _pe_issuer_scale(plan, issuer_idx),
                    )
                ),
                "target_units": float(
                    quantity_column(
                        buffers.private_equity_opportunities.target_units[month, issuer_idx, rollout],
                        _pe_issuer_scale(plan, issuer_idx),
                    )
                ),
                "proceeds_usd": float(
                    usd_column(buffers.private_equity_opportunities.proceeds[month, issuer_idx, rollout])
                ),
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
        purchase_month_index=plan.lot_purchase_month.astype(np.int64)[lots],
        units_sold=lot_quantity_column(plan, lots, units),
        cost_basis_consumed_usd=usd_column(basis),
        proceeds_usd=usd_column(proceeds),
        proceeds_account_id=codes_to_strings(plan, proceeds_account_codes),
    )

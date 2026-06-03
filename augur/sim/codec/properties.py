"""Property domain decoders. The compile-side twin is `PropertyCompileOutput` /
`_compile_properties` in `augur.sim.compiler`."""

from __future__ import annotations

import numpy as np
import polars as pl

from augur.sim.buffers import SimulationBuffers
from augur.sim.codec.helpers import (
    codes_to_strings,
    frame_from_columns,
    r_first_view,
    state_axes,
    state_history_frame_from_columns,
)
from augur.sim.compiler import CompiledSimulation
from augur.sim.events import EVENT_FRAMES
from augur.sim.state import PROPERTY_STAKE_FRAME, PROPERTY_STATE_FRAME


def decode_property_state(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    basis = r_first_view(buffers.state.property_basis_state)  # (H+1, r, p)
    active = r_first_view(buffers.state.property_active_state)
    h1, r, p = basis.shape
    months, rollouts, props = state_axes(h1, r, p)
    mask = active.reshape(-1)
    property_ids = codes_to_strings(plan, plan.properties.id)
    location_ids = codes_to_strings(plan, plan.properties.location_id)
    return state_history_frame_from_columns(
        {
            "rollout_index": rollouts[mask],
            "month_index": months[mask],
            "property_id": property_ids[props[mask]],
            "location_id": location_ids[props[mask]],
            "purchase_month_index": plan.properties.month.astype(np.int64)[props[mask]],
            "adjusted_basis_usd": basis.reshape(-1)[mask],
        },
        PROPERTY_STATE_FRAME,
    )


def decode_property_stakes(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = r_first_view(buffers.state.property_active_state)  # (H+1, r, p)
    h1, r, p = active.shape
    months, rollouts, props = state_axes(h1, r, p)
    mask = active.reshape(-1)
    # The mask + (month, rollout, property) axes are in R-first order, so the per-property
    # state buffers must be viewed R-first too before flattening — the raw buffers are
    # (snapshot, property, rollout). Flattening them raw and applying the R-first mask
    # cross-assigns stake values between properties once property_count > 1.
    ownership = r_first_view(buffers.state.property_ownership_state)
    contribution = r_first_view(buffers.state.property_contribution_state)
    equity = r_first_view(buffers.state.property_equity_state)
    property_ids = codes_to_strings(plan, plan.properties.id)
    buyer_ids = codes_to_strings(plan, plan.properties.buyer_agent)
    return state_history_frame_from_columns(
        {
            "rollout_index": rollouts[mask],
            "month_index": months[mask],
            "property_id": property_ids[props[mask]],
            "agent_id": buyer_ids[props[mask]],
            "ownership_pct": ownership.reshape(-1)[mask],
            "contribution_used_usd": contribution.reshape(-1)[mask],
            "equity_ledger_usd": equity.reshape(-1)[mask],
        },
        PROPERTY_STAKE_FRAME,
    )


def decode_property_purchases(
    plan: CompiledSimulation, buffers: SimulationBuffers
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Returns (property_purchases_frame, derived_transfers_frame).

    The transfers frame is the subset of purchases whose `property_transfer_active` flag is
    set — the buyer-cash transfer that goes alongside the purchase event.
    """

    active = buffers.properties.purchase_active  # (M, P, R)
    if active.any():
        months, props, rollouts = np.argwhere(active).T
    else:
        months = props = rollouts = np.array([], dtype=np.int64)
    cause_ids = codes_to_strings(plan, plan.properties.cause)[months, props]
    property_ids = codes_to_strings(plan, plan.properties.id)[props]
    location_ids = codes_to_strings(plan, plan.properties.location_id)[props]
    buyer_agents = codes_to_strings(plan, plan.properties.buyer_agent)[props]
    buyer_accounts = codes_to_strings(plan, plan.properties.buyer_account)[props]
    seller_agents = codes_to_strings(plan, plan.properties.seller_agent)[props]
    seller_accounts = codes_to_strings(plan, plan.properties.seller_account)[props]
    purchases = frame_from_columns(
        EVENT_FRAMES.property_purchases,
        rollout_index=rollouts,
        month_index=months,
        cause_id=cause_ids,
        property_id=property_ids,
        location_id=location_ids,
        buyer_agent_id=buyer_agents,
        purchase_price_usd=plan.properties.purchase_price.astype(np.float64)[props],
        closing_cost_usd=plan.properties.closing_cost.astype(np.float64)[props],
        adjusted_basis_usd=plan.properties.adjusted_basis.astype(np.float64)[props],
        ownership_pct=plan.properties.ownership.astype(np.float64)[props],
        stake_contribution_usd=plan.properties.stake_contribution.astype(np.float64)[props],
        equity_ledger_usd=plan.properties.equity_ledger.astype(np.float64)[props],
    )
    # Derived buyer-cash transfers: subset where `property_transfer_active` also holds.
    transfer_mask = buffers.properties.transfer_active[months, props, rollouts]
    if transfer_mask.any():
        m_t = months[transfer_mask]
        p_t = props[transfer_mask]
        r_t = rollouts[transfer_mask]
        cause_t = np.array([f"{c}_buyer_cash" for c in cause_ids[transfer_mask]], dtype=object)
        transfers = frame_from_columns(
            EVENT_FRAMES.transfers,
            rollout_index=r_t,
            month_index=m_t,
            cause_id=cause_t,
            from_agent_id=buyer_agents[transfer_mask],
            from_account_id=buyer_accounts[transfer_mask],
            to_agent_id=seller_agents[transfer_mask],
            to_account_id=seller_accounts[transfer_mask],
            amount_usd=plan.properties.stake_contribution.astype(np.float64)[p_t],
            income_category=np.full(p_t.size, None, dtype=object),
        )
    else:
        transfers = EVENT_FRAMES.transfers.empty()
    return purchases, transfers

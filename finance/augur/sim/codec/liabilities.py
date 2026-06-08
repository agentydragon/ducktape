"""Liability domain decoders: principal/payment state, mortgage origination + payment
events. The compile-side twin is `LiabilityCompileOutput` + `_compile_liabilities` in
`augur.sim.compiler`."""

from __future__ import annotations

import numpy as np
import polars as pl

from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.codec.helpers import (
    code_column,
    codes_to_strings,
    frame_from_columns,
    r_first_view,
    state_axes,
    state_history_frame_from_columns,
    usd_column,
)
from finance.augur.sim.compiler import CompiledSimulation
from finance.augur.sim.events import EVENT_FRAMES
from finance.augur.sim.state import LIABILITY_FRAME


def decode_liabilities(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    principal = r_first_view(buffers.state.liability_principal_state)  # (H+1, R, n_liab)
    active = r_first_view(buffers.state.liability_active_state)
    h1, r, n_liab = principal.shape
    months, rollouts, liabs = state_axes(h1, r, n_liab)
    mask = active.reshape(-1)
    liab_idx = liabs[mask]
    property_slot = plan.liabilities.property_slot.astype(np.int64)
    property_id_codes = plan.properties.id[property_slot]
    origination_per_liab = plan.properties.month.astype(np.int64)[property_slot]
    return state_history_frame_from_columns(
        {
            "rollout_index": rollouts[mask],
            "month_index": months[mask],
            "liability_id": code_column(plan, plan.liabilities.codes[liab_idx]),
            "agent_id": code_column(plan, plan.liabilities.agent[liab_idx]),
            "payment_account_id": code_column(plan, plan.liabilities.payment_account[liab_idx]),
            "counterparty_agent_id": code_column(plan, plan.liabilities.counterparty_agent[liab_idx]),
            "counterparty_account_id": code_column(plan, plan.liabilities.counterparty_account[liab_idx]),
            "property_id": code_column(plan, property_id_codes[liab_idx]),
            "principal_usd": usd_column(principal.reshape(-1)[mask]),
            "annual_interest_rate": plan.liabilities.annual_rate.astype(np.float64)[liab_idx],
            "term_months": plan.liabilities.term_months.astype(np.int64)[liab_idx],
            "origination_month_index": origination_per_liab[liab_idx],
            "monthly_payment_usd": usd_column(
                r_first_view(buffers.state.liability_monthly_payment_state).reshape(-1)[mask]
            ),
            "interest_paid_ytd_usd": usd_column(
                r_first_view(buffers.state.liability_interest_ytd_state).reshape(-1)[mask]
            ),
            "principal_paid_ytd_usd": usd_column(
                r_first_view(buffers.state.liability_principal_ytd_state).reshape(-1)[mask]
            ),
        },
        LIABILITY_FRAME,
    )


def decode_mortgage_originations(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = buffers.properties.mortgage_origination_active  # (M, liab, R)
    if active.any():
        months, liabs, rollouts = np.argwhere(active).T
    else:
        months = liabs = rollouts = np.array([], dtype=np.int64)
    props = plan.liabilities.property_slot.astype(np.int64)[liabs]
    cause_codes_per_event = plan.properties.cause[months, props]
    cause_text = codes_to_strings(plan, cause_codes_per_event)
    cause_ids = np.array([f"{c}_mortgage_origination" for c in cause_text], dtype=object)
    return frame_from_columns(
        EVENT_FRAMES.mortgage_originations,
        rollout_index=rollouts,
        month_index=months,
        cause_id=cause_ids,
        liability_id=codes_to_strings(plan, plan.liabilities.codes)[liabs],
        agent_id=codes_to_strings(plan, plan.liabilities.agent)[liabs],
        payment_account_id=codes_to_strings(plan, plan.liabilities.payment_account)[liabs],
        counterparty_agent_id=codes_to_strings(plan, plan.liabilities.counterparty_agent)[liabs],
        counterparty_account_id=codes_to_strings(plan, plan.liabilities.counterparty_account)[liabs],
        property_id=codes_to_strings(plan, plan.properties.id)[props],
        principal_usd=usd_column(plan.liabilities.principal[liabs]),
        annual_interest_rate=plan.liabilities.annual_rate.astype(np.float64)[liabs],
        term_months=plan.liabilities.term_months.astype(np.int64)[liabs],
        monthly_payment_usd=usd_column(plan.liabilities.monthly_payment[liabs]),
    )


def decode_mortgage_payments(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = buffers.properties.mortgage_payment_active  # (M, liab, R)
    if active.any():
        months, liabs, rollouts = np.argwhere(active).T
    else:
        months = liabs = rollouts = np.array([], dtype=np.int64)
    props = plan.liabilities.property_slot.astype(np.int64)[liabs]
    liability_ids = codes_to_strings(plan, plan.liabilities.codes)[liabs]
    cause_ids = np.array([f"{lid}_payment_m{m}" for lid, m in zip(liability_ids, months, strict=True)], dtype=object)
    return frame_from_columns(
        EVENT_FRAMES.mortgage_payments,
        rollout_index=rollouts,
        month_index=months,
        cause_id=cause_ids,
        liability_id=liability_ids,
        agent_id=codes_to_strings(plan, plan.liabilities.agent)[liabs],
        counterparty_agent_id=codes_to_strings(plan, plan.liabilities.counterparty_agent)[liabs],
        property_id=codes_to_strings(plan, plan.properties.id)[props],
        from_account_id=codes_to_strings(plan, plan.liabilities.payment_account)[liabs],
        to_account_id=codes_to_strings(plan, plan.liabilities.counterparty_account)[liabs],
        interest_usd=usd_column(buffers.properties.mortgage_payment_interest[months, liabs, rollouts]),
        principal_usd=usd_column(buffers.properties.mortgage_payment_principal[months, liabs, rollouts]),
        total_payment_usd=usd_column(buffers.properties.mortgage_payment_total[months, liabs, rollouts]),
    )

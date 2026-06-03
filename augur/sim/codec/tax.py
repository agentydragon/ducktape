"""Tax domain decoders: ordinary income, capital gains, tax liabilities, and
year-end accrual/settlement events. The compile-side twin is `TaxCompileOutput` +
`TaxLiabilityCompileOutput` + `_compile_tax`/`_compile_tax_liabilities` in
`augur.sim.compiler`."""

from __future__ import annotations

import numpy as np
import polars as pl

from augur.sim.buffers import SimulationBuffers
from augur.sim.codec.helpers import (
    active_state_axes,
    codes_to_strings,
    frame_from_columns,
    r_first_view,
    state_axes,
    state_history_frame_from_columns,
)
from augur.sim.compiler import CompiledSimulation
from augur.sim.enums import CapitalGainClassification
from augur.sim.events import EVENT_FRAMES
from augur.sim.state import CAPITAL_GAINS_YTD_FRAME, ORDINARY_INCOME_YTD_FRAME, TAX_LIABILITIES_FRAME


def decode_ordinary_income(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    state = r_first_view(buffers.state.ordinary_state)  # (H+1, r, p)
    h1, r, p = state.shape
    months, rollouts, profiles = state_axes(h1, r, p)
    return state_history_frame_from_columns(
        {
            "rollout_index": rollouts,
            "month_index": months,
            "agent_id": codes_to_strings(plan, plan.tax.profile_agent)[profiles],
            "ordinary_income_usd": state.reshape(-1),
        },
        ORDINARY_INCOME_YTD_FRAME,
    )


def decode_capital_gains(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    # capital_gain_state: (H+1, r, p, 2). Last axis is (LTCG, STCG) per *_CAPITAL_GAIN_CODE.
    # Mask-filter keeps only `active_state[m, r, p, cls]` rows. The two `cls` codes happen to be
    # 0 and 1, with LTCG = LONG_TERM... = 0, STCG = SHORT_TERM... = 1, but iterate explicitly so
    # the classification column matches the legacy decoder's row order ((profile, ltcg, stcg)).
    state = r_first_view(buffers.state.capital_gain_state)
    active = r_first_view(buffers.state.capital_gain_active_state)
    h1, r, p, _c = state.shape
    months = np.broadcast_to(np.arange(h1, dtype=np.int64)[:, None, None, None], (h1, r, p, 2))
    rollouts = np.broadcast_to(np.arange(r, dtype=np.int64)[None, :, None, None], (h1, r, p, 2))
    profiles = np.broadcast_to(np.arange(p, dtype=np.int64)[None, None, :, None], (h1, r, p, 2))
    # Order class slots so LTCG (index CapitalGainClassification.LONG_TERM) comes first within each profile.
    cls_order = np.array([CapitalGainClassification.LONG_TERM, CapitalGainClassification.SHORT_TERM], dtype=np.int64)
    classification_labels = np.array(["ltcg", "stcg"], dtype=object)
    state_o = state[:, :, :, cls_order]
    active_o = active[:, :, :, cls_order]
    cls_labels = np.broadcast_to(classification_labels[None, None, None, :], (h1, r, p, 2))
    mask = active_o.reshape(-1)
    agent_ids = codes_to_strings(plan, plan.capital_gain_agent_codes)
    return state_history_frame_from_columns(
        {
            "rollout_index": rollouts.reshape(-1)[mask],
            "month_index": months.reshape(-1)[mask],
            "agent_id": agent_ids[profiles.reshape(-1)[mask]],
            "classification": cls_labels.reshape(-1)[mask],
            "gain_usd": state_o.reshape(-1)[mask],
        },
        CAPITAL_GAINS_YTD_FRAME,
    )


def decode_tax_liabilities(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    state = r_first_view(buffers.state.tax_liability_state)  # (H+1, r, s)
    active = r_first_view(buffers.state.tax_liability_active_state)
    months, rollouts, slots = active_state_axes(active)
    profile_per_slot = plan.tax_liabilities.profile_index.astype(np.int64)
    link_per_slot = plan.tax_liabilities.link_index.astype(np.int64)
    agent_per_profile = codes_to_strings(plan, plan.tax.profile_agent)
    juris_per_link = codes_to_strings(plan, plan.tax.link_jurisdiction)
    return state_history_frame_from_columns(
        {
            "rollout_index": rollouts,
            "month_index": months,
            "agent_id": agent_per_profile[profile_per_slot[slots]],
            "jurisdiction_id": juris_per_link[link_per_slot[slots]],
            "tax_year_end_month": plan.tax_liabilities.year_end_month.astype(np.int64)[slots],
            "amount_owed_usd": state[months, rollouts, slots],
        },
        TAX_LIABILITIES_FRAME,
    )


def decode_tax_accruals(plan: CompiledSimulation, buffers: SimulationBuffers) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Returns (tax_accruals_frame, tax_breakdowns_frame). Same active mask, two output frames."""

    active = buffers.taxes.accrual_active  # (M, link, R)
    if active.any():
        months, links, rollouts = np.argwhere(active).T
    else:
        months = links = rollouts = np.array([], dtype=np.int64)
    profiles = plan.tax.link_profile.astype(np.int64)[links]
    agent_ids = codes_to_strings(plan, plan.tax.profile_agent)[profiles]
    jurisdiction_ids = codes_to_strings(plan, plan.tax.link_jurisdiction)[links]
    # cause_id is f"{agent_id}_{jurisdiction_id}_year_end_accrual_m{month}".
    cause_ids = np.array(
        [f"{a}_{j}_year_end_accrual_m{m}" for a, j, m in zip(agent_ids, jurisdiction_ids, months, strict=True)],
        dtype=object,
    )
    totals = buffers.taxes.accrual_amount[months, links, rollouts]
    accruals = frame_from_columns(
        EVENT_FRAMES.tax_accruals,
        rollout_index=rollouts,
        month_index=months,
        cause_id=cause_ids,
        agent_id=agent_ids,
        jurisdiction_id=jurisdiction_ids,
        tax_year_end_month=months,
        amount_usd=totals,
    )
    breakdowns = frame_from_columns(
        EVENT_FRAMES.tax_breakdowns,
        rollout_index=rollouts,
        month_index=months,
        cause_id=cause_ids,
        agent_id=agent_ids,
        jurisdiction_id=jurisdiction_ids,
        tax_year_end_month=months,
        ordinary_income_usd=buffers.taxes.breakdown_ordinary[months, links, rollouts],
        ltcg_usd=buffers.taxes.breakdown_ltcg[months, links, rollouts],
        stcg_usd=buffers.taxes.breakdown_stcg[months, links, rollouts],
        standard_deduction_usd=plan.tax.link_standard_deduction.astype(np.float64)[links],
        mortgage_interest_deduction_usd=buffers.taxes.breakdown_mortgage_interest_deduction[months, links, rollouts],
        salt_deduction_usd=buffers.taxes.breakdown_salt_deduction[months, links, rollouts],
        itemized_deduction_usd=buffers.taxes.breakdown_itemized_deduction[months, links, rollouts],
        ordinary_taxable_usd=buffers.taxes.breakdown_ordinary_taxable[months, links, rollouts],
        capital_gain_taxable_usd=buffers.taxes.breakdown_capital_taxable[months, links, rollouts],
        ordinary_tax_usd=buffers.taxes.breakdown_ordinary_tax[months, links, rollouts],
        capital_gain_tax_usd=buffers.taxes.breakdown_capital_tax[months, links, rollouts],
        total_tax_usd=totals,
    )
    return accruals, breakdowns


def decode_tax_settlements(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = buffers.taxes.settlement_active  # (M, profile, R)
    if active.any():
        months, profiles, rollouts = np.argwhere(active).T
    else:
        months = profiles = rollouts = np.array([], dtype=np.int64)
    agent_ids = codes_to_strings(plan, plan.tax.profile_agent)[profiles]
    year_end = buffers.taxes.settlement_year_end_month[months, profiles, rollouts].astype(np.int64)
    tax_years = (year_end - 11) // 12
    cause_ids = np.array([f"{a}_tax_settlement_y{y}" for a, y in zip(agent_ids, tax_years, strict=True)], dtype=object)
    return frame_from_columns(
        EVENT_FRAMES.tax_settlements,
        rollout_index=rollouts,
        month_index=months,
        cause_id=cause_ids,
        agent_id=agent_ids,
        tax_year_end_month=year_end,
        amount_usd=buffers.taxes.settlement_amount[months, profiles, rollouts],
    )

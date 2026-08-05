"""Host-side scatter from JAX scan outputs into simulation buffers."""

from __future__ import annotations

import jax
import numpy as np

from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.compiler import CompiledSimulation
from finance.augur.sim.engine.jax_types import _ScanMeta


def scatter_ys_to_buffers(
    plan: CompiledSimulation, buffers: SimulationBuffers, meta: _ScanMeta, ys: tuple, sale_disp: tuple
) -> None:
    """Scatter compiled-program outputs back into NumPy buffers.

    This is pure host code. It performs one batched device-to-host transfer for the stacked monthly
    `ys` pytree and horizon-collapsed scheduled-sale disposition carry, then writes the decoded leaves
    into the preallocated NumPy buffers using the structural targets carried by `meta`.
    """

    p = plan.slot_plan
    r = p.rollout_count
    horizon = meta.horizon
    link_count = meta.link_count
    folded_purchases = meta.folded_purchases
    folded_liquidity = meta.folded_liquidity
    folded_pe = meta.folded_pe
    folded_lifecycle = meta.folded_lifecycle
    folded_pr = meta.folded_pr
    folded_sale_events = meta.folded_sale_events
    cash0 = np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, r))
    lot0 = np.broadcast_to(plan.lot_initial_quantity[:, None], (p.lot_count, r))
    ys, sale_disp = jax.device_get((ys, sale_disp))
    (
        cash_h,
        ordinary_h,
        lot_h,
        lot_basis_h,
        cg_active_h,
        cg_ytd_h,
        prop_active_h,
        prop_basis_h,
        prop_contribution_h,
        prop_equity_h,
        prop_cum_dep_h,
        prop_occupied_h,
        liab_active_h,
        liab_principal_h,
        liab_monthly_h,
        liab_interest_ytd_h,
        liab_principal_ytd_h,
        failed_h,
        failed_month_h,
        t_active,
        t_amount,
        pc_active,
        pc_amount,
        ob_active,
        ob_due,
        ob_paid,
        ob_short,
        ob_fail,
        *rest,
    ) = ys
    # Five variable-length tail groups, sliced by compile-time presence: sale slabs (5 if any sales),
    # property-event slabs (2 if any purchases), mortgage-event slabs (5 if any liabilities), tax slabs
    # (18 = 13 breakdowns + 2 tax-liability snapshots + 3 settlement events, if any tax links), and
    # liquidity slabs (5 if any liquidity policies).
    n_sale = 0  # scheduled-sale dispositions are carried out-of-band (`sale_disp`), not in `ys`
    n_purchase = 2 if folded_purchases else 0
    n_mortgage = 5 if p.liability_count > 0 else 0
    n_tax = 18 if link_count > 0 else 0
    n_liquidity = 5 if folded_liquidity else 0
    n_pe = 13 if folded_pe else 0
    n_le_fired = 1 if folded_lifecycle else 0
    n_pr_fired = 1 if folded_pr else 0
    o1 = n_sale
    o2 = o1 + n_purchase
    o3 = o2 + n_mortgage
    o4 = o3 + n_tax
    o5 = o4 + n_liquidity
    o6 = o5 + n_pe
    o7 = o6 + n_le_fired
    o8 = o7 + n_pr_fired
    purchase_h = rest[o1:o2]  # o1 == 0 (scheduled-sale dispositions are carried, not in `ys`)
    mortgage_h = rest[o2:o3]
    tax_h = rest[o3:o4]
    liquidity_h = rest[o4:o5]
    pe_h = rest[o5:o6]
    le_fired_h = rest[o6:o7]
    pr_fired_h = rest[o7:o8]
    sale_trace_h = rest[o8:]

    buffers.state.cash_state[0] = np.asarray(cash0)
    buffers.state.cash_state[1:] = np.asarray(cash_h)
    buffers.state.ordinary_state[1:] = np.asarray(ordinary_h)
    buffers.state.lot_state[0] = np.asarray(lot0)
    buffers.state.lot_state[1:] = np.asarray(lot_h)
    buffers.state.lot_cost_basis_state[0] = np.broadcast_to(plan.lot_cost_basis_per_unit[:, None], (p.lot_count, r))
    buffers.state.lot_cost_basis_state[1:] = np.asarray(lot_basis_h)
    buffers.state.capital_gain_active_state[1:] = np.asarray(cg_active_h)
    buffers.state.capital_gain_state[1:] = np.asarray(cg_ytd_h)
    buffers.state.property_active_state[1:] = np.asarray(prop_active_h)
    buffers.state.property_basis_state[1:] = np.asarray(prop_basis_h)
    buffers.state.property_contribution_state[1:] = np.asarray(prop_contribution_h)
    buffers.state.property_equity_state[1:] = np.asarray(prop_equity_h)
    buffers.state.property_cumulative_depreciation_state[1:] = np.asarray(prop_cum_dep_h)
    buffers.state.property_owner_occupied_months_state[1:] = np.asarray(prop_occupied_h)
    buffers.state.liability_active_state[1:] = np.asarray(liab_active_h)
    buffers.state.liability_principal_state[1:] = np.asarray(liab_principal_h)
    buffers.state.liability_monthly_payment_state[1:] = np.asarray(liab_monthly_h)
    buffers.state.liability_interest_ytd_state[1:] = np.asarray(liab_interest_ytd_h)
    buffers.state.liability_principal_ytd_state[1:] = np.asarray(liab_principal_ytd_h)
    buffers.state.rollout_failed_state[1:] = np.asarray(failed_h)
    buffers.state.rollout_failed_month_state[1:] = np.asarray(failed_month_h)
    buffers.transfers.active[:] = np.asarray(t_active)
    buffers.transfers.amount[:] = np.asarray(t_amount)
    buffers.property_cashflows.active[:] = np.asarray(pc_active)
    buffers.property_cashflows.amount[:] = np.asarray(pc_amount)
    buffers.obligations.active[:] = np.asarray(ob_active)
    buffers.obligations.due[:] = np.asarray(ob_due)
    buffers.obligations.paid[:] = np.asarray(ob_paid)
    buffers.obligations.shortfall[:] = np.asarray(ob_short)
    buffers.obligations.failure_active[:] = np.asarray(ob_fail)

    # Scheduled-sale dispositions: the carry holds `(scheduled_sale, lot, R)` already indexed by each
    # sale's slot (the firing month is static, so the decoder re-derives it).
    disp_units_h, disp_basis_h, disp_proceeds_h, oversell_h = sale_disp
    if bool(np.asarray(oversell_h)):  # match the eager engine's hard error on the first oversell
        raise ValueError("scheduled asset sale exceeds available lots")
    disp = buffers.lot_dispositions.scheduled
    disp.units[:] = np.asarray(disp_units_h)
    disp.basis[:] = np.asarray(disp_basis_h)
    disp.proceeds[:] = np.asarray(disp_proceeds_h)
    disp.active[:] = disp.units > 0

    if folded_purchases:
        # Stacks are `(horizon, num_real_purchases, R)`; scatter each to its property column.
        purchase_active_np, transfer_active_np = (np.asarray(a) for a in purchase_h)
        for i, fp in enumerate(folded_purchases):
            buffers.properties.purchase_active[:, fp.buffer_index] = purchase_active_np[:, i]
            buffers.properties.transfer_active[:, fp.buffer_index] = transfer_active_np[:, i]
    if mortgage_h:
        # Per-liability mortgage event stacks `(horizon, liability_count, R)`.
        orig_h, pay_active_h, pay_interest_h, pay_principal_h, pay_total_h = (np.asarray(a) for a in mortgage_h)
        props_buf = buffers.properties
        props_buf.mortgage_origination_active[:] = orig_h
        props_buf.mortgage_payment_active[:] = pay_active_h
        props_buf.mortgage_payment_interest[:] = pay_interest_h
        props_buf.mortgage_payment_principal[:] = pay_principal_h
        props_buf.mortgage_payment_total[:] = pay_total_h
    if tax_h:
        # 13 per-(month, link) breakdown stacks + tax-liability snapshots + 3 settlement event stacks.
        *breakdown_h, taxliab_amount_h, taxliab_active_h, settle_active_h, settle_amount_h, settle_year_end_h = (
            np.asarray(a) for a in tax_h
        )
        taxes = buffers.taxes
        taxes.accrual_active[:] = breakdown_h[0] > 0
        for buf, slab in zip(
            (
                taxes.accrual_amount,
                taxes.breakdown_ordinary,
                taxes.breakdown_ltcg,
                taxes.breakdown_stcg,
                taxes.breakdown_standard_deduction,
                taxes.breakdown_mortgage_interest_deduction,
                taxes.breakdown_salt_deduction,
                taxes.breakdown_itemized_deduction,
                taxes.breakdown_ordinary_taxable,
                taxes.breakdown_capital_taxable,
                taxes.breakdown_ordinary_tax,
                taxes.breakdown_capital_tax,
            ),
            breakdown_h[1:],
            strict=True,
        ):
            buf[:] = slab
        n_prof = settle_active_h.shape[1]
        taxes.settlement_active[:, :n_prof] = settle_active_h
        taxes.settlement_amount[:, :n_prof] = settle_amount_h
        taxes.settlement_year_end_month[:, :n_prof] = settle_year_end_h

        # Reconstruct the sparse tax-liability change log by diffing per-month snapshots: a year-end
        # accrual (0 -> tax) and a true-up settlement (tax -> 0) each change a slot's balance; record
        # the post-change balance at month m+1 for every slot that changed that month.
        prev_amount = np.zeros_like(taxliab_amount_h[0])
        prev_active = np.zeros_like(taxliab_active_h[0])
        for m in range(horizon):
            changed = np.flatnonzero(
                (taxliab_amount_h[m] != prev_amount).any(axis=1) | (taxliab_active_h[m] != prev_active).any(axis=1)
            )
            if changed.size:
                buffers.tax_liability_changes.record(
                    snapshot_month=m + 1, slots=changed, amount=taxliab_amount_h[m], active=taxliab_active_h[m]
                )
            prev_amount, prev_active = taxliab_amount_h[m], taxliab_active_h[m]
    if liquidity_h:
        # Per-(month, policy, asset) liquidity disposition stacks + the per-obligation attempt-policy.
        liq_active_h, liq_units_h, liq_basis_h, liq_proceeds_h, attempt_h = (np.asarray(a) for a in liquidity_h)
        liq = buffers.lot_dispositions.liquidity
        liq.active[:] = liq_active_h
        liq.units[:] = liq_units_h
        liq.basis[:] = liq_basis_h
        liq.proceeds[:] = liq_proceeds_h
        buffers.obligations.attempt_policy[:] = attempt_h
    if pe_h:
        # 4 per-(month, issuer, kind) disposition stacks + 9 per-(month, issuer) opportunity stacks.
        pe_active_h, pe_units_h, pe_basis_h, pe_proceeds_h = (np.asarray(a) for a in pe_h[:4])
        pe = buffers.lot_dispositions.pe
        pe.active[:] = pe_active_h
        pe.units[:] = pe_units_h
        pe.basis[:] = pe_basis_h
        pe.proceeds[:] = pe_proceeds_h
        opp = buffers.private_equity_opportunities
        (opp_active, opp_outcome, opp_floor, opp_lnw, opp_short, opp_units, opp_sellable, opp_target, opp_proceeds) = (
            np.asarray(a) for a in pe_h[4:]
        )
        opp.active[:] = opp_active.astype(bool)
        opp.outcome[:] = opp_outcome
        opp.floor[:] = opp_floor
        opp.liquid_net_worth[:] = opp_lnw
        opp.shortfall[:] = opp_short
        opp.units_held[:] = opp_units
        opp.sellable_units[:] = opp_sellable
        opp.target_units[:] = opp_target
        opp.proceeds[:] = opp_proceeds
    if le_fired_h:
        # `le_fired_h[0]` is `(horizon, n_lifecycle_events, R)`; each event fires once at its month.
        fired_np = np.asarray(le_fired_h[0])
        for pos, ev in enumerate(folded_lifecycle):
            buffers.lifecycle.fired[ev.event_index] = fired_np[ev.month, pos]
    if pr_fired_h:
        pr_fired_np = np.asarray(pr_fired_h[0])
        for pos, (ei, ev_month) in enumerate(folded_pr):
            buffers.primary_residence.fired[ei] = pr_fired_np[ev_month, pos]
    if sale_trace_h:
        # 7 stacks `(horizon, n_sale_events, R)` in the lifecycle.sale_* field order.
        trace_np = [np.asarray(a) for a in sale_trace_h]
        sale_fields = (
            buffers.lifecycle.sale_gross_proceeds,
            buffers.lifecycle.sale_mortgage_payoff,
            buffers.lifecycle.sale_net_cash,
            buffers.lifecycle.sale_realized_gain,
            buffers.lifecycle.sale_recapture,
            buffers.lifecycle.sale_section_121_exclusion,
            buffers.lifecycle.sale_long_term_gain,
        )
        for pos, (i, ev_month) in enumerate(folded_sale_events):
            for field, stack in zip(sale_fields, trace_np, strict=True):
                field[i] = stack[ev_month, pos]

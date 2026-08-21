"""Host-side scatter from named JAX scan outputs into simulation buffers."""

from __future__ import annotations

import jax
import numpy as np

from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.compiler import CompiledSimulation
from finance.augur.sim.engine.jax_types import _DenseFinalOutput, _DenseScanOutput, _Static


def check_purchase_slot_exhaustion(plan: CompiledSimulation, ta_buy_count: np.ndarray) -> None:
    """Abort the whole run if any target-allocation sleeve wanted more purchases than it had slots.

    Aborting rather than dropping the surplus purchases, and aborting the RUN rather than failing
    the affected rollouts, are the same decision made twice. A dropped purchase is a policy that
    silently stopped investing partway through the horizon; and failing only the rollouts that hit
    the wall would drop exactly the paths that traded most, which — since trading tracks volatility
    — biases the surviving distribution toward calm and makes every result optimistic.
    """

    counts = np.asarray(ta_buy_count)  # (policy, sleeve, R)
    if not counts.size:
        return
    configured = (plan.target_allocation_purchase_slots >= 0).sum(axis=2)  # (policy, sleeve)
    wanted = counts.max(axis=2)
    over = np.argwhere(wanted > configured)
    if not over.size:
        return
    policy_idx, sleeve_idx = (int(x) for x in over[0])
    prefixes = plan.target_allocation_policies.cause_id_prefixes
    raise ValueError(
        f"target-allocation policy {prefixes[policy_idx]!r} sleeve {sleeve_idx} ran out of purchase slots: "
        f"{int(configured[policy_idx, sleeve_idx])} configured, {int(wanted[policy_idx, sleeve_idx])} needed. "
        "Raise `purchase_slots_per_sleeve` — every purchase needs its own lot, because it has its own "
        "basis and its own holding period."
    )


def scatter_ys_to_buffers(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    structure: _Static,
    ys: _DenseScanOutput,
    final_state: _DenseFinalOutput,
) -> None:
    """Scatter one named device-output pytree into the stable NumPy buffer interface."""

    p = plan.slot_plan
    r = p.rollout_count
    cash0 = np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, r))
    lot0 = np.broadcast_to(plan.lot_initial_quantity[:, None], (p.lot_count, r))
    ys, final_state = jax.device_get((ys, final_state))

    state = ys.state
    buffers.state.cash_state[0] = np.asarray(cash0)
    buffers.state.cash_state[1:] = np.asarray(state.cash)
    buffers.state.ordinary_state[1:] = np.asarray(state.ordinary)
    buffers.state.lot_state[0] = np.asarray(lot0)
    buffers.state.lot_state[1:] = np.asarray(state.lots)
    buffers.state.capital_gain_active_state[1:] = np.asarray(state.capital_gain_active)
    buffers.state.capital_gain_state[1:] = np.asarray(state.capital_gain_ytd)
    buffers.state.property_active_state[1:] = np.asarray(state.property_active)
    buffers.state.property_basis_state[1:] = np.asarray(state.property_basis)
    buffers.state.property_contribution_state[1:] = np.asarray(state.property_contribution)
    buffers.state.property_equity_state[1:] = np.asarray(state.property_equity)
    buffers.state.property_cumulative_depreciation_state[1:] = np.asarray(state.property_cumulative_depreciation)
    buffers.state.property_owner_occupied_months_state[1:] = np.asarray(state.property_owner_occupied_months)
    buffers.state.liability_active_state[1:] = np.asarray(state.liability_active)
    buffers.state.liability_principal_state[1:] = np.asarray(state.liability_principal)
    buffers.state.liability_monthly_payment_state[1:] = np.asarray(state.liability_monthly_payment)
    buffers.state.liability_interest_ytd_state[1:] = np.asarray(state.liability_interest_ytd)
    buffers.state.liability_principal_ytd_state[1:] = np.asarray(state.liability_principal_ytd)
    buffers.state.rollout_failed_state[1:] = np.asarray(state.failed)
    buffers.state.rollout_failed_month_state[1:] = np.asarray(state.failed_month)
    buffers.state.spending_tier_state[0] = np.broadcast_to(
        plan.obligations.tiered_spending.initial_tier[:, None], (p.spending_policy_count, r)
    )
    buffers.state.spending_tier_state[1:] = np.asarray(state.spending_tier)

    buffers.transfers.active[:] = np.asarray(ys.transfers.active)
    buffers.transfers.amount[:] = np.asarray(ys.transfers.amount)
    buffers.property_cashflows.active[:] = np.asarray(ys.property_cashflows.active)
    buffers.property_cashflows.amount[:] = np.asarray(ys.property_cashflows.amount)
    buffers.obligations.active[:] = np.asarray(ys.obligations.active)
    buffers.obligations.due[:] = np.asarray(ys.obligations.due)
    buffers.obligations.paid[:] = np.asarray(ys.obligations.paid)
    buffers.obligations.shortfall[:] = np.asarray(ys.obligations.shortfall)
    buffers.obligations.failure_active[:] = np.asarray(ys.obligations.failure_active)

    buffers.state.lot_cost_basis_state[:] = np.asarray(final_state.lot_cost_basis)
    buffers.state.lot_purchase_month_state[:] = np.asarray(final_state.lot_purchase_month)
    if bool(np.asarray(final_state.sale_oversell)):
        raise ValueError("scheduled asset sale exceeds available lots")
    check_purchase_slot_exhaustion(plan, np.asarray(final_state.target_allocation_buy_count))
    scheduled = final_state.scheduled_dispositions
    buffers.lot_dispositions.scheduled.active[:] = np.asarray(scheduled.active)
    buffers.lot_dispositions.scheduled.units[:] = np.asarray(scheduled.units)
    buffers.lot_dispositions.scheduled.basis[:] = np.asarray(scheduled.basis)
    buffers.lot_dispositions.scheduled.proceeds[:] = np.asarray(scheduled.proceeds)

    purchases = ys.property_purchases
    purchase_active = np.asarray(purchases.active)
    purchase_transfer_active = np.asarray(purchases.transfer_active)
    for position, purchase in enumerate(structure.folded_purchases):
        buffers.properties.purchase_active[:, purchase.buffer_index] = purchase_active[:, position]
        buffers.properties.transfer_active[:, purchase.buffer_index] = purchase_transfer_active[:, position]

    mortgage = ys.mortgages
    liability_count = p.liability_count
    buffers.properties.mortgage_origination_active[:, :liability_count] = np.asarray(mortgage.origination_active)
    buffers.properties.mortgage_payment_active[:, :liability_count] = np.asarray(mortgage.payment_active)
    buffers.properties.mortgage_payment_interest[:, :liability_count] = np.asarray(mortgage.payment_interest)
    buffers.properties.mortgage_payment_principal[:, :liability_count] = np.asarray(mortgage.payment_principal)
    buffers.properties.mortgage_payment_total[:, :liability_count] = np.asarray(mortgage.payment_total)

    taxes = ys.taxes
    tax_buffers = buffers.taxes
    tax_buffers.accrual_active[:] = np.asarray(taxes.accrual_active) > 0
    tax_buffers.accrual_amount[:] = np.asarray(taxes.accrual_amount)
    tax_buffers.breakdown_ordinary[:] = np.asarray(taxes.ordinary_income)
    tax_buffers.breakdown_ltcg[:] = np.asarray(taxes.long_term_capital_gain)
    tax_buffers.breakdown_stcg[:] = np.asarray(taxes.short_term_capital_gain)
    tax_buffers.breakdown_standard_deduction[:] = np.asarray(taxes.standard_deduction)
    tax_buffers.breakdown_mortgage_interest_deduction[:] = np.asarray(taxes.mortgage_interest_deduction)
    tax_buffers.breakdown_salt_deduction[:] = np.asarray(taxes.salt_deduction)
    tax_buffers.breakdown_itemized_deduction[:] = np.asarray(taxes.itemized_deduction)
    tax_buffers.breakdown_ordinary_taxable[:] = np.asarray(taxes.ordinary_taxable)
    tax_buffers.breakdown_capital_taxable[:] = np.asarray(taxes.capital_gain_taxable)
    tax_buffers.breakdown_ordinary_tax[:] = np.asarray(taxes.ordinary_tax)
    tax_buffers.breakdown_capital_tax[:] = np.asarray(taxes.capital_gain_tax)
    profile_count = p.tax_profile_count
    tax_buffers.settlement_active[:, :profile_count] = np.asarray(taxes.settlement_active)
    tax_buffers.settlement_amount[:, :profile_count] = np.asarray(taxes.settlement_amount)
    tax_buffers.settlement_year_end_month[:, :profile_count] = np.asarray(taxes.settlement_year_end)
    _record_tax_liability_changes(
        buffers, amount=np.asarray(taxes.liability_amount), active=np.asarray(taxes.liability_active)
    )

    target = ys.target_allocation
    target_dispositions = target.dispositions
    buffers.lot_dispositions.target_allocation.active[:] = np.asarray(target_dispositions.active)
    buffers.lot_dispositions.target_allocation.units[:] = np.asarray(target_dispositions.units)
    buffers.lot_dispositions.target_allocation.basis[:] = np.asarray(target_dispositions.basis)
    buffers.lot_dispositions.target_allocation.proceeds[:] = np.asarray(target_dispositions.proceeds)
    buffers.obligations.attempt_policy[:] = np.asarray(target.obligation_attempt_policy)

    private_equity = ys.private_equity
    pe_dispositions = private_equity.dispositions
    buffers.lot_dispositions.pe.active[:] = np.asarray(pe_dispositions.active)
    buffers.lot_dispositions.pe.units[:] = np.asarray(pe_dispositions.units)
    buffers.lot_dispositions.pe.basis[:] = np.asarray(pe_dispositions.basis)
    buffers.lot_dispositions.pe.proceeds[:] = np.asarray(pe_dispositions.proceeds)
    opportunities = private_equity.opportunities
    buffers.private_equity_opportunities.active[:] = np.asarray(opportunities.active).astype(bool)
    buffers.private_equity_opportunities.outcome[:] = np.asarray(opportunities.outcome)
    buffers.private_equity_opportunities.floor[:] = np.asarray(opportunities.floor)
    buffers.private_equity_opportunities.liquid_net_worth[:] = np.asarray(opportunities.liquid_net_worth)
    buffers.private_equity_opportunities.shortfall[:] = np.asarray(opportunities.shortfall)
    buffers.private_equity_opportunities.units_held[:] = np.asarray(opportunities.units_held)
    buffers.private_equity_opportunities.sellable_units[:] = np.asarray(opportunities.sellable_units)
    buffers.private_equity_opportunities.target_units[:] = np.asarray(opportunities.target_units)
    buffers.private_equity_opportunities.proceeds[:] = np.asarray(opportunities.proceeds)

    lifecycle_fired = np.asarray(ys.lifecycle.fired)
    for position, event in enumerate(structure.folded_lifecycle):
        buffers.lifecycle.fired[event.event_index] = lifecycle_fired[event.month, position]
    primary_residence_fired = np.asarray(ys.primary_residence_fired)
    for position, (event_index, event_month) in enumerate(structure.folded_pr):
        buffers.primary_residence.fired[event_index] = primary_residence_fired[event_month, position]

    sale_traces = ys.lifecycle.property_sales
    sale_fields = (
        (buffers.lifecycle.sale_gross_proceeds, np.asarray(sale_traces.gross_proceeds)),
        (buffers.lifecycle.sale_mortgage_payoff, np.asarray(sale_traces.mortgage_payoff)),
        (buffers.lifecycle.sale_net_cash, np.asarray(sale_traces.net_cash)),
        (buffers.lifecycle.sale_realized_gain, np.asarray(sale_traces.realized_gain)),
        (buffers.lifecycle.sale_recapture, np.asarray(sale_traces.depreciation_recapture)),
        (buffers.lifecycle.sale_section_121_exclusion, np.asarray(sale_traces.section_121_exclusion)),
        (buffers.lifecycle.sale_long_term_gain, np.asarray(sale_traces.long_term_capital_gain)),
    )
    for position, (event_index, event_month) in enumerate(structure.folded_sale_events):
        for field, values in sale_fields:
            field[event_index] = values[event_month, position]


def _record_tax_liability_changes(buffers: SimulationBuffers, *, amount: np.ndarray, active: np.ndarray) -> None:
    """Reconstruct sparse tax-liability changes from named per-month snapshots."""

    if amount.shape[0] == 0:
        return
    previous_amount = np.zeros_like(amount[0])
    previous_active = np.zeros_like(active[0])
    for month in range(amount.shape[0]):
        changed = np.flatnonzero(
            (amount[month] != previous_amount).any(axis=1) | (active[month] != previous_active).any(axis=1)
        )
        if changed.size:
            buffers.tax_liability_changes.record(
                snapshot_month=month + 1, slots=changed, amount=amount[month], active=active[month]
            )
        previous_amount, previous_active = amount[month], active[month]

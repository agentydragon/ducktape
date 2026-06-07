"""Dense-array simulation engine."""

from __future__ import annotations

import numpy as np

from augur.sim.buffers import (
    DispositionGroup,
    LifecycleEventBuffers,
    LotDispositionEventBuffers,
    ObligationEventBuffers,
    PrimaryResidenceEventBuffers,
    PrivateEquityOpportunityEventBuffers,
    PropertyCashflowEventBuffers,
    PropertyEventBuffers,
    SimulationBuffers,
    StateHistoryBuffers,
    TaxEventBuffers,
    TaxLiabilityChangeLog,
    TransferEventBuffers,
)
from augur.sim.codec.plan import DenseSimulationResult
from augur.sim.compiler import CompiledSimulation, compile_simulation
from augur.sim.compiler.helpers import NO_CODE
from augur.sim.engine.jax_engine import run_jax_scan
from augur.sim.enums import PrivateEquityDispositionKind
from augur.sim.external_series import ExternalSeriesContext
from augur.sim.locations import Location
from augur.sim.runtime import load_jurisdictions_for
from augur.sim.scenario import Scenario


def run_dense_simulation(
    scenario: Scenario, *, rollout_count: int, external_series: ExternalSeriesContext, locations: dict[str, Location]
) -> DenseSimulationResult:
    plan = compile_simulation(
        scenario,
        rollout_count=rollout_count,
        external_series=external_series,
        jurisdictions=load_jurisdictions_for(scenario),
        locations=locations,
    )
    buffers = _allocate_buffers(plan)
    run_jax_scan(plan, buffers)
    return DenseSimulationResult(plan=plan, buffers=buffers, external_series=external_series)


def _allocate_buffers(plan: CompiledSimulation) -> SimulationBuffers:
    p = plan.slot_plan
    h = p.event_months
    s = p.snapshot_months
    r = p.rollout_count
    lot_axis = max(1, p.lot_count)
    liability_event_axis = max(1, p.liability_count)
    buffers = SimulationBuffers(
        state=StateHistoryBuffers(
            # State-history buffers keep the rollout axis last to match the JAX scan's output
            # layout (it emits per-(count, R) arrays): (snapshot, count, R) for 2-axis state,
            # (snapshot, count_a, count_b, R) for the 3-axis capital-gain split. Decoders move R
            # to axis 1 via `r_first_view` when they read these.
            # cash_state[S, C, R]
            cash_state=np.zeros((s, p.cash_count, r), dtype=np.int64),
            # lot_state[S, L, R]
            lot_state=np.zeros((s, p.lot_count, r), dtype=np.int64),
            # ordinary_state[S, P, R]
            ordinary_state=np.zeros((s, p.tax_profile_count, r), dtype=np.int64),
            # capital_gain_*_state[S, G, classification, R]
            capital_gain_active_state=np.zeros((s, p.capital_gain_agent_count, 2, r), dtype=np.bool_),
            capital_gain_state=np.zeros((s, p.capital_gain_agent_count, 2, r), dtype=np.int64),
            # tax_liability_*_state[S, T, R]
            # property_*_state[S, P, R]
            property_active_state=np.zeros((s, p.property_count, r), dtype=np.bool_),
            property_basis_state=np.zeros((s, p.property_count, r), dtype=np.int64),
            property_contribution_state=np.zeros((s, p.property_count, r), dtype=np.int64),
            property_equity_state=np.zeros((s, p.property_count, r), dtype=np.int64),
            # liability_*_state[S, B, R]
            liability_active_state=np.zeros((s, p.liability_count, r), dtype=np.bool_),
            liability_principal_state=np.zeros((s, p.liability_count, r), dtype=np.int64),
            liability_monthly_payment_state=np.zeros((s, p.liability_count, r), dtype=np.int64),
            liability_interest_ytd_state=np.zeros((s, p.liability_count, r), dtype=np.int64),
            liability_principal_ytd_state=np.zeros((s, p.liability_count, r), dtype=np.int64),
            # property_cumulative_depreciation_state[S, P, R]
            property_cumulative_depreciation_state=np.zeros((s, p.property_count, r), dtype=np.int64),
            # property_owner_occupied_months_state[S, P, R]
            property_owner_occupied_months_state=np.zeros((s, p.property_count, r), dtype=np.int64),
            # rollout failure state[S, R] (1D R retained on trailing axis)
            rollout_failed_state=np.zeros((s, r), dtype=np.bool_),
            rollout_failed_month_state=np.full((s, r), NO_CODE, dtype=np.int64),
        ),
        transfers=TransferEventBuffers(
            # transfer_*[H, T, R]
            active=np.zeros((h, p.max_transfer_slots, r), dtype=np.bool_),
            amount=np.zeros((h, p.max_transfer_slots, r), dtype=np.int64),
        ),
        property_cashflows=PropertyCashflowEventBuffers(
            active=np.zeros((h, p.max_property_cashflow_slots, r), dtype=np.bool_),
            amount=np.zeros((h, p.max_property_cashflow_slots, r), dtype=np.int64),
        ),
        properties=PropertyEventBuffers(
            # property_*_active[H, P, R]
            transfer_active=np.zeros((h, p.property_count, r), dtype=np.bool_),
            purchase_active=np.zeros((h, p.property_count, r), dtype=np.bool_),
            # mortgage_*[H, max(1, B), R]
            mortgage_origination_active=np.zeros((h, liability_event_axis, r), dtype=np.bool_),
            mortgage_payment_active=np.zeros((h, liability_event_axis, r), dtype=np.bool_),
            mortgage_payment_interest=np.zeros((h, liability_event_axis, r), dtype=np.int64),
            mortgage_payment_principal=np.zeros((h, liability_event_axis, r), dtype=np.int64),
            mortgage_payment_total=np.zeros((h, liability_event_axis, r), dtype=np.int64),
        ),
        lot_dispositions=LotDispositionEventBuffers(
            # scheduled disposition buffers[D, max(1, L), R] — each sale fires once, so the horizon axis
            # is collapsed; the firing month is recovered from `plan.sales.month` at decode time.
            scheduled=DispositionGroup(
                active=np.zeros((p.scheduled_sale_count, lot_axis, r), dtype=np.bool_),
                units=np.zeros((p.scheduled_sale_count, lot_axis, r), dtype=np.int64),
                basis=np.zeros((p.scheduled_sale_count, lot_axis, r), dtype=np.int64),
                proceeds=np.zeros((p.scheduled_sale_count, lot_axis, r), dtype=np.int64),
            ),
            # liquidity disposition buffers[H, Q, A, max(1, L), R]
            liquidity=DispositionGroup(
                active=np.zeros(
                    (h, p.liquidity_policy_count, p.max_liquidity_policy_assets, lot_axis, r), dtype=np.bool_
                ),
                units=np.zeros(
                    (h, p.liquidity_policy_count, p.max_liquidity_policy_assets, lot_axis, r), dtype=np.int64
                ),
                basis=np.zeros(
                    (h, p.liquidity_policy_count, p.max_liquidity_policy_assets, lot_axis, r), dtype=np.int64
                ),
                proceeds=np.zeros(
                    (h, p.liquidity_policy_count, p.max_liquidity_policy_assets, lot_axis, r), dtype=np.int64
                ),
            ),
            # PE disposition buffers[H, PE_issuer, PE_disposition_kind, max(1, L), R]
            pe=DispositionGroup(
                active=np.zeros((h, p.pe_issuer_count, len(PrivateEquityDispositionKind), lot_axis, r), dtype=np.bool_),
                units=np.zeros((h, p.pe_issuer_count, len(PrivateEquityDispositionKind), lot_axis, r), dtype=np.int64),
                basis=np.zeros((h, p.pe_issuer_count, len(PrivateEquityDispositionKind), lot_axis, r), dtype=np.int64),
                proceeds=np.zeros(
                    (h, p.pe_issuer_count, len(PrivateEquityDispositionKind), lot_axis, r), dtype=np.int64
                ),
            ),
        ),
        private_equity_opportunities=PrivateEquityOpportunityEventBuffers(
            active=np.zeros((h, p.pe_issuer_count, r), dtype=np.bool_),
            outcome=np.full((h, p.pe_issuer_count, r), NO_CODE, dtype=np.int64),
            floor=np.zeros((h, p.pe_issuer_count, r), dtype=np.int64),
            liquid_net_worth=np.zeros((h, p.pe_issuer_count, r), dtype=np.int64),
            shortfall=np.zeros((h, p.pe_issuer_count, r), dtype=np.int64),
            units_held=np.zeros((h, p.pe_issuer_count, r), dtype=np.int64),
            sellable_units=np.zeros((h, p.pe_issuer_count, r), dtype=np.int64),
            target_units=np.zeros((h, p.pe_issuer_count, r), dtype=np.int64),
            proceeds=np.zeros((h, p.pe_issuer_count, r), dtype=np.int64),
        ),
        taxes=TaxEventBuffers(
            # tax accrual/breakdown buffers[H, max(1, J), R]
            accrual_active=np.zeros((h, p.tax_link_count, r), dtype=np.bool_),
            accrual_amount=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_ordinary=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_ltcg=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_stcg=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_standard_deduction=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_mortgage_interest_deduction=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_salt_deduction=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_itemized_deduction=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_ordinary_taxable=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_capital_taxable=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_ordinary_tax=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            breakdown_capital_tax=np.zeros((h, p.tax_link_count, r), dtype=np.int64),
            # tax settlement buffers[H, max(1, tax_profile_count), R]
            settlement_active=np.zeros((h, p.max_tax_settlement_slots, r), dtype=np.bool_),
            settlement_amount=np.zeros((h, p.max_tax_settlement_slots, r), dtype=np.int64),
            settlement_year_end_month=np.full((h, p.max_tax_settlement_slots, r), NO_CODE, dtype=np.int64),
        ),
        obligations=ObligationEventBuffers(
            # obligation buffers[H, O, R]
            active=np.zeros((h, p.max_obligation_slots, r), dtype=np.bool_),
            due=np.zeros((h, p.max_obligation_slots, r), dtype=np.int64),
            paid=np.zeros((h, p.max_obligation_slots, r), dtype=np.int64),
            shortfall=np.zeros((h, p.max_obligation_slots, r), dtype=np.int64),
            attempt_policy=np.full((h, p.max_obligation_slots, r), NO_CODE, dtype=np.int64),
            failure_active=np.zeros((h, p.max_obligation_slots, r), dtype=np.bool_),
        ),
        primary_residence=PrimaryResidenceEventBuffers(
            fired=np.zeros((max(1, int(plan.primary_residence_events.month.shape[0])), r), dtype=np.bool_)
        ),
        lifecycle=LifecycleEventBuffers(
            fired=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.bool_),
            sale_gross_proceeds=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.int64),
            sale_mortgage_payoff=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.int64),
            sale_net_cash=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.int64),
            sale_realized_gain=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.int64),
            sale_recapture=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.int64),
            sale_section_121_exclusion=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.int64),
            sale_long_term_gain=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.int64),
        ),
        tax_liability_changes=TaxLiabilityChangeLog(),
    )
    buffers.validate(plan)
    return buffers

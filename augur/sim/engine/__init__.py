"""Dense-array simulation engine."""

from __future__ import annotations

import numpy as np

from augur.sim.buffers import (
    CurrentStateBuffers,
    DispositionGroup,
    LifecycleEventBuffers,
    LotDispositionEventBuffers,
    ObligationEventBuffers,
    PrimaryResidenceEventBuffers,
    PrivateEquityOpportunityEventBuffers,
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
from augur.sim.engine.phases import (
    _apply_depreciation_accrual,
    _apply_lifecycle_events,
    _apply_liquidity_policy_sales,
    _apply_obligation_accruals,
    _apply_obligation_settlement,
    _apply_owner_occupied_month,
    _apply_pe_tenders,
    _apply_primary_residence_events,
    _apply_property_purchases,
    _apply_scheduled_asset_sales,
    _apply_scheduled_transfers,
    _apply_tax_accruals,
)
from augur.sim.enums import PrivateEquityDispositionKind
from augur.sim.external_series import ExternalSeriesContext
from augur.sim.locations import Location
from augur.sim.runtime import load_jurisdictions_for
from augur.sim.scenario import Scenario


def simulate_with_external_series_dense_result(
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
    current = _allocate_current_state(plan)
    _snapshot_current_state(buffers.state, current, snapshot_index=0)
    for month in range(plan.horizon_months):
        _run_month_step(plan, buffers, current, month)
    return DenseSimulationResult(plan=plan, buffers=buffers, external_series=external_series)


def _allocate_current_state(plan: CompiledSimulation) -> CurrentStateBuffers:
    p = plan.slot_plan
    r = p.rollout_count
    # Per-rollout state is R-last so per-step broadcasts (`current.foo[slot, :] += amount`)
    # are contiguous and `current.foo[..., r]` is a contiguous per-rollout view.
    current = CurrentStateBuffers(
        cash=np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, r)).copy(),
        lot_remaining=np.broadcast_to(plan.lot_initial_quantity[:, None], (p.lot_count, r)).copy(),
        ordinary_ytd=np.zeros((p.tax_profile_count, r), dtype=np.float64),
        capital_gain_active=np.zeros((p.capital_gain_agent_count, 2, r), dtype=np.bool_),
        capital_gain_ytd=np.zeros((p.capital_gain_agent_count, 2, r), dtype=np.float64),
        capital_loss_carryforward=np.zeros((p.capital_gain_agent_count, r), dtype=np.float64),
        tax_liability_active=np.zeros((p.tax_liability_count, r), dtype=np.bool_),
        tax_liability_amount=np.zeros((p.tax_liability_count, r), dtype=np.float64),
        property_active=np.zeros((p.property_count, r), dtype=np.bool_),
        property_basis=np.zeros((p.property_count, r), dtype=np.float64),
        property_ownership=np.zeros((p.property_count, r), dtype=np.float64),
        property_contribution=np.zeros((p.property_count, r), dtype=np.float64),
        property_equity=np.zeros((p.property_count, r), dtype=np.float64),
        liability_active=np.zeros((p.liability_count, r), dtype=np.bool_),
        liability_principal=np.zeros((p.liability_count, r), dtype=np.float64),
        liability_monthly_payment=np.zeros((p.liability_count, r), dtype=np.float64),
        liability_interest_ytd=np.zeros((p.liability_count, r), dtype=np.float64),
        liability_principal_ytd=np.zeros((p.liability_count, r), dtype=np.float64),
        property_tax_ytd=np.zeros((p.tax_profile_count, r), dtype=np.float64),
        property_cumulative_depreciation=np.zeros((p.property_count, r), dtype=np.float64),
        property_depreciation_ytd=np.zeros((p.property_count, r), dtype=np.float64),
        # Broadcast the compile-time initial rented_fraction across rollouts. Lifecycle events
        # may then mutate per-(property, rollout) state at runtime.
        property_rented_fraction=np.broadcast_to(plan.property_rented_fraction[:, None], (p.property_count, r)).copy(),
        property_building_basis=np.broadcast_to(plan.property_building_basis[:, None], (p.property_count, r)).copy(),
        property_owner_occupied_months=np.zeros((p.property_count, r), dtype=np.int64),
        agent_primary_residence_property=plan.initial_primary_residence_property_index.copy(),
        recapture_section_1250_ytd=np.zeros((p.tax_profile_count, r), dtype=np.float64),
        liability_rental_interest_ytd=np.zeros((p.liability_count, r), dtype=np.float64),
        failed=np.zeros(r, dtype=np.bool_),
        failed_month=np.full(r, NO_CODE, dtype=np.int64),
    )
    current.validate(p)
    return current


def _snapshot_current_state(state: StateHistoryBuffers, current: CurrentStateBuffers, *, snapshot_index: int) -> None:
    # Both `current.*` and `state.*[s]` are R-last; no transpose needed.
    state.cash_state[snapshot_index] = current.cash
    state.lot_state[snapshot_index] = current.lot_remaining
    state.ordinary_state[snapshot_index] = current.ordinary_ytd
    state.capital_gain_active_state[snapshot_index] = current.capital_gain_active
    state.capital_gain_state[snapshot_index] = current.capital_gain_ytd
    # Tax liabilities are not snapshotted here: their per-month balance is piecewise-constant
    # and captured sparsely in buffers.tax_liability_changes (accrual + settlement events).
    state.property_active_state[snapshot_index] = current.property_active
    state.property_basis_state[snapshot_index] = current.property_basis
    state.property_ownership_state[snapshot_index] = current.property_ownership
    state.property_contribution_state[snapshot_index] = current.property_contribution
    state.property_equity_state[snapshot_index] = current.property_equity
    state.liability_active_state[snapshot_index] = current.liability_active
    state.liability_principal_state[snapshot_index] = current.liability_principal
    state.liability_monthly_payment_state[snapshot_index] = current.liability_monthly_payment
    state.liability_interest_ytd_state[snapshot_index] = current.liability_interest_ytd
    state.liability_principal_ytd_state[snapshot_index] = current.liability_principal_ytd
    state.property_cumulative_depreciation_state[snapshot_index] = current.property_cumulative_depreciation
    state.property_owner_occupied_months_state[snapshot_index] = current.property_owner_occupied_months
    state.rollout_failed_state[snapshot_index] = current.failed
    state.rollout_failed_month_state[snapshot_index] = current.failed_month


def _zero_failed_state(current: CurrentStateBuffers) -> None:
    failed = current.failed
    if not failed.any():
        return
    current.cash[:, failed] = 0.0
    current.lot_remaining[:, failed] = 0.0
    current.ordinary_ytd[:, failed] = 0.0
    current.capital_gain_ytd[:, :, failed] = 0.0
    current.capital_loss_carryforward[:, failed] = 0.0
    current.tax_liability_amount[:, failed] = 0.0
    current.property_basis[:, failed] = 0.0
    current.property_ownership[:, failed] = 0.0
    current.property_contribution[:, failed] = 0.0
    current.property_equity[:, failed] = 0.0
    current.liability_principal[:, failed] = 0.0
    current.liability_monthly_payment[:, failed] = 0.0
    current.liability_interest_ytd[:, failed] = 0.0
    current.liability_principal_ytd[:, failed] = 0.0


def _run_month_step(
    plan: CompiledSimulation, buffers: SimulationBuffers, current: CurrentStateBuffers, month: int
) -> None:
    _apply_primary_residence_events(plan, buffers, current, month)
    _apply_lifecycle_events(plan, buffers, current, month)
    _apply_scheduled_transfers(plan, buffers, current, month)
    _apply_property_purchases(plan, buffers, current, month)
    _apply_scheduled_asset_sales(plan, buffers, current, month)
    _apply_obligation_accruals(plan, buffers, current, month)
    _apply_liquidity_policy_sales(plan, buffers, current, month)
    _apply_obligation_settlement(plan, buffers, current, month)
    # PE tender sales fire after obligation settlement so the policy compares against the
    # post-settlement liquid net worth (cash already moved out for this month's bills) and the
    # cap-gain accrual from any tender is captured by the year-end tax pass below.
    _apply_pe_tenders(plan, buffers, current, month)
    # Primary-residence month counter: §121 24-of-60 window machinery. Increments before
    # depreciation accrual so same-month primary-residence/rental events are reflected.
    _apply_owner_occupied_month(plan, current)
    # §168 monthly depreciation accrual for rented properties; must run before tax accruals so
    # the year-end pass sees this month's contribution in property_depreciation_ytd.
    _apply_depreciation_accrual(plan, current)
    # Tax accruals run last so December's mortgage payment has already landed its interest into
    # `liability_interest_ytd` before the year-end MID computation reads it.
    _apply_tax_accruals(plan, buffers, current, month)
    _zero_failed_state(current)
    _snapshot_current_state(buffers.state, current, snapshot_index=month + 1)


def _allocate_buffers(plan: CompiledSimulation) -> SimulationBuffers:
    p = plan.slot_plan
    h = p.event_months
    s = p.snapshot_months
    r = p.rollout_count
    lot_axis = max(1, p.lot_count)
    liability_event_axis = max(1, p.liability_count)
    buffers = SimulationBuffers(
        state=StateHistoryBuffers(
            # All state-history buffers are R-last per B0: (snapshot, count, R) for 2-axis
            # state, (snapshot, count_a, count_b, R) for the 3-axis capital-gain split.
            # cash_state[S, C, R]
            cash_state=np.zeros((s, p.cash_count, r), dtype=np.float64),
            # lot_state[S, L, R]
            lot_state=np.zeros((s, p.lot_count, r), dtype=np.float64),
            # ordinary_state[S, P, R]
            ordinary_state=np.zeros((s, p.tax_profile_count, r), dtype=np.float64),
            # capital_gain_*_state[S, G, classification, R]
            capital_gain_active_state=np.zeros((s, p.capital_gain_agent_count, 2, r), dtype=np.bool_),
            capital_gain_state=np.zeros((s, p.capital_gain_agent_count, 2, r), dtype=np.float64),
            # tax_liability_*_state[S, T, R]
            # property_*_state[S, P, R]
            property_active_state=np.zeros((s, p.property_count, r), dtype=np.bool_),
            property_basis_state=np.zeros((s, p.property_count, r), dtype=np.float64),
            property_ownership_state=np.zeros((s, p.property_count, r), dtype=np.float64),
            property_contribution_state=np.zeros((s, p.property_count, r), dtype=np.float64),
            property_equity_state=np.zeros((s, p.property_count, r), dtype=np.float64),
            # liability_*_state[S, B, R]
            liability_active_state=np.zeros((s, p.liability_count, r), dtype=np.bool_),
            liability_principal_state=np.zeros((s, p.liability_count, r), dtype=np.float64),
            liability_monthly_payment_state=np.zeros((s, p.liability_count, r), dtype=np.float64),
            liability_interest_ytd_state=np.zeros((s, p.liability_count, r), dtype=np.float64),
            liability_principal_ytd_state=np.zeros((s, p.liability_count, r), dtype=np.float64),
            # property_cumulative_depreciation_state[S, P, R]
            property_cumulative_depreciation_state=np.zeros((s, p.property_count, r), dtype=np.float64),
            # property_owner_occupied_months_state[S, P, R]
            property_owner_occupied_months_state=np.zeros((s, p.property_count, r), dtype=np.int64),
            # rollout failure state[S, R] (1D R retained on trailing axis)
            rollout_failed_state=np.zeros((s, r), dtype=np.bool_),
            rollout_failed_month_state=np.full((s, r), NO_CODE, dtype=np.int64),
        ),
        transfers=TransferEventBuffers(
            # transfer_*[H, T, R]
            active=np.zeros((h, p.max_transfer_slots, r), dtype=np.bool_),
            amount=np.zeros((h, p.max_transfer_slots, r), dtype=np.float64),
        ),
        properties=PropertyEventBuffers(
            # property_*_active[H, P, R]
            transfer_active=np.zeros((h, p.property_count, r), dtype=np.bool_),
            purchase_active=np.zeros((h, p.property_count, r), dtype=np.bool_),
            # mortgage_*[H, max(1, B), R]
            mortgage_origination_active=np.zeros((h, liability_event_axis, r), dtype=np.bool_),
            mortgage_payment_active=np.zeros((h, liability_event_axis, r), dtype=np.bool_),
            mortgage_payment_interest=np.zeros((h, liability_event_axis, r), dtype=np.float64),
            mortgage_payment_principal=np.zeros((h, liability_event_axis, r), dtype=np.float64),
            mortgage_payment_total=np.zeros((h, liability_event_axis, r), dtype=np.float64),
        ),
        lot_dispositions=LotDispositionEventBuffers(
            # scheduled disposition buffers[H, D, max(1, L), R]
            scheduled=DispositionGroup(
                active=np.zeros((h, p.scheduled_sale_count, lot_axis, r), dtype=np.bool_),
                units=np.zeros((h, p.scheduled_sale_count, lot_axis, r), dtype=np.float64),
                basis=np.zeros((h, p.scheduled_sale_count, lot_axis, r), dtype=np.float64),
                proceeds=np.zeros((h, p.scheduled_sale_count, lot_axis, r), dtype=np.float64),
            ),
            # liquidity disposition buffers[H, Q, A, max(1, L), R]
            liquidity=DispositionGroup(
                active=np.zeros(
                    (h, p.liquidity_policy_count, p.max_liquidity_policy_assets, lot_axis, r), dtype=np.bool_
                ),
                units=np.zeros(
                    (h, p.liquidity_policy_count, p.max_liquidity_policy_assets, lot_axis, r), dtype=np.float64
                ),
                basis=np.zeros(
                    (h, p.liquidity_policy_count, p.max_liquidity_policy_assets, lot_axis, r), dtype=np.float64
                ),
                proceeds=np.zeros(
                    (h, p.liquidity_policy_count, p.max_liquidity_policy_assets, lot_axis, r), dtype=np.float64
                ),
            ),
            # PE disposition buffers[H, PE_issuer, PE_disposition_kind, max(1, L), R]
            pe=DispositionGroup(
                active=np.zeros((h, p.pe_issuer_count, len(PrivateEquityDispositionKind), lot_axis, r), dtype=np.bool_),
                units=np.zeros(
                    (h, p.pe_issuer_count, len(PrivateEquityDispositionKind), lot_axis, r), dtype=np.float64
                ),
                basis=np.zeros(
                    (h, p.pe_issuer_count, len(PrivateEquityDispositionKind), lot_axis, r), dtype=np.float64
                ),
                proceeds=np.zeros(
                    (h, p.pe_issuer_count, len(PrivateEquityDispositionKind), lot_axis, r), dtype=np.float64
                ),
            ),
        ),
        private_equity_opportunities=PrivateEquityOpportunityEventBuffers(
            active=np.zeros((h, p.pe_issuer_count, r), dtype=np.bool_),
            outcome=np.full((h, p.pe_issuer_count, r), NO_CODE, dtype=np.int64),
            floor=np.zeros((h, p.pe_issuer_count, r), dtype=np.float64),
            liquid_net_worth=np.zeros((h, p.pe_issuer_count, r), dtype=np.float64),
            shortfall=np.zeros((h, p.pe_issuer_count, r), dtype=np.float64),
            units_held=np.zeros((h, p.pe_issuer_count, r), dtype=np.float64),
            sellable_units=np.zeros((h, p.pe_issuer_count, r), dtype=np.float64),
            target_units=np.zeros((h, p.pe_issuer_count, r), dtype=np.float64),
            proceeds=np.zeros((h, p.pe_issuer_count, r), dtype=np.float64),
        ),
        taxes=TaxEventBuffers(
            # tax accrual/breakdown buffers[H, max(1, J), R]
            accrual_active=np.zeros((h, p.tax_link_count, r), dtype=np.bool_),
            accrual_amount=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_ordinary=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_ltcg=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_stcg=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_standard_deduction=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_mortgage_interest_deduction=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_salt_deduction=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_itemized_deduction=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_ordinary_taxable=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_capital_taxable=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_ordinary_tax=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            breakdown_capital_tax=np.zeros((h, p.tax_link_count, r), dtype=np.float64),
            # tax settlement buffers[H, max(1, tax_profile_count), R]
            settlement_active=np.zeros((h, p.max_tax_settlement_slots, r), dtype=np.bool_),
            settlement_amount=np.zeros((h, p.max_tax_settlement_slots, r), dtype=np.float64),
            settlement_year_end_month=np.full((h, p.max_tax_settlement_slots, r), NO_CODE, dtype=np.int64),
        ),
        obligations=ObligationEventBuffers(
            # obligation buffers[H, O, R]
            active=np.zeros((h, p.max_obligation_slots, r), dtype=np.bool_),
            due=np.zeros((h, p.max_obligation_slots, r), dtype=np.float64),
            paid=np.zeros((h, p.max_obligation_slots, r), dtype=np.float64),
            shortfall=np.zeros((h, p.max_obligation_slots, r), dtype=np.float64),
            attempt_policy=np.full((h, p.max_obligation_slots, r), NO_CODE, dtype=np.int64),
            failure_active=np.zeros((h, p.max_obligation_slots, r), dtype=np.bool_),
        ),
        primary_residence=PrimaryResidenceEventBuffers(
            fired=np.zeros((max(1, int(plan.primary_residence_events.month.shape[0])), r), dtype=np.bool_)
        ),
        lifecycle=LifecycleEventBuffers(
            fired=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.bool_),
            sale_gross_proceeds=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.float64),
            sale_mortgage_payoff=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.float64),
            sale_net_cash=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.float64),
            sale_realized_gain=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.float64),
            sale_recapture=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.float64),
            sale_section_121_exclusion=np.zeros(
                (max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.float64
            ),
            sale_long_term_gain=np.zeros((max(1, int(plan.lifecycle_events.month.shape[0])), r), dtype=np.float64),
        ),
        tax_liability_changes=TaxLiabilityChangeLog(),
    )
    buffers.validate(plan)
    return buffers

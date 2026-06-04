"""JAX simulation engine — in-progress parity port of the NumPy engine.

The dense engine is being ported to JAX phase-by-phase (see <augur/plans/jax_migration.md>). Each
phase is a functional `jnp.at[]` translation of its NumPy counterpart in `phases.py`. Parity is
verified by running the existing simulator test suite under both backends (the autouse `backend`
fixture in `augur/sim/conftest.py` parameterizes every test over NumPy and JAX); the JAX variants
for scenarios touching not-yet-ported phases fail until the port lands them. Selection is via
`sim_backend.current_backend()`.

`run_jax(plan, buffers)` fills the (already NumPy-allocated, zeroed) `buffers` from a JAX run.
Un-ported phases / branches are no-ops, so the JAX backend is correct only for scenarios that
exercise only the ported paths — which is exactly what the passing parity tests use.

Ported so far (in `_run_month_step` order):
- scheduled / recurring transfers;
- property purchases (cash + mortgage origination);
- scheduled asset sales (FIFO lot matching + capital-gain classification + lot-disposition log);
- liquidity-policy sales;
- obligation accruals + settlement with failure tracking and `_zero_failed_state`, for the
  CONFIGURED_OBLIGATION, PROPERTY_TAX, and MORTGAGE_PAYMENT source kinds (incl. the mortgage
  interest/principal split into liability state).

Not yet ported (no-op): property sale, depreciation, owner-occupied months, PE tenders, lifecycle,
primary residence, the estimated-tax obligation source kinds, and the year-end tax machinery
(accrual, SALT, MID, LTCG brackets, settlements) — so the property-tax/mortgage SALT/Schedule-E and
MID deduction splits are also deferred until that lands.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import jax.numpy as jnp
import numpy as np

from augur.sim.buffers import SimulationBuffers
from augur.sim.codec.plan import CompiledSimulation
from augur.sim.compiler.helpers import AMOUNT_FIXED
from augur.sim.compiler.obligations import ObligationCompileOutput
from augur.sim.compiler.transfers import TransferCompileOutput
from augur.sim.enums import CapitalGainClassification, ObligationSource
from augur.sim.tensor_fifo import lot_order_for_pool


@dataclass(frozen=True)
class LiabilityState:
    """Per-(liability, rollout) mortgage state threaded through the month loop (all R-last `[L, R]`).

    `rental_interest_ytd` is the rented-share slice of `interest_ytd` (Schedule E vs MID split); it
    stays at the NumPy reference's behavior of not being zeroed on rollout failure.
    """

    active: jnp.ndarray
    principal: jnp.ndarray
    monthly_payment: jnp.ndarray
    interest_ytd: jnp.ndarray
    principal_ytd: jnp.ndarray
    rental_interest_ytd: jnp.ndarray


def run_jax(plan: CompiledSimulation, buffers: SimulationBuffers) -> None:
    p = plan.slot_plan
    r = p.rollout_count

    cash = jnp.asarray(np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, r)))
    lot_remaining = jnp.asarray(np.broadcast_to(plan.lot_initial_quantity[:, None], (p.lot_count, r)))
    ordinary_ytd = jnp.zeros((p.tax_profile_count, r))
    capital_gain_active = jnp.zeros((p.capital_gain_agent_count, 2, r), dtype=bool)
    capital_gain_ytd = jnp.zeros((p.capital_gain_agent_count, 2, r))
    property_active = jnp.zeros((p.property_count, r), dtype=bool)
    property_basis = jnp.zeros((p.property_count, r))
    property_ownership = jnp.zeros((p.property_count, r))
    property_contribution = jnp.zeros((p.property_count, r))
    property_equity = jnp.zeros((p.property_count, r))
    property_rented_fraction = jnp.asarray(
        np.broadcast_to(plan.property_rented_fraction[:, None], (p.property_count, r))
    )
    liabilities = LiabilityState(
        active=jnp.zeros((p.liability_count, r), dtype=bool),
        principal=jnp.zeros((p.liability_count, r)),
        monthly_payment=jnp.zeros((p.liability_count, r)),
        interest_ytd=jnp.zeros((p.liability_count, r)),
        principal_ytd=jnp.zeros((p.liability_count, r)),
        rental_interest_ytd=jnp.zeros((p.liability_count, r)),
    )
    failed = jnp.zeros(r, dtype=bool)
    # int32 (not int64): x64 is disabled, so a jnp.int64 request truncates with a warning. Values
    # are tiny (month index or -1); the NumPy int64 state buffer upcasts on assignment.
    failed_month = jnp.full(r, -1, dtype=jnp.int32)
    external_values = jnp.asarray(plan.external_values)

    # Snapshot index 0 is the pre-month-0 opening state (initial cash + lots; all else zero, already
    # set by `_allocate_buffers`; `rollout_failed_month_state` already NO_CODE).
    buffers.state.cash_state[0] = np.asarray(cash)
    buffers.state.lot_state[0] = np.asarray(lot_remaining)

    for month in range(plan.horizon_months):
        active = ~failed

        cash, ordinary_ytd, transfer_active, transfer_amount = _apply_scheduled_transfers(
            plan.transfers, cash, ordinary_ytd, active, external_values, month, r
        )
        buffers.transfers.active[month] = np.asarray(transfer_active)
        buffers.transfers.amount[month] = np.asarray(transfer_amount)

        (
            cash,
            property_active,
            property_basis,
            property_ownership,
            property_contribution,
            property_equity,
            liabilities,
        ) = _apply_property_purchases(
            plan,
            buffers,
            cash,
            property_active,
            property_basis,
            property_ownership,
            property_contribution,
            property_equity,
            liabilities,
            active,
            month,
        )

        cash, lot_remaining, capital_gain_active, capital_gain_ytd = _apply_scheduled_asset_sales(
            plan, buffers, cash, lot_remaining, capital_gain_active, capital_gain_ytd, active, external_values, month
        )

        ob_active, ob_due = _apply_obligation_accruals(
            plan, property_active, liabilities, active, external_values, month, r
        )
        cash, lot_remaining, capital_gain_active, capital_gain_ytd = _apply_liquidity_policy_sales(
            plan,
            buffers,
            cash,
            lot_remaining,
            capital_gain_active,
            capital_gain_ytd,
            ob_active,
            ob_due,
            active,
            external_values,
            month,
            r,
        )
        funded = _obligation_group_funded(plan.obligations, cash, ob_active, ob_due, month, r)
        cash, ordinary_ytd, liabilities, failed, failed_month, ob_paid, ob_shortfall, ob_failure = (
            _apply_obligation_settlement(
                plan,
                buffers,
                cash,
                ordinary_ytd,
                liabilities,
                property_rented_fraction,
                failed,
                failed_month,
                ob_active,
                ob_due,
                funded,
                month,
            )
        )
        buffers.obligations.active[month] = np.asarray(ob_active)
        buffers.obligations.due[month] = np.asarray(ob_due)
        buffers.obligations.paid[month] = np.asarray(ob_paid)
        buffers.obligations.shortfall[month] = np.asarray(ob_shortfall)
        buffers.obligations.failure_active[month] = np.asarray(ob_failure)

        keep = ~failed
        cash, lot_remaining, ordinary_ytd, capital_gain_ytd = (
            cash * keep,
            lot_remaining * keep,
            ordinary_ytd * keep,
            capital_gain_ytd * keep[None, None, :],
        )
        # `_zero_failed_state` zeros property dollar fields (but not property_active) for failed rollouts.
        property_basis, property_ownership, property_contribution, property_equity = (
            property_basis * keep,
            property_ownership * keep,
            property_contribution * keep,
            property_equity * keep,
        )
        # It also zeros liability dollar fields (principal/payment/interest_ytd/principal_ytd) but
        # leaves `active` and `rental_interest_ytd` untouched.
        liabilities = replace(
            liabilities,
            principal=liabilities.principal * keep,
            monthly_payment=liabilities.monthly_payment * keep,
            interest_ytd=liabilities.interest_ytd * keep,
            principal_ytd=liabilities.principal_ytd * keep,
        )

        buffers.state.cash_state[month + 1] = np.asarray(cash)
        buffers.state.lot_state[month + 1] = np.asarray(lot_remaining)
        buffers.state.ordinary_state[month + 1] = np.asarray(ordinary_ytd)
        buffers.state.capital_gain_active_state[month + 1] = np.asarray(capital_gain_active)
        buffers.state.capital_gain_state[month + 1] = np.asarray(capital_gain_ytd)
        buffers.state.property_active_state[month + 1] = np.asarray(property_active)
        buffers.state.property_basis_state[month + 1] = np.asarray(property_basis)
        buffers.state.property_ownership_state[month + 1] = np.asarray(property_ownership)
        buffers.state.property_contribution_state[month + 1] = np.asarray(property_contribution)
        buffers.state.property_equity_state[month + 1] = np.asarray(property_equity)
        buffers.state.liability_active_state[month + 1] = np.asarray(liabilities.active)
        buffers.state.liability_principal_state[month + 1] = np.asarray(liabilities.principal)
        buffers.state.liability_monthly_payment_state[month + 1] = np.asarray(liabilities.monthly_payment)
        buffers.state.liability_interest_ytd_state[month + 1] = np.asarray(liabilities.interest_ytd)
        buffers.state.liability_principal_ytd_state[month + 1] = np.asarray(liabilities.principal_ytd)
        buffers.state.rollout_failed_state[month + 1] = np.asarray(failed)
        buffers.state.rollout_failed_month_state[month + 1] = np.asarray(failed_month)


def _amount_values(
    *,
    amount_kind: int,
    amount_fixed: float,
    amount_base: float,
    amount_series: int,
    amount_base_month: int,
    amount_period: int,
    external_values: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> jnp.ndarray:
    """Port of `phases._amount_values`: a fixed or series-indexed per-rollout amount."""
    if amount_kind == AMOUNT_FIXED:
        return jnp.full(rollout_count, amount_fixed)
    reset_month = amount_base_month + ((month - amount_base_month) // amount_period) * amount_period
    base_level = external_values[amount_series, :, amount_base_month]
    reset_level = external_values[amount_series, :, reset_month]
    return amount_base * reset_level / base_level


def _apply_scheduled_transfers(
    transfers: TransferCompileOutput,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Functional port of `phases._apply_scheduled_transfers`. `active = ~failed` masks every write."""
    slot_count = transfers.cause.shape[1]
    transfer_active = jnp.zeros((slot_count, rollout_count), dtype=bool)
    transfer_amount = jnp.zeros((slot_count, rollout_count))
    for slot in range(slot_count):
        if int(transfers.cause[month, slot]) < 0:
            continue
        amount = jnp.where(
            active,
            _amount_values(
                amount_kind=int(transfers.amount_kind[month, slot]),
                amount_fixed=float(transfers.amount_fixed[month, slot]),
                amount_base=float(transfers.amount_base[month, slot]),
                amount_series=int(transfers.amount_series[month, slot]),
                amount_base_month=int(transfers.amount_base_month[month, slot]),
                amount_period=int(transfers.amount_period[month, slot]),
                external_values=external_values,
                month=month,
                rollout_count=rollout_count,
            ),
            0.0,
        )
        transfer_active = transfer_active.at[slot].set(active)
        transfer_amount = transfer_amount.at[slot].set(amount)
        from_slot = int(transfers.from_slot[month, slot])
        if from_slot >= 0:
            cash = cash.at[from_slot].add(-amount)
        to_slot = int(transfers.to_slot[month, slot])
        if to_slot >= 0:
            cash = cash.at[to_slot].add(amount)
        income_profile = int(transfers.income_profile[month, slot])
        if income_profile >= 0:
            ordinary_ytd = ordinary_ytd.at[income_profile].add(amount)
        deduction_profile = int(transfers.deduction_profile[month, slot])
        if deduction_profile >= 0:
            ordinary_ytd = ordinary_ytd.at[deduction_profile].add(-amount)
    return cash, ordinary_ytd, transfer_active, transfer_amount


def _apply_scheduled_asset_sales(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    cash: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    capital_gain_active: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Functional port of `phases._apply_scheduled_asset_sales` (FIFO sell + capital gains)."""
    sales = plan.sales
    cost_basis_per_unit = jnp.asarray(plan.lot_cost_basis_per_unit)
    for sale in range(sales.month.shape[0]):
        if int(sales.month[sale]) != month:
            continue
        # `lot_order_for_pool` is host-side (plan data only) — the FIFO order is a static index array.
        ordered_lots = lot_order_for_pool(
            lot_agent_codes=plan.lot_agent_codes,
            lot_account_codes=plan.lot_account_codes,
            lot_asset_codes=plan.lot_asset_codes,
            lot_purchase_month=plan.lot_purchase_month,
            lot_id_codes=plan.lot_id_codes,
            agent_code=int(sales.agent[sale]),
            account_code=int(sales.source_account[sale]),
            asset_code=int(sales.asset[sale]),
        )
        target_units = jnp.where(active, float(sales.quantity[sale]), 0.0)
        unit_price = _sale_unit_price(sales, external_values, month, sale, lot_remaining.shape[1])
        sold_units, proceeds, basis, oversell = _fifo_sell_units(
            lot_remaining.T, ordered_lots, target_units, unit_price, cost_basis_per_unit
        )
        if bool(oversell.any()):
            raise ValueError("scheduled asset sale exceeds available lots")

        lot_remaining = lot_remaining - sold_units.T
        proceeds_slot = int(sales.proceeds_slot[sale])
        if proceeds_slot >= 0:
            cash = cash.at[proceeds_slot].add(proceeds.sum(axis=1))
        capital_gain_active, capital_gain_ytd = _record_capital_gains(
            plan, capital_gain_active, capital_gain_ytd, month, int(sales.agent[sale]), sold_units, proceeds - basis
        )
        buffers.lot_dispositions.scheduled.active[month, sale] = np.asarray((sold_units > 0.0).T)
        buffers.lot_dispositions.scheduled.units[month, sale] += np.asarray(sold_units.T)
        buffers.lot_dispositions.scheduled.basis[month, sale] += np.asarray(basis.T)
        buffers.lot_dispositions.scheduled.proceeds[month, sale] += np.asarray(proceeds.T)
    return cash, lot_remaining, capital_gain_active, capital_gain_ytd


def _sale_unit_price(sales, external_values: jnp.ndarray, month: int, sale: int, rollout_count: int) -> jnp.ndarray:
    fixed_price = float(sales.price_fixed[sale])
    if not np.isnan(fixed_price):
        return jnp.full(rollout_count, fixed_price)
    return external_values[int(sales.price_series[sale]), :, month]


def _fifo_sell_units(
    lot_remaining: jnp.ndarray,
    ordered_lots: np.ndarray,
    target_units: jnp.ndarray,
    unit_price: jnp.ndarray,
    cost_basis_per_unit: jnp.ndarray,
    epsilon: float = 1e-9,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `tensor_fifo.fifo_sell_units`: vectorized cumulative-sum FIFO over `[R, L]` lots."""
    ordered_quantity = lot_remaining[:, ordered_lots]
    available_units = ordered_quantity.sum(axis=1)
    oversell = target_units > available_units + epsilon
    effective_target = jnp.where(oversell, 0.0, target_units)
    before_units = jnp.cumsum(ordered_quantity, axis=1) - ordered_quantity
    sold_ordered = jnp.clip(effective_target[:, None] - before_units, 0.0, ordered_quantity)
    proceeds_ordered = sold_ordered * unit_price[:, None]
    basis_ordered = sold_ordered * cost_basis_per_unit[ordered_lots][None, :]
    zeros = jnp.zeros_like(lot_remaining)
    sold_units = zeros.at[:, ordered_lots].set(sold_ordered)
    proceeds = zeros.at[:, ordered_lots].set(proceeds_ordered)
    basis = zeros.at[:, ordered_lots].set(basis_ordered)
    return sold_units, proceeds, basis, oversell


def _record_capital_gains(
    plan: CompiledSimulation,
    capital_gain_active: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    month: int,
    agent_code: int,
    sold_units: jnp.ndarray,
    gains: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Port of `phases._record_capital_gains`: classify each lot's gain long/short and accrue."""
    for profile in range(plan.capital_gain_agent_codes.shape[0]):
        if int(plan.capital_gain_agent_codes[profile]) != agent_code:
            continue
        for lot in range(plan.lot_id_codes.shape[0]):
            classification = (
                int(CapitalGainClassification.LONG_TERM)
                if month - int(plan.lot_purchase_month[lot]) >= 12
                else int(CapitalGainClassification.SHORT_TERM)
            )
            became_active = sold_units[:, lot] > 0.0
            capital_gain_active = capital_gain_active.at[profile, classification].set(
                capital_gain_active[profile, classification] | became_active
            )
            capital_gain_ytd = capital_gain_ytd.at[profile, classification].add(gains[:, lot])
    return capital_gain_active, capital_gain_ytd


def _apply_obligation_accruals(
    plan: CompiledSimulation,
    property_active: jnp.ndarray,
    liabilities: LiabilityState,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Functional port of `phases._apply_obligation_accruals` (CONFIGURED + PROPERTY_TAX + MORTGAGE_PAYMENT)."""
    obligations = plan.obligations
    slot_count = obligations.cause.shape[1]
    accrual_active = jnp.zeros((slot_count, rollout_count), dtype=bool)
    accrual_due = jnp.zeros((slot_count, rollout_count))
    for slot in range(slot_count):
        if int(obligations.cause[month, slot]) < 0 or int(obligations.source_kind[month, slot]) < 0:
            continue
        source_kind = int(obligations.source_kind[month, slot])
        if source_kind == ObligationSource.CONFIGURED_OBLIGATION:
            amount = _amount_values(
                amount_kind=int(obligations.amount_kind[month, slot]),
                amount_fixed=float(obligations.amount_fixed[month, slot]),
                amount_base=float(obligations.amount_base[month, slot]),
                amount_series=int(obligations.amount_series[month, slot]),
                amount_base_month=int(obligations.amount_base_month[month, slot]),
                amount_period=int(obligations.amount_period[month, slot]),
                external_values=external_values,
                month=month,
                rollout_count=rollout_count,
            )
            slot_active = active & (amount > 0.0)
        elif source_kind == ObligationSource.PROPERTY_TAX:
            prop = int(obligations.source_index[month, slot])
            if int(plan.properties.month[prop]) >= month:
                continue  # property tax accrues only after the purchase month
            rate = float(obligations.amount_fixed[month, slot])
            if np.isnan(rate):
                rate = float(plan.properties.location_tax_rate[prop])
            ad_valorem_monthly = float(plan.properties.initial_assessed_value[prop]) * rate / 12.0
            non_ad_valorem_monthly = float(plan.properties.special_assessment_annual_usd[prop]) / 12.0
            amount = jnp.full(rollout_count, ad_valorem_monthly + non_ad_valorem_monthly)
            slot_active = active & property_active[prop] & (amount > 0.0)
        elif source_kind == ObligationSource.MORTGAGE_PAYMENT:
            liab = int(obligations.source_index[month, slot])
            prop = int(plan.liabilities.property_slot[liab])
            if int(plan.properties.month[prop]) >= month:
                continue  # mortgage payments accrue only after the purchase month
            interest = liabilities.principal[liab] * float(plan.liabilities.annual_rate[liab]) / 12.0
            amount = jnp.minimum(liabilities.monthly_payment[liab], liabilities.principal[liab] + interest)
            slot_active = active & liabilities.active[liab] & (liabilities.principal[liab] > 0.0) & (amount > 0.0)
        else:
            continue  # estimated-tax source kinds not yet ported
        accrual_active = accrual_active.at[slot].set(slot_active)
        accrual_due = accrual_due.at[slot].set(jnp.where(slot_active, amount, 0.0))
    return accrual_active, accrual_due


def _obligation_group_funded(
    obligations: ObligationCompileOutput,
    cash: jnp.ndarray,
    accrual_active: jnp.ndarray,
    accrual_due: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> jnp.ndarray:
    """Port of `phases._obligation_group_funded`: an account funds all its obligations or none."""
    slot_count = accrual_active.shape[0]
    agent_row = obligations.agent[month]
    from_row = obligations.from_slot[month]
    funded = jnp.zeros((slot_count, rollout_count), dtype=bool)
    for slot in range(slot_count):
        from_slot = int(from_row[slot])
        group = jnp.asarray((agent_row == int(agent_row[slot])) & (from_row == from_slot))
        group_due = jnp.where(group[:, None] & accrual_active, accrual_due, 0.0).sum(axis=0)
        available = cash[from_slot] if from_slot >= 0 else jnp.zeros(rollout_count)
        funded = funded.at[slot].set(accrual_active[slot] & (available >= group_due - 1e-9))
    return funded


def _apply_obligation_settlement(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    liabilities: LiabilityState,
    property_rented_fraction: jnp.ndarray,
    failed: jnp.ndarray,
    failed_month: jnp.ndarray,
    accrual_active: jnp.ndarray,
    accrual_due: jnp.ndarray,
    funded: jnp.ndarray,
    month: int,
) -> tuple[jnp.ndarray, jnp.ndarray, LiabilityState, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Functional port of `phases._apply_obligation_settlement` (CONFIGURED + PROPERTY_TAX + MORTGAGE_PAYMENT).

    The deduction path uses each obligation's compile-time `deductible_fraction`; the property-tax
    SALT/Schedule-E split (runtime `property_rented_fraction`) lands with the tax-machinery port.
    """
    obligations = plan.obligations
    slot_count = accrual_active.shape[0]
    paid_buffer = jnp.zeros_like(accrual_due)
    shortfall_buffer = jnp.zeros_like(accrual_due)
    failure_active = jnp.zeros_like(accrual_active)
    for slot in range(slot_count):
        source_kind = int(obligations.source_kind[month, slot])
        if source_kind not in (
            ObligationSource.CONFIGURED_OBLIGATION,
            ObligationSource.PROPERTY_TAX,
            ObligationSource.MORTGAGE_PAYMENT,
        ):
            continue
        amount = accrual_due[slot]
        paid = accrual_active[slot] & funded[slot]
        paid_buffer = paid_buffer.at[slot].set(jnp.where(paid, amount, 0.0))
        from_slot = int(obligations.from_slot[month, slot])
        if from_slot >= 0:
            cash = cash.at[from_slot].add(jnp.where(paid, -amount, 0.0))
        to_slot = int(obligations.to_slot[month, slot])
        if to_slot >= 0:
            cash = cash.at[to_slot].add(jnp.where(paid, amount, 0.0))
        if source_kind == ObligationSource.MORTGAGE_PAYMENT:
            liabilities = _apply_mortgage_payment(
                plan,
                buffers,
                liabilities,
                property_rented_fraction,
                month=month,
                liability_slot=int(obligations.source_index[month, slot]),
                paid=paid,
                amount=amount,
            )
        # CONFIGURED obligations carry a compile-time deductible_fraction (no property tie).
        deduction_profile = int(obligations.deduction_profile[month, slot])
        if deduction_profile >= 0:
            deductible_fraction = float(obligations.deductible_fraction[month, slot])
            ordinary_ytd = ordinary_ytd.at[deduction_profile].add(jnp.where(paid, -amount * deductible_fraction, 0.0))

        slot_failed = accrual_active[slot] & ~funded[slot]
        shortfall_buffer = shortfall_buffer.at[slot].set(jnp.where(slot_failed, amount, 0.0))
        failure_active = failure_active.at[slot].set(slot_failed)
        first_failure = slot_failed & (failed_month < 0)
        failed_month = jnp.where(first_failure, month, failed_month)
        failed = failed | slot_failed
    return cash, ordinary_ytd, liabilities, failed, failed_month, paid_buffer, shortfall_buffer, failure_active


def _apply_mortgage_payment(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    liabilities: LiabilityState,
    property_rented_fraction: jnp.ndarray,
    *,
    month: int,
    liability_slot: int,
    paid: jnp.ndarray,
    amount: jnp.ndarray,
) -> LiabilityState:
    """Port of `phases._apply_mortgage_payment`: split a paid mortgage bill into interest/principal."""
    principal_before = liabilities.principal[liability_slot]
    interest = jnp.minimum(principal_before * float(plan.liabilities.annual_rate[liability_slot]) / 12.0, amount)
    principal = jnp.minimum(jnp.maximum(amount - interest, 0.0), principal_before)

    buffers.properties.mortgage_payment_active[month, liability_slot] = np.asarray(paid)
    buffers.properties.mortgage_payment_interest[month, liability_slot] = np.asarray(jnp.where(paid, interest, 0.0))
    buffers.properties.mortgage_payment_principal[month, liability_slot] = np.asarray(jnp.where(paid, principal, 0.0))
    buffers.properties.mortgage_payment_total[month, liability_slot] = np.asarray(jnp.where(paid, amount, 0.0))

    new_principal = jnp.where(paid, jnp.maximum(0.0, principal_before - principal), principal_before)
    interest_ytd = liabilities.interest_ytd[liability_slot] + jnp.where(paid, interest, 0.0)
    principal_ytd = liabilities.principal_ytd[liability_slot] + jnp.where(paid, principal, 0.0)
    rental_interest_ytd = liabilities.rental_interest_ytd[liability_slot]
    prop_slot = int(plan.liabilities.property_slot[liability_slot])
    if prop_slot >= 0:
        rented = property_rented_fraction[prop_slot]
        rental_interest_ytd = rental_interest_ytd + jnp.where(paid, interest * rented, 0.0)
    return replace(
        liabilities,
        principal=liabilities.principal.at[liability_slot].set(new_principal),
        interest_ytd=liabilities.interest_ytd.at[liability_slot].set(interest_ytd),
        principal_ytd=liabilities.principal_ytd.at[liability_slot].set(principal_ytd),
        rental_interest_ytd=liabilities.rental_interest_ytd.at[liability_slot].set(rental_interest_ytd),
    )


def _fifo_sell_dollars(
    lot_remaining: jnp.ndarray,
    ordered_lots: np.ndarray,
    target_dollars: jnp.ndarray,
    unit_price: jnp.ndarray,
    cost_basis_per_unit: jnp.ndarray,
    epsilon: float = 1e-9,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `tensor_fifo.fifo_sell_dollars`: FIFO sell a dollar target, ceiling-rounding units."""
    ordered_quantity = lot_remaining[:, ordered_lots]
    available_value = ordered_quantity * unit_price[:, None]
    oversell = target_dollars > available_value.sum(axis=1) + epsilon
    effective_target = jnp.where(oversell, 0.0, target_dollars)
    before_value = jnp.cumsum(available_value, axis=1) - available_value
    sold_value_ordered = jnp.clip(effective_target[:, None] - before_value, 0.0, available_value)
    price_col = unit_price[:, None]
    sold_units_ordered = jnp.clip(
        jnp.ceil(jnp.where(price_col > 0.0, sold_value_ordered / jnp.where(price_col > 0.0, price_col, 1.0), 0.0)),
        0.0,
        ordered_quantity,
    )
    proceeds_ordered = sold_units_ordered * price_col
    basis_ordered = sold_units_ordered * cost_basis_per_unit[ordered_lots][None, :]
    zeros = jnp.zeros_like(lot_remaining)
    sold_units = zeros.at[:, ordered_lots].set(sold_units_ordered)
    proceeds = zeros.at[:, ordered_lots].set(proceeds_ordered)
    basis = zeros.at[:, ordered_lots].set(basis_ordered)
    return sold_units, proceeds, basis, oversell


def _apply_liquidity_policy_sales(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    cash: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    capital_gain_active: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    obligation_active: jnp.ndarray,
    obligation_due: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Functional port of `phases._apply_liquidity_policy_sales`: sell assets FIFO to fund cash needs."""
    policies = plan.liquidity_policies
    cost_basis_per_unit = jnp.asarray(plan.lot_cost_basis_per_unit)
    for policy in range(policies.agent.shape[0]):
        policy_agent = int(policies.agent[policy])
        policy_cash_slot = int(policies.cash_slot[policy])

        matching_obligations = np.flatnonzero(
            (plan.obligations.agent[month] == policy_agent) & (plan.obligations.from_slot[month] == policy_cash_slot)
        )
        if matching_obligations.size:
            matching_active = obligation_active[matching_obligations]
            hard_demand = jnp.where(matching_active, obligation_due[matching_obligations], 0.0).sum(axis=0)
            for row, slot in enumerate(matching_obligations):
                prior = buffers.obligations.attempt_policy[month, slot]
                buffers.obligations.attempt_policy[month, slot] = np.where(
                    np.asarray(matching_active[row]), policy, prior
                )
        else:
            hard_demand = jnp.zeros(rollout_count)

        cash_balance = cash[policy_cash_slot] if policy_cash_slot >= 0 else jnp.zeros(rollout_count)
        required_sale = jnp.maximum(hard_demand - cash_balance, 0.0)
        post_required_cash = cash_balance + required_sale - hard_demand
        buffer_trigger_values = _amount_values(
            amount_kind=int(policies.trigger_kind[policy]),
            amount_fixed=float(policies.trigger_fixed[policy]),
            amount_base=float(policies.trigger_base[policy]),
            amount_series=int(policies.trigger_series[policy]),
            amount_base_month=int(policies.trigger_base_month[policy]),
            amount_period=int(policies.trigger_period[policy]),
            external_values=external_values,
            month=month,
            rollout_count=rollout_count,
        )
        buffer_sale_values = _amount_values(
            amount_kind=int(policies.sale_kind[policy]),
            amount_fixed=float(policies.sale_fixed[policy]),
            amount_base=float(policies.sale_base[policy]),
            amount_series=int(policies.sale_series[policy]),
            amount_base_month=int(policies.sale_base_month[policy]),
            amount_period=int(policies.sale_period[policy]),
            external_values=external_values,
            month=month,
            rollout_count=rollout_count,
        )
        buffer_sale = jnp.where(
            (buffer_sale_values > 0.0) & (post_required_cash < buffer_trigger_values), buffer_sale_values, 0.0
        )
        remaining_target = jnp.where(active, required_sale + buffer_sale, 0.0)
        if not bool(jnp.any((hard_demand > 0.0) | (remaining_target > 0.0))):
            continue

        for asset_idx in range(policies.assets.shape[1]):
            asset_code = int(policies.assets[policy, asset_idx])
            series_index = int(policies.asset_series[policy, asset_idx])
            if asset_code < 0 or series_index < 0 or not bool(jnp.any(remaining_target > 0.0)):
                continue
            raw_price = external_values[series_index, :, month]
            valid_price = jnp.isfinite(raw_price) & (raw_price > 0.0)
            unit_price = jnp.where(valid_price, raw_price, 0.0)

            for source_account in policies.source_accounts[policy]:
                source_account_code = int(source_account)
                if source_account_code < 0 or not bool(jnp.any(remaining_target > 0.0)):
                    continue
                ordered_lots = lot_order_for_pool(
                    lot_agent_codes=plan.lot_agent_codes,
                    lot_account_codes=plan.lot_account_codes,
                    lot_asset_codes=plan.lot_asset_codes,
                    lot_purchase_month=plan.lot_purchase_month,
                    lot_id_codes=plan.lot_id_codes,
                    agent_code=policy_agent,
                    account_code=source_account_code,
                    asset_code=asset_code,
                )
                if ordered_lots.size == 0:
                    continue
                available_value = lot_remaining[ordered_lots, :].sum(axis=0) * unit_price
                target_dollars = jnp.where(
                    valid_price & active, jnp.minimum(jnp.maximum(remaining_target, 0.0), available_value), 0.0
                )
                if not bool(jnp.any(target_dollars > 0.0)):
                    continue
                sold_units, proceeds, basis, oversell = _fifo_sell_dollars(
                    lot_remaining.T, ordered_lots, target_dollars, unit_price, cost_basis_per_unit
                )
                if bool(oversell.any()):
                    raise ValueError("liquidity policy attempted to sell more than available lots")
                lot_remaining = lot_remaining - sold_units.T
                total_proceeds = proceeds.sum(axis=1)
                if policy_cash_slot >= 0:
                    cash = cash.at[policy_cash_slot].add(total_proceeds)
                capital_gain_active, capital_gain_ytd = _record_capital_gains(
                    plan, capital_gain_active, capital_gain_ytd, month, policy_agent, sold_units, proceeds - basis
                )
                disposition = buffers.lot_dispositions.liquidity
                disposition.active[month, policy, asset_idx] |= np.asarray((sold_units > 0.0).T)
                disposition.units[month, policy, asset_idx] += np.asarray(sold_units.T)
                disposition.basis[month, policy, asset_idx] += np.asarray(basis.T)
                disposition.proceeds[month, policy, asset_idx] += np.asarray(proceeds.T)
                remaining_target = jnp.maximum(remaining_target - total_proceeds, 0.0)
    return cash, lot_remaining, capital_gain_active, capital_gain_ytd


def _apply_property_purchases(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    cash: jnp.ndarray,
    property_active: jnp.ndarray,
    property_basis: jnp.ndarray,
    property_ownership: jnp.ndarray,
    property_contribution: jnp.ndarray,
    property_equity: jnp.ndarray,
    liabilities: LiabilityState,
    active: jnp.ndarray,
    month: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, LiabilityState]:
    """Functional port of `phases._apply_property_purchases` (cash + mortgage-financed purchases)."""
    properties = plan.properties
    for prop in range(properties.month.shape[0]):
        if int(properties.month[prop]) != month:
            continue
        buffers.properties.purchase_active[month, prop] = np.asarray(active)
        property_active = property_active.at[prop].set(active | property_active[prop])
        property_basis = property_basis.at[prop].set(
            jnp.where(active, float(properties.adjusted_basis[prop]), property_basis[prop])
        )
        property_ownership = property_ownership.at[prop].set(
            jnp.where(active, float(properties.ownership[prop]), property_ownership[prop])
        )
        property_contribution = property_contribution.at[prop].set(
            jnp.where(active, float(properties.stake_contribution[prop]), property_contribution[prop])
        )
        property_equity = property_equity.at[prop].set(
            jnp.where(active, float(properties.equity_ledger[prop]), property_equity[prop])
        )
        buyer_cash = float(properties.stake_contribution[prop])
        if buyer_cash > 0.0:
            buffers.properties.transfer_active[month, prop] = np.asarray(active)
            buyer_slot = int(properties.buyer_slot[prop])
            if buyer_slot >= 0:
                cash = cash.at[buyer_slot].add(jnp.where(active, -buyer_cash, 0.0))
            seller_slot = int(properties.seller_slot[prop])
            if seller_slot >= 0:
                cash = cash.at[seller_slot].add(jnp.where(active, buyer_cash, 0.0))

        mortgage_slot = int(properties.mortgage_slot[prop])
        if mortgage_slot >= 0:
            buffers.properties.mortgage_origination_active[month, mortgage_slot] = np.asarray(active)
            liabilities = replace(
                liabilities,
                active=liabilities.active.at[mortgage_slot].set(active | liabilities.active[mortgage_slot]),
                principal=liabilities.principal.at[mortgage_slot].set(
                    jnp.where(
                        active, float(plan.liabilities.principal[mortgage_slot]), liabilities.principal[mortgage_slot]
                    )
                ),
                monthly_payment=liabilities.monthly_payment.at[mortgage_slot].set(
                    jnp.where(
                        active,
                        float(plan.liabilities.monthly_payment[mortgage_slot]),
                        liabilities.monthly_payment[mortgage_slot],
                    )
                ),
                interest_ytd=liabilities.interest_ytd.at[mortgage_slot].set(
                    jnp.where(active, 0.0, liabilities.interest_ytd[mortgage_slot])
                ),
                principal_ytd=liabilities.principal_ytd.at[mortgage_slot].set(
                    jnp.where(active, 0.0, liabilities.principal_ytd[mortgage_slot])
                ),
            )
    return (
        cash,
        property_active,
        property_basis,
        property_ownership,
        property_contribution,
        property_equity,
        liabilities,
    )

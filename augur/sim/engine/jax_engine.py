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
- obligation accruals + settlement with failure tracking and `_zero_failed_state`, for every
  source kind (CONFIGURED_OBLIGATION, PROPERTY_TAX with the SALT/Schedule-E split, MORTGAGE_PAYMENT
  with the interest/principal split, and ESTIMATED_TAX / ESTIMATED_TAX_Q4 / TAX_TRUE_UP);
- PE tenders (LNW-floor tender / public-market / forced-sale / forced-recovery sales + opportunity
  trace);
- the December year-end tax machinery: Schedule-E rental-interest/depreciation deductions,
  §1211/§1212 capital-loss netting, the two-pass SALT walk over MID + LTCG brackets + the §1250
  worksheet, tax-liability accrual, and the true-up settlement (the latter in float64, since a
  ~$50k liability must settle to exactly zero — float32 leaves a ~$0.004 residual).

Not yet ported (no-op): property sale, §168 depreciation accrual, owner-occupied-month tracking,
lifecycle events, and primary-residence assignment — so depreciation_ytd / recapture stay zero,
which is correct for non-rental, non-sale scenarios.

Float32 note: tax amounts, cash flows, and settlements match the float64 reference to within a few
parts in 1e8, but a handful of existing tests assert breakdown fields (income, deductions) to
`rel=1e-9` / `abs=1e-6`, which float32 cannot meet on $10k–$200k values; those JAX variants fail on
precision alone, not logic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import jax.numpy as jnp
import numpy as np

from augur.model.series import PrivateEquityRegimeCode
from augur.sim.buffers import SimulationBuffers
from augur.sim.codec.plan import CompiledSimulation
from augur.sim.compiler.helpers import AMOUNT_FIXED, NO_CODE
from augur.sim.compiler.obligations import ObligationCompileOutput
from augur.sim.compiler.transfers import TransferCompileOutput
from augur.sim.enums import (
    CapitalGainClassification,
    ObligationSource,
    PrivateEquityDispositionKind,
    PrivateEquityOpportunityOutcome,
)
from augur.sim.tax import net_capital_gains_with_carryforward
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


@dataclass(frozen=True)
class TaxLiabilityState:
    """Per-(tax-liability-slot, rollout) outstanding tax owed (`[tax_liability_count, R]`)."""

    active: jnp.ndarray
    amount: jnp.ndarray


def run_jax(plan: CompiledSimulation, buffers: SimulationBuffers) -> None:
    p = plan.slot_plan
    r = p.rollout_count

    cash = jnp.asarray(np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, r)))
    lot_remaining = jnp.asarray(np.broadcast_to(plan.lot_initial_quantity[:, None], (p.lot_count, r)))
    ordinary_ytd = jnp.zeros((p.tax_profile_count, r))
    capital_gain_active = jnp.zeros((p.capital_gain_agent_count, 2, r), dtype=bool)
    capital_gain_ytd = jnp.zeros((p.capital_gain_agent_count, 2, r))
    capital_loss_carryforward = jnp.zeros((p.capital_gain_agent_count, r))
    # Tax YTD buckets the year-end pass reads; fed by property-tax settlement, depreciation, and
    # property sale (the latter two not yet ported, so these stay zero for non-rental scenarios).
    property_tax_ytd = jnp.zeros((p.tax_profile_count, r))
    property_depreciation_ytd = jnp.zeros((p.property_count, r))
    recapture_section_1250_ytd = jnp.zeros((p.tax_profile_count, r))
    tax_liability = TaxLiabilityState(
        active=jnp.zeros((p.tax_liability_count, r), dtype=bool), amount=jnp.zeros((p.tax_liability_count, r))
    )
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
            plan, property_active, liabilities, tax_liability, active, external_values, month, r
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
        (
            cash,
            ordinary_ytd,
            liabilities,
            tax_liability,
            property_tax_ytd,
            failed,
            failed_month,
            ob_paid,
            ob_shortfall,
            ob_failure,
        ) = _apply_obligation_settlement(
            plan,
            buffers,
            cash,
            ordinary_ytd,
            liabilities,
            tax_liability,
            property_rented_fraction,
            property_tax_ytd,
            failed,
            failed_month,
            ob_active,
            ob_due,
            funded,
            month,
            p.tax_profile_count,
            r,
        )
        buffers.obligations.active[month] = np.asarray(ob_active)
        buffers.obligations.due[month] = np.asarray(ob_due)
        buffers.obligations.paid[month] = np.asarray(ob_paid)
        buffers.obligations.shortfall[month] = np.asarray(ob_shortfall)
        buffers.obligations.failure_active[month] = np.asarray(ob_failure)

        # PE tenders fire after settlement so the LNW floor compares against post-settlement cash.
        cash, lot_remaining, capital_gain_active, capital_gain_ytd = _apply_pe_tenders(
            plan,
            buffers,
            cash,
            lot_remaining,
            capital_gain_active,
            capital_gain_ytd,
            ~failed,
            external_values,
            month,
            r,
        )

        # Year-end (December) tax accrual: creates this year's tax liabilities, which next year's
        # estimated-tax / true-up obligations read and settle.
        (
            ordinary_ytd,
            capital_gain_ytd,
            capital_loss_carryforward,
            liabilities,
            property_tax_ytd,
            recapture_section_1250_ytd,
            property_depreciation_ytd,
            tax_liability,
        ) = _apply_tax_accruals(
            plan,
            buffers,
            ordinary_ytd,
            capital_gain_active,
            capital_gain_ytd,
            capital_loss_carryforward,
            liabilities,
            property_tax_ytd,
            property_depreciation_ytd,
            recapture_section_1250_ytd,
            tax_liability,
            ~failed,
            month,
            r,
        )

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
        # `_zero_failed_state` also zeros capital-loss carryforward and outstanding tax liabilities.
        capital_loss_carryforward = capital_loss_carryforward * keep
        tax_liability = replace(tax_liability, amount=tax_liability.amount * keep)

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
    tax_liability: TaxLiabilityState,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Functional port of `phases._apply_obligation_accruals` (all source kinds incl. estimated tax)."""
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
        elif source_kind == ObligationSource.ESTIMATED_TAX:
            quarterly = float(plan.tax.profile_prior_year_tax[int(obligations.source_index[month, slot])]) / 4.0
            amount = jnp.full(rollout_count, quarterly)
            slot_active = active & (amount > 0.0)
        elif source_kind in (ObligationSource.ESTIMATED_TAX_Q4, ObligationSource.TAX_TRUE_UP):
            profile = int(obligations.source_index[month, slot])
            tax_year_end = (month // 12 - 1) * 12 + 11
            actual = _actual_tax_for_profile_year(
                plan, tax_liability, profile_index=profile, year_end_month=tax_year_end, rollout_count=rollout_count
            )
            prior_year = float(plan.tax.profile_prior_year_tax[profile])
            safe_harbor = jnp.minimum(prior_year, actual)
            if source_kind == ObligationSource.ESTIMATED_TAX_Q4:
                amount = jnp.maximum(safe_harbor - prior_year * 0.75, 0.0)
            else:
                amount = jnp.maximum(actual - safe_harbor, 0.0)
            slot_active = active & (amount > 0.0)
        else:
            continue
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


_ESTIMATED_TAX_KINDS = (ObligationSource.ESTIMATED_TAX, ObligationSource.ESTIMATED_TAX_Q4, ObligationSource.TAX_TRUE_UP)


def _apply_obligation_settlement(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    liabilities: LiabilityState,
    tax_liability: TaxLiabilityState,
    property_rented_fraction: jnp.ndarray,
    property_tax_ytd: jnp.ndarray,
    failed: jnp.ndarray,
    failed_month: jnp.ndarray,
    accrual_active: jnp.ndarray,
    accrual_due: jnp.ndarray,
    funded: jnp.ndarray,
    month: int,
    tax_profile_count: int,
    rollout_count: int,
) -> tuple[
    jnp.ndarray,
    jnp.ndarray,
    LiabilityState,
    TaxLiabilityState,
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
]:
    """Functional port of `phases._apply_obligation_settlement` (all source kinds + tax settlements).

    The deduction path uses each obligation's compile-time `deductible_fraction`; the property-tax
    SALT/Schedule-E split (runtime `property_rented_fraction`) lands with the tax-machinery port.
    """
    obligations = plan.obligations
    slot_count = accrual_active.shape[0]
    paid_buffer = jnp.zeros_like(accrual_due)
    shortfall_buffer = jnp.zeros_like(accrual_due)
    failure_active = jnp.zeros_like(accrual_active)
    # Tax-settlement candidates are float64 numpy (see `_actual_tax_for_profile_year`).
    candidate = np.zeros((tax_profile_count, rollout_count), dtype=np.float64)
    candidate_year_end = np.full((tax_profile_count, rollout_count), NO_CODE, dtype=np.int64)
    payment_failed = np.zeros((tax_profile_count, rollout_count), dtype=bool)
    for slot in range(slot_count):
        source_kind = int(obligations.source_kind[month, slot])
        if source_kind not in (
            ObligationSource.CONFIGURED_OBLIGATION,
            ObligationSource.PROPERTY_TAX,
            ObligationSource.MORTGAGE_PAYMENT,
            *_ESTIMATED_TAX_KINDS,
        ):
            continue
        active_slot = accrual_active[slot]
        if source_kind == ObligationSource.TAX_TRUE_UP:
            profile = int(obligations.source_index[month, slot])
            tax_year_end = (month // 12 - 1) * 12 + 11
            actual = _actual_tax_for_profile_year(
                plan, tax_liability, profile_index=profile, year_end_month=tax_year_end, rollout_count=rollout_count
            )
            active_slot_np = np.asarray(active_slot)
            candidate[profile] = np.where(active_slot_np, actual, candidate[profile])
            candidate_year_end[profile] = np.where(active_slot_np, tax_year_end, candidate_year_end[profile])
        amount = accrual_due[slot]
        paid = active_slot & funded[slot]
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
        # Property-tax payments accumulate (owner-use share) into the payer's YTD bucket for the
        # year-end federal SALT pass; the rented share routes to Schedule E via deduction_profile.
        property_tax_profile = int(obligations.property_tax_profile[month, slot])
        property_slot = int(obligations.property_slot[month, slot])
        if property_tax_profile >= 0:
            owner_share = 1.0 - property_rented_fraction[property_slot]
            property_tax_ytd = property_tax_ytd.at[property_tax_profile].add(jnp.where(paid, amount * owner_share, 0.0))
        # Schedule E / itemized deduction: property-tax obligations use the runtime rented fraction;
        # other deductible obligations use the compile-time deductible_fraction.
        deduction_profile = int(obligations.deduction_profile[month, slot])
        if deduction_profile >= 0:
            if property_slot >= 0:
                rented = property_rented_fraction[property_slot]
                ordinary_ytd = ordinary_ytd.at[deduction_profile].add(jnp.where(paid, -amount * rented, 0.0))
            else:
                deductible_fraction = float(obligations.deductible_fraction[month, slot])
                ordinary_ytd = ordinary_ytd.at[deduction_profile].add(
                    jnp.where(paid, -amount * deductible_fraction, 0.0)
                )

        slot_failed = active_slot & ~funded[slot]
        shortfall_buffer = shortfall_buffer.at[slot].set(jnp.where(slot_failed, amount, 0.0))
        failure_active = failure_active.at[slot].set(slot_failed)
        first_failure = slot_failed & (failed_month < 0)
        failed_month = jnp.where(first_failure, month, failed_month)
        failed = failed | slot_failed
        if source_kind in _ESTIMATED_TAX_KINDS:
            profile = int(obligations.source_index[month, slot])
            payment_failed[profile] = payment_failed[profile] | np.asarray(slot_failed)

    tax_liability = _apply_tax_settlements(
        plan,
        buffers,
        tax_liability,
        month=month,
        candidate=candidate,
        candidate_year_end=candidate_year_end,
        payment_failed=payment_failed,
    )
    return (
        cash,
        ordinary_ytd,
        liabilities,
        tax_liability,
        property_tax_ytd,
        failed,
        failed_month,
        paid_buffer,
        shortfall_buffer,
        failure_active,
    )


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


def _compute_liquid_net_worth(
    plan: CompiledSimulation,
    cash: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    external_values: jnp.ndarray,
    policy_idx: int,
    month: int,
) -> jnp.ndarray:
    """Port of `phases._compute_liquid_net_worth`: owner cash + non-PE lot value at current marks."""
    owner_cash_mask = jnp.asarray(plan.pe_policies.owner_cash_mask[policy_idx])
    cash_total = (cash * owner_cash_mask[:, None]).sum(axis=0)
    lot_mask = plan.pe_policies.owner_non_pe_lot_mask[policy_idx]
    if not lot_mask.any():
        return cash_total
    lot_indices = np.flatnonzero(lot_mask)
    series_indices = plan.lot_asset_series_index[lot_indices]
    valid = series_indices >= 0
    prices = external_values[np.where(valid, series_indices, 0), :, month]
    prices = jnp.nan_to_num(jnp.where(jnp.asarray(valid)[:, None], prices, 0.0), nan=0.0)
    lot_value = (lot_remaining[lot_indices, :] * prices).sum(axis=0)
    return cash_total + lot_value


def _record_pe_opportunity(
    buffers: SimulationBuffers,
    *,
    month: int,
    issuer_idx: int,
    active: jnp.ndarray,
    outcome: jnp.ndarray,
    floor: jnp.ndarray,
    liquid_net_worth: jnp.ndarray,
    shortfall: jnp.ndarray,
    units_held: jnp.ndarray,
    sellable_units: jnp.ndarray,
    target_units: jnp.ndarray,
    proceeds: jnp.ndarray,
) -> None:
    """Port of `phases._record_pe_opportunity`: log the per-rollout tender opportunity trace.

    Each (month, issuer) cell is written exactly once, so masking inactive rollouts to the buffer's
    zero init matches the NumPy reference's active-only assignment.
    """
    dest = buffers.private_equity_opportunities
    dest.active[month, issuer_idx] = np.asarray(active)
    dest.outcome[month, issuer_idx] = np.asarray(jnp.where(active, outcome, 0))
    dest.floor[month, issuer_idx] = np.asarray(jnp.where(active, floor, 0.0))
    dest.liquid_net_worth[month, issuer_idx] = np.asarray(jnp.where(active, liquid_net_worth, 0.0))
    dest.shortfall[month, issuer_idx] = np.asarray(jnp.where(active, shortfall, 0.0))
    dest.units_held[month, issuer_idx] = np.asarray(jnp.where(active, units_held, 0.0))
    dest.sellable_units[month, issuer_idx] = np.asarray(jnp.where(active, sellable_units, 0.0))
    dest.target_units[month, issuer_idx] = np.asarray(jnp.where(active, target_units, 0.0))
    dest.proceeds[month, issuer_idx] = np.asarray(jnp.where(active, proceeds, 0.0))


def _apply_pe_sale_result(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    cash: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    capital_gain_active: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    *,
    month: int,
    issuer_idx: int,
    policy_idx: int,
    disposition_kind: PrivateEquityDispositionKind,
    sold_units: jnp.ndarray,
    proceeds: jnp.ndarray,
    basis: jnp.ndarray,
    oversell: jnp.ndarray,
    oversell_label: str,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `phases._apply_pe_sale_result`: book a PE FIFO sale (cash, lots, cap gains, log)."""
    if bool(oversell.any()):
        raise ValueError(f"{oversell_label} attempted to sell more than available lots for a PE issuer")
    lot_remaining = lot_remaining - sold_units.T
    proceeds_slot = int(plan.pe_policies.proceeds_cash_slot[policy_idx])
    if proceeds_slot >= 0:
        cash = cash.at[proceeds_slot].add(proceeds.sum(axis=1))
    owner_code = int(plan.pe_policies.owner_agent[policy_idx])
    capital_gain_active, capital_gain_ytd = _record_capital_gains(
        plan, capital_gain_active, capital_gain_ytd, month, owner_code, sold_units, proceeds - basis
    )
    kind_idx = int(disposition_kind)
    pe = buffers.lot_dispositions.pe
    pe.active[month, issuer_idx, kind_idx] |= np.asarray((sold_units > 0.0).T)
    pe.units[month, issuer_idx, kind_idx] += np.asarray(sold_units.T)
    pe.basis[month, issuer_idx, kind_idx] += np.asarray(basis.T)
    pe.proceeds[month, issuer_idx, kind_idx] += np.asarray(proceeds.T)
    return cash, lot_remaining, capital_gain_active, capital_gain_ytd


def _apply_pe_target_units_sale(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    cash: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    capital_gain_active: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    *,
    month: int,
    issuer_idx: int,
    policy_idx: int,
    ordered_lots: np.ndarray,
    mark: jnp.ndarray,
    target_units: jnp.ndarray,
    disposition_kind: PrivateEquityDispositionKind,
    oversell_label: str,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `phases._apply_pe_target_units_sale`: FIFO-sell a per-rollout unit target at the mark."""
    if not bool((target_units > 0.0).any()):
        return cash, lot_remaining, capital_gain_active, capital_gain_ytd
    sold_units, proceeds, basis, oversell = _fifo_sell_units(
        lot_remaining.T, ordered_lots, target_units, mark, jnp.asarray(plan.lot_cost_basis_per_unit)
    )
    return _apply_pe_sale_result(
        plan,
        buffers,
        cash,
        lot_remaining,
        capital_gain_active,
        capital_gain_ytd,
        month=month,
        issuer_idx=issuer_idx,
        policy_idx=policy_idx,
        disposition_kind=disposition_kind,
        sold_units=sold_units,
        proceeds=proceeds,
        basis=basis,
        oversell=oversell,
        oversell_label=oversell_label,
    )


def _apply_pe_tenders(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    cash: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    capital_gain_active: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Functional port of `phases._apply_pe_tenders`: LNW-floor-driven private-equity tender sales."""
    issuers = plan.pe_issuers
    channels = plan.pe_channels
    policies = plan.pe_policies
    cost_basis_per_unit = jnp.asarray(plan.lot_cost_basis_per_unit)
    for issuer_idx in range(issuers.codes.shape[0]):
        if int(issuers.codes[issuer_idx]) < 0:
            continue
        policy_idx = int(issuers.policy_index[issuer_idx])
        mark_np = channels.marks[issuer_idx, :, month]
        if not np.isfinite(mark_np).all() or (mark_np < 0.0).any():
            raise ValueError("private-equity mark series produced a negative or non-finite value")
        mark = jnp.asarray(mark_np)
        positive_mark = mark > 0.0
        tender_active = jnp.asarray(channels.sale_opportunity_active[issuer_idx, :, month]) & active
        public_market_active = jnp.asarray(channels.regime_codes[issuer_idx, :, month]) == int(
            PrivateEquityRegimeCode.PUBLIC_MARKET
        )
        liquidity_blocked = jnp.asarray(channels.liquidity_blocked[issuer_idx, :, month])
        forced_sale_fraction = jnp.asarray(channels.forced_sale_fractions[issuer_idx, :, month])
        forced_recovery_cashout_usd_np = channels.forced_recovery_cashout_usd[issuer_idx, :, month]
        if (forced_recovery_cashout_usd_np < 0.0).any():
            raise ValueError("private-equity forced-recovery cashout series produced a negative value")
        forced_recovery_cashout_usd = jnp.asarray(forced_recovery_cashout_usd_np)

        lot_indices = np.flatnonzero(issuers.lot_mask[issuer_idx])
        if lot_indices.size == 0:
            continue
        ordered_lots = lot_indices[np.argsort(plan.lot_purchase_month[lot_indices], kind="stable")]
        sale_capacity_fraction = jnp.asarray(channels.sale_capacity_fractions[issuer_idx, :, month])
        eligible_fraction = jnp.asarray(channels.eligible_fractions[issuer_idx, :, month])
        units_held = lot_remaining[ordered_lots, :].sum(axis=0)

        if policy_idx < 0:
            _record_pe_opportunity(
                buffers,
                month=month,
                issuer_idx=issuer_idx,
                active=tender_active,
                outcome=jnp.full(rollout_count, int(PrivateEquityOpportunityOutcome.NO_POLICY)),
                floor=jnp.zeros(rollout_count),
                liquid_net_worth=jnp.zeros(rollout_count),
                shortfall=jnp.zeros(rollout_count),
                units_held=units_held,
                sellable_units=units_held * sale_capacity_fraction * eligible_fraction,
                target_units=jnp.zeros(rollout_count),
                proceeds=jnp.zeros(rollout_count),
            )
            continue

        recovery_active = (forced_recovery_cashout_usd > 0.0) & active & (units_held > 0.0)
        if bool(recovery_active.any()):
            safe_units = jnp.where(units_held > 0.0, units_held, 1.0)
            recovery_unit_price = jnp.where(units_held > 0.0, forced_recovery_cashout_usd / safe_units, 1.0)
            sold_units, proceeds, basis, oversell = _fifo_sell_units(
                lot_remaining.T,
                ordered_lots,
                jnp.where(recovery_active, units_held, 0.0),
                recovery_unit_price,
                cost_basis_per_unit,
            )
            cash, lot_remaining, capital_gain_active, capital_gain_ytd = _apply_pe_sale_result(
                plan,
                buffers,
                cash,
                lot_remaining,
                capital_gain_active,
                capital_gain_ytd,
                month=month,
                issuer_idx=issuer_idx,
                policy_idx=policy_idx,
                disposition_kind=PrivateEquityDispositionKind.FORCED_RECOVERY,
                sold_units=sold_units,
                proceeds=proceeds,
                basis=basis,
                oversell=oversell,
                oversell_label="PE forced recovery",
            )

        units_held = lot_remaining[ordered_lots, :].sum(axis=0)
        forced_sale_active = (forced_sale_fraction > 0.0) & active & positive_mark & (units_held > 0.0)
        if bool(forced_sale_active.any()):
            sold_units, proceeds, basis, oversell = _fifo_sell_units(
                lot_remaining.T,
                ordered_lots,
                jnp.where(forced_sale_active, units_held * forced_sale_fraction, 0.0),
                mark,
                cost_basis_per_unit,
            )
            cash, lot_remaining, capital_gain_active, capital_gain_ytd = _apply_pe_sale_result(
                plan,
                buffers,
                cash,
                lot_remaining,
                capital_gain_active,
                capital_gain_ytd,
                month=month,
                issuer_idx=issuer_idx,
                policy_idx=policy_idx,
                disposition_kind=PrivateEquityDispositionKind.FORCED_SALE,
                sold_units=sold_units,
                proceeds=proceeds,
                basis=basis,
                oversell=oversell,
                oversell_label="PE forced sale",
            )

        floor = _amount_values(
            amount_kind=int(policies.floor_kind[policy_idx]),
            amount_fixed=float(policies.floor_fixed[policy_idx]),
            amount_base=float(policies.floor_base[policy_idx]),
            amount_series=int(policies.floor_series[policy_idx]),
            amount_base_month=int(policies.floor_base_month[policy_idx]),
            amount_period=int(policies.floor_period[policy_idx]),
            external_values=external_values,
            month=month,
            rollout_count=rollout_count,
        )
        lnw = _compute_liquid_net_worth(plan, cash, lot_remaining, external_values, policy_idx, month)
        shortfall = jnp.maximum(0.0, floor - lnw)
        units_held = lot_remaining[ordered_lots, :].sum(axis=0)
        sellable_units = units_held * sale_capacity_fraction * eligible_fraction
        shortfall_units = jnp.where(positive_mark, shortfall / jnp.where(positive_mark, mark, 1.0), 0.0)
        opportunity_active = (tender_active | public_market_active) & active & ~liquidity_blocked & positive_mark
        target_units = jnp.where(opportunity_active, jnp.minimum(shortfall_units, sellable_units), 0.0)

        outcome = jnp.full(rollout_count, int(PrivateEquityOpportunityOutcome.SOLD))
        outcome = jnp.where(shortfall <= 0.0, int(PrivateEquityOpportunityOutcome.FLOOR_SATISFIED), outcome)
        outcome = jnp.where(
            (sale_capacity_fraction * eligible_fraction) <= 0.0,
            int(PrivateEquityOpportunityOutcome.CAPACITY_ZERO),
            outcome,
        )
        outcome = jnp.where(~positive_mark, int(PrivateEquityOpportunityOutcome.NONPOSITIVE_MARK), outcome)
        outcome = jnp.where(liquidity_blocked, int(PrivateEquityOpportunityOutcome.LIQUIDITY_BLOCKED), outcome)
        outcome = jnp.where(units_held <= 0.0, int(PrivateEquityOpportunityOutcome.NO_UNITS), outcome)
        _record_pe_opportunity(
            buffers,
            month=month,
            issuer_idx=issuer_idx,
            active=tender_active,
            outcome=outcome,
            floor=floor,
            liquid_net_worth=lnw,
            shortfall=shortfall,
            units_held=units_held,
            sellable_units=sellable_units,
            target_units=target_units,
            proceeds=target_units * mark,
        )
        if not bool((target_units > 0.0).any()):
            continue

        cash, lot_remaining, capital_gain_active, capital_gain_ytd = _apply_pe_target_units_sale(
            plan,
            buffers,
            cash,
            lot_remaining,
            capital_gain_active,
            capital_gain_ytd,
            month=month,
            issuer_idx=issuer_idx,
            policy_idx=policy_idx,
            ordered_lots=ordered_lots,
            mark=mark,
            target_units=jnp.where(tender_active & ~public_market_active, target_units, 0.0),
            disposition_kind=PrivateEquityDispositionKind.TENDER,
            oversell_label="PE tender",
        )
        cash, lot_remaining, capital_gain_active, capital_gain_ytd = _apply_pe_target_units_sale(
            plan,
            buffers,
            cash,
            lot_remaining,
            capital_gain_active,
            capital_gain_ytd,
            month=month,
            issuer_idx=issuer_idx,
            policy_idx=policy_idx,
            ordered_lots=ordered_lots,
            mark=mark,
            target_units=jnp.where(public_market_active, target_units, 0.0),
            disposition_kind=PrivateEquityDispositionKind.PUBLIC_MARKET,
            oversell_label="PE public market sale",
        )
    return cash, lot_remaining, capital_gain_active, capital_gain_ytd


def _apply_brackets(amount: jnp.ndarray, *, upper: np.ndarray, rate: np.ndarray, count: int) -> jnp.ndarray:
    """Port of `phases._apply_brackets`: progressive bracket tax on `amount`."""
    if count <= 0:
        return jnp.zeros_like(amount)
    upper = jnp.asarray(upper[:count])
    rate = jnp.asarray(rate[:count])
    previous_upper = jnp.concatenate([jnp.zeros(1), upper[:-1]])
    slice_top = jnp.minimum(amount[:, None], upper[None, :])
    in_bracket = jnp.maximum(slice_top - previous_upper[None, :], 0.0)
    return (in_bracket * rate[None, :]).sum(axis=1)


def _apply_ltcg_brackets(
    ltcg_amount: jnp.ndarray, ordinary_taxable: jnp.ndarray, *, upper: np.ndarray, rate: np.ndarray, count: int
) -> jnp.ndarray:
    """Port of `phases._apply_ltcg_brackets`: LTCG stacked on top of ordinary taxable income."""
    if count <= 0:
        return jnp.zeros_like(ltcg_amount)
    upper = jnp.asarray(upper[:count])
    rate = jnp.asarray(rate[:count])
    previous_upper = jnp.concatenate([jnp.zeros(1), upper[:-1]])
    total_taxable = ordinary_taxable + ltcg_amount
    slice_top = jnp.minimum(total_taxable[:, None], upper[None, :])
    slice_bottom = jnp.maximum(ordinary_taxable[:, None], previous_upper[None, :])
    in_bracket = jnp.maximum(slice_top - slice_bottom, 0.0)
    return (in_bracket * rate[None, :]).sum(axis=1)


def _compute_tax_for_link(
    plan: CompiledSimulation,
    ordinary_ytd: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    recapture_section_1250_ytd: jnp.ndarray,
    liabilities: LiabilityState,
    *,
    link: int,
    salt_deduction: jnp.ndarray,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `phases._compute_tax_for_link`: one link's bracket math (MID + SALT + §1250 + LTCG)."""
    t = plan.tax
    profile = int(t.link_profile[link])
    gain_profile = int(plan.tax_profile_capital_gain_index[profile])
    ordinary = ordinary_ytd[profile]
    ltcg = capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM]
    stcg = capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM]
    recapture = recapture_section_1250_ytd[profile]
    section_1250_rate = float(t.link_section_1250_rate[link])
    standard_deduction = float(t.link_standard_deduction[link])
    if bool(plan.mid.link_active[link]):
        owner_interest_ytd = liabilities.interest_ytd - liabilities.rental_interest_ytd
        mortgage_interest_deduction = jnp.asarray(plan.mid.principal_ratio[link]) @ owner_interest_ytd
    else:
        mortgage_interest_deduction = jnp.zeros(rollout_count)
    itemized_deduction = mortgage_interest_deduction + salt_deduction
    deduction_used = jnp.maximum(itemized_deduction, standard_deduction)

    federal_style_section_1250 = section_1250_rate > 0.0
    ordinary_for_brackets = ordinary if federal_style_section_1250 else ordinary + recapture

    ordinary_upper = t.link_ordinary_upper[link]
    ordinary_rate = t.link_ordinary_rate[link]
    ordinary_count = int(t.link_ordinary_count[link])
    if int(t.link_has_ltcg[link]) == 1:
        ordinary_taxable = jnp.maximum(ordinary_for_brackets + stcg - deduction_used, 0.0)
        capital_taxable = ltcg
        ordinary_tax = _apply_brackets(ordinary_taxable, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count)
        ltcg_tax = _apply_ltcg_brackets(
            ltcg,
            ordinary_taxable,
            upper=t.link_ltcg_upper[link],
            rate=t.link_ltcg_rate[link],
            count=int(t.link_ltcg_count[link]),
        )
    else:
        ordinary_taxable = jnp.maximum(ordinary_for_brackets + ltcg + stcg - deduction_used, 0.0)
        capital_taxable = jnp.zeros(rollout_count)
        ordinary_tax = _apply_brackets(ordinary_taxable, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count)
        ltcg_tax = jnp.zeros(rollout_count)

    if federal_style_section_1250:
        ordinary_tax_with_recapture = _apply_brackets(
            ordinary_taxable + recapture, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count
        )
        implied_recapture_tax = jnp.maximum(ordinary_tax_with_recapture - ordinary_tax, 0.0)
        section_1250_tax = jnp.minimum(implied_recapture_tax, recapture * section_1250_rate)
    else:
        section_1250_tax = jnp.zeros(rollout_count)

    capital_tax = ltcg_tax + section_1250_tax
    return mortgage_interest_deduction, itemized_deduction, ordinary_taxable, capital_taxable, ordinary_tax, capital_tax


def _write_tax_link_buffers(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    ordinary_ytd: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    tax_liability: TaxLiabilityState,
    *,
    link: int,
    month: int,
    active: jnp.ndarray,
    standard_deduction: float,
    mortgage_interest_deduction: jnp.ndarray,
    salt_deduction: jnp.ndarray,
    itemized_deduction: jnp.ndarray,
    ordinary_taxable: jnp.ndarray,
    capital_taxable: jnp.ndarray,
    ordinary_tax: jnp.ndarray,
    capital_tax: jnp.ndarray,
) -> tuple[jnp.ndarray, TaxLiabilityState]:
    """Port of `phases._write_tax_link_buffers`: write a link's breakdown + accrue its tax liability."""
    profile = int(plan.tax.link_profile[link])
    gain_profile = int(plan.tax_profile_capital_gain_index[profile])
    ordinary = ordinary_ytd[profile]
    ltcg = capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM]
    stcg = capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM]
    tax = ordinary_tax + capital_tax
    a = np.asarray(active)
    taxes = buffers.taxes
    taxes.accrual_active[month, link] = a
    taxes.accrual_amount[month, link] = np.asarray(jnp.where(active, tax, 0.0))
    taxes.breakdown_ordinary[month, link] = np.asarray(jnp.where(active, ordinary, 0.0))
    taxes.breakdown_ltcg[month, link] = np.asarray(jnp.where(active, ltcg, 0.0))
    taxes.breakdown_stcg[month, link] = np.asarray(jnp.where(active, stcg, 0.0))
    taxes.breakdown_standard_deduction[month, link] = np.asarray(jnp.where(active, standard_deduction, 0.0))
    taxes.breakdown_mortgage_interest_deduction[month, link] = np.asarray(
        jnp.where(active, mortgage_interest_deduction, 0.0)
    )
    taxes.breakdown_salt_deduction[month, link] = np.asarray(jnp.where(active, salt_deduction, 0.0))
    taxes.breakdown_itemized_deduction[month, link] = np.asarray(jnp.where(active, itemized_deduction, 0.0))
    taxes.breakdown_ordinary_taxable[month, link] = np.asarray(jnp.where(active, ordinary_taxable, 0.0))
    taxes.breakdown_capital_taxable[month, link] = np.asarray(jnp.where(active, capital_taxable, 0.0))
    taxes.breakdown_ordinary_tax[month, link] = np.asarray(jnp.where(active, ordinary_tax, 0.0))
    taxes.breakdown_capital_tax[month, link] = np.asarray(jnp.where(active, capital_tax, 0.0))

    tax_slot = _tax_liability_slot_for(plan, profile_index=profile, link_index=link, year_end_month=month)
    if tax_slot >= 0:
        tax_liability = replace(
            tax_liability,
            active=tax_liability.active.at[tax_slot].set(active | tax_liability.active[tax_slot]),
            amount=tax_liability.amount.at[tax_slot].set(jnp.where(active, tax, tax_liability.amount[tax_slot])),
        )
    return tax, tax_liability


def _tax_liability_slot_for(
    plan: CompiledSimulation, *, profile_index: int, link_index: int, year_end_month: int
) -> int:
    slots = np.flatnonzero(
        (plan.tax_liabilities.profile_index == profile_index)
        & (plan.tax_liabilities.link_index == link_index)
        & (plan.tax_liabilities.year_end_month == year_end_month)
    )
    return int(slots[0]) if slots.size else NO_CODE


def _actual_tax_for_profile_year(
    plan: CompiledSimulation,
    tax_liability: TaxLiabilityState,
    *,
    profile_index: int,
    year_end_month: int,
    rollout_count: int,
) -> np.ndarray:
    # float64 sum: the true-up settlement must drive the (float32) liability to exactly zero; a
    # float32 re-sum of ~$50k liabilities leaves a ~$0.004 residual that breaks the ==0 assertion.
    slots = np.flatnonzero(
        (plan.tax_liabilities.profile_index == profile_index) & (plan.tax_liabilities.year_end_month == year_end_month)
    )
    if slots.size == 0:
        return np.zeros(rollout_count, dtype=np.float64)
    return np.where(
        np.asarray(tax_liability.active[slots]), np.asarray(tax_liability.amount[slots], dtype=np.float64), 0.0
    ).sum(axis=0)


def _apply_tax_accruals(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    ordinary_ytd: jnp.ndarray,
    capital_gain_active: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    capital_loss_carryforward: jnp.ndarray,
    liabilities: LiabilityState,
    property_tax_ytd: jnp.ndarray,
    property_depreciation_ytd: jnp.ndarray,
    recapture_section_1250_ytd: jnp.ndarray,
    tax_liability: TaxLiabilityState,
    active: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> tuple[
    jnp.ndarray, jnp.ndarray, jnp.ndarray, LiabilityState, jnp.ndarray, jnp.ndarray, jnp.ndarray, TaxLiabilityState
]:
    """Port of `phases._apply_tax_accruals`: the December year-end tax pass (two-pass SALT)."""
    if month % 12 != 11:
        return (
            ordinary_ytd,
            capital_gain_ytd,
            capital_loss_carryforward,
            liabilities,
            property_tax_ytd,
            recapture_section_1250_ytd,
            property_depreciation_ytd,
            tax_liability,
        )

    # Schedule E: rented-share mortgage interest + §168 depreciation deduct from ordinary income.
    for lia in range(liabilities.rental_interest_ytd.shape[0]):
        profile = int(plan.liability_owner_profile_index[lia])
        if profile >= 0:
            ordinary_ytd = ordinary_ytd.at[profile].add(-jnp.where(active, liabilities.rental_interest_ytd[lia], 0.0))
    for prop in range(plan.property_owner_profile_index.shape[0]):
        profile = int(plan.property_owner_profile_index[prop])
        if profile >= 0:
            ordinary_ytd = ordinary_ytd.at[profile].add(-jnp.where(active, property_depreciation_ytd[prop], 0.0))
    property_depreciation_ytd = property_depreciation_ytd * (~active)

    # §1211/§1212 capital-loss netting (once per capital-gain agent) via the shared NumPy util.
    processed: set[int] = set()
    for profile in range(ordinary_ytd.shape[0]):
        gain_profile = int(plan.tax_profile_capital_gain_index[profile])
        if gain_profile < 0 or gain_profile in processed:
            continue
        processed.add(gain_profile)
        net_st, net_lt, ordinary_offset, carryforward_out = net_capital_gains_with_carryforward(
            np.asarray(capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM]),
            np.asarray(capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM]),
            np.asarray(capital_loss_carryforward[gain_profile]),
        )
        capital_gain_ytd = capital_gain_ytd.at[gain_profile, CapitalGainClassification.SHORT_TERM].set(
            jnp.where(active, jnp.asarray(net_st), capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM])
        )
        capital_gain_ytd = capital_gain_ytd.at[gain_profile, CapitalGainClassification.LONG_TERM].set(
            jnp.where(active, jnp.asarray(net_lt), capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM])
        )
        ordinary_ytd = ordinary_ytd.at[profile].add(-jnp.where(active, jnp.asarray(ordinary_offset), 0.0))
        capital_loss_carryforward = capital_loss_carryforward.at[gain_profile].set(
            jnp.where(active, jnp.asarray(carryforward_out), capital_loss_carryforward[gain_profile])
        )

    link_count = plan.tax.link_profile.shape[0]
    annual_tax_by_link = jnp.zeros((rollout_count, max(1, link_count)))
    zero_salt = jnp.zeros(rollout_count)
    for link in range(link_count):
        if bool(plan.salt.link_active[link]):
            continue
        mid, itemized, ord_taxable, cap_taxable, ord_tax, cap_tax = _compute_tax_for_link(
            plan,
            ordinary_ytd,
            capital_gain_ytd,
            recapture_section_1250_ytd,
            liabilities,
            link=link,
            salt_deduction=zero_salt,
            rollout_count=rollout_count,
        )
        tax, tax_liability = _write_tax_link_buffers(
            plan,
            buffers,
            ordinary_ytd,
            capital_gain_ytd,
            tax_liability,
            link=link,
            month=month,
            active=active,
            standard_deduction=float(plan.tax.link_standard_deduction[link]),
            mortgage_interest_deduction=mid,
            salt_deduction=zero_salt,
            itemized_deduction=itemized,
            ordinary_taxable=ord_taxable,
            capital_taxable=cap_taxable,
            ordinary_tax=ord_tax,
            capital_tax=cap_tax,
        )
        annual_tax_by_link = annual_tax_by_link.at[:, link].set(tax)

    year_index = month // 12
    cap_year_index = min(year_index, plan.salt.cap_by_year.shape[1] - 1)
    for link in range(link_count):
        if not bool(plan.salt.link_active[link]):
            continue
        profile = int(plan.tax.link_profile[link])
        state_tax_total = annual_tax_by_link @ jnp.asarray(plan.salt.contributing_mask[link].astype(np.float64))
        salt_total = property_tax_ytd[profile] + state_tax_total
        salt_deduction = jnp.minimum(salt_total, float(plan.salt.cap_by_year[link, cap_year_index]))
        mid, itemized, ord_taxable, cap_taxable, ord_tax, cap_tax = _compute_tax_for_link(
            plan,
            ordinary_ytd,
            capital_gain_ytd,
            recapture_section_1250_ytd,
            liabilities,
            link=link,
            salt_deduction=salt_deduction,
            rollout_count=rollout_count,
        )
        tax, tax_liability = _write_tax_link_buffers(
            plan,
            buffers,
            ordinary_ytd,
            capital_gain_ytd,
            tax_liability,
            link=link,
            month=month,
            active=active,
            standard_deduction=float(plan.tax.link_standard_deduction[link]),
            mortgage_interest_deduction=mid,
            salt_deduction=salt_deduction,
            itemized_deduction=itemized,
            ordinary_taxable=ord_taxable,
            capital_taxable=cap_taxable,
            ordinary_tax=ord_tax,
            capital_tax=cap_tax,
        )
        annual_tax_by_link = annual_tax_by_link.at[:, link].set(tax)

    # Year-end YTD resets for active rollouts.
    keep = ~active
    for profile in range(ordinary_ytd.shape[0]):
        ordinary_ytd = ordinary_ytd.at[profile].set(ordinary_ytd[profile] * keep)
        gain_profile = int(plan.tax_profile_capital_gain_index[profile])
        ltcg_active = active & capital_gain_active[gain_profile, CapitalGainClassification.LONG_TERM]
        stcg_active = active & capital_gain_active[gain_profile, CapitalGainClassification.SHORT_TERM]
        capital_gain_ytd = capital_gain_ytd.at[gain_profile, CapitalGainClassification.LONG_TERM].set(
            jnp.where(ltcg_active, 0.0, capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM])
        )
        capital_gain_ytd = capital_gain_ytd.at[gain_profile, CapitalGainClassification.SHORT_TERM].set(
            jnp.where(stcg_active, 0.0, capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM])
        )
    liabilities = replace(
        liabilities,
        interest_ytd=liabilities.interest_ytd * keep,
        rental_interest_ytd=liabilities.rental_interest_ytd * keep,
    )
    property_tax_ytd = property_tax_ytd * keep
    recapture_section_1250_ytd = recapture_section_1250_ytd * keep

    created_slots = np.flatnonzero(plan.tax_liabilities.year_end_month == month)
    buffers.tax_liability_changes.record(
        snapshot_month=month + 1,
        slots=created_slots,
        amount=np.asarray(tax_liability.amount),
        active=np.asarray(tax_liability.active),
    )
    return (
        ordinary_ytd,
        capital_gain_ytd,
        capital_loss_carryforward,
        liabilities,
        property_tax_ytd,
        recapture_section_1250_ytd,
        property_depreciation_ytd,
        tax_liability,
    )


def _settle_tax_liabilities_for_profile_year(
    plan: CompiledSimulation,
    tax_liability: TaxLiabilityState,
    *,
    profile_index: int,
    year_end_month: int,
    settlement_amount: np.ndarray,
    active: np.ndarray,
) -> TaxLiabilityState:
    """Port of `phases._settle_tax_liabilities_for_profile_year`: pro-rata pay down a year's slots.

    Computed in float64 (see `_actual_tax_for_profile_year`) so a full true-up zeros the liability.
    """
    slots = np.flatnonzero(
        (plan.tax_liabilities.profile_index == profile_index) & (plan.tax_liabilities.year_end_month == year_end_month)
    )
    if slots.size == 0:
        return tax_liability
    slot_amounts = np.asarray(tax_liability.amount[slots], dtype=np.float64)
    eligible = np.where(np.asarray(tax_liability.active[slots]), slot_amounts, 0.0)
    outstanding = eligible.sum(axis=0)
    settlement = np.where(active, settlement_amount, 0.0)
    weights = np.divide(eligible, outstanding[None, :], out=np.zeros_like(eligible), where=outstanding[None, :] > 0.0)
    settled = np.minimum(eligible, weights * settlement[None, :])
    return replace(
        tax_liability, amount=tax_liability.amount.at[slots].set(jnp.asarray(np.maximum(0.0, slot_amounts - settled)))
    )


def _apply_tax_settlements(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    tax_liability: TaxLiabilityState,
    *,
    month: int,
    candidate: np.ndarray,
    candidate_year_end: np.ndarray,
    payment_failed: np.ndarray,
) -> TaxLiabilityState:
    """Port of `phases._apply_tax_settlements`: record + apply true-up settlements per profile-year."""
    for profile in range(candidate.shape[0]):
        active = (candidate[profile] > 0.0) & ~payment_failed[profile]
        if not bool(active.any()):
            continue
        buffers.taxes.settlement_active[month, profile] = active
        buffers.taxes.settlement_amount[month, profile] = np.where(active, candidate[profile], 0.0)
        buffers.taxes.settlement_year_end_month[month, profile] = np.where(active, candidate_year_end[profile], NO_CODE)
        for year_end_month in np.unique(candidate_year_end[profile][active]):
            if int(year_end_month) < 0:
                continue
            year_active = active & (candidate_year_end[profile] == int(year_end_month))
            tax_liability = _settle_tax_liabilities_for_profile_year(
                plan,
                tax_liability,
                profile_index=profile,
                year_end_month=int(year_end_month),
                settlement_amount=candidate[profile],
                active=year_active,
            )
            settled_slots = np.flatnonzero(
                (plan.tax_liabilities.profile_index == profile)
                & (plan.tax_liabilities.year_end_month == int(year_end_month))
            )
            buffers.tax_liability_changes.record(
                snapshot_month=month + 1,
                slots=settled_slots,
                amount=np.asarray(tax_liability.amount),
                active=np.asarray(tax_liability.active),
            )
    return tax_liability

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
`rel=1e-9` / `abs=1e-6`, which float32 cannot meet on $10k-$200k values; those JAX variants fail on
precision alone, not logic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import jax
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
    LifecycleKind,
    ObligationSource,
    PrivateEquityDispositionKind,
    PrivateEquityOpportunityOutcome,
)
from augur.sim.tax import net_capital_gains_with_carryforward
from augur.sim.tensor_fifo import lot_order_for_pool

SECTION_121_LOOKBACK_MONTHS = 60
SECTION_121_MIN_QUALIFYING_MONTHS = 24


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
    # Tax YTD buckets the year-end pass reads; property_tax_ytd is fed by property-tax settlement,
    # property_depreciation_ytd by the depreciation accrual, recapture by property sale (not yet ported).
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
    property_building_basis = jnp.asarray(np.broadcast_to(plan.property_building_basis[:, None], (p.property_count, r)))
    property_cumulative_depreciation = jnp.zeros((p.property_count, r))
    property_owner_occupied_months = jnp.zeros((p.property_count, r), dtype=jnp.int32)
    # Per-agent (rollout-independent) primary-residence assignment; mutated in place by events.
    agent_primary_residence_property = np.array(plan.initial_primary_residence_property_index, dtype=np.int64)
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

        _apply_primary_residence_events(plan, buffers, agent_primary_residence_property, failed, month)
        lifecycle_state = _apply_lifecycle_events(
            plan,
            buffers,
            _LifecycleState(
                cash=cash,
                property_active=property_active,
                property_rented_fraction=property_rented_fraction,
                property_building_basis=property_building_basis,
                liabilities=liabilities,
                recapture_section_1250_ytd=recapture_section_1250_ytd,
                capital_gain_active=capital_gain_active,
                capital_gain_ytd=capital_gain_ytd,
            ),
            property_cumulative_depreciation,
            property_owner_occupied_months,
            agent_primary_residence_property,
            external_values,
            failed,
            month,
        )
        cash = lifecycle_state.cash
        property_active = lifecycle_state.property_active
        property_rented_fraction = lifecycle_state.property_rented_fraction
        property_building_basis = lifecycle_state.property_building_basis
        liabilities = lifecycle_state.liabilities
        recapture_section_1250_ytd = lifecycle_state.recapture_section_1250_ytd
        capital_gain_active = lifecycle_state.capital_gain_active
        capital_gain_ytd = lifecycle_state.capital_gain_ytd

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

        # Owner-occupied-month counter (§121) then §168 depreciation accrual — both before the
        # year-end tax pass so December's depreciation lands in the Schedule-E YTD it reads.
        property_owner_occupied_months = _apply_owner_occupied_month(
            plan,
            property_active,
            property_rented_fraction,
            agent_primary_residence_property,
            property_owner_occupied_months,
            failed,
        )
        property_cumulative_depreciation, property_depreciation_ytd = _apply_depreciation_accrual(
            property_active,
            property_rented_fraction,
            property_building_basis,
            property_cumulative_depreciation,
            property_depreciation_ytd,
            failed,
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
        buffers.state.property_cumulative_depreciation_state[month + 1] = np.asarray(property_cumulative_depreciation)
        buffers.state.property_owner_occupied_months_state[month + 1] = np.asarray(property_owner_occupied_months)
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


def _scatter_rows(target: jnp.ndarray, indices: jnp.ndarray, values: jnp.ndarray) -> jnp.ndarray:
    """Sentinel-aware segment scatter-add: add `values[s]` into `target[indices[s]]`, ignoring
    rows where `indices[s] < 0`. Duplicate indices accumulate. Branch-free (no per-row Python
    loop / `if idx >= 0`): a `-1` index is redirected to a padding row that is then sliced off."""
    dump = target.shape[0]
    padded = jnp.concatenate([target, jnp.zeros((1, *target.shape[1:]), target.dtype)], axis=0)
    idx = jnp.where(indices < 0, dump, indices)
    return padded.at[idx].add(values)[:dump]


def _np_gather(arr: np.ndarray, idx: np.ndarray, fill: float) -> np.ndarray:
    """Host-side gather tolerating an empty source array (returns `fill` for every slot when the
    plan array has no rows, e.g. a scenario with no properties / liabilities / tax profiles)."""
    if arr.shape[0] == 0:
        return np.full(idx.shape, fill, dtype=arr.dtype)
    return np.asarray(arr[idx])


def _gather_rows(source: jnp.ndarray, idx: jnp.ndarray) -> jnp.ndarray:
    """Gather `source[idx[s]]` into `(slots, rollouts)`, tolerating an empty source (`idx` is
    expected pre-clamped to valid rows; rows for inapplicable slots are masked off by the caller).
    A 0-row source (e.g. a scenario with no properties/liabilities) yields zeros."""
    if source.shape[0] == 0:
        return jnp.zeros((idx.shape[0], *source.shape[1:]), source.dtype)
    return source[idx]


def _amount_values_vec(
    amount_kind: jnp.ndarray,
    amount_fixed: jnp.ndarray,
    amount_base: jnp.ndarray,
    amount_series: jnp.ndarray,
    amount_base_month: jnp.ndarray,
    amount_period: jnp.ndarray,
    external_values: jnp.ndarray,
    month: jnp.ndarray,
    rollout_count: int,
) -> jnp.ndarray:
    """`_amount_values` vectorized over slots (branch-free): returns `(slots, rollouts)`.

    The series path is computed for every slot and selected against the fixed amount by the
    `AMOUNT_FIXED` mask; `-1` series / non-positive periods are sanitized to safe indices so the
    (unused) series math never indexes out of range or divides by zero on fixed slots.
    """
    if external_values.shape[0] == 0:
        # No exogenous series in this scenario, so every amount is necessarily fixed; skip the
        # series gather (it would index a size-0 axis). `shape[0]` is static under jit.
        return jnp.broadcast_to(amount_fixed[:, None], (amount_kind.shape[0], rollout_count))
    safe_period = jnp.where(amount_period > 0, amount_period, 1)
    reset_month = amount_base_month + ((month - amount_base_month) // safe_period) * safe_period
    safe_series = jnp.where(amount_series >= 0, amount_series, 0)
    rows = jnp.arange(rollout_count)
    base_level = external_values[safe_series[:, None], rows[None, :], amount_base_month[:, None]]
    reset_level = external_values[safe_series[:, None], rows[None, :], reset_month[:, None]]
    series_amount = amount_base[:, None] * reset_level / base_level
    return jnp.where((amount_kind == AMOUNT_FIXED)[:, None], amount_fixed[:, None], series_amount)


@jax.jit
def _transfers_jit(
    cause: jnp.ndarray,
    amount_kind: jnp.ndarray,
    amount_fixed: jnp.ndarray,
    amount_base: jnp.ndarray,
    amount_series: jnp.ndarray,
    amount_base_month: jnp.ndarray,
    amount_period: jnp.ndarray,
    from_slot: jnp.ndarray,
    to_slot: jnp.ndarray,
    income_profile: jnp.ndarray,
    deduction_profile: jnp.ndarray,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Branch-free, jit-compiled scheduled-transfer step (all slots vectorized; `month` traced)."""
    rollout_count = cash.shape[1]
    fire = (cause >= 0)[:, None] & active[None, :]  # (slots, rollouts)
    raw = _amount_values_vec(
        amount_kind,
        amount_fixed,
        amount_base,
        amount_series,
        amount_base_month,
        amount_period,
        external_values,
        month,
        rollout_count,
    )
    amounts = jnp.where(fire, raw, 0.0)
    cash = _scatter_rows(cash, from_slot, -amounts)
    cash = _scatter_rows(cash, to_slot, amounts)
    ordinary_ytd = _scatter_rows(ordinary_ytd, income_profile, amounts)
    ordinary_ytd = _scatter_rows(ordinary_ytd, deduction_profile, -amounts)
    return cash, ordinary_ytd, fire, amounts


def _apply_scheduled_transfers(
    transfers: TransferCompileOutput,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Functional port of `phases._apply_scheduled_transfers` (branch-free, jit-compiled core)."""
    # Annotated local: `jax.jit` types its wrapped callable as returning Any (mypy no-any-return).
    out: tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray] = _transfers_jit(
        jnp.asarray(transfers.cause[month]),
        jnp.asarray(transfers.amount_kind[month]),
        jnp.asarray(transfers.amount_fixed[month]),
        jnp.asarray(transfers.amount_base[month]),
        jnp.asarray(transfers.amount_series[month]),
        jnp.asarray(transfers.amount_base_month[month]),
        jnp.asarray(transfers.amount_period[month]),
        jnp.asarray(transfers.from_slot[month]),
        jnp.asarray(transfers.to_slot[month]),
        jnp.asarray(transfers.income_profile[month]),
        jnp.asarray(transfers.deduction_profile[month]),
        cash,
        ordinary_ytd,
        active,
        external_values,
        jnp.asarray(month),
    )
    return out


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
    """Port of `phases._record_capital_gains`: classify each lot's gain long/short and accrue.

    Branch-free: the per-lot long/short split is a static `(L,)` boolean mask (holding period vs
    the lot's purchase month), so the whole `[2, R]` classification block is one masked sum/any —
    no per-lot scatter loop, no data-dependent branching. The only Python loop is over the
    statically-known capital-gain profiles matching `agent_code`.
    """
    long_mask = jnp.asarray(month - plan.lot_purchase_month >= 12)  # (L,)
    masks = jnp.stack([long_mask, ~long_mask])  # (2, L), rows ordered LONG_TERM=0, SHORT_TERM=1
    sold = sold_units > 0.0  # (R, L)
    # einsum over lots: (2, L) x (R, L) -> (2, R) per-classification gain sums and activity flags.
    gains_by_class = jnp.einsum("cl,rl->cr", masks.astype(gains.dtype), gains)
    active_by_class = (masks[:, None, :] & sold[None, :, :]).any(axis=2)  # (2, R)
    for profile in np.flatnonzero(plan.capital_gain_agent_codes == agent_code).tolist():
        capital_gain_active = capital_gain_active.at[profile].set(capital_gain_active[profile] | active_by_class)
        capital_gain_ytd = capital_gain_ytd.at[profile].add(gains_by_class)
    return capital_gain_active, capital_gain_ytd


@jax.jit
def _obligation_accruals_jit(
    kind: jnp.ndarray,
    valid_slot: jnp.ndarray,
    amount_kind: jnp.ndarray,
    amount_fixed: jnp.ndarray,
    amount_base: jnp.ndarray,
    amount_series: jnp.ndarray,
    amount_base_month: jnp.ndarray,
    amount_period: jnp.ndarray,
    prop_idx: jnp.ndarray,
    pt_amount: jnp.ndarray,
    pt_prop_month: jnp.ndarray,
    liab_idx: jnp.ndarray,
    mort_rate: jnp.ndarray,
    mort_prop_month: jnp.ndarray,
    est_quarterly: jnp.ndarray,
    est_prior: jnp.ndarray,
    trueup_sel: jnp.ndarray,
    property_active: jnp.ndarray,
    liab_principal: jnp.ndarray,
    liab_monthly: jnp.ndarray,
    liab_active: jnp.ndarray,
    taxliab_active: jnp.ndarray,
    taxliab_amount: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Branch-free obligation accrual: every source kind's `(slots, rollouts)` due amount is computed,
    then selected by `kind`. Per-slot static data (rates, indices, the true-up selection matrix) is
    precomputed host-side; only the runtime state gathers (`property_active`, liability principal,
    tax-liability balances) are traced."""
    rollout_count = active.shape[0]
    k = kind[:, None]
    configured = _amount_values_vec(
        amount_kind,
        amount_fixed,
        amount_base,
        amount_series,
        amount_base_month,
        amount_period,
        external_values,
        month,
        rollout_count,
    )
    property_tax = jnp.broadcast_to(pt_amount[:, None], configured.shape)
    property_mask = _gather_rows(property_active, prop_idx) & (pt_prop_month[:, None] < month)
    principal = _gather_rows(liab_principal, liab_idx)
    mortgage = jnp.minimum(_gather_rows(liab_monthly, liab_idx), principal + principal * mort_rate[:, None] / 12.0)
    mortgage_mask = _gather_rows(liab_active, liab_idx) & (principal > 0.0) & (mort_prop_month[:, None] < month)
    estimated = jnp.broadcast_to(est_quarterly[:, None], configured.shape)
    actual = trueup_sel @ jnp.where(taxliab_active, taxliab_amount, 0.0)  # (slots, rollouts)
    safe_harbor = jnp.minimum(est_prior[:, None], actual)
    q4 = jnp.maximum(safe_harbor - est_prior[:, None] * 0.75, 0.0)
    true_up = jnp.maximum(actual - safe_harbor, 0.0)

    amount = jnp.select(
        [
            k == ObligationSource.CONFIGURED_OBLIGATION,
            k == ObligationSource.PROPERTY_TAX,
            k == ObligationSource.MORTGAGE_PAYMENT,
            k == ObligationSource.ESTIMATED_TAX,
            k == ObligationSource.ESTIMATED_TAX_Q4,
            k == ObligationSource.TAX_TRUE_UP,
        ],
        [configured, property_tax, mortgage, estimated, q4, true_up],
        default=0.0,
    )
    kind_mask = jnp.select(
        [k == ObligationSource.PROPERTY_TAX, k == ObligationSource.MORTGAGE_PAYMENT],
        [property_mask, mortgage_mask],
        default=True,
    )
    slot_active = valid_slot[:, None] & active[None, :] & kind_mask & (amount > 0.0)
    return slot_active, jnp.where(slot_active, amount, 0.0)


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
    """Functional port of `phases._apply_obligation_accruals` (branch-free, jit-compiled core).

    Per-slot static plan data and the true-up (profile, year-end) selection matrix are resolved
    host-side for this month, then the jitted core computes/selects every source kind vectorized.
    """
    ob = plan.obligations
    props = plan.properties
    liabs = plan.liabilities
    kind = ob.source_kind[month]
    src = ob.source_index[month]
    valid_slot = (ob.cause[month] >= 0) & (kind >= 0)
    # All plan arrays indexed below (properties, liabilities, tax profiles) may be empty when a
    # scenario has none of that entity, so every host-side gather goes through `_np_gather`; the
    # results for inapplicable slots are masked off by `kind` in the jitted core regardless.
    # PROPERTY_TAX: rate = obligation's amount_fixed (NaN -> location reference rate); monthly due =
    # ad-valorem on assessed value + flat special assessment / 12.
    prop_idx = np.where(kind == ObligationSource.PROPERTY_TAX, src, 0)
    pt_fixed_rate = ob.amount_fixed[month]
    pt_rate = np.where(np.isnan(pt_fixed_rate), _np_gather(props.location_tax_rate, prop_idx, 0.0), pt_fixed_rate)
    pt_amount = (
        _np_gather(props.initial_assessed_value, prop_idx, 0.0) * pt_rate / 12.0
        + _np_gather(props.special_assessment_annual_usd, prop_idx, 0.0) / 12.0
    )
    pt_prop_month = _np_gather(props.month, prop_idx, 0)
    # MORTGAGE_PAYMENT: indexed by liability; gated on the liability's property purchase month.
    liab_idx = np.where(kind == ObligationSource.MORTGAGE_PAYMENT, src, 0)
    liab_property_slot = _np_gather(liabs.property_slot, liab_idx, -1)
    mort_prop_month = _np_gather(props.month, np.where(liab_property_slot >= 0, liab_property_slot, 0), 0)
    mort_rate = _np_gather(liabs.annual_rate, liab_idx, 0.0)
    # ESTIMATED_TAX*: indexed by tax profile; the true-up reads the prior tax year's liabilities.
    prof_idx = np.where(kind >= ObligationSource.ESTIMATED_TAX, src, 0)
    est_prior = _np_gather(plan.tax.profile_prior_year_tax, prof_idx, 0.0)
    est_quarterly = est_prior / 4.0
    tax_year_end = (month // 12 - 1) * 12 + 11
    trueup_sel = (plan.tax_liabilities.profile_index[None, :] == prof_idx[:, None]) & (
        plan.tax_liabilities.year_end_month[None, :] == tax_year_end
    )
    # Annotated local: `jax.jit` types its wrapped callable as returning Any (mypy no-any-return).
    accrual: tuple[jnp.ndarray, jnp.ndarray] = _obligation_accruals_jit(
        jnp.asarray(kind),
        jnp.asarray(valid_slot),
        jnp.asarray(ob.amount_kind[month]),
        jnp.asarray(ob.amount_fixed[month]),
        jnp.asarray(ob.amount_base[month]),
        jnp.asarray(ob.amount_series[month]),
        jnp.asarray(ob.amount_base_month[month]),
        jnp.asarray(ob.amount_period[month]),
        jnp.asarray(prop_idx),
        jnp.asarray(pt_amount),
        jnp.asarray(pt_prop_month),
        jnp.asarray(liab_idx),
        jnp.asarray(mort_rate),
        jnp.asarray(mort_prop_month),
        jnp.asarray(est_quarterly),
        jnp.asarray(est_prior),
        jnp.asarray(trueup_sel.astype(np.float64)),
        property_active,
        liabilities.principal,
        liabilities.monthly_payment,
        liabilities.active,
        tax_liability.active,
        tax_liability.amount,
        active,
        external_values,
        jnp.asarray(month),
    )
    return accrual


@jax.jit
def _obligation_group_funded_jit(
    group_matrix: jnp.ndarray,
    from_slot: jnp.ndarray,
    cash: jnp.ndarray,
    accrual_active: jnp.ndarray,
    accrual_due: jnp.ndarray,
) -> jnp.ndarray:
    """Branch-free funding check: each obligation group (same agent + from-account) is funded for a
    rollout iff that account's cash covers the group's total due. The per-slot group is encoded as
    a static `(slots, slots)` membership matrix, so the group sums are one matmul."""
    due_masked = jnp.where(accrual_active, accrual_due, 0.0)  # (slots, rollouts)
    group_due = group_matrix.astype(due_masked.dtype) @ due_masked  # (slots, rollouts)
    cash_padded = jnp.concatenate([cash, jnp.zeros((1, cash.shape[1]), cash.dtype)], axis=0)
    available = cash_padded[jnp.where(from_slot < 0, cash.shape[0], from_slot)]  # (slots, rollouts), -1 -> 0
    return accrual_active & (available >= group_due - 1e-9)


def _obligation_group_funded(
    obligations: ObligationCompileOutput,
    cash: jnp.ndarray,
    accrual_active: jnp.ndarray,
    accrual_due: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> jnp.ndarray:
    """Port of `phases._obligation_group_funded` (branch-free, jit-compiled)."""
    agent_row = obligations.agent[month]
    from_row = obligations.from_slot[month]
    # Group membership is static plan data: slots i, j share a group iff same agent and from-account.
    group_matrix = (agent_row[:, None] == agent_row[None, :]) & (from_row[:, None] == from_row[None, :])
    # Annotated local: `jax.jit` types its wrapped callable as returning Any (mypy no-any-return).
    funded: jnp.ndarray = _obligation_group_funded_jit(
        jnp.asarray(group_matrix), jnp.asarray(from_row), cash, accrual_active, accrual_due
    )
    return funded


_ESTIMATED_TAX_KINDS = (ObligationSource.ESTIMATED_TAX, ObligationSource.ESTIMATED_TAX_Q4, ObligationSource.TAX_TRUE_UP)


@jax.jit
def _settlement_core_jit(
    from_slot: jnp.ndarray,
    to_slot: jnp.ndarray,
    deduction_profile: jnp.ndarray,
    deductible_fraction: jnp.ndarray,
    property_tax_profile: jnp.ndarray,
    property_slot_idx: jnp.ndarray,
    has_property_slot: jnp.ndarray,
    has_property_tax_profile: jnp.ndarray,
    has_deduction: jnp.ndarray,
    accrual_active: jnp.ndarray,
    accrual_due: jnp.ndarray,
    funded: jnp.ndarray,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    property_tax_ytd: jnp.ndarray,
    property_rented_fraction: jnp.ndarray,
    failed: jnp.ndarray,
    failed_month: jnp.ndarray,
    month: jnp.ndarray,
) -> tuple[
    jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray
]:
    """Branch-free core of obligation settlement: per-slot pay/fail, the funded cash move, the
    property-tax owner-share YTD accumulation, and the Schedule-E/itemized deduction — all
    vectorized over slots (duplicate from/to/profile indices accumulate via `_scatter_rows`).

    Failure ordering is month-stable: every slot that fails this month would stamp the same
    `month`, so the per-rollout first-failure month is `month` iff any slot fails and it had not
    failed before. Mortgage liability updates and tax settlement are handled by the caller.
    """
    paid = accrual_active & funded
    slot_failed = accrual_active & ~funded
    paid_amount = jnp.where(paid, accrual_due, 0.0)
    cash = _scatter_rows(cash, from_slot, -paid_amount)
    cash = _scatter_rows(cash, to_slot, paid_amount)
    rented = _gather_rows(property_rented_fraction, property_slot_idx)  # (slots, rollouts)
    property_tax_ytd = _scatter_rows(
        property_tax_ytd,
        property_tax_profile,
        jnp.where(has_property_tax_profile[:, None], paid_amount * (1.0 - rented), 0.0),
    )
    deductible = jnp.where(has_property_slot[:, None], rented, deductible_fraction[:, None])
    ordinary_ytd = _scatter_rows(
        ordinary_ytd, deduction_profile, jnp.where(has_deduction[:, None], -paid_amount * deductible, 0.0)
    )
    shortfall = jnp.where(slot_failed, accrual_due, 0.0)
    failed_this = slot_failed.any(axis=0)
    failed_month = jnp.where(failed_this & (failed_month < 0), month, failed_month)
    failed = failed | failed_this
    return paid, paid_amount, cash, ordinary_ytd, property_tax_ytd, shortfall, slot_failed, failed, failed_month


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
    # Vectorized core: the per-slot cash move + Schedule-E/SALT deductions + failure tracking,
    # branch-free over all slots (duplicate from/to/profile indices accumulate via `_scatter_rows`).
    property_slot = obligations.property_slot[month]
    # Annotated local: `jax.jit` types its wrapped callable as returning Any (mypy no-any-return).
    core: tuple[
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
        jnp.ndarray,
    ] = _settlement_core_jit(
        jnp.asarray(obligations.from_slot[month]),
        jnp.asarray(obligations.to_slot[month]),
        jnp.asarray(obligations.deduction_profile[month]),
        jnp.asarray(obligations.deductible_fraction[month]),
        jnp.asarray(obligations.property_tax_profile[month]),
        jnp.asarray(np.where(property_slot < 0, 0, property_slot)),
        jnp.asarray(property_slot >= 0),
        jnp.asarray(obligations.property_tax_profile[month] >= 0),
        jnp.asarray(obligations.deduction_profile[month] >= 0),
        accrual_active,
        accrual_due,
        funded,
        cash,
        ordinary_ytd,
        property_tax_ytd,
        property_rented_fraction,
        failed,
        failed_month,
        jnp.asarray(month),
    )
    paid, paid_buffer, cash, ordinary_ytd, property_tax_ytd, shortfall_buffer, failure_active, failed, failed_month = (
        core
    )

    # Mortgage payments (liability interest/principal split + buffers) and the tax-settlement
    # machinery stay per-(few)-slot for now; both need branch-free forms before the lax.scan stage.
    candidate = np.zeros((tax_profile_count, rollout_count), dtype=np.float64)
    candidate_year_end = np.full((tax_profile_count, rollout_count), NO_CODE, dtype=np.int64)
    payment_failed = np.zeros((tax_profile_count, rollout_count), dtype=bool)
    tax_year_end = (month // 12 - 1) * 12 + 11
    for slot in range(slot_count):
        source_kind = int(obligations.source_kind[month, slot])
        if source_kind == ObligationSource.MORTGAGE_PAYMENT:
            liabilities = _apply_mortgage_payment(
                plan,
                buffers,
                liabilities,
                property_rented_fraction,
                month=month,
                liability_slot=int(obligations.source_index[month, slot]),
                paid=paid[slot],
                amount=accrual_due[slot],
            )
        elif source_kind == ObligationSource.TAX_TRUE_UP:
            profile = int(obligations.source_index[month, slot])
            actual = _actual_tax_for_profile_year(
                plan, tax_liability, profile_index=profile, year_end_month=tax_year_end, rollout_count=rollout_count
            )
            active_slot_np = np.asarray(accrual_active[slot])
            candidate[profile] = np.where(active_slot_np, actual, candidate[profile])
            candidate_year_end[profile] = np.where(active_slot_np, tax_year_end, candidate_year_end[profile])
        if source_kind in _ESTIMATED_TAX_KINDS:
            profile = int(obligations.source_index[month, slot])
            payment_failed[profile] = payment_failed[profile] | np.asarray(failure_active[slot])

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
    upper_edges = jnp.asarray(upper[:count])
    bracket_rates = jnp.asarray(rate[:count])
    previous_upper = jnp.concatenate([jnp.zeros(1), upper_edges[:-1]])
    slice_top = jnp.minimum(amount[:, None], upper_edges[None, :])
    in_bracket = jnp.maximum(slice_top - previous_upper[None, :], 0.0)
    return (in_bracket * bracket_rates[None, :]).sum(axis=1)


def _apply_ltcg_brackets(
    ltcg_amount: jnp.ndarray, ordinary_taxable: jnp.ndarray, *, upper: np.ndarray, rate: np.ndarray, count: int
) -> jnp.ndarray:
    """Port of `phases._apply_ltcg_brackets`: LTCG stacked on top of ordinary taxable income."""
    if count <= 0:
        return jnp.zeros_like(ltcg_amount)
    upper_edges = jnp.asarray(upper[:count])
    bracket_rates = jnp.asarray(rate[:count])
    previous_upper = jnp.concatenate([jnp.zeros(1), upper_edges[:-1]])
    total_taxable = ordinary_taxable + ltcg_amount
    slice_top = jnp.minimum(total_taxable[:, None], upper_edges[None, :])
    slice_bottom = jnp.maximum(ordinary_taxable[:, None], previous_upper[None, :])
    in_bracket = jnp.maximum(slice_top - slice_bottom, 0.0)
    return (in_bracket * bracket_rates[None, :]).sum(axis=1)


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
    eligible = np.where(
        np.asarray(tax_liability.active[slots]), np.asarray(tax_liability.amount[slots], dtype=np.float64), 0.0
    )
    return np.asarray(eligible.sum(axis=0), dtype=np.float64)


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


def _apply_primary_residence_events(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    agent_primary_residence_property: np.ndarray,
    failed: jnp.ndarray,
    month: int,
) -> None:
    """Port of `phases._apply_primary_residence_events`: assign agents' primary residences.

    `agent_primary_residence_property` is a per-agent (rollout-independent) int array mutated in place.
    """
    starts = plan.primary_residence_events.month_starts
    if month + 1 >= starts.shape[0]:
        return
    begin, end = int(starts[month]), int(starts[month + 1])
    active = np.asarray(~failed)
    if begin == end or not active.any():
        return
    for event_index in range(begin, end):
        agent_slot = int(plan.primary_residence_events.agent_slot[event_index])
        agent_primary_residence_property[agent_slot] = int(plan.primary_residence_events.property_slot[event_index])
        buffers.primary_residence.fired[event_index] = active


@dataclass(frozen=True)
class _LifecycleState:
    """The engine state mutated by lifecycle events (FRACTION/CAPITAL_IMPROVEMENT/SALE), threaded
    as one bundle to keep the run-loop call site readable."""

    cash: jnp.ndarray
    property_active: jnp.ndarray
    property_rented_fraction: jnp.ndarray
    property_building_basis: jnp.ndarray
    liabilities: LiabilityState
    recapture_section_1250_ytd: jnp.ndarray
    capital_gain_active: jnp.ndarray
    capital_gain_ytd: jnp.ndarray


def _apply_lifecycle_events(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    state: _LifecycleState,
    property_cumulative_depreciation: jnp.ndarray,
    property_owner_occupied_months: jnp.ndarray,
    agent_primary_residence_property: np.ndarray,
    external_values: jnp.ndarray,
    failed: jnp.ndarray,
    month: int,
) -> _LifecycleState:
    """Functional port of `phases._apply_lifecycle_events` (FRACTION + CAPITAL_IMPROVEMENT + SALE)."""
    starts = plan.lifecycle_events.month_starts
    if month + 1 >= starts.shape[0]:
        return state
    begin, end = int(starts[month]), int(starts[month + 1])
    active_rollout = ~failed
    if begin == end or not bool(active_rollout.any()):
        return state
    le = plan.lifecycle_events
    for i in range(begin, end):
        prop = int(le.property_slot[i])
        kind = int(le.kind[i])
        active_property = active_rollout & state.property_active[prop]
        if not bool(active_property.any()):
            continue
        if kind == LifecycleKind.FRACTION:
            state = replace(
                state,
                property_rented_fraction=state.property_rented_fraction.at[prop].set(
                    jnp.where(active_property, float(le.rented_fraction[i]), state.property_rented_fraction[prop])
                ),
            )
        elif kind == LifecycleKind.CAPITAL_IMPROVEMENT:
            amount = float(le.amount[i])
            owner_cash_slot = int(plan.properties.buyer_slot[prop])
            cash = state.cash
            if owner_cash_slot >= 0:
                cash = cash.at[owner_cash_slot].add(jnp.where(active_property, -amount, 0.0))
            state = replace(
                state,
                cash=cash,
                property_building_basis=state.property_building_basis.at[prop].add(
                    jnp.where(active_property, amount, 0.0)
                ),
            )
        elif kind == LifecycleKind.SALE:
            state = _apply_property_sale(
                plan,
                buffers,
                state,
                property_cumulative_depreciation,
                property_owner_occupied_months,
                agent_primary_residence_property,
                external_values,
                month=month,
                event_index=i,
                prop=prop,
                closing_cost_pct=float(le.amount[i]),
                active_property=active_property,
            )
        buffers.lifecycle.fired[i] = np.asarray(active_property)
    return state


def _apply_property_sale(
    plan: CompiledSimulation,
    buffers: SimulationBuffers,
    state: _LifecycleState,
    property_cumulative_depreciation: jnp.ndarray,
    property_owner_occupied_months: jnp.ndarray,
    agent_primary_residence_property: np.ndarray,
    external_values: jnp.ndarray,
    *,
    month: int,
    event_index: int,
    prop: int,
    closing_cost_pct: float,
    active_property: jnp.ndarray,
) -> _LifecycleState:
    """Port of `phases._apply_property_sale`: market value, §1250 recapture, §121 exclusion, payoff."""
    rollout_count = state.cash.shape[1]
    series_idx = int(plan.property_home_value_series_index[prop])
    if series_idx < 0:
        raise RuntimeError("property sale reached the engine without a home-value series")
    market_value = (
        float(plan.properties.purchase_price[prop])
        * external_values[series_idx, :, month]
        / (external_values[series_idx, :, 0])
    )
    gross_proceeds = market_value * (1.0 - closing_cost_pct / 100.0)

    capex = state.property_building_basis[prop] - float(plan.property_building_basis[prop])
    cum_dep = property_cumulative_depreciation[prop]
    realized_gain = gross_proceeds - (float(plan.properties.purchase_price[prop]) + capex - cum_dep)
    recapture = jnp.minimum(jnp.maximum(realized_gain, 0.0), cum_dep)
    post_recapture_gain = jnp.maximum(realized_gain - recapture, 0.0)

    # §121 ownership/use test: owner-occupied months strictly inside the 60-month lookback window.
    # `property_owner_occupied_months` is the pre-this-month cumulative (incremented later), and the
    # lookback snapshot is the cumulative as of `month - 60`.
    current_cum = np.asarray(property_owner_occupied_months[prop])
    lookback = max(0, month - SECTION_121_LOOKBACK_MONTHS)
    snapshot_cum = buffers.state.property_owner_occupied_months_state[lookback, prop].astype(np.int64)
    qualifies = jnp.asarray((current_cum - snapshot_cum) >= SECTION_121_MIN_QUALIFYING_MONTHS)
    owner_profile = int(plan.property_owner_profile_index[prop])
    exclusion_cap = float(plan.tax.profile_section_121_exclusion[owner_profile]) if owner_profile >= 0 else 0.0
    section_121_exclusion = jnp.where(qualifies, jnp.minimum(post_recapture_gain, exclusion_cap), 0.0)
    ltcg = post_recapture_gain - section_121_exclusion

    # Pay off any outstanding mortgage on this property (all rollouts; matches the reference), then
    # net cash to the owner is gross minus payoff for the selling rollouts.
    liabilities = state.liabilities
    mortgage_payoff = jnp.zeros(rollout_count)
    for lia in range(plan.liabilities.property_slot.shape[0]):
        if int(plan.liabilities.property_slot[lia]) == prop:
            mortgage_payoff = mortgage_payoff + liabilities.principal[lia]
            liabilities = replace(
                liabilities,
                principal=liabilities.principal.at[lia].set(jnp.zeros(rollout_count)),
                active=liabilities.active.at[lia].set(jnp.zeros(rollout_count, dtype=bool)),
            )
    net_cash = gross_proceeds - mortgage_payoff
    cash = state.cash
    owner_cash_slot = int(plan.properties.buyer_slot[prop])
    if owner_cash_slot >= 0:
        cash = cash.at[owner_cash_slot].add(jnp.where(active_property, net_cash, 0.0))

    recapture_ytd = state.recapture_section_1250_ytd
    capital_gain_active = state.capital_gain_active
    capital_gain_ytd = state.capital_gain_ytd
    if owner_profile >= 0:
        recapture_ytd = recapture_ytd.at[owner_profile].add(jnp.where(active_property, recapture, 0.0))
        gain_profile = int(plan.tax_profile_capital_gain_index[owner_profile])
        if gain_profile >= 0:
            lt = CapitalGainClassification.LONG_TERM
            capital_gain_ytd = capital_gain_ytd.at[gain_profile, lt].add(jnp.where(active_property, ltcg, 0.0))
            capital_gain_active = capital_gain_active.at[gain_profile, lt].set(
                capital_gain_active[gain_profile, lt] | active_property
            )

    # Freeze the property for the selling rollouts; cumulative depreciation is preserved as record.
    property_active = state.property_active.at[prop].set(state.property_active[prop] & ~active_property)
    property_rented_fraction = state.property_rented_fraction.at[prop].set(
        jnp.where(active_property, 0.0, state.property_rented_fraction[prop])
    )
    property_building_basis = state.property_building_basis.at[prop].set(
        jnp.where(active_property, 0.0, state.property_building_basis[prop])
    )
    owner_agent_slot = int(plan.property_owner_agent_index[prop])
    if owner_agent_slot >= 0 and int(agent_primary_residence_property[owner_agent_slot]) == prop:
        agent_primary_residence_property[owner_agent_slot] = NO_CODE

    lifecycle = buffers.lifecycle
    lifecycle.sale_gross_proceeds[event_index] = np.asarray(jnp.where(active_property, gross_proceeds, 0.0))
    lifecycle.sale_mortgage_payoff[event_index] = np.asarray(jnp.where(active_property, mortgage_payoff, 0.0))
    lifecycle.sale_net_cash[event_index] = np.asarray(jnp.where(active_property, net_cash, 0.0))
    lifecycle.sale_realized_gain[event_index] = np.asarray(jnp.where(active_property, realized_gain, 0.0))
    lifecycle.sale_recapture[event_index] = np.asarray(jnp.where(active_property, recapture, 0.0))
    lifecycle.sale_section_121_exclusion[event_index] = np.asarray(
        jnp.where(active_property, section_121_exclusion, 0.0)
    )
    lifecycle.sale_long_term_gain[event_index] = np.asarray(jnp.where(active_property, ltcg, 0.0))

    return replace(
        state,
        cash=cash,
        property_active=property_active,
        property_rented_fraction=property_rented_fraction,
        property_building_basis=property_building_basis,
        liabilities=liabilities,
        recapture_section_1250_ytd=recapture_ytd,
        capital_gain_active=capital_gain_active,
        capital_gain_ytd=capital_gain_ytd,
    )


@jax.jit
def _owner_occupied_jit(
    property_active: jnp.ndarray,
    property_rented_fraction: jnp.ndarray,
    property_owner_occupied_months: jnp.ndarray,
    is_primary: jnp.ndarray,
    active: jnp.ndarray,
) -> jnp.ndarray:
    """Branch-free §121 owner-occupied-month counter: a property counts for a rollout this month iff
    it is active, the owner's primary residence (static `is_primary` mask), and not fully rented."""
    owner_occupied = active[None, :] & property_active & (property_rented_fraction < 1.0) & is_primary[:, None]
    return property_owner_occupied_months + owner_occupied.astype(property_owner_occupied_months.dtype)


def _apply_owner_occupied_month(
    plan: CompiledSimulation,
    property_active: jnp.ndarray,
    property_rented_fraction: jnp.ndarray,
    agent_primary_residence_property: np.ndarray,
    property_owner_occupied_months: jnp.ndarray,
    failed: jnp.ndarray,
) -> jnp.ndarray:
    """Port of `phases._apply_owner_occupied_month` (branch-free, jit-compiled)."""
    owner_agent = plan.property_owner_agent_index
    primary_of_owner = _np_gather(agent_primary_residence_property, np.where(owner_agent < 0, 0, owner_agent), NO_CODE)
    is_primary = (owner_agent >= 0) & (primary_of_owner == np.arange(owner_agent.shape[0]))
    occupied: jnp.ndarray = _owner_occupied_jit(
        property_active, property_rented_fraction, property_owner_occupied_months, jnp.asarray(is_primary), ~failed
    )
    return occupied


@jax.jit
def _apply_depreciation_accrual(
    property_active: jnp.ndarray,
    property_rented_fraction: jnp.ndarray,
    property_building_basis: jnp.ndarray,
    property_cumulative_depreciation: jnp.ndarray,
    property_depreciation_ytd: jnp.ndarray,
    failed: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Port of `phases._apply_depreciation_accrual`: §168 straight-line monthly depreciation,
    branch-free over all properties (one masked elementwise accrual)."""
    monthly_dep = jnp.where(
        (~failed)[None, :] & property_active, property_building_basis * property_rented_fraction / (27.5 * 12.0), 0.0
    )
    return property_cumulative_depreciation + monthly_dep, property_depreciation_ytd + monthly_dep

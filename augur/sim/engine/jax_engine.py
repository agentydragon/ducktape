"""JAX simulation engine — in-progress parity port of the NumPy engine.

The dense engine is being ported to JAX phase-by-phase (see <augur/plans/jax_migration.md>). Each
phase is a functional `jnp.at[]` translation of its NumPy counterpart in `phases.py`, verified
against the NumPy reference by a parity test at float32 tolerance. Selection is via
`sim_backend.current_backend()`.

`run_jax(plan, buffers)` fills the (already NumPy-allocated, zeroed) `buffers` from a JAX run.
Un-ported phases / branches are no-ops, so the JAX backend is correct only for scenarios that
exercise only the ported paths — which is exactly what the parity tests use, growing as more land.

Ported so far (in `_run_month_step` order):
- scheduled / recurring transfers;
- obligation accruals + settlement for the CONFIGURED_OBLIGATION source kind, with failure tracking
  and `_zero_failed_state`.

Not yet ported (no-op): property purchase/sale, asset sales, liquidity sales, PE tenders,
depreciation, owner-occupied months, tax accruals/settlements, lifecycle, primary residence, and the
mortgage / property-tax / estimated-tax obligation source kinds.
"""

from __future__ import annotations

import jax.numpy as jnp
import numpy as np

from augur.sim.buffers import SimulationBuffers
from augur.sim.codec.plan import CompiledSimulation
from augur.sim.compiler.helpers import AMOUNT_FIXED
from augur.sim.compiler.obligations import ObligationCompileOutput
from augur.sim.compiler.transfers import TransferCompileOutput
from augur.sim.enums import ObligationSource


def run_jax(plan: CompiledSimulation, buffers: SimulationBuffers) -> None:
    p = plan.slot_plan
    rollout_count = p.rollout_count

    cash = jnp.asarray(np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, rollout_count)))
    ordinary_ytd = jnp.zeros((p.tax_profile_count, rollout_count))
    failed = jnp.zeros(rollout_count, dtype=bool)
    failed_month = jnp.full(rollout_count, -1, dtype=jnp.int64)
    external_values = jnp.asarray(plan.external_values)

    # Snapshot index 0 is the pre-month-0 opening state (initial cash; all else zero, already set by
    # `_allocate_buffers`). `rollout_failed_month_state` is already NO_CODE there too.
    buffers.state.cash_state[0] = np.asarray(cash)

    for month in range(plan.horizon_months):
        active = ~failed

        cash, ordinary_ytd, transfer_active, transfer_amount = _apply_scheduled_transfers(
            plan.transfers, cash, ordinary_ytd, active, external_values, month, rollout_count
        )
        buffers.transfers.active[month] = np.asarray(transfer_active)
        buffers.transfers.amount[month] = np.asarray(transfer_amount)

        ob_active, ob_due = _apply_obligation_accruals(plan.obligations, active, external_values, month, rollout_count)
        funded = _obligation_group_funded(plan.obligations, cash, ob_active, ob_due, month, rollout_count)
        cash, ordinary_ytd, failed, failed_month, ob_paid, ob_shortfall, ob_failure = _apply_obligation_settlement(
            plan.obligations, cash, ordinary_ytd, failed, failed_month, ob_active, ob_due, funded, month
        )
        buffers.obligations.active[month] = np.asarray(ob_active)
        buffers.obligations.due[month] = np.asarray(ob_due)
        buffers.obligations.paid[month] = np.asarray(ob_paid)
        buffers.obligations.shortfall[month] = np.asarray(ob_shortfall)
        buffers.obligations.failure_active[month] = np.asarray(ob_failure)

        cash, ordinary_ytd = _zero_failed_state(cash, ordinary_ytd, failed)

        buffers.state.cash_state[month + 1] = np.asarray(cash)
        buffers.state.ordinary_state[month + 1] = np.asarray(ordinary_ytd)
        buffers.state.rollout_failed_state[month + 1] = np.asarray(failed)
        buffers.state.rollout_failed_month_state[month + 1] = np.asarray(failed_month)


def _zero_failed_state(
    cash: jnp.ndarray, ordinary_ytd: jnp.ndarray, failed: jnp.ndarray
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Port of `_zero_failed_state` for the currently-modelled fields (cash, ordinary income)."""
    keep = ~failed
    return cash * keep, ordinary_ytd * keep


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


def _apply_obligation_accruals(
    obligations: ObligationCompileOutput,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Functional port of `phases._apply_obligation_accruals` (CONFIGURED_OBLIGATION kind only)."""
    slot_count = obligations.cause.shape[1]
    accrual_active = jnp.zeros((slot_count, rollout_count), dtype=bool)
    accrual_due = jnp.zeros((slot_count, rollout_count))
    for slot in range(slot_count):
        if int(obligations.cause[month, slot]) < 0 or int(obligations.source_kind[month, slot]) < 0:
            continue
        if int(obligations.source_kind[month, slot]) != ObligationSource.CONFIGURED_OBLIGATION:
            continue  # other source kinds (mortgage / property-tax / estimated-tax) not yet ported
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
    obligations: ObligationCompileOutput,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    failed: jnp.ndarray,
    failed_month: jnp.ndarray,
    accrual_active: jnp.ndarray,
    accrual_due: jnp.ndarray,
    funded: jnp.ndarray,
    month: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Functional port of `phases._apply_obligation_settlement` (CONFIGURED_OBLIGATION kind only)."""
    slot_count = accrual_active.shape[0]
    paid_buffer = jnp.zeros_like(accrual_due)
    shortfall_buffer = jnp.zeros_like(accrual_due)
    failure_active = jnp.zeros_like(accrual_active)
    for slot in range(slot_count):
        if int(obligations.source_kind[month, slot]) != ObligationSource.CONFIGURED_OBLIGATION:
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
    return cash, ordinary_ytd, failed, failed_month, paid_buffer, shortfall_buffer, failure_active

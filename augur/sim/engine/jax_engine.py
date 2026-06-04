"""JAX simulation engine — in-progress parity port of the NumPy engine.

The dense engine is being ported to JAX phase-by-phase (see <augur/plans/jax_migration.md>). Each
phase is verified against the NumPy reference by a parity test at float32 tolerance; selection is via
`sim_backend.current_backend()`.

`run_jax(plan, buffers)` fills the (already NumPy-allocated, zeroed) `buffers` from a JAX run. Phases
not yet ported are no-ops, so the JAX backend is correct only for scenarios that exercise only the
ported phases — which is exactly what the parity tests use, growing as more phases land.

Ported so far:
- scheduled / recurring transfers (cash movement + taxable-income routing + the transfer event log).

Not yet ported (no-op): property purchase/sale, asset sales, obligations, liquidity, PE tenders,
depreciation, tax accruals/settlements, lifecycle, primary residence. Failure tracking is likewise
not modelled yet (those phases are what trigger it), so the ported scenarios must not fail.
"""

from __future__ import annotations

import jax.numpy as jnp
import numpy as np

from augur.sim.buffers import SimulationBuffers
from augur.sim.codec.plan import CompiledSimulation
from augur.sim.compiler.helpers import AMOUNT_FIXED
from augur.sim.compiler.transfers import TransferCompileOutput


def run_jax(plan: CompiledSimulation, buffers: SimulationBuffers) -> None:
    p = plan.slot_plan
    rollout_count = p.rollout_count

    cash = jnp.asarray(np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, rollout_count)))
    ordinary_ytd = jnp.zeros((p.tax_profile_count, rollout_count))
    external_values = jnp.asarray(plan.external_values)

    # Snapshot index 0 is the pre-month-0 opening state (initial cash; everything else zero, which
    # `_allocate_buffers` already set). Subsequent snapshots are end-of-month s-1 at index s.
    buffers.state.cash_state[0] = np.asarray(cash)

    for month in range(plan.horizon_months):
        cash, ordinary_ytd, transfer_active, transfer_amount = _apply_scheduled_transfers(
            plan.transfers, cash, ordinary_ytd, external_values, month, rollout_count
        )
        buffers.transfers.active[month] = np.asarray(transfer_active)
        buffers.transfers.amount[month] = np.asarray(transfer_amount)
        buffers.state.cash_state[month + 1] = np.asarray(cash)
        buffers.state.ordinary_state[month + 1] = np.asarray(ordinary_ytd)


def _apply_scheduled_transfers(
    transfers: TransferCompileOutput,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Functional JAX port of `phases._apply_scheduled_transfers` (no-failure case).

    Branching on `cause`/`from_slot`/… is host-side: those are static compiled plan data, so the
    per-slot Python loop and `int(...)` reads happen at trace time, not on traced values.
    """
    slot_count = transfers.cause.shape[1]
    transfer_active = jnp.zeros((slot_count, rollout_count), dtype=bool)
    transfer_amount = jnp.zeros((slot_count, rollout_count))
    for slot in range(slot_count):
        if int(transfers.cause[month, slot]) < 0:
            continue
        amount = _amount_values(transfers, external_values, month=month, slot=slot, rollout_count=rollout_count)
        transfer_active = transfer_active.at[slot].set(jnp.ones(rollout_count, dtype=bool))
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


def _amount_values(
    transfers: TransferCompileOutput, external_values: jnp.ndarray, *, month: int, slot: int, rollout_count: int
) -> jnp.ndarray:
    if int(transfers.amount_kind[month, slot]) == AMOUNT_FIXED:
        return jnp.full(rollout_count, float(transfers.amount_fixed[month, slot]))
    base = float(transfers.amount_base[month, slot])
    series_index = int(transfers.amount_series[month, slot])
    base_month = int(transfers.amount_base_month[month, slot])
    adjustment_period = int(transfers.amount_period[month, slot])
    reset_month = base_month + ((month - base_month) // adjustment_period) * adjustment_period
    base_level = external_values[series_index, :, base_month]
    reset_level = external_values[series_index, :, reset_month]
    return base * reset_level / base_level

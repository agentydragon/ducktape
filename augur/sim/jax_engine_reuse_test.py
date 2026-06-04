"""Engine-level test for the JAX scan program cache's config-value reuse contract.

`run_jax_scan` caches the compiled program by a content fingerprint of the plan that EXCLUDES the
swept numeric-config fields (tax brackets/rates/standard deduction, MID principal ratio, transfer
amounts, per-lot cost basis): those flow in as traced inputs. So two plans that differ only in those
values share one compiled program. These tests prove that reuse is CORRECT — after caching plan A's
program, running a plan B that perturbs a traced field reuses A's program yet still produces B's
(NumPy-reference) result. The suite-wide parity tests each run a single scenario, so they cannot catch
a swept field that is wrongly baked into the program (which would make plan B silently reuse plan A's
value).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import numpy as np
import pytest_bazel
from numpy.typing import NDArray

from augur.product.asset_key import SP500AssetKey
from augur.sim.buffers import SimulationBuffers
from augur.sim.compiler import compile_simulation
from augur.sim.compiler.plan import CompiledSimulation
from augur.sim.engine import _allocate_buffers, _allocate_current_state, _run_month_step, _snapshot_current_state
from augur.sim.engine.jax_engine import _plan_fingerprint, run_jax_scan
from augur.sim.external_series import materialize_external_series
from augur.sim.runtime import load_jurisdictions_for
from augur.sim.scenario import (
    Agent,
    FilingStatus,
    InitialAccountBalance,
    InitialLot,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    TaxProfile,
)


def _tax_scenario() -> Scenario:
    """W-2 income + a SINGLE filer with prior-year tax: the December year-end pass accrues a real
    federal + CA liability, so the income-tax bracket rates and the paycheck amount drive the output."""
    return Scenario(
        agents=[Agent(agent_id="payroll"), Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=35,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=120_000.0 / 12.0,
                income_category="ordinary",
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
                prior_year_tax_usd=15_000.0,
            )
        ],
        horizon_months=36,
    )


def _sale_scenario() -> Scenario:
    """A long-held SP500 lot sold mid-horizon: the realized capital gain depends on the lot cost basis."""
    return Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="alice_sp500",
                agent_id="alice",
                account_id="brokerage",
                asset=SP500AssetKey(),
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit_usd=80.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=3,
                cause_id="alice_sells_sp500",
                agent_id="alice",
                source_account_id="brokerage",
                asset=SP500AssetKey(),
                quantity=100.0,
                price_per_unit_usd=120.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[],
        horizon_months=6,
    )


def _compile(scenario: Scenario, *, rollout_count: int) -> CompiledSimulation:
    external_series = materialize_external_series(
        scenario.external_series,
        rollout_seeds=tuple(range(rollout_count)),
        horizon_months=int(scenario.horizon_months),
    )
    return compile_simulation(
        scenario,
        rollout_count=rollout_count,
        external_series=external_series,
        jurisdictions=load_jurisdictions_for(scenario),
        locations={},
    )


def _run_jax(plan: CompiledSimulation) -> SimulationBuffers:
    buffers = _allocate_buffers(plan)
    run_jax_scan(plan, buffers)
    return buffers


def _run_numpy(plan: CompiledSimulation) -> SimulationBuffers:
    buffers = _allocate_buffers(plan)
    current = _allocate_current_state(plan)
    _snapshot_current_state(buffers.state, current, snapshot_index=0)
    for month in range(plan.horizon_months):
        _run_month_step(plan, buffers, current, month)
    return buffers


def _assert_value_sweep_reuses(
    scenario: Scenario,
    perturb: Callable[[CompiledSimulation], CompiledSimulation],
    extract: Callable[[SimulationBuffers], NDArray[np.float64]],
) -> None:
    """Cache plan A, perturb a traced value into plan B, and assert B reuses A's program (same
    fingerprint) yet still matches the NumPy reference for B — and that the perturbation moved the
    output (so the reuse path isn't vacuously correct)."""
    plan_a = _compile(scenario, rollout_count=2)
    out_a = extract(_run_jax(plan_a)).copy()  # warms the program cache for this structure

    plan_b = perturb(plan_a)
    assert _plan_fingerprint(plan_b) == _plan_fingerprint(plan_a)  # B must reuse A's compiled program

    out_b_jax = extract(_run_jax(plan_b))
    out_b_numpy = extract(_run_numpy(plan_b))
    np.testing.assert_allclose(out_b_jax, out_b_numpy, rtol=1e-5, atol=1e-3)
    assert not np.allclose(out_b_jax, out_a)


def test_tax_rate_sweep_reuses_program_correctly() -> None:
    _assert_value_sweep_reuses(
        _tax_scenario(),
        lambda p: replace(p, tax=replace(p.tax, link_ordinary_rate=p.tax.link_ordinary_rate * 1.3)),
        lambda b: b.taxes.accrual_amount,
    )


def test_transfer_amount_sweep_reuses_program_correctly() -> None:
    # Bump the (fixed) paycheck amount; NaN entries (non-fixed slots) are left untouched.
    def perturb(p: CompiledSimulation) -> CompiledSimulation:
        fixed = p.transfers.amount_fixed
        return replace(p, transfers=replace(p.transfers, amount_fixed=np.where(np.isnan(fixed), fixed, fixed + 1_000.0)))

    _assert_value_sweep_reuses(_tax_scenario(), perturb, lambda b: b.taxes.accrual_amount)


def test_cost_basis_sweep_reuses_program_correctly() -> None:
    _assert_value_sweep_reuses(
        _sale_scenario(),
        lambda p: replace(p, lot_cost_basis_per_unit=p.lot_cost_basis_per_unit * 0.5),
        lambda b: b.state.capital_gain_state,
    )


def test_structural_change_busts_the_fingerprint() -> None:
    # A baked (non-traced) structural field IS fingerprinted, so changing it forces a recompile rather
    # than incorrect reuse. `link_ordinary_count` (active bracket count) is such a baked feature.
    plan_a = _compile(_tax_scenario(), rollout_count=2)
    plan_c = replace(plan_a, tax=replace(plan_a.tax, link_ordinary_count=plan_a.tax.link_ordinary_count + 1))
    assert _plan_fingerprint(plan_c) != _plan_fingerprint(plan_a)


if __name__ == "__main__":
    pytest_bazel.main()

"""Engine-level test for the JAX scan program cache's config-value reuse contract.

`run_jax_scan` caches the compiled program by a content fingerprint of the plan that EXCLUDES the
swept numeric-config fields (tax brackets/rates/standard deduction, the MID principal-ratio matrix):
those flow in as traced inputs. So two plans that differ only in those values share one compiled
program. This test proves that reuse is CORRECT — after caching plan A's program, running a plan B
that perturbs a traced tax field reuses A's program yet still produces B's (NumPy-reference) tax. The
suite-wide parity tests each run a single scenario, so they cannot catch a swept field that is wrongly
baked into the program (which would make plan B silently reuse plan A's value).
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest_bazel

from augur.sim.buffers import SimulationBuffers
from augur.sim.compiler import compile_simulation
from augur.sim.compiler.plan import CompiledSimulation
from augur.sim.engine import _allocate_buffers, _allocate_current_state, _run_month_step, _snapshot_current_state
from augur.sim.engine.jax_engine import _plan_fingerprint, run_jax_scan
from augur.sim.external_series import materialize_external_series
from augur.sim.runtime import load_jurisdictions_for
from augur.sim.scenario import Agent, FilingStatus, InitialAccountBalance, RecurringTransfer, Scenario, TaxProfile


def _tax_scenario() -> Scenario:
    """W-2 income + a SINGLE filer with prior-year tax: the December year-end pass accrues a real
    federal + CA liability, so the income-tax bracket rates actually drive the output."""
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


def test_tax_rate_sweep_reuses_program_correctly() -> None:
    plan_a = _compile(_tax_scenario(), rollout_count=2)
    accrual_a = _run_jax(plan_a).taxes.accrual_amount.copy()  # warms the program cache for this structure

    # Plan B: same structure, only the income-tax bracket rates scaled up. Rates are traced inputs, so
    # the fingerprint is unchanged and plan B must reuse plan A's compiled program.
    plan_b = replace(plan_a, tax=replace(plan_a.tax, link_ordinary_rate=plan_a.tax.link_ordinary_rate * 1.3))
    assert _plan_fingerprint(plan_b) == _plan_fingerprint(plan_a)

    accrual_b_jax = _run_jax(plan_b).taxes.accrual_amount
    accrual_b_numpy = _run_numpy(plan_b).taxes.accrual_amount

    # Reuse is correct: JAX (reusing A's program with B's traced rates) matches the NumPy reference for B,
    np.testing.assert_allclose(accrual_b_jax, accrual_b_numpy, rtol=1e-5, atol=1e-3)
    # and the higher rates actually changed the accrued tax (so the reuse path isn't vacuously correct).
    assert not np.allclose(accrual_b_jax, accrual_a)


def test_structural_change_busts_the_fingerprint() -> None:
    # A baked (non-traced) structural field IS fingerprinted, so changing it forces a recompile rather
    # than incorrect reuse. `link_ordinary_count` (active bracket count) is such a baked feature.
    plan_a = _compile(_tax_scenario(), rollout_count=2)
    plan_c = replace(plan_a, tax=replace(plan_a.tax, link_ordinary_count=plan_a.tax.link_ordinary_count + 1))
    assert _plan_fingerprint(plan_c) != _plan_fingerprint(plan_a)


if __name__ == "__main__":
    pytest_bazel.main()

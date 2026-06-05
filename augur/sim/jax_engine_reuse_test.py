"""Engine-level test: swept numeric config flows through the JAX scan program as TRACED input.

The tax brackets/rates/standard deduction, MID principal ratio, transfer amounts, per-lot cost basis,
initial balances, property basis and mortgage principal enter the compiled program as traced inputs
(see `_TracedConfig`), not as baked constants. Each test perturbs one such value and asserts the JAX
engine produces the perturbed plan's correct (NumPy-reference) result — and that the perturbation
actually moved the output. They guard against a swept field being wrongly baked into the program,
which the single-scenario parity tests cannot catch.
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
from augur.sim.engine.jax_engine import _program_impl, run_jax_scan
from augur.sim.external_series import materialize_external_series
from augur.sim.locations import Location
from augur.sim.runtime import load_jurisdictions_for
from augur.sim.scenario import (
    Agent,
    FilingStatus,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledPropertyPurchase,
    TaxProfile,
)

_SF = {
    "sf": Location(
        location_id="sf", display_name="SF", jurisdiction_ids=["federal_us"], annual_property_tax_rate=0.0118
    )
}


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


def _compile(scenario: Scenario, *, rollout_count: int, locations: dict[str, Location]) -> CompiledSimulation:
    external_series = materialize_external_series(
        scenario.external_series, rollout_seeds=tuple(range(rollout_count)), horizon_months=int(scenario.horizon_months)
    )
    return compile_simulation(
        scenario,
        rollout_count=rollout_count,
        external_series=external_series,
        jurisdictions=load_jurisdictions_for(scenario),
        locations=locations,
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


def _assert_value_sweep_correct(
    scenario: Scenario,
    perturb: Callable[[CompiledSimulation], CompiledSimulation],
    extract: Callable[[SimulationBuffers], NDArray[np.float64]],
    *,
    locations: dict[str, Location] | None = None,
) -> None:
    """Perturb one traced value into plan B and assert the JAX engine matches the NumPy reference for B
    — and that the perturbation moved the output (so the swept field isn't wrongly baked / ignored)."""
    plan_a = _compile(scenario, rollout_count=2, locations=locations or {})
    out_a = extract(_run_jax(plan_a)).copy()

    plan_b = perturb(plan_a)
    out_b_jax = extract(_run_jax(plan_b))
    out_b_numpy = extract(_run_numpy(plan_b))
    np.testing.assert_allclose(out_b_jax, out_b_numpy, rtol=1e-5, atol=1e-3)
    assert not np.allclose(out_b_jax, out_a)


def test_tax_rate_sweep_produces_correct_result() -> None:
    _assert_value_sweep_correct(
        _tax_scenario(),
        lambda p: replace(p, tax=replace(p.tax, link_ordinary_rate=p.tax.link_ordinary_rate * 1.3)),
        lambda b: b.taxes.accrual_amount,
    )


def test_transfer_amount_sweep_produces_correct_result() -> None:
    # Bump the (fixed) paycheck amount; NaN entries (non-fixed slots) are left untouched.
    def perturb(p: CompiledSimulation) -> CompiledSimulation:
        fixed = p.transfers.amount_fixed
        return replace(
            p, transfers=replace(p.transfers, amount_fixed=np.where(np.isnan(fixed), fixed, fixed + 1_000.0))
        )

    _assert_value_sweep_correct(_tax_scenario(), perturb, lambda b: b.taxes.accrual_amount)


def test_cost_basis_sweep_produces_correct_result() -> None:
    _assert_value_sweep_correct(
        _sale_scenario(),
        lambda p: replace(p, lot_cost_basis_per_unit=p.lot_cost_basis_per_unit * 0.5),
        lambda b: b.state.capital_gain_state,
    )


def test_initial_balance_sweep_produces_correct_result() -> None:
    _assert_value_sweep_correct(
        _tax_scenario(),
        lambda p: replace(p, cash_initial_balance=p.cash_initial_balance + 50_000.0),
        lambda b: b.state.cash_state,
    )


def _financed_purchase_scenario() -> Scenario:
    """A mortgage-financed home purchase: the property basis and the originated mortgage principal /
    payment drive the property + liability state."""
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="seller"), Agent(agent_id="lender")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=300_000.0),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="lender", account_id="checking", balance_usd=0.0),
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=0,
                cause_id="alice_buys_home",
                property_id="home",
                location_id="sf",
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="seller",
                purchase_price_usd=500_000.0,
                down_payment_usd=100_000.0,
                buyer_closing_cost_usd=0.0,
                rented_fraction=0.0,
                mortgage=MortgageFinancing(
                    liability_id="alice_mortgage",
                    lender_agent_id="lender",
                    principal_usd=400_000.0,
                    annual_interest_rate=0.06,
                    term_months=360,
                ),
            )
        ],
        tax_profiles=[],
        horizon_months=3,
    )


def test_property_basis_sweep_produces_correct_result() -> None:
    _assert_value_sweep_correct(
        _financed_purchase_scenario(),
        lambda p: replace(p, properties=replace(p.properties, adjusted_basis=p.properties.adjusted_basis * 1.2)),
        lambda b: b.state.property_basis_state,
        locations=_SF,
    )


def test_mortgage_principal_sweep_produces_correct_result() -> None:
    _assert_value_sweep_correct(
        _financed_purchase_scenario(),
        lambda p: replace(p, liabilities=replace(p.liabilities, principal=p.liabilities.principal * 1.1)),
        lambda b: b.state.liability_principal_state,
        locations=_SF,
    )


def test_native_cache_reuses_executable_across_structure_and_sweeps() -> None:
    """JAX's OWN compile cache (`_program_impl._cache_size()`) reuses the compiled executable: an
    identical-structure second call adds 0 compiles, a traced value/seed sweep adds 0, and a structural
    change adds exactly 1. This is what makes repeated `run_jax_scan` not recompile the scan program."""
    plan_a = _compile(_tax_scenario(), rollout_count=2, locations={})

    _run_jax(plan_a)
    base = _program_impl._cache_size()

    # Identical structure (same plan): zero additional compiles.
    _run_jax(plan_a)
    assert _program_impl._cache_size() == base

    # Traced value sweep (same structure, perturbed bracket rates): zero additional compiles.
    plan_b = replace(plan_a, tax=replace(plan_a.tax, link_ordinary_rate=plan_a.tax.link_ordinary_rate * 1.3))
    _run_jax(plan_b)
    assert _program_impl._cache_size() == base

    # Seed sweep (same structure, different rollout draws): zero additional compiles.
    plan_seed = _compile(_tax_scenario(), rollout_count=2, locations={})
    plan_seed = replace(plan_seed, external_values=plan_seed.external_values * 1.01)
    _run_jax(plan_seed)
    assert _program_impl._cache_size() == base

    # Structural change (different rollout_count -> different shapes & `SlotPlan`): exactly one more.
    plan_struct = _compile(_tax_scenario(), rollout_count=3, locations={})
    _run_jax(plan_struct)
    assert _program_impl._cache_size() == base + 1


if __name__ == "__main__":
    pytest_bazel.main()

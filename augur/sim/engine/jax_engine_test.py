"""Parity tests for the in-progress JAX engine port: JAX vs the NumPy reference.

Each test runs the same scenario through both backends and asserts the dense buffers match at
float32 tolerance. Scenarios here exercise only the phases ported so far; the suite grows as more
phases land.
"""

from __future__ import annotations

import numpy as np
import pytest_bazel

from augur.model.sim_backend import SimBackend, use_backend
from augur.sim.codec.plan import DenseSimulationResult
from augur.sim.external_series import materialize_external_series
from augur.sim.scenario import Agent, InitialAccountBalance, RecurringObligation, Scenario, ScheduledTransfer
from augur.sim.simulate import simulate_dense_with_external_series


def _simulate(scenario: Scenario, backend: SimBackend, *, rollout_count: int) -> DenseSimulationResult:
    external_series = materialize_external_series(
        scenario.external_series, rollout_seeds=tuple(range(rollout_count)), horizon_months=int(scenario.horizon_months)
    )
    with use_backend(backend):
        return simulate_dense_with_external_series(
            scenario, rollout_count=rollout_count, external_series=external_series, locations={}
        )


def _assert_engine_parity(scenario: Scenario, *, rollout_count: int = 4) -> None:
    numpy_result = _simulate(scenario, SimBackend.NUMPY, rollout_count=rollout_count)
    jax_result = _simulate(scenario, SimBackend.JAX, rollout_count=rollout_count)

    numpy_state, jax_state = numpy_result.buffers.state, jax_result.buffers.state
    for name in ("cash_state", "ordinary_state"):
        np.testing.assert_allclose(getattr(jax_state, name), getattr(numpy_state, name), rtol=1e-5, atol=1e-5)
    np.testing.assert_array_equal(jax_state.rollout_failed_state, numpy_state.rollout_failed_state)
    np.testing.assert_array_equal(jax_state.rollout_failed_month_state, numpy_state.rollout_failed_month_state)

    numpy_ob, jax_ob = numpy_result.buffers.obligations, jax_result.buffers.obligations
    for name in ("due", "paid", "shortfall"):
        np.testing.assert_allclose(getattr(jax_ob, name), getattr(numpy_ob, name), rtol=1e-5, atol=1e-5)
    np.testing.assert_array_equal(jax_ob.active, numpy_ob.active)
    np.testing.assert_array_equal(jax_ob.failure_active, numpy_ob.failure_active)

    np.testing.assert_allclose(
        jax_result.buffers.transfers.amount, numpy_result.buffers.transfers.amount, rtol=1e-5, atol=1e-5
    )
    np.testing.assert_array_equal(jax_result.buffers.transfers.active, numpy_result.buffers.transfers.active)


def test_scheduled_transfers_engine_parity() -> None:
    _assert_engine_parity(
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="bob")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=10.0),
                InitialAccountBalance(agent_id="bob", account_id="checking", balance_usd=20.0),
            ],
            scheduled_transfers=[
                ScheduledTransfer(
                    month=0,
                    cause_id="bob_gives_alice_5",
                    from_agent_id="bob",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount_usd=5.0,
                )
            ],
            tax_profiles=[],
            horizon_months=1,
        )
    )


def test_obligation_settlement_and_failure_engine_parity() -> None:
    # alice pays $40/mo rent from $100; she funds months 0 and 1, then can't cover month 2 — exercising
    # the paid path, the funding shortfall, failure tracking, and `_zero_failed_state` carrying forward.
    _assert_engine_parity(
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0),
                InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
            ],
            recurring_obligations=[
                RecurringObligation(
                    start_month=0,
                    obligation_id="rent",
                    obligation_type="outside_rent",
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="landlord",
                    to_account_id="checking",
                    amount_due_usd=40.0,
                )
            ],
            tax_profiles=[],
            horizon_months=4,
        )
    )


if __name__ == "__main__":
    pytest_bazel.main()

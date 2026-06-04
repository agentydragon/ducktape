"""Parity tests for the in-progress JAX engine port: JAX vs the NumPy reference.

Each test runs the same scenario through both backends and asserts the dense buffers match at
float32 tolerance. Scenarios here exercise only the phases ported so far (transfers); the suite
grows as more phases land.
"""

from __future__ import annotations

import numpy as np
import pytest_bazel

from augur.model.sim_backend import SimBackend, use_backend
from augur.sim.codec.plan import DenseSimulationResult
from augur.sim.external_series import materialize_external_series
from augur.sim.scenario import Agent, InitialAccountBalance, Scenario, ScheduledTransfer
from augur.sim.simulate import simulate_dense_with_external_series


def _transfers_only_scenario() -> Scenario:
    return Scenario(
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


def _simulate(scenario: Scenario, backend: SimBackend, *, rollout_count: int) -> DenseSimulationResult:
    external_series = materialize_external_series(
        scenario.external_series, rollout_seeds=tuple(range(rollout_count)), horizon_months=int(scenario.horizon_months)
    )
    with use_backend(backend):
        return simulate_dense_with_external_series(
            scenario, rollout_count=rollout_count, external_series=external_series, locations={}
        )


def test_scheduled_transfers_engine_parity() -> None:
    scenario = _transfers_only_scenario()
    numpy_result = _simulate(scenario, SimBackend.NUMPY, rollout_count=4)
    jax_result = _simulate(scenario, SimBackend.JAX, rollout_count=4)

    np.testing.assert_allclose(
        jax_result.buffers.state.cash_state, numpy_result.buffers.state.cash_state, rtol=1e-5, atol=1e-5
    )
    np.testing.assert_allclose(
        jax_result.buffers.state.ordinary_state, numpy_result.buffers.state.ordinary_state, rtol=1e-5, atol=1e-5
    )
    np.testing.assert_allclose(
        jax_result.buffers.transfers.amount, numpy_result.buffers.transfers.amount, rtol=1e-5, atol=1e-5
    )
    np.testing.assert_array_equal(jax_result.buffers.transfers.active, numpy_result.buffers.transfers.active)


if __name__ == "__main__":
    pytest_bazel.main()

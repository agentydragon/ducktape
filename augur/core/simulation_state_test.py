"""Tests for `augur.core.simulation_state`."""

from __future__ import annotations

import numpy as np
import pytest_bazel

from augur.core.simulation_state import AssetHolding, AssetKind, SimulationState


def test_simulation_state_construction_and_lookup() -> None:
    rollout_count = 4
    state = SimulationState(
        month_position=12,
        cash_by_account={"checking": np.array([1000.0, 2000.0, 3000.0, 4000.0])},
        holdings={
            "sp500": AssetHolding(
                asset_id="sp500",
                asset_kind=AssetKind.GENERIC_SP500,
                units=np.full(rollout_count, 100.0),
                basis_usd=np.full(rollout_count, 10_000.0),
            ),
            "crypto": AssetHolding(
                asset_id="crypto",
                asset_kind=AssetKind.CRYPTO,
                units=np.array([0.5, 1.0, 1.5, 2.0]),
                basis_usd=np.array([10_000.0, 20_000.0, 30_000.0, 40_000.0]),
            ),
        },
    )
    np.testing.assert_array_equal(state.cash("checking"), [1000.0, 2000.0, 3000.0, 4000.0])
    sp500 = state.holding("sp500")
    assert sp500.asset_kind is AssetKind.GENERIC_SP500
    np.testing.assert_array_equal(sp500.units, [100.0, 100.0, 100.0, 100.0])
    crypto = state.holding("crypto")
    assert crypto.asset_kind is AssetKind.CRYPTO
    np.testing.assert_array_equal(crypto.basis_usd, [10_000.0, 20_000.0, 30_000.0, 40_000.0])


if __name__ == "__main__":
    pytest_bazel.main()

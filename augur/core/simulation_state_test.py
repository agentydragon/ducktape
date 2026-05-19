"""Tests for `augur.core.simulation_state`."""

from __future__ import annotations

import numpy as np
import pytest_bazel

from augur.core.simulation_state import (
    AgentState,
    AssetHolding,
    AssetKind,
    LiabilityBalance,
    LiabilityKind,
    PropertyStake,
    PropertyState,
    SimulationState,
)


def _make_owner_only_state(*, month_position: int = 12) -> SimulationState:
    rollouts = 3
    return SimulationState(
        month_position=month_position,
        agents={
            "owner": AgentState(
                actor_id="owner",
                cash_by_account={"checking": np.array([1000.0, 2000.0, 3000.0])},
                holdings={
                    "sp500": AssetHolding(
                        asset_id="sp500",
                        asset_kind=AssetKind.GENERIC_SP500,
                        units=np.full(rollouts, 100.0),
                        basis_usd=np.full(rollouts, 10_000.0),
                    )
                },
                liabilities={
                    "mortgage:home": LiabilityBalance(
                        liability_id="mortgage:home",
                        liability_kind=LiabilityKind.MORTGAGE,
                        property_id="home",
                        principal_usd=np.full(rollouts, 450_000.0),
                        interest_accrued_this_month_usd=np.full(rollouts, 1500.0),
                        principal_paid_this_month_usd=np.full(rollouts, 600.0),
                    )
                },
                property_stakes={
                    "home": PropertyStake(
                        property_id="home",
                        ownership_pct=np.ones(rollouts),
                        contribution_used_usd=np.full(rollouts, 100_000.0),
                        equity_ledger_usd=np.full(rollouts, 100_000.0),
                    )
                },
            )
        },
        properties={
            "home": PropertyState(
                property_id="home",
                live=np.ones(rollouts),
                value_usd=np.full(rollouts, 600_000.0),
                cumulative_depreciation_usd=np.full(rollouts, 5000.0),
            )
        },
    )


def test_owner_only_state_accessors_return_expected_arrays() -> None:
    state = _make_owner_only_state()
    owner = state.agent("owner")
    np.testing.assert_array_equal(owner.cash("checking"), [1000.0, 2000.0, 3000.0])

    sp500 = owner.holding("sp500")
    assert sp500.asset_kind is AssetKind.GENERIC_SP500
    np.testing.assert_array_equal(sp500.units, [100.0, 100.0, 100.0])

    mortgage = owner.liability("mortgage:home")
    assert mortgage.liability_kind is LiabilityKind.MORTGAGE
    assert mortgage.property_id == "home"
    np.testing.assert_array_equal(mortgage.principal_usd, [450_000.0, 450_000.0, 450_000.0])

    stake = owner.stake("home")
    np.testing.assert_array_equal(stake.ownership_pct, [1.0, 1.0, 1.0])

    home = state.property("home")
    np.testing.assert_array_equal(home.live, [1.0, 1.0, 1.0])
    np.testing.assert_array_equal(home.value_usd, [600_000.0, 600_000.0, 600_000.0])


def test_owner_plus_partner_state_carries_two_agents() -> None:
    rollouts = 2
    state = SimulationState(
        month_position=0,
        agents={
            "owner": AgentState(
                actor_id="owner",
                cash_by_account={"checking": np.array([50_000.0, 60_000.0])},
                holdings={},
                liabilities={},
                property_stakes={
                    "home": PropertyStake(
                        property_id="home",
                        ownership_pct=np.array([0.6, 0.6]),
                        contribution_used_usd=np.array([120_000.0, 120_000.0]),
                        equity_ledger_usd=np.array([120_000.0, 120_000.0]),
                    )
                },
            ),
            "partner": AgentState(
                actor_id="partner",
                cash_by_account={},
                holdings={},
                liabilities={},
                property_stakes={
                    "home": PropertyStake(
                        property_id="home",
                        ownership_pct=np.array([0.4, 0.4]),
                        contribution_used_usd=np.array([80_000.0, 80_000.0]),
                        equity_ledger_usd=np.array([80_000.0, 80_000.0]),
                    )
                },
            ),
        },
        properties={
            "home": PropertyState(
                property_id="home",
                live=np.ones(rollouts),
                value_usd=np.full(rollouts, 500_000.0),
                cumulative_depreciation_usd=np.zeros(rollouts),
            )
        },
    )
    assert sorted(state.agents.keys()) == ["owner", "partner"]
    np.testing.assert_array_equal(state.agent("owner").stake("home").ownership_pct, [0.6, 0.6])
    np.testing.assert_array_equal(state.agent("partner").stake("home").ownership_pct, [0.4, 0.4])
    # Partner has no cash account / holdings / liabilities — just a stake.
    assert state.agent("partner").cash_by_account == {}
    assert state.agent("partner").holdings == {}
    assert state.agent("partner").liabilities == {}


def test_no_property_scenario_has_empty_property_dicts() -> None:
    rollouts = 1
    state = SimulationState(
        month_position=0,
        agents={
            "owner": AgentState(
                actor_id="owner",
                cash_by_account={"checking": np.array([100.0])},
                holdings={},
                liabilities={},
                property_stakes={},
            )
        },
        properties={},
    )
    assert state.properties == {}
    assert state.agent("owner").property_stakes == {}
    assert state.agent("owner").liabilities == {}
    np.testing.assert_array_equal(state.agent("owner").cash("checking"), np.array([100.0]))
    assert rollouts == 1  # touch to keep variable named


def test_unsecured_liability_has_no_property_id() -> None:
    state = SimulationState(
        month_position=0,
        agents={
            "owner": AgentState(
                actor_id="owner",
                cash_by_account={},
                holdings={},
                liabilities={
                    "tax_payable": LiabilityBalance(
                        liability_id="tax_payable",
                        liability_kind=LiabilityKind.TAX_PAYABLE,
                        property_id=None,
                        principal_usd=np.array([2500.0]),
                        interest_accrued_this_month_usd=np.array([0.0]),
                        principal_paid_this_month_usd=np.array([0.0]),
                    )
                },
                property_stakes={},
            )
        },
        properties={},
    )
    tax = state.agent("owner").liability("tax_payable")
    assert tax.property_id is None
    assert tax.liability_kind is LiabilityKind.TAX_PAYABLE


if __name__ == "__main__":
    pytest_bazel.main()

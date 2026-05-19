"""Tests for `augur.core.simulation_state`."""

from __future__ import annotations

import numpy as np
import pytest_bazel

from augur.core.simulation_state import (
    ASSET_HOLDING_FRAME_SCHEMA,
    CASH_BALANCE_FRAME_SCHEMA,
    LIABILITY_FRAME_SCHEMA,
    PROPERTY_STAKE_FRAME_SCHEMA,
    PROPERTY_STATE_FRAME_SCHEMA,
    AgentState,
    AssetHolding,
    AssetKind,
    LiabilityBalance,
    LiabilityKind,
    PropertyStake,
    PropertyState,
    SimulationState,
    SimulationStateFrames,
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


def test_simulation_state_frames_from_owner_only_state() -> None:
    """`SimulationStateFrames.from_nested(...)` lays the nested-dict
    state out as polars long-form frames keyed by `rollout_index`.
    Single-agent / single-account scenarios produce one row per
    rollout per leaf."""
    state = _make_owner_only_state()
    frames = SimulationStateFrames.from_nested(state, rollout_count=3)

    assert frames.month_position == 12
    assert frames.rollout_count == 3

    # Schemas match the declared shapes exactly.
    assert frames.cash.schema == CASH_BALANCE_FRAME_SCHEMA
    assert frames.assets.schema == ASSET_HOLDING_FRAME_SCHEMA
    assert frames.liabilities.schema == LIABILITY_FRAME_SCHEMA
    assert frames.property_stakes.schema == PROPERTY_STAKE_FRAME_SCHEMA
    assert frames.properties.schema == PROPERTY_STATE_FRAME_SCHEMA

    # `cash_balance(...)` returns the rollout-sorted Series for the
    # asked-for `(actor_id, account_id)` pair; values round-trip.
    cash_series = frames.cash_balance(actor_id="owner", account_id="checking")
    np.testing.assert_array_equal(cash_series.to_numpy(), [1000.0, 2000.0, 3000.0])

    # SP500 holding flattened to one row per rollout with the asset_kind discriminator.
    sp500_rows = frames.assets.filter(
        (frames.assets["actor_id"] == "owner") & (frames.assets["asset_id"] == "sp500")
    ).sort("rollout_index")
    np.testing.assert_array_equal(sp500_rows.get_column("units").to_numpy(), [100.0, 100.0, 100.0])
    np.testing.assert_array_equal(sp500_rows.get_column("basis_usd").to_numpy(), [10_000.0, 10_000.0, 10_000.0])
    assert set(sp500_rows.get_column("asset_kind").unique().to_list()) == {"generic_sp500"}


def test_simulation_state_frames_empty_when_no_agents() -> None:
    """An empty SimulationState (no agents, no properties) produces
    empty frames with the right schemas — useful as a starting point
    before policies populate state."""
    empty = SimulationState(month_position=0, agents={}, properties={})
    frames = SimulationStateFrames.from_nested(empty, rollout_count=5)
    assert frames.cash.is_empty()
    assert frames.assets.is_empty()
    assert frames.liabilities.is_empty()
    assert frames.property_stakes.is_empty()
    assert frames.properties.is_empty()
    assert frames.cash.schema == CASH_BALANCE_FRAME_SCHEMA


def test_simulation_state_frames_two_actors_flatten_to_rows() -> None:
    """Owner-plus-partner scenarios go from nested dicts (one
    AgentState per actor) to a single frame with `actor_id` as a
    column. Multi-actor cardinality stays in row count, not schema."""
    rollouts = 2
    state = SimulationState(
        month_position=4,
        agents={
            "owner": AgentState(
                actor_id="owner",
                cash_by_account={"checking": np.array([5_000.0, 6_000.0])},
                holdings={},
                liabilities={},
                property_stakes={
                    "home": PropertyStake(
                        property_id="home",
                        ownership_pct=np.full(rollouts, 0.7),
                        contribution_used_usd=np.full(rollouts, 0.0),
                        equity_ledger_usd=np.full(rollouts, 25_000.0),
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
                        ownership_pct=np.full(rollouts, 0.3),
                        contribution_used_usd=np.full(rollouts, 12_000.0),
                        equity_ledger_usd=np.full(rollouts, 8_000.0),
                    )
                },
            ),
        },
        properties={},
    )
    frames = SimulationStateFrames.from_nested(state, rollout_count=rollouts)

    # One row per (actor_id, rollout) on the stake frame. Adding a
    # third agent would add more rows on the SAME frame, not a new column.
    assert frames.property_stakes.height == 4
    owners_pct = (
        frames.property_stakes.filter(frames.property_stakes["actor_id"] == "owner")
        .sort("rollout_index")
        .get_column("ownership_pct")
        .to_numpy()
    )
    partner_pct = (
        frames.property_stakes.filter(frames.property_stakes["actor_id"] == "partner")
        .sort("rollout_index")
        .get_column("ownership_pct")
        .to_numpy()
    )
    np.testing.assert_array_equal(owners_pct, [0.7, 0.7])
    np.testing.assert_array_equal(partner_pct, [0.3, 0.3])

    # Cash frame only has rows for the owner (partner has no cash accounts).
    assert set(frames.cash.get_column("actor_id").unique().to_list()) == {"owner"}


def test_simulation_state_frames_liability_property_id_is_nullable() -> None:
    """`liability_frame.property_id` is non-null for mortgages
    (secured), null for tax_payable (unsecured). The polars schema
    keeps it as a String column with nulls — no separate secured /
    unsecured table."""
    state = SimulationState(
        month_position=11,
        agents={
            "owner": AgentState(
                actor_id="owner",
                cash_by_account={},
                holdings={},
                liabilities={
                    "mortgage:home": LiabilityBalance(
                        liability_id="mortgage:home",
                        liability_kind=LiabilityKind.MORTGAGE,
                        property_id="home",
                        principal_usd=np.array([400_000.0]),
                        interest_accrued_this_month_usd=np.array([1_500.0]),
                        principal_paid_this_month_usd=np.array([500.0]),
                    ),
                    "tax_payable": LiabilityBalance(
                        liability_id="tax_payable",
                        liability_kind=LiabilityKind.TAX_PAYABLE,
                        property_id=None,
                        principal_usd=np.array([2_500.0]),
                        interest_accrued_this_month_usd=np.array([0.0]),
                        principal_paid_this_month_usd=np.array([0.0]),
                    ),
                },
                property_stakes={},
            )
        },
        properties={},
    )
    frames = SimulationStateFrames.from_nested(state, rollout_count=1)
    rows = {row["liability_id"]: row for row in frames.liabilities.iter_rows(named=True)}
    assert rows["mortgage:home"]["property_id"] == "home"
    assert rows["tax_payable"]["property_id"] is None


if __name__ == "__main__":
    pytest_bazel.main()

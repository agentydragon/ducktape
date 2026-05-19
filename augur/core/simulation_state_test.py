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
    AssetEntry,
    AssetKind,
    CashEntry,
    LiabilityEntry,
    LiabilityKind,
    PropertyStakeEntry,
    PropertyStateEntry,
    SimulationStateFrames,
)


def _owner_only_frames(*, month_position: int = 12) -> SimulationStateFrames:
    rollouts = 3
    return SimulationStateFrames.build(
        month_position=month_position,
        rollout_count=rollouts,
        cash_entries=[
            CashEntry(actor_id="owner", account_id="checking", balance_usd=np.array([1000.0, 2000.0, 3000.0]))
        ],
        asset_entries=[
            AssetEntry(
                actor_id="owner",
                asset_id="sp500",
                asset_kind=AssetKind.GENERIC_SP500,
                units=np.full(rollouts, 100.0),
                basis_usd=np.full(rollouts, 10_000.0),
            )
        ],
        liability_entries=[
            LiabilityEntry(
                actor_id="owner",
                liability_id="mortgage:home",
                liability_kind=LiabilityKind.MORTGAGE,
                property_id="home",
                principal_usd=np.full(rollouts, 450_000.0),
                interest_accrued_this_month_usd=np.full(rollouts, 1500.0),
                principal_paid_this_month_usd=np.full(rollouts, 600.0),
            )
        ],
        property_stake_entries=[
            PropertyStakeEntry(
                actor_id="owner",
                property_id="home",
                ownership_pct=np.ones(rollouts),
                contribution_used_usd=np.full(rollouts, 100_000.0),
                equity_ledger_usd=np.full(rollouts, 100_000.0),
            )
        ],
        property_state_entries=[
            PropertyStateEntry(
                property_id="home",
                live=np.ones(rollouts),
                value_usd=np.full(rollouts, 600_000.0),
                cumulative_depreciation_usd=np.full(rollouts, 5000.0),
            )
        ],
    )


def test_owner_only_frames_round_trip_accessors() -> None:
    """`SimulationStateFrames.build(...)` produces frames keyed by
    `rollout_index`; the per-leaf accessors return `(rollouts,)` numpy
    arrays sorted by rollout."""
    frames = _owner_only_frames()
    assert frames.month_position == 12
    assert frames.rollout_count == 3
    assert frames.cash.schema == CASH_BALANCE_FRAME_SCHEMA
    assert frames.assets.schema == ASSET_HOLDING_FRAME_SCHEMA
    assert frames.liabilities.schema == LIABILITY_FRAME_SCHEMA
    assert frames.property_stakes.schema == PROPERTY_STAKE_FRAME_SCHEMA
    assert frames.properties.schema == PROPERTY_STATE_FRAME_SCHEMA

    np.testing.assert_array_equal(
        frames.cash_balance(actor_id="owner", account_id="checking"), [1000.0, 2000.0, 3000.0]
    )
    np.testing.assert_array_equal(frames.asset_units(actor_id="owner", asset_id="sp500"), [100.0, 100.0, 100.0])
    np.testing.assert_array_equal(
        frames.asset_basis(actor_id="owner", asset_id="sp500"), [10_000.0, 10_000.0, 10_000.0]
    )


def test_empty_frames_have_correct_schemas() -> None:
    """An empty `SimulationStateFrames` (no entries) yields empty
    frames with the right schemas — useful as a starting point before
    policies populate state."""
    frames = SimulationStateFrames.build(
        month_position=0,
        rollout_count=5,
        cash_entries=[],
        asset_entries=[],
        liability_entries=[],
        property_stake_entries=[],
        property_state_entries=[],
    )
    assert frames.cash.is_empty()
    assert frames.assets.is_empty()
    assert frames.liabilities.is_empty()
    assert frames.property_stakes.is_empty()
    assert frames.properties.is_empty()
    assert frames.cash.schema == CASH_BALANCE_FRAME_SCHEMA


def test_owner_plus_partner_stakes_flatten_to_rows() -> None:
    """Owner-plus-partner scenarios add rows with `actor_id="partner"`
    to whichever frames the partner participates in — they don't
    widen the schema. Adding a third agent would just add more rows."""
    rollouts = 2
    frames = SimulationStateFrames.build(
        month_position=4,
        rollout_count=rollouts,
        cash_entries=[CashEntry(actor_id="owner", account_id="checking", balance_usd=np.array([5_000.0, 6_000.0]))],
        asset_entries=[],
        liability_entries=[],
        property_stake_entries=[
            PropertyStakeEntry(
                actor_id="owner",
                property_id="home",
                ownership_pct=np.full(rollouts, 0.7),
                contribution_used_usd=np.full(rollouts, 0.0),
                equity_ledger_usd=np.full(rollouts, 25_000.0),
            ),
            PropertyStakeEntry(
                actor_id="partner",
                property_id="home",
                ownership_pct=np.full(rollouts, 0.3),
                contribution_used_usd=np.full(rollouts, 12_000.0),
                equity_ledger_usd=np.full(rollouts, 8_000.0),
            ),
        ],
        property_state_entries=[],
    )

    # One row per (actor_id, rollout) — 4 rows total, same schema.
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

    # Cash frame only has rows for the owner — partner has no cash entry.
    assert set(frames.cash.get_column("actor_id").unique().to_list()) == {"owner"}


def test_liability_property_id_nullable_for_unsecured_debts() -> None:
    """`liability_frame.property_id` is non-null for mortgages
    (secured) and null for tax_payable (unsecured). One frame, no
    separate secured / unsecured table."""
    frames = SimulationStateFrames.build(
        month_position=11,
        rollout_count=1,
        cash_entries=[],
        asset_entries=[],
        liability_entries=[
            LiabilityEntry(
                actor_id="owner",
                liability_id="mortgage:home",
                liability_kind=LiabilityKind.MORTGAGE,
                property_id="home",
                principal_usd=np.array([400_000.0]),
                interest_accrued_this_month_usd=np.array([1_500.0]),
                principal_paid_this_month_usd=np.array([500.0]),
            ),
            LiabilityEntry(
                actor_id="owner",
                liability_id="tax_payable",
                liability_kind=LiabilityKind.TAX_PAYABLE,
                property_id=None,
                principal_usd=np.array([2_500.0]),
                interest_accrued_this_month_usd=np.array([0.0]),
                principal_paid_this_month_usd=np.array([0.0]),
            ),
        ],
        property_stake_entries=[],
        property_state_entries=[],
    )
    rows = {row["liability_id"]: row for row in frames.liabilities.iter_rows(named=True)}
    assert rows["mortgage:home"]["property_id"] == "home"
    assert rows["tax_payable"]["property_id"] is None


if __name__ == "__main__":
    pytest_bazel.main()

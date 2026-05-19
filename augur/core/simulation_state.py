"""Polars long-form working-state frames for the simulation engine.

State at one month boundary is held as a `SimulationStateFrames` bundle:
one polars long-form frame per state kind, each keyed by
`rollout_index` plus the natural entity-id columns (`actor_id`,
`account_id`, `asset_id`, `liability_id`, `property_id`). Per-month
operations are polars expressions over `rollout_index`; the rollout
dimension is silent — same vectorization as today's `current_cash +
sale_usd` numpy add, just routed through polars.

The schemas are sister shapes of the persistent append-only logs in
`augur.core.action_log` (cashflow_log, asset_change_log, liability_log,
property_state_log) minus the `month_index` column — the working frame
is the cross-section at one month boundary, with the month carried on
`SimulationStateFrames.month_position`.

Frames are built **root-out**: each per-kind builder takes a list of
per-leaf entries (e.g. one `(actor_id, account_id, balance_usd)`
tuple per (agent, account) pair), with the `(rollouts,)` numeric
vectors already produced by the engine. There is no nested-dict
intermediate — the migration goes from the engine's authoritative
state (1D locals + property matrices) directly to long-form rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import polars as pl


class AssetKind(StrEnum):
    """Asset-class discriminator on `asset_holding_frame.asset_kind`."""

    GENERIC_SP500 = "generic_sp500"
    CRYPTO = "crypto"
    PRIVATE_EQUITY = "private_equity"


class LiabilityKind(StrEnum):
    """Liability-class discriminator on `liability_frame.liability_kind`.

    `MORTGAGE` is secured against a property (non-null `property_id`).
    `TAX_PAYABLE` is unsecured (null `property_id`) — seam for accrued-
    but-unpaid tax, not yet populated by the engine."""

    MORTGAGE = "mortgage"
    TAX_PAYABLE = "tax_payable"


CASH_BALANCE_FRAME_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "actor_id": pl.Utf8(),
    "account_id": pl.Utf8(),
    "balance_usd": pl.Float64(),
}

ASSET_HOLDING_FRAME_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "actor_id": pl.Utf8(),
    "asset_id": pl.Utf8(),
    "asset_kind": pl.Utf8(),
    "units": pl.Float64(),
    "basis_usd": pl.Float64(),
}

LIABILITY_FRAME_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "actor_id": pl.Utf8(),
    "liability_id": pl.Utf8(),
    "liability_kind": pl.Utf8(),
    # Non-null for liabilities secured against a property (mortgages);
    # null for unsecured liabilities (tax_payable).
    "property_id": pl.Utf8(),
    "principal_usd": pl.Float64(),
    "interest_accrued_this_month_usd": pl.Float64(),
    "principal_paid_this_month_usd": pl.Float64(),
}

PROPERTY_STAKE_FRAME_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "actor_id": pl.Utf8(),
    "property_id": pl.Utf8(),
    "ownership_pct": pl.Float64(),
    "contribution_used_usd": pl.Float64(),
    "equity_ledger_usd": pl.Float64(),
}

PROPERTY_STATE_FRAME_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "property_id": pl.Utf8(),
    "live": pl.Float64(),
    "value_usd": pl.Float64(),
    "cumulative_depreciation_usd": pl.Float64(),
}


@dataclass(frozen=True)
class CashEntry:
    actor_id: str
    account_id: str
    balance_usd: np.ndarray  # (rollouts,)


@dataclass(frozen=True)
class AssetEntry:
    actor_id: str
    asset_id: str
    asset_kind: AssetKind
    units: np.ndarray  # (rollouts,)
    basis_usd: np.ndarray  # (rollouts,)


@dataclass(frozen=True)
class LiabilityEntry:
    actor_id: str
    liability_id: str
    liability_kind: LiabilityKind
    property_id: str | None
    principal_usd: np.ndarray  # (rollouts,)
    interest_accrued_this_month_usd: np.ndarray  # (rollouts,)
    principal_paid_this_month_usd: np.ndarray  # (rollouts,)


@dataclass(frozen=True)
class PropertyStakeEntry:
    actor_id: str
    property_id: str
    ownership_pct: np.ndarray  # (rollouts,)
    contribution_used_usd: np.ndarray  # (rollouts,)
    equity_ledger_usd: np.ndarray  # (rollouts,)


@dataclass(frozen=True)
class PropertyStateEntry:
    property_id: str
    live: np.ndarray  # (rollouts,)
    value_usd: np.ndarray  # (rollouts,)
    cumulative_depreciation_usd: np.ndarray  # (rollouts,)


@dataclass(frozen=True)
class SimulationStateFrames:
    """Per-month working state as polars long-form frames.

    G1 of the state-vector refactor (see
    `augur/plans/state_vector_simulation_refactor.md`) makes the engine
    read / write through this bundle exclusively; the 1D `current_cash`
    / `remaining_*` locals fall away as call sites migrate."""

    month_position: int
    rollout_count: int
    cash: pl.DataFrame
    assets: pl.DataFrame
    liabilities: pl.DataFrame
    property_stakes: pl.DataFrame
    properties: pl.DataFrame

    @classmethod
    def build(
        cls,
        *,
        month_position: int,
        rollout_count: int,
        cash_entries: list[CashEntry],
        asset_entries: list[AssetEntry],
        liability_entries: list[LiabilityEntry],
        property_stake_entries: list[PropertyStakeEntry],
        property_state_entries: list[PropertyStateEntry],
    ) -> SimulationStateFrames:
        return cls(
            month_position=month_position,
            rollout_count=rollout_count,
            cash=_cash_balance_frame(cash_entries, rollout_count=rollout_count),
            assets=_asset_holding_frame(asset_entries, rollout_count=rollout_count),
            liabilities=_liability_frame(liability_entries, rollout_count=rollout_count),
            property_stakes=_property_stake_frame(property_stake_entries, rollout_count=rollout_count),
            properties=_property_state_frame(property_state_entries, rollout_count=rollout_count),
        )

    def cash_balance(self, *, actor_id: str, account_id: str) -> np.ndarray:
        """Return the `(rollouts,)` cash balance array for one
        `(actor_id, account_id)` pair, sorted by `rollout_index`."""
        return (
            self.cash.filter((pl.col("actor_id") == actor_id) & (pl.col("account_id") == account_id))
            .sort("rollout_index")
            .get_column("balance_usd")
            .to_numpy()
        )

    def asset_units(self, *, actor_id: str, asset_id: str) -> np.ndarray:
        return (
            self.assets.filter((pl.col("actor_id") == actor_id) & (pl.col("asset_id") == asset_id))
            .sort("rollout_index")
            .get_column("units")
            .to_numpy()
        )

    def asset_basis(self, *, actor_id: str, asset_id: str) -> np.ndarray:
        return (
            self.assets.filter((pl.col("actor_id") == actor_id) & (pl.col("asset_id") == asset_id))
            .sort("rollout_index")
            .get_column("basis_usd")
            .to_numpy()
        )


def _broadcast_rollouts(values: np.ndarray, expected_rollouts: int) -> np.ndarray:
    if values.shape != (expected_rollouts,):
        msg = f"expected shape ({expected_rollouts},), got {values.shape}"
        raise ValueError(msg)
    return values.astype(np.float64, copy=False)


def _cash_balance_frame(entries: list[CashEntry], *, rollout_count: int) -> pl.DataFrame:
    if not entries:
        return pl.DataFrame(schema=CASH_BALANCE_FRAME_SCHEMA)
    rollout_axis: list[np.ndarray] = []
    actor_ids: list[str] = []
    account_ids: list[str] = []
    balances: list[np.ndarray] = []
    for entry in entries:
        balance_1d = _broadcast_rollouts(entry.balance_usd, rollout_count)
        rollout_axis.append(np.arange(rollout_count, dtype=np.int64))
        actor_ids.extend([entry.actor_id] * rollout_count)
        account_ids.extend([entry.account_id] * rollout_count)
        balances.append(balance_1d)
    return pl.DataFrame(
        {
            "rollout_index": np.concatenate(rollout_axis),
            "actor_id": actor_ids,
            "account_id": account_ids,
            "balance_usd": np.concatenate(balances),
        },
        schema=CASH_BALANCE_FRAME_SCHEMA,
    )


def _asset_holding_frame(entries: list[AssetEntry], *, rollout_count: int) -> pl.DataFrame:
    if not entries:
        return pl.DataFrame(schema=ASSET_HOLDING_FRAME_SCHEMA)
    rollout_axis: list[np.ndarray] = []
    actor_ids: list[str] = []
    asset_ids: list[str] = []
    asset_kinds: list[str] = []
    units: list[np.ndarray] = []
    basis: list[np.ndarray] = []
    for entry in entries:
        units_1d = _broadcast_rollouts(entry.units, rollout_count)
        basis_1d = _broadcast_rollouts(entry.basis_usd, rollout_count)
        rollout_axis.append(np.arange(rollout_count, dtype=np.int64))
        actor_ids.extend([entry.actor_id] * rollout_count)
        asset_ids.extend([entry.asset_id] * rollout_count)
        asset_kinds.extend([entry.asset_kind.value] * rollout_count)
        units.append(units_1d)
        basis.append(basis_1d)
    return pl.DataFrame(
        {
            "rollout_index": np.concatenate(rollout_axis),
            "actor_id": actor_ids,
            "asset_id": asset_ids,
            "asset_kind": asset_kinds,
            "units": np.concatenate(units),
            "basis_usd": np.concatenate(basis),
        },
        schema=ASSET_HOLDING_FRAME_SCHEMA,
    )


def _liability_frame(entries: list[LiabilityEntry], *, rollout_count: int) -> pl.DataFrame:
    if not entries:
        return pl.DataFrame(schema=LIABILITY_FRAME_SCHEMA)
    rollout_axis: list[np.ndarray] = []
    actor_ids: list[str] = []
    liability_ids: list[str] = []
    kinds: list[str] = []
    property_ids: list[str | None] = []
    principals: list[np.ndarray] = []
    interest: list[np.ndarray] = []
    principal_paid: list[np.ndarray] = []
    for entry in entries:
        principal_1d = _broadcast_rollouts(entry.principal_usd, rollout_count)
        interest_1d = _broadcast_rollouts(entry.interest_accrued_this_month_usd, rollout_count)
        paid_1d = _broadcast_rollouts(entry.principal_paid_this_month_usd, rollout_count)
        rollout_axis.append(np.arange(rollout_count, dtype=np.int64))
        actor_ids.extend([entry.actor_id] * rollout_count)
        liability_ids.extend([entry.liability_id] * rollout_count)
        kinds.extend([entry.liability_kind.value] * rollout_count)
        property_ids.extend([entry.property_id] * rollout_count)
        principals.append(principal_1d)
        interest.append(interest_1d)
        principal_paid.append(paid_1d)
    return pl.DataFrame(
        {
            "rollout_index": np.concatenate(rollout_axis),
            "actor_id": actor_ids,
            "liability_id": liability_ids,
            "liability_kind": kinds,
            "property_id": property_ids,
            "principal_usd": np.concatenate(principals),
            "interest_accrued_this_month_usd": np.concatenate(interest),
            "principal_paid_this_month_usd": np.concatenate(principal_paid),
        },
        schema=LIABILITY_FRAME_SCHEMA,
    )


def _property_stake_frame(entries: list[PropertyStakeEntry], *, rollout_count: int) -> pl.DataFrame:
    if not entries:
        return pl.DataFrame(schema=PROPERTY_STAKE_FRAME_SCHEMA)
    rollout_axis: list[np.ndarray] = []
    actor_ids: list[str] = []
    property_ids: list[str] = []
    ownership: list[np.ndarray] = []
    contribution: list[np.ndarray] = []
    equity: list[np.ndarray] = []
    for entry in entries:
        ownership_1d = _broadcast_rollouts(entry.ownership_pct, rollout_count)
        contribution_1d = _broadcast_rollouts(entry.contribution_used_usd, rollout_count)
        equity_1d = _broadcast_rollouts(entry.equity_ledger_usd, rollout_count)
        rollout_axis.append(np.arange(rollout_count, dtype=np.int64))
        actor_ids.extend([entry.actor_id] * rollout_count)
        property_ids.extend([entry.property_id] * rollout_count)
        ownership.append(ownership_1d)
        contribution.append(contribution_1d)
        equity.append(equity_1d)
    return pl.DataFrame(
        {
            "rollout_index": np.concatenate(rollout_axis),
            "actor_id": actor_ids,
            "property_id": property_ids,
            "ownership_pct": np.concatenate(ownership),
            "contribution_used_usd": np.concatenate(contribution),
            "equity_ledger_usd": np.concatenate(equity),
        },
        schema=PROPERTY_STAKE_FRAME_SCHEMA,
    )


def _property_state_frame(entries: list[PropertyStateEntry], *, rollout_count: int) -> pl.DataFrame:
    if not entries:
        return pl.DataFrame(schema=PROPERTY_STATE_FRAME_SCHEMA)
    rollout_axis: list[np.ndarray] = []
    property_ids: list[str] = []
    live: list[np.ndarray] = []
    value: list[np.ndarray] = []
    depreciation: list[np.ndarray] = []
    for entry in entries:
        live_1d = _broadcast_rollouts(entry.live, rollout_count)
        value_1d = _broadcast_rollouts(entry.value_usd, rollout_count)
        depr_1d = _broadcast_rollouts(entry.cumulative_depreciation_usd, rollout_count)
        rollout_axis.append(np.arange(rollout_count, dtype=np.int64))
        property_ids.extend([entry.property_id] * rollout_count)
        live.append(live_1d)
        value.append(value_1d)
        depreciation.append(depr_1d)
    return pl.DataFrame(
        {
            "rollout_index": np.concatenate(rollout_axis),
            "property_id": property_ids,
            "live": np.concatenate(live),
            "value_usd": np.concatenate(value),
            "cumulative_depreciation_usd": np.concatenate(depreciation),
        },
        schema=PROPERTY_STATE_FRAME_SCHEMA,
    )

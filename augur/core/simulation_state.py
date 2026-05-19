"""Working state object for the simulation engine's per-month loop.

`SimulationState` carries the per-(rollout, asset/account/property) values
that the engine's main month loop reads / writes at one month boundary.
Today the engine threads many separate `(rollouts,)` 1D locals
(`current_cash`, `remaining_sp500_units`, ...) and snapshots them into
`(rollouts, months)` matrices at end-of-month. The state-vector
simulation refactor (see
`augur/plans/state_vector_simulation_refactor.md`) introduces a single
agent-centric `SimulationState` bundle; later phases make it the source
of truth, drop the 1D locals, and derive matrices from per-month action
logs.

Agent-centric shape: per-agent state (accounts, holdings, liabilities,
property stakes) lives under `state.agents[actor_id]`; per-property
*shared* facts (value, depreciation, live mask) live under
`state.properties[property_id]`. Single-actor scenarios still go through
`state.agents` — the dict has one entry keyed by
`primary_owner_actor_id`. Owner-plus-partner scenarios add a second
entry for the partner; the partner typically has empty
`cash_by_account` / `holdings` / `liabilities` and only a
`property_stakes` entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import polars as pl


class AssetKind(StrEnum):
    """Categories of asset holdings tracked in `AgentState.holdings`."""

    GENERIC_SP500 = "generic_sp500"
    CRYPTO = "crypto"
    PRIVATE_EQUITY = "private_equity"


class LiabilityKind(StrEnum):
    """Categories of debts tracked in `AgentState.liabilities`.

    `MORTGAGE` is secured against a property; `TAX_PAYABLE` is a seam for
    Phase 4 — not populated by the engine yet."""

    MORTGAGE = "mortgage"
    TAX_PAYABLE = "tax_payable"


@dataclass(frozen=True)
class AssetHolding:
    """Per-rollout holding of a single asset."""

    asset_id: str
    asset_kind: AssetKind
    units: np.ndarray
    basis_usd: np.ndarray


@dataclass(frozen=True)
class LiabilityBalance:
    """A debt owed by an agent. `property_id` is non-null when the
    liability is secured against a property (mortgages); None for
    unsecured liabilities (tax_payable, ...)."""

    liability_id: str
    liability_kind: LiabilityKind
    property_id: str | None
    principal_usd: np.ndarray
    interest_accrued_this_month_usd: np.ndarray
    principal_paid_this_month_usd: np.ndarray


@dataclass(frozen=True)
class PropertyStake:
    """One agent's relationship to one property at the current month.

    Captures the partner-equity ledger fields per-(agent, property).
    Single-owner scenarios still populate a stake with
    `ownership_pct = 1.0` so downstream consumers can read ownership
    uniformly without special-casing the partnered case."""

    property_id: str
    ownership_pct: np.ndarray
    contribution_used_usd: np.ndarray
    equity_ledger_usd: np.ndarray


@dataclass(frozen=True)
class PropertyState:
    """Per-property facts shared across all agents at this month.

    `live` is `(rollouts,)` float (1.0 alive, 0.0 post-sale) to match
    the engine's existing `property_live_mask`, which is used as a
    multiplier."""

    property_id: str
    live: np.ndarray
    value_usd: np.ndarray
    cumulative_depreciation_usd: np.ndarray


@dataclass(frozen=True)
class AgentState:
    """Per-agent state: accounts they own, assets they hold, debts
    they owe, stakes they hold in shared properties. All numeric
    fields nested in the dicts are `(rollouts,)` numpy vectors at this
    `month_position`."""

    actor_id: str
    cash_by_account: dict[str, np.ndarray]
    holdings: dict[str, AssetHolding]
    liabilities: dict[str, LiabilityBalance]
    property_stakes: dict[str, PropertyStake]

    def cash(self, account_id: str) -> np.ndarray:
        return self.cash_by_account[account_id]

    def holding(self, asset_id: str) -> AssetHolding:
        return self.holdings[asset_id]

    def liability(self, liability_id: str) -> LiabilityBalance:
        return self.liabilities[liability_id]

    def stake(self, property_id: str) -> PropertyStake:
        return self.property_stakes[property_id]


@dataclass(frozen=True)
class SimulationState:
    """Snapshot of per-rollout simulation state at one month boundary.

    `month_position` is the 0-indexed column position into the
    simulation's month axis. `agents` is keyed by actor_id and carries
    each agent's accounts/holdings/liabilities/property-stakes;
    `properties` is keyed by property_id and carries shared
    world-facts about each property.

    In the current phases the engine maintains this object in parallel
    with its 1D locals and `(rollouts, months)` matrices — the state
    object isn't yet the source of truth; it's a scaffold proving the
    shape. Later phases drop the locals and switch matrix derivation
    onto the action log."""

    month_position: int
    agents: dict[str, AgentState]
    properties: dict[str, PropertyState]

    def agent(self, actor_id: str) -> AgentState:
        return self.agents[actor_id]

    def property(self, property_id: str) -> PropertyState:
        return self.properties[property_id]


# --- Polars long-form working-state frames -------------------------------
#
# These schemas describe the same cross-section of state that the
# `SimulationState` nested-dict tree above carries, but in long form
# (one row per (rollout, entity-id) tuple). They are the canonical
# representation under the state-vector simulation refactor: the
# engine's per-month reads / writes operate against frames, every
# operation is a polars expression over the `rollout_index` column,
# and per-month decisions append directly to the matching persistent
# log (`cashflow_log` / `asset_change_log` / `liability_log` / ...)
# without an intermediate nested-dict materialization.
#
# Schemas drop the `month_index` column that the persistent log
# carries — the frames here are the cross-section at one month
# boundary, with the month carried on `SimulationStateFrames.month_position`.
#
# Today these are populated alongside the nested-dict view via
# `SimulationStateFrames.from_nested(...)`; G1 of the refactor switches
# the engine's read sites onto the frames and lets the nested-dict view
# fall away.

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
    # `property_id` is non-null only for liabilities secured against a
    # property (mortgages); unsecured liabilities (tax_payable) carry
    # null.
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


def _broadcast_rollouts(values: np.ndarray, expected_rollouts: int) -> np.ndarray:
    if values.shape != (expected_rollouts,):
        msg = f"expected shape ({expected_rollouts},), got {values.shape}"
        raise ValueError(msg)
    return values.astype(np.float64, copy=False)


@dataclass(frozen=True)
class SimulationStateFrames:
    """Polars long-form view of `SimulationState` at one month boundary.

    All frames are keyed by `rollout_index` (plus the natural entity-id
    columns for each kind). Schemas match the persistent
    `cashflow_log` / `asset_change_log` / `liability_log` / etc. shapes
    minus the `month_index` column (the month boundary is carried on
    `month_position` here). The engine's per-month operations are
    polars expressions over these frames; per-month decisions append
    directly to the matching persistent log.

    G1 of the state-vector refactor (see
    `augur/plans/state_vector_simulation_refactor.md`) migrates the
    engine's read sites onto this representation."""

    month_position: int
    rollout_count: int
    cash: pl.DataFrame
    assets: pl.DataFrame
    liabilities: pl.DataFrame
    property_stakes: pl.DataFrame
    properties: pl.DataFrame

    @classmethod
    def from_nested(cls, state: SimulationState, *, rollout_count: int) -> SimulationStateFrames:
        """Build the polars long-form view from a nested-dict
        `SimulationState`. Round-trippable with `to_nested(...)` for
        single-actor / single-account scenarios — multi-leaf shapes
        flatten to rows (no information loss).

        `rollout_count` is required because empty agents produce empty
        per-kind blocks and the row builders below need an explicit
        per-rollout dimension."""
        return cls(
            month_position=state.month_position,
            rollout_count=rollout_count,
            cash=_build_cash_frame(state, rollout_count=rollout_count),
            assets=_build_asset_frame(state, rollout_count=rollout_count),
            liabilities=_build_liability_frame(state, rollout_count=rollout_count),
            property_stakes=_build_property_stake_frame(state, rollout_count=rollout_count),
            properties=_build_property_state_frame_cross_section(state, rollout_count=rollout_count),
        )

    def cash_balance(self, *, actor_id: str, account_id: str) -> pl.Series:
        """Return the `(rollouts,)` cash balance series for one
        `(actor_id, account_id)` pair. Rows are sorted by
        `rollout_index` ascending so the returned series aligns with
        the engine's numpy convention."""
        return (
            self.cash.filter((pl.col("actor_id") == actor_id) & (pl.col("account_id") == account_id))
            .sort("rollout_index")
            .get_column("balance_usd")
        )


def _build_cash_frame(state: SimulationState, *, rollout_count: int) -> pl.DataFrame:
    rollout_axis: list[np.ndarray] = []
    actor_ids: list[str] = []
    account_ids: list[str] = []
    balances: list[np.ndarray] = []
    for actor_id, agent in state.agents.items():
        for account_id, balance in agent.cash_by_account.items():
            balance_1d = _broadcast_rollouts(balance, rollout_count)
            rollout_axis.append(np.arange(rollout_count, dtype=np.int64))
            actor_ids.extend([actor_id] * rollout_count)
            account_ids.extend([account_id] * rollout_count)
            balances.append(balance_1d)
    if not rollout_axis:
        return pl.DataFrame(schema=CASH_BALANCE_FRAME_SCHEMA)
    return pl.DataFrame(
        {
            "rollout_index": np.concatenate(rollout_axis),
            "actor_id": actor_ids,
            "account_id": account_ids,
            "balance_usd": np.concatenate(balances),
        },
        schema=CASH_BALANCE_FRAME_SCHEMA,
    )


def _build_asset_frame(state: SimulationState, *, rollout_count: int) -> pl.DataFrame:
    rollout_axis: list[np.ndarray] = []
    actor_ids: list[str] = []
    asset_ids: list[str] = []
    asset_kinds: list[str] = []
    units: list[np.ndarray] = []
    basis: list[np.ndarray] = []
    for actor_id, agent in state.agents.items():
        for asset_id, holding in agent.holdings.items():
            units_1d = _broadcast_rollouts(holding.units, rollout_count)
            basis_1d = _broadcast_rollouts(holding.basis_usd, rollout_count)
            rollout_axis.append(np.arange(rollout_count, dtype=np.int64))
            actor_ids.extend([actor_id] * rollout_count)
            asset_ids.extend([asset_id] * rollout_count)
            asset_kinds.extend([holding.asset_kind.value] * rollout_count)
            units.append(units_1d)
            basis.append(basis_1d)
    if not rollout_axis:
        return pl.DataFrame(schema=ASSET_HOLDING_FRAME_SCHEMA)
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


def _build_liability_frame(state: SimulationState, *, rollout_count: int) -> pl.DataFrame:
    rollout_axis: list[np.ndarray] = []
    actor_ids: list[str] = []
    liability_ids: list[str] = []
    kinds: list[str] = []
    property_ids: list[str | None] = []
    principals: list[np.ndarray] = []
    interest: list[np.ndarray] = []
    principal_paid: list[np.ndarray] = []
    for actor_id, agent in state.agents.items():
        for liability_id, liab in agent.liabilities.items():
            principal_1d = _broadcast_rollouts(liab.principal_usd, rollout_count)
            interest_1d = _broadcast_rollouts(liab.interest_accrued_this_month_usd, rollout_count)
            paid_1d = _broadcast_rollouts(liab.principal_paid_this_month_usd, rollout_count)
            rollout_axis.append(np.arange(rollout_count, dtype=np.int64))
            actor_ids.extend([actor_id] * rollout_count)
            liability_ids.extend([liability_id] * rollout_count)
            kinds.extend([liab.liability_kind.value] * rollout_count)
            property_ids.extend([liab.property_id] * rollout_count)
            principals.append(principal_1d)
            interest.append(interest_1d)
            principal_paid.append(paid_1d)
    if not rollout_axis:
        return pl.DataFrame(schema=LIABILITY_FRAME_SCHEMA)
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


def _build_property_stake_frame(state: SimulationState, *, rollout_count: int) -> pl.DataFrame:
    rollout_axis: list[np.ndarray] = []
    actor_ids: list[str] = []
    property_ids: list[str] = []
    ownership: list[np.ndarray] = []
    contribution: list[np.ndarray] = []
    equity: list[np.ndarray] = []
    for actor_id, agent in state.agents.items():
        for property_id, stake in agent.property_stakes.items():
            ownership_1d = _broadcast_rollouts(stake.ownership_pct, rollout_count)
            contribution_1d = _broadcast_rollouts(stake.contribution_used_usd, rollout_count)
            equity_1d = _broadcast_rollouts(stake.equity_ledger_usd, rollout_count)
            rollout_axis.append(np.arange(rollout_count, dtype=np.int64))
            actor_ids.extend([actor_id] * rollout_count)
            property_ids.extend([property_id] * rollout_count)
            ownership.append(ownership_1d)
            contribution.append(contribution_1d)
            equity.append(equity_1d)
    if not rollout_axis:
        return pl.DataFrame(schema=PROPERTY_STAKE_FRAME_SCHEMA)
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


def _build_property_state_frame_cross_section(state: SimulationState, *, rollout_count: int) -> pl.DataFrame:
    rollout_axis: list[np.ndarray] = []
    property_ids: list[str] = []
    live: list[np.ndarray] = []
    value: list[np.ndarray] = []
    depreciation: list[np.ndarray] = []
    for property_id, prop in state.properties.items():
        live_1d = _broadcast_rollouts(prop.live, rollout_count)
        value_1d = _broadcast_rollouts(prop.value_usd, rollout_count)
        depr_1d = _broadcast_rollouts(prop.cumulative_depreciation_usd, rollout_count)
        rollout_axis.append(np.arange(rollout_count, dtype=np.int64))
        property_ids.extend([property_id] * rollout_count)
        live.append(live_1d)
        value.append(value_1d)
        depreciation.append(depr_1d)
    if not rollout_axis:
        return pl.DataFrame(schema=PROPERTY_STATE_FRAME_SCHEMA)
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

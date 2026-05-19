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

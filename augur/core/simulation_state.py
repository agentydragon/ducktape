"""Working state object for the simulation engine's per-month loop.

`SimulationState` carries the per-(rollout, asset/account) values that the
engine's main month loop reads / writes at one month boundary. Today the
engine threads many separate `(rollouts,)` 1D locals (`current_cash`,
`remaining_sp500_units`, `remaining_sp500_basis`,
`remaining_crypto_quantity`, `remaining_crypto_basis`, ...) and snapshots
them into `(rollouts, months)` matrices at end-of-month. Phase 1 of the
state-vector simulation refactor (see
`augur/plans/state_vector_simulation_refactor.md`) introduces a single
`SimulationState` object that bundles these per-rollout vectors; later
phases will make it the source of truth, drop the 1D locals, and derive
the matrices from a per-month action log.

Phase 1 scope: cash (one checking account) plus SP500 and crypto holdings.
PE state, liabilities, and property state come in Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np


class AssetKind(StrEnum):
    """Categories of asset holdings tracked in `SimulationState`.

    SP500 and crypto are publicly-marked assets with a single per-portfolio
    aggregate position in the engine today. Private equity is per-holding
    and currently lives in `_PrivateEquityFundingState`; it migrates into
    `SimulationState.holdings` in Phase 3."""

    GENERIC_SP500 = "generic_sp500"
    CRYPTO = "crypto"
    PRIVATE_EQUITY = "private_equity"


@dataclass(frozen=True)
class AssetHolding:
    """Per-rollout holding of a single asset.

    `units` and `basis_usd` are `(rollouts,)` numpy vectors. `asset_id` is
    the holding identifier (typically scenario-defined); `asset_kind`
    discriminates the asset class for downstream consumers (taxation,
    valuation)."""

    asset_id: str
    asset_kind: AssetKind
    units: np.ndarray
    basis_usd: np.ndarray


@dataclass(frozen=True)
class SimulationState:
    """Snapshot of per-rollout simulation state at one month boundary.

    `month_position` is the 0-indexed column position into the simulation's
    month axis (`market_bundle.month_index[month_position]` gives the
    absolute calendar month). `cash_by_account` and `holdings` are keyed by
    string IDs; each value is a `(rollouts,)` vector (cash) or an
    `AssetHolding` carrying `(rollouts,)` vectors (units, basis).

    In Phase 1 the engine maintains this object in parallel with its 1D
    locals and `(rollouts, months)` matrices — the state object isn't yet
    the source of truth; it's a scaffold proving the shape works. Later
    phases drop the locals and snapshot the state into the matrices (or
    derive matrices from the action log once that exists).
    """

    month_position: int
    cash_by_account: dict[str, np.ndarray]
    holdings: dict[str, AssetHolding]

    def cash(self, account_id: str) -> np.ndarray:
        return self.cash_by_account[account_id]

    def holding(self, asset_id: str) -> AssetHolding:
        return self.holdings[asset_id]

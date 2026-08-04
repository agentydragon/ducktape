"""Compiler-wide shared helpers: integer-code sentinel, amount kinds,
StringTable, and the small `_slot`/`_amount_arrays`/`_empty_month_matrix`
utilities every per-domain compile function uses.

Lives alongside the per-domain compile modules under `augur/sim/compiler/`
so per-domain files can import these without pulling in the orchestrator
(`compiler/__init__.py`) and triggering a load-time cycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from finance.augur.model.series import LevelSeriesKey
from finance.augur.product.asset_key import AssetKey
from finance.augur.sim.fixed_point import usd_to_cents
from finance.augur.sim.scenario import FixedAmount, SeriesIndexedAmount

NO_CODE = -1
AMOUNT_FIXED = 0
AMOUNT_SERIES_INDEXED = 1
ORDINARY_DEDUCTION_CATEGORY = "ordinary"


class StringTable:
    def __init__(self) -> None:
        self._by_value: dict[str, int] = {}
        self.values: list[str] = []

    def intern(self, value: str | None) -> int:
        if value is None:
            return NO_CODE
        existing = self._by_value.get(value)
        if existing is not None:
            return existing
        code = len(self.values)
        self._by_value[value] = code
        self.values.append(value)
        return code

    def require(self, value: str) -> int:
        return self.intern(value)


class AssetTable:
    """Typed intern table for lot/sale/chain `AssetKey` identities — the asset twin of
    `StringTable`. Keyed by the typed key (no wire-string round-trip), so the same asset
    interns to the same code wherever it appears and the engine's asset-code matching holds.
    Codes index `CompiledSimulation.assets`; decode lifts them back to `AssetKey` typed."""

    def __init__(self) -> None:
        self._by_key: dict[AssetKey, int] = {}
        self.values: list[AssetKey] = []

    def intern(self, asset: AssetKey | None) -> int:
        if asset is None:
            return NO_CODE
        existing = self._by_key.get(asset)
        if existing is not None:
            return existing
        code = len(self.values)
        self._by_key[asset] = code
        self.values.append(asset)
        return code

    def require(self, asset: AssetKey) -> int:
        return self.intern(asset)


EXTERNAL_AGENT_ID = "external"
EXTERNAL_ACCOUNT_ID = "rest_of_world"


@dataclass(frozen=True)
class AccountSlots:
    """Cash-slot resolution, including the row the rest of the world settles against.

    Every cashflow is double-entry: it debits one cash row and credits another. Counterparties
    the scenario does not model — an employer, a landlord, a bond issuer, a tax authority — are
    not holes in that scheme, they are the `external` row. A paycheck is a transfer FROM the
    rest of the world, not cash conjured from nothing.

    Before this, an unmodeled counterparty resolved to `NO_CODE`, and the engine's scatter sent
    those rows to a padding row it then sliced off. That silently discarded the money, which is
    exactly what made a leak invisible: nothing failed, the total just did not add up. With every
    flow landing on a real row, total cash is conserved, and one assertion over that total
    catches any leak anywhere — including ones nobody thought to guard.

    `NO_CODE` keeps its other meaning untouched: a padding entry in a `(month, slot)` table for
    a month where nothing fires is a genuine no-op, not a flow to anywhere.
    """

    by_key: Mapping[tuple[str, str], int]
    external: int

    def resolve(self, agent_id: str, account_id: str) -> int:
        """The row a flow to/from this (agent, account) settles on.

        An unknown pair is neither an error nor a hole — it is outside the model, so it settles
        against `external`.
        """

        return self.by_key.get((agent_id, account_id), self.external)

    def require(self, agent_id: str, account_id: str, *, owner: str) -> int:
        """`resolve`, for a position held BY a modeled agent, where "outside the model" is not
        a possible answer.

        A bond whose account does not exist is a typo, not a counterparty: settling its coupons
        against the rest of the world would hand the agent's own income to nobody.
        """

        resolved = self.by_key.get((agent_id, account_id))
        if resolved is None:
            known = ", ".join(sorted(f"{agent}/{account}" for agent, account in self.by_key)) or "<none>"
            raise ValueError(
                f"{owner} pays into account {account_id!r} of agent {agent_id!r}, which has no cash "
                f"account in this scenario. Known (agent/account) pairs: {known}"
            )
        return resolved


def amount_arrays(
    amount: Any, series_index_by_id: dict[LevelSeriesKey, int]
) -> tuple[int, float, float, int, int, int]:
    if isinstance(amount, int | float):
        return AMOUNT_FIXED, float(amount), 0.0, NO_CODE, 0, 1
    if isinstance(amount, FixedAmount):
        return AMOUNT_FIXED, float(amount.amount_usd), 0.0, NO_CODE, 0, 1
    if isinstance(amount, SeriesIndexedAmount):
        return (
            AMOUNT_SERIES_INDEXED,
            0.0,
            float(amount.base_amount_usd),
            series_index_by_id[amount.series],
            int(amount.base_month_index),
            int(amount.adjustment_period_months),
        )
    raise TypeError(f"unsupported amount spec: {amount!r}")


def amount_arrays_cents(
    amount: Any, series_index_by_id: dict[LevelSeriesKey, int]
) -> tuple[int, np.int64, np.int64, int, int, int]:
    if isinstance(amount, int | float):
        return AMOUNT_FIXED, usd_to_cents(amount), np.int64(0), NO_CODE, 0, 1
    if isinstance(amount, FixedAmount):
        return AMOUNT_FIXED, usd_to_cents(amount.amount_usd), np.int64(0), NO_CODE, 0, 1
    if isinstance(amount, SeriesIndexedAmount):
        return (
            AMOUNT_SERIES_INDEXED,
            np.int64(0),
            usd_to_cents(amount.base_amount_usd),
            series_index_by_id[amount.series],
            int(amount.base_month_index),
            int(amount.adjustment_period_months),
        )
    raise TypeError(f"unsupported amount spec: {amount!r}")


def empty_month_matrix(months: int, slots: int, dtype: Any, fill: int | float = 0) -> np.ndarray:
    matrix = np.empty((months, max(1, slots)), dtype=dtype)
    matrix[...] = fill
    return matrix

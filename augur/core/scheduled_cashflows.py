"""Pre-computed per-month inputs to the simulation engine.

A scheduled cashflow is a per-(rollout, month) input that does NOT depend
on policy decisions taken inside the simulation loop:

  - rental income and operating expenses from a held property,
  - one-shot property-sale cash inflow at the sale month,
  - partner scheduled contributions,
  - property-tax / HOA / insurance / maintenance obligation accruals.

Today the engine reads these from a fistful of `(rollouts, months)` ndarrays
(`property_cash_flow.column(...)`, `partner_equity.column(...)`,
`disposition.column(...)`, plus the four `*_obligation_due` matrices).
This module folds them into a single long-form polars frame keyed by
`(rollout_index, month_index, kind)`. The frame is the canonical
representation; the engine still reads from cached `(rollouts, months)`
ndarrays for per-month-loop speed via `ScheduledCashflows.amount_at(...)`.

Phase 0 of the state-vector simulation refactor
(`augur/plans/state_vector_simulation_refactor.md`): introduce the schema
and builder. Subsequent phases will rewire the engine to read this frame
exclusively and drop the per-kind matrices.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import polars as pl


class ScheduledCashflowKind(StrEnum):
    """What kind of scheduled per-month input this row carries.

    Cash-flow kinds add to a cash account (sign matters; positive = inflow,
    negative = outflow). Accrual kinds increase an obligation balance that
    the engine's policy chain must settle within the month (always
    non-negative). The discriminator is the only column the engine reads
    to dispatch the row to the right downstream consumer."""

    # Cash-flow kinds — `amount_usd` is signed; engine adds directly to cash.
    PROPERTY_NET_CASH_FLOW = "property_net_cash_flow"
    PROPERTY_SALE_CASH_FLOW = "property_sale_cash_flow"
    PARTNER_CONTRIBUTION_USED = "partner_contribution_used"

    # Obligation-accrual kinds — `amount_usd` is the dollar amount due.
    # The engine's policy chain settles each via cash payment or asset
    # sale; the same dollar accrues into the accounting trace as a
    # liability + expense pair.
    PROPERTY_TAX_ACCRUAL = "property_tax_accrual"
    HOA_ACCRUAL = "hoa_accrual"
    INSURANCE_ACCRUAL = "insurance_accrual"
    MAINTENANCE_ACCRUAL = "maintenance_accrual"


SCHEDULED_CASHFLOW_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "month_index": pl.Int64(),
    "kind": pl.Utf8(),
    "amount_usd": pl.Float64(),
}


_CASHFLOW_KINDS: tuple[ScheduledCashflowKind, ...] = (
    ScheduledCashflowKind.PROPERTY_NET_CASH_FLOW,
    ScheduledCashflowKind.PROPERTY_SALE_CASH_FLOW,
    ScheduledCashflowKind.PARTNER_CONTRIBUTION_USED,
)
_ACCRUAL_KINDS: tuple[ScheduledCashflowKind, ...] = (
    ScheduledCashflowKind.PROPERTY_TAX_ACCRUAL,
    ScheduledCashflowKind.HOA_ACCRUAL,
    ScheduledCashflowKind.INSURANCE_ACCRUAL,
    ScheduledCashflowKind.MAINTENANCE_ACCRUAL,
)


@dataclass(frozen=True)
class ScheduledCashflows:
    """Canonical scheduled-inputs frame plus a per-kind ndarray cache.

    The canonical representation is `frame` — a long-form polars frame
    keyed by `(rollout_index, month_index, kind)`. The engine's per-month
    loop reads via `amount_at(kind=..., month_position=...)` which returns
    a cached `(rollouts,)` slice from the per-kind `(rollouts, months)`
    ndarray. Cashflow kinds (signed) and accrual kinds (non-negative) are
    both present; consumers discriminate on `kind`.
    """

    frame: pl.DataFrame
    month_index: np.ndarray  # (months,) absolute calendar months
    _by_kind: dict[ScheduledCashflowKind, np.ndarray]

    def amount_at(self, *, kind: ScheduledCashflowKind, month_position: int) -> np.ndarray:
        """Return the `(rollouts,)` per-rollout amount for `kind` at the
        given month position (0-indexed offset into the simulation's
        month axis)."""
        return self._by_kind[kind][:, month_position]

    def matrix(self, kind: ScheduledCashflowKind) -> np.ndarray:
        """Return the `(rollouts, months)` matrix for `kind`. Engine uses
        this for column-major bulk reads (e.g. accumulator construction)."""
        return self._by_kind[kind]


def build_scheduled_cashflows(
    *,
    rollout_count: int,
    month_index: np.ndarray,
    property_net_cash_flow_usd: np.ndarray,
    property_sale_cash_flow_usd: np.ndarray,
    partner_contribution_used_usd: np.ndarray,
    property_tax_accrual_usd: np.ndarray,
    hoa_accrual_usd: np.ndarray,
    insurance_accrual_usd: np.ndarray,
    maintenance_accrual_usd: np.ndarray,
) -> ScheduledCashflows:
    """Build the scheduled-cashflow frame from per-kind `(rollouts, months)`
    matrices.

    All matrices must share shape `(rollout_count, len(month_index))`.
    Property-related cashflows / accruals are expected to already be
    masked by `property_live_mask` (zero in post-sale months); this
    builder does no masking of its own.
    """
    month_count = int(month_index.size)
    expected_shape = (rollout_count, month_count)
    by_kind: dict[ScheduledCashflowKind, np.ndarray] = {
        ScheduledCashflowKind.PROPERTY_NET_CASH_FLOW: property_net_cash_flow_usd,
        ScheduledCashflowKind.PROPERTY_SALE_CASH_FLOW: property_sale_cash_flow_usd,
        ScheduledCashflowKind.PARTNER_CONTRIBUTION_USED: partner_contribution_used_usd,
        ScheduledCashflowKind.PROPERTY_TAX_ACCRUAL: property_tax_accrual_usd,
        ScheduledCashflowKind.HOA_ACCRUAL: hoa_accrual_usd,
        ScheduledCashflowKind.INSURANCE_ACCRUAL: insurance_accrual_usd,
        ScheduledCashflowKind.MAINTENANCE_ACCRUAL: maintenance_accrual_usd,
    }
    for kind, matrix in by_kind.items():
        if matrix.shape != expected_shape:
            raise ValueError(
                f"scheduled-cashflow matrix {kind.value} has shape {matrix.shape}, expected {expected_shape}"
            )

    rollout_axis = np.repeat(np.arange(rollout_count, dtype=np.int64), month_count)
    month_axis = np.tile(month_index.astype(np.int64), rollout_count)
    blocks: list[pl.DataFrame] = []
    for kind, matrix in by_kind.items():
        blocks.append(
            pl.DataFrame(
                {
                    "rollout_index": rollout_axis,
                    "month_index": month_axis,
                    "kind": [kind.value] * (rollout_count * month_count),
                    "amount_usd": matrix.reshape(-1),
                },
                schema=SCHEDULED_CASHFLOW_SCHEMA,
            )
        )
    frame = pl.concat(blocks)
    return ScheduledCashflows(frame=frame, month_index=month_index, _by_kind=by_kind)


def derive_per_kind_matrices(
    frame: pl.DataFrame, *, rollout_count: int, month_index: np.ndarray
) -> dict[ScheduledCashflowKind, np.ndarray]:
    """Inverse of `build_scheduled_cashflows`: unmelt the frame back to
    per-kind `(rollouts, months)` ndarrays. Used by tests / debugging
    tools that need to verify the frame is round-tripable."""
    month_count = int(month_index.size)
    out: dict[ScheduledCashflowKind, np.ndarray] = {}
    for kind in (*_CASHFLOW_KINDS, *_ACCRUAL_KINDS):
        rows = (
            frame.filter(pl.col("kind") == kind.value)
            .sort(["rollout_index", "month_index"])
            .select("amount_usd")
            .to_numpy()
            .reshape(rollout_count, month_count)
        )
        out[kind] = rows
    return out

"""Append-only logs of state changes emitted by the simulation engine.

These logs are the eventual source of truth for the engine's per-(rollout,
month) state evolution. State matrices (`cash`,
`remaining_*_units_by_month`, …) become *derived* views over the logs +
initial state. See `augur/plans/state_vector_simulation_refactor.md` for
the full plan.

Phase 2 of the refactor introduces:

  - `CASHFLOW_LOG_SCHEMA` — one row per (rollout, month, actor_id,
    account_id, cause) cash delta.
  - `build_cashflow_log_from_scheduled(...)` — fold a `ScheduledCashflows`
    frame's cash-flow kinds into the log shape, attributed to one
    (actor_id, account_id) pair. Today the engine still maintains the
    `cash` matrix from its 1D `current_cash` local;
    `derive_cash_matrix(...)` reconstructs that matrix from the log +
    initial cash and downstream phases will assert parity then drop the
    matrix maintenance.

This module also exposes `PROPERTY_STATE_SCHEMA` and
`build_property_state_frame(...)` — the per-(rollout, month, property)
long-form frame whose rows are *facts* about each property
(live mask, value, cumulative depreciation), built once from the
precomputed property matrices. Not a log (no events accumulate);
included here because the rest of the long-form derived frames are
also defined here.

Asset-change and liability logs come in later phases.
"""

from __future__ import annotations

from enum import StrEnum

import numpy as np
import polars as pl

from augur.core.scheduled_cashflows import ScheduledCashflowKind, ScheduledCashflows


class CashflowCause(StrEnum):
    """Discriminator for why a cashflow row exists.

    Today's set covers scheduled per-month inputs; future phases add
    OBLIGATION_PAYMENT, ASSET_SALE_PROCEEDS, ASSET_PURCHASE,
    SPEND_POLICY, PARTNER_CONTRIBUTION_DECISION, ANNUAL_TAX_SETTLEMENT,
    etc. — emitted from inside settlement and the policy chain."""

    PROPERTY_NET_CASH_FLOW = "property_net_cash_flow"
    PROPERTY_SALE_CASH_FLOW = "property_sale_cash_flow"
    PARTNER_CONTRIBUTION_USED = "partner_contribution_used"


CASHFLOW_LOG_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "month_index": pl.Int64(),
    "actor_id": pl.Utf8(),
    "account_id": pl.Utf8(),
    "amount_delta_usd": pl.Float64(),
    "cause": pl.Utf8(),
}


_SCHEDULED_TO_CAUSE: dict[ScheduledCashflowKind, CashflowCause] = {
    ScheduledCashflowKind.PROPERTY_NET_CASH_FLOW: CashflowCause.PROPERTY_NET_CASH_FLOW,
    ScheduledCashflowKind.PROPERTY_SALE_CASH_FLOW: CashflowCause.PROPERTY_SALE_CASH_FLOW,
    ScheduledCashflowKind.PARTNER_CONTRIBUTION_USED: CashflowCause.PARTNER_CONTRIBUTION_USED,
}


def build_cashflow_log_from_scheduled(scheduled: ScheduledCashflows, *, actor_id: str, account_id: str) -> pl.DataFrame:
    """Fold a `ScheduledCashflows` frame's cash-flow kinds into the
    cashflow-log schema, attributed to a single `(actor_id, account_id)`
    pair.

    Engine today has one cash account (the primary owner's checking) for
    every scheduled cashflow; multi-account / multi-agent emission lands
    in Phase 5 alongside per-policy log emission. The output frame has
    one row per `(rollout, month, kind)` where the amount is non-zero;
    0-amount rows are dropped. `PROPERTY_NET_CASH_FLOW` and
    `PARTNER_CONTRIBUTION_USED` rows at month=0 are dropped to match the
    engine's `if month > 0:` guards at the main-loop application site.
    """
    blocks: list[pl.DataFrame] = []
    for kind, cause in _SCHEDULED_TO_CAUSE.items():
        matrix = scheduled.matrix(kind)
        rollout_count, month_count = matrix.shape
        rollout_axis = np.repeat(np.arange(rollout_count, dtype=np.int64), month_count)
        month_axis = np.tile(scheduled.month_index.astype(np.int64), rollout_count)
        amounts = matrix.reshape(-1)
        frame = pl.DataFrame(
            {
                "rollout_index": rollout_axis,
                "month_index": month_axis,
                "actor_id": [actor_id] * amounts.size,
                "account_id": [account_id] * amounts.size,
                "amount_delta_usd": amounts,
                "cause": [cause.value] * amounts.size,
            },
            schema=CASHFLOW_LOG_SCHEMA,
        )
        if kind is ScheduledCashflowKind.PROPERTY_NET_CASH_FLOW:
            frame = frame.filter(pl.col("month_index") > scheduled.month_index[0])
        if kind is ScheduledCashflowKind.PARTNER_CONTRIBUTION_USED:
            frame = frame.filter(pl.col("month_index") > scheduled.month_index[0])
        frame = frame.filter(pl.col("amount_delta_usd") != 0.0)
        blocks.append(frame)
    return pl.concat(blocks) if blocks else pl.DataFrame(schema=CASHFLOW_LOG_SCHEMA)


def derive_cash_matrix(
    log: pl.DataFrame,
    *,
    actor_id: str,
    account_id: str,
    initial_balance_per_rollout: np.ndarray,
    rollout_count: int,
    month_index: np.ndarray,
) -> np.ndarray:
    """Derive a `(rollouts, len(month_index))` cash matrix for one
    `(actor_id, account_id)` pair from `log` + the per-rollout starting
    balance. Each `[:, M]` column is `initial + sum_{m <= M}
    amount_delta_usd` for that pair."""
    month_count = int(month_index.size)
    filtered = log.filter((pl.col("actor_id") == actor_id) & (pl.col("account_id") == account_id))
    if filtered.height == 0:
        return np.broadcast_to(initial_balance_per_rollout[:, None], (rollout_count, month_count)).copy()
    per_month = (
        filtered.group_by(["rollout_index", "month_index"])
        .agg(pl.col("amount_delta_usd").sum())
        .sort(["rollout_index", "month_index"])
    )
    month_position_lookup = {int(m): idx for idx, m in enumerate(month_index.tolist())}
    deltas = np.zeros((rollout_count, month_count), dtype=np.float64)
    for row in per_month.iter_rows(named=True):
        position = month_position_lookup.get(int(row["month_index"]))
        if position is None:
            continue
        deltas[int(row["rollout_index"]), position] = row["amount_delta_usd"]
    cumulative = np.cumsum(deltas, axis=1)
    return initial_balance_per_rollout[:, None] + cumulative


PROPERTY_STATE_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "month_index": pl.Int64(),
    "property_id": pl.Utf8(),
    "live": pl.Float64(),
    "value_usd": pl.Float64(),
    "cumulative_depreciation_usd": pl.Float64(),
}


def build_property_state_frame(
    *,
    property_id: str,
    month_index: np.ndarray,
    live: np.ndarray,
    value_usd: np.ndarray,
    cumulative_depreciation_usd: np.ndarray,
) -> pl.DataFrame:
    """Build the long-form `property_state_frame` for one property.

    Inputs are `(rollouts, months)` matrices precomputed by the engine
    (`property_live_mask`, `property_value`, and
    `disposition.column("cumulative_property_depreciation_usd")`); the
    output has one row per `(rollout, month)` with the property facts
    flattened to long form. Property facts are shared across all
    agents — no `actor_id` keying."""
    rollout_count, month_count = live.shape
    if value_usd.shape != live.shape or cumulative_depreciation_usd.shape != live.shape:
        msg = f"shape mismatch: live={live.shape}, value={value_usd.shape}, depr={cumulative_depreciation_usd.shape}"
        raise ValueError(msg)
    if month_index.size != month_count:
        msg = f"month_index size {month_index.size} does not match matrix month axis {month_count}"
        raise ValueError(msg)
    rollout_axis = np.repeat(np.arange(rollout_count, dtype=np.int64), month_count)
    month_axis = np.tile(month_index.astype(np.int64), rollout_count)
    return pl.DataFrame(
        {
            "rollout_index": rollout_axis,
            "month_index": month_axis,
            "property_id": [property_id] * (rollout_count * month_count),
            "live": live.reshape(-1),
            "value_usd": value_usd.reshape(-1),
            "cumulative_depreciation_usd": cumulative_depreciation_usd.reshape(-1),
        },
        schema=PROPERTY_STATE_SCHEMA,
    )

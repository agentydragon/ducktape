"""Append-only logs of state changes emitted by the simulation engine.

These logs are the eventual source of truth for the engine's per-(rollout,
month) state evolution. State matrices (`cash`,
`remaining_*_units_by_month`, …) become *derived* views over the logs +
initial state. See `augur/plans/state_vector_simulation_refactor.md` for
the full plan.

This module exposes:

  - `CASHFLOW_LOG_SCHEMA` — one row per (rollout, month, actor_id,
    account_id, cause) cash delta.
  - `build_cashflow_log_from_scheduled(...)` — fold a `ScheduledCashflows`
    frame's cash-flow kinds into the log shape.
  - `derive_cash_matrix(...)` — running-balance reconstruction.
  - `PROPERTY_STATE_SCHEMA` and `build_property_state_frame(...)` —
    long-form per-(rollout, month, property) facts (live, value,
    cumulative depreciation).
  - `ASSET_CHANGE_LOG_SCHEMA` and `derive_per_month_taxable_gain_matrix(...)`
    — unified sale-event log replacing today's three parallel
    per-asset-class record lists. Capital gains across SP500 /
    crypto / PE / property all share this shape, with `asset_kind`
    and `tax_treatment` discriminators. Year-end tax is a group-by
    on `tax_treatment`; the per-month per-asset-class gain matrices
    (`generic_sp500_sale_gain`, etc.) become filter+group_by views
    over this log instead of imperative accumulators in the engine.

Liability and stake logs come in later phases.
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


class AssetKindForLog(StrEnum):
    """Asset-kind discriminator on the `asset_change_log` events frame.

    Mirrors `augur.core.simulation_state.AssetKind` plus `PROPERTY` for
    property dispositions. Kept here (rather than imported) to avoid a
    circular dep with simulation_state — the values are stable strings
    that downstream consumers (tax math, materializers) filter on."""

    GENERIC_SP500 = "generic_sp500"
    CRYPTO = "crypto"
    PRIVATE_EQUITY = "private_equity"
    PROPERTY = "property"


class TaxTreatment(StrEnum):
    """Tax-treatment bucket on a capital-gain event. Drives how
    `taxable_gain_usd` feeds the year-end tax computation.

    LONG_TERM_CAPITAL: held >1y, federal LTCG rates + state.
    SHORT_TERM_CAPITAL: held ≤1y, ordinary rates.
    DEPRECIATION_RECAPTURE_1250: federal §1250 unrecaptured gain, capped
        at 25%. Today only emitted from property sale events.
    """

    LONG_TERM_CAPITAL = "long_term_capital"
    SHORT_TERM_CAPITAL = "short_term_capital"
    DEPRECIATION_RECAPTURE_1250 = "depreciation_recapture_1250"


ASSET_CHANGE_LOG_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "month_index": pl.Int64(),
    "actor_id": pl.Utf8(),
    "asset_id": pl.Utf8(),
    "asset_kind": pl.Utf8(),
    "delta_units": pl.Float64(),
    "delta_basis_usd": pl.Float64(),
    "cash_proceeds_usd": pl.Float64(),
    "taxable_gain_usd": pl.Float64(),
    "tax_treatment": pl.Utf8(),  # null for purchases / non-taxable changes
    "cause_kind": pl.Utf8(),
    "cause_id": pl.Utf8(),
}


def derive_per_month_taxable_gain_matrix(
    events: pl.DataFrame,
    *,
    rollout_count: int,
    month_index: np.ndarray,
    asset_kind: AssetKindForLog | None = None,
    tax_treatment: TaxTreatment | None = None,
    actor_id: str | None = None,
) -> np.ndarray:
    """Group taxable-gain events by `(rollout, month)` and sum, producing
    a `(rollouts, len(month_index))` matrix that replaces today's
    imperative per-asset-class gain matrices.

    Filters narrow the events frame before the group-by:

      - `asset_kind`: filter to one asset class (e.g. SP500 only).
      - `tax_treatment`: filter to one tax bucket (e.g. recapture only).
      - `actor_id`: filter to one agent (single-actor scenarios pass
        the primary owner; multi-actor scenarios materialize per-actor
        tax separately).

    Pass none of these to sum all gains across everything (rare —
    typically year-end tax math filters by `tax_treatment` per bucket).
    """
    month_count = int(month_index.size)
    if events.height == 0:
        return np.zeros((rollout_count, month_count), dtype=np.float64)
    filtered = events
    if asset_kind is not None:
        filtered = filtered.filter(pl.col("asset_kind") == asset_kind.value)
    if tax_treatment is not None:
        filtered = filtered.filter(pl.col("tax_treatment") == tax_treatment.value)
    if actor_id is not None:
        filtered = filtered.filter(pl.col("actor_id") == actor_id)
    if filtered.height == 0:
        return np.zeros((rollout_count, month_count), dtype=np.float64)
    per_month = (
        filtered.group_by(["rollout_index", "month_index"])
        .agg(pl.col("taxable_gain_usd").sum())
        .sort(["rollout_index", "month_index"])
    )
    month_position_lookup = {int(m): idx for idx, m in enumerate(month_index.tolist())}
    matrix = np.zeros((rollout_count, month_count), dtype=np.float64)
    for row in per_month.iter_rows(named=True):
        position = month_position_lookup.get(int(row["month_index"]))
        if position is None:
            continue
        matrix[int(row["rollout_index"]), position] = row["taxable_gain_usd"]
    return matrix


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

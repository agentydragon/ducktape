"""Column-major builders + materializers for per-rollout-per-month event streams.

The simulation engine accumulates per-event records (effects, decisions,
obligations, failure events, …) during `run_scenario_vectorized`. Historically
these were `list[PydanticModel]` accumulated row-by-row inside the per-month
loop, then sorted, `model_copy(update=trajectory_id)`'d, frozen into
`tuple[Effect, ...]` on `ScenarioRunArrays`. py-spy showed ~75% of simulate
time going to that Pydantic construction + copy + sort dance.

This module holds the column-major replacement: long-format polars frames
keyed by `(rollout_index, month_index, …)`, identity-joined once at the end
of the run instead of per-record `model_copy`. The Pydantic `tuple[...]`
surface stays — `ScenarioRunArrays` exposes `@property` shims that
materialize records lazily from the underlying frame(s) so test access and
wire-response paths read unchanged.

See `augur/plans/event_stream_polars_refactor.md` for the migration plan and
target shape. Roots-to-leaves: each root data table maps to one or more
Pydantic streams via projection/filter/join; the streams are not their own
roots.

Migrated so far:

* **Obligation lifecycle** (root) → `Obligation`, `SettlementResult`,
  `FailureEvent` (the latter two are filter+projection over the same root
  frame; one accumulator, three Pydantic surfaces).
* **Funding decisions** (root) → `FundingDecision` (separate cardinality
  from obligations — multiple funding decisions per obligation when the
  policy tries cash, then sells SP500, then crypto, etc.).
* **Lot dispositions** (root) → `LotDisposition` (one row per tax-lot
  consumption during a sale event).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import polars as pl

from augur.core.accounting import LotAssetClass, LotDisposition
from augur.core.scenario_set import (
    AccountType,
    AssetType,
    FailureEvent,
    FailureEventType,
    FundingDecision,
    FundingDecisionType,
    FundingSourceType,
    Obligation,
    ObligationStatus,
    ObligationType,
    SettlementResult,
    SettlementStatus,
)


class StreamFrameBuilder:
    """Accumulates per-recorder-call row-blocks (`dict[str, np.ndarray | list]`)
    and concatenates them into one `pl.DataFrame` on `build()`.

    Schema is declared up-front so empty builders still produce a frame with
    the right columns + dtypes, and individual `extend` calls don't have to
    care about column order.

    Builders are mutable; callers own the lifecycle (typically one builder
    per stream per scenario run, fed by recorders during the per-month loop,
    drained once at end-of-run).

    `block_count()` + `build_slice(start)` exist for mid-run consumers
    (e.g. `_estimated_payments_credit_per_year_usd`) that need to read back
    the rows just emitted by a sub-loop — the equivalent of
    `recorded_list[initial_count:]` for the legacy Python-list path."""

    def __init__(self, schema: dict[str, pl.DataType]) -> None:
        self._schema = schema
        self._blocks: list[dict[str, Any]] = []

    def extend(self, columns: dict[str, Any]) -> None:
        if missing := set(self._schema) - set(columns):
            raise KeyError(f"missing columns for stream block: {sorted(missing)}")
        if extra := set(columns) - set(self._schema):
            raise KeyError(f"unexpected columns for stream block: {sorted(extra)}")
        self._blocks.append(columns)

    def build(self) -> pl.DataFrame:
        return self._concat(self._blocks)

    def block_count(self) -> int:
        return len(self._blocks)

    def build_slice(self, start_block_index: int) -> pl.DataFrame:
        return self._concat(self._blocks[start_block_index:])

    def _concat(self, blocks: list[dict[str, Any]]) -> pl.DataFrame:
        if not blocks:
            return pl.DataFrame(schema=self._schema)
        return pl.concat([pl.DataFrame(block, schema=self._schema) for block in blocks])


# Identity columns added to every event stream at end-of-run by
# `join_trajectory_identity`. Mirrors the four trace-identity fields in
# `scenario_set._TraceBase` (`path_set_id`, `exogenous_path_id`,
# `scenario_input_id`, `projection_trajectory_id`).
_IDENTITY_COLUMN_NAMES: tuple[str, ...] = (
    "path_set_id",
    "exogenous_path_id",
    "scenario_input_id",
    "projection_trajectory_id",
)


def build_identity_frame(identity_by_rollout: Mapping[int, Mapping[str, str]]) -> pl.DataFrame:
    """Materialize the `rollout_index -> {trajectory identity fields}` mapping
    built by `_trace_identity_by_rollout` into a small polars frame for
    one-shot left-joining onto event-stream frames."""

    rows = sorted(identity_by_rollout.items())
    columns: dict[str, list[Any]] = {"rollout_index": [int(rollout) for rollout, _ in rows]}
    for column_name in _IDENTITY_COLUMN_NAMES:
        columns[column_name] = [identity.get(column_name) for _, identity in rows]
    schema = {"rollout_index": pl.Int64} | dict.fromkeys(_IDENTITY_COLUMN_NAMES, pl.String)
    return pl.DataFrame(columns, schema=schema)


def join_trajectory_identity(df: pl.DataFrame, identity_df: pl.DataFrame) -> pl.DataFrame:
    """Stamp the per-rollout trajectory identity columns onto an event-stream
    frame in one shot, replacing the per-record `model_copy(update=...)` pass
    in `_with_trajectory_identity`."""

    return df.join(identity_df, on="rollout_index", how="left")


# -- obligation lifecycle ------------------------------------------------------
#
# Single source of truth for `Obligation`, `SettlementResult`, `FailureEvent`.
# All three Pydantic surfaces are projection/filter views over the same frame:
#
# * `obligations`         — the frame itself.
# * `settlement_results`  — strict column subset (drop `creditor_id`,
#                           `due_month_index`, `source_policy_id`, `required`).
#                           Pydantic uses `SettlementStatus` for the `status`
#                           field but the values are identical to
#                           `ObligationStatus` so the column is shared.
# * `failure_events`      — filter `unpaid_amount_usd > 0 & required`, then
#                           derive `failure_event_id = obligation_id + ":failure"`.
#                           Sort by `(month, rollout, failure_event_type,
#                           failure_event_id)` matches the legacy
#                           `_sorted_failure_events` key because failure_event_type
#                           is always `UNSETTLED_OBLIGATION` and `failure_event_id`
#                           sorts lex-equivalent to `obligation_id`.

OBLIGATION_LIFECYCLE_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "obligation_id": pl.String,
    "obligation_type": pl.String,
    "actor_id": pl.String,
    "creditor_id": pl.String,
    "due_month_index": pl.Int64,
    "amount_due_usd": pl.Float64,
    "amount_paid_usd": pl.Float64,
    "unpaid_amount_usd": pl.Float64,
    "status": pl.String,
    "source_policy_id": pl.String,
    "required": pl.Boolean,
}

_OBLIGATION_LIFECYCLE_SORT_KEY: tuple[str, ...] = ("month_index", "rollout_index", "obligation_type", "obligation_id")


def sort_obligation_lifecycle(df: pl.DataFrame) -> pl.DataFrame:
    """Polars equivalent of `_sorted_obligations` / `_sorted_settlement_results`
    over the legacy Pydantic lists. `_sorted_failure_events` uses
    `(month, rollout, failure_event_type, failure_event_id)` but
    `failure_event_type` is single-valued and `failure_event_id` sorts
    lex-equivalent to `obligation_id`, so this same key reproduces it on
    the filtered subset."""

    return df.sort(list(_OBLIGATION_LIFECYCLE_SORT_KEY))


def materialize_obligations(df: pl.DataFrame) -> Iterator[Obligation]:
    for row in df.iter_rows(named=True):
        yield Obligation(
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            obligation_id=row["obligation_id"],
            obligation_type=ObligationType(row["obligation_type"]),
            actor_id=row["actor_id"],
            creditor_id=row["creditor_id"],
            due_month_index=int(row["due_month_index"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            unpaid_amount_usd=float(row["unpaid_amount_usd"]),
            status=ObligationStatus(row["status"]),
            source_policy_id=row["source_policy_id"],
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )


def materialize_settlement_results(df: pl.DataFrame) -> Iterator[SettlementResult]:
    for row in df.iter_rows(named=True):
        yield SettlementResult(
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            obligation_id=row["obligation_id"],
            obligation_type=ObligationType(row["obligation_type"]),
            actor_id=row["actor_id"],
            status=SettlementStatus(row["status"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            unpaid_amount_usd=float(row["unpaid_amount_usd"]),
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )


# -- funding decisions ---------------------------------------------------------
#
# Separate cardinality from obligations (multiple funding decisions per
# obligation when the policy tries cash, then sells SP500, then crypto, etc.),
# so this is its own root frame. Sort key matches the legacy
# `_sorted_funding_decisions` Python-list sort:
# `(month, rollout, fillna(policy_sequence_index, -1), decision_type,
#   fillna(policy_id, ""), obligation_id)`.

FUNDING_DECISION_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "obligation_id": pl.String,
    "decision_type": pl.String,
    "actor_id": pl.String,
    "policy_id": pl.String,
    "policy_sequence_index": pl.Int64,
    "source_type": pl.String,
    "source_account_id": pl.String,
    "source_account_type": pl.String,
    "source_asset_id": pl.String,
    "source_asset_type": pl.String,
    "available_cash_usd": pl.Float64,
    "requested_cash_usd": pl.Float64,
    "requested_sale_usd": pl.Float64,
    "funded_cash_usd": pl.Float64,
    "shortfall_usd": pl.Float64,
}


def sort_funding_decisions(df: pl.DataFrame) -> pl.DataFrame:
    """Polars equivalent of `_sorted_funding_decisions` over the Pydantic list.
    `policy_sequence_index = None` sorted as `-1` and `policy_id = None`
    sorted as empty string in the legacy code, so we fill-null those columns
    into transient sort keys."""

    return (
        df.with_columns(
            pl.col("policy_sequence_index").fill_null(-1).alias("_sort_seq"),
            pl.col("policy_id").fill_null("").alias("_sort_pid"),
        )
        .sort(["month_index", "rollout_index", "_sort_seq", "decision_type", "_sort_pid", "obligation_id"])
        .drop(["_sort_seq", "_sort_pid"])
    )


def _row_optional_enum(row: dict[str, Any], column: str, enum_cls: type) -> Any:
    value = row.get(column)
    if value is None:
        return None
    return enum_cls(value)


def materialize_funding_decisions(df: pl.DataFrame) -> Iterator[FundingDecision]:
    for row in df.iter_rows(named=True):
        yield FundingDecision(
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            obligation_id=row["obligation_id"],
            decision_type=FundingDecisionType(row["decision_type"]),
            actor_id=row["actor_id"],
            policy_id=row["policy_id"],
            policy_sequence_index=row["policy_sequence_index"],
            source_type=_row_optional_enum(row, "source_type", FundingSourceType),
            source_account_id=row["source_account_id"],
            source_account_type=_row_optional_enum(row, "source_account_type", AccountType),
            source_asset_id=row["source_asset_id"],
            source_asset_type=_row_optional_enum(row, "source_asset_type", AssetType),
            available_cash_usd=float(row["available_cash_usd"]),
            requested_cash_usd=float(row["requested_cash_usd"]),
            requested_sale_usd=float(row["requested_sale_usd"]),
            funded_cash_usd=float(row["funded_cash_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )


# -- lot dispositions ----------------------------------------------------------
#
# One row per tax-lot consumption during a sale (property, SP500 stock, crypto,
# private equity). Sort key from the legacy `_sorted_lot_dispositions` is
# `(month, rollout, asset_class, lot_disposition_id)`.

LOT_DISPOSITION_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "lot_disposition_id": pl.String,
    "journal_entry_id": pl.String,
    "lot_id": pl.String,
    "asset_class": pl.String,
    "proceeds_usd": pl.Float64,
    "cost_basis_usd": pl.Float64,
    "realized_gain_usd": pl.Float64,
    "taxable_gain_usd": pl.Float64,
    "quantity_sold": pl.Float64,
    "tax_expense_usd": pl.Float64,
}


def sort_lot_dispositions(df: pl.DataFrame) -> pl.DataFrame:
    return df.sort(["month_index", "rollout_index", "asset_class", "lot_disposition_id"])


def materialize_lot_dispositions(df: pl.DataFrame) -> Iterator[LotDisposition]:
    for row in df.iter_rows(named=True):
        yield LotDisposition(
            lot_disposition_id=row["lot_disposition_id"],
            journal_entry_id=row["journal_entry_id"],
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            lot_id=row["lot_id"],
            asset_class=LotAssetClass(row["asset_class"]),
            proceeds_usd=float(row["proceeds_usd"]),
            cost_basis_usd=float(row["cost_basis_usd"]),
            realized_gain_usd=float(row["realized_gain_usd"]),
            taxable_gain_usd=float(row["taxable_gain_usd"]),
            quantity_sold=row["quantity_sold"],  # nullable
            tax_expense_usd=float(row["tax_expense_usd"]),
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )


def materialize_failure_events(df: pl.DataFrame) -> Iterator[FailureEvent]:
    """`df` is the full obligation lifecycle frame; we filter to the failed
    rows (`unpaid_amount_usd > 0 & required`) inside this function so callers
    don't have to keep two frames around."""

    failed = df.filter((pl.col("unpaid_amount_usd") > 0) & pl.col("required"))
    for row in failed.iter_rows(named=True):
        obligation_id = row["obligation_id"]
        yield FailureEvent(
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            failure_event_id=f"{obligation_id}:failure",
            failure_event_type=FailureEventType.UNSETTLED_OBLIGATION,
            obligation_id=obligation_id,
            actor_id=row["actor_id"],
            unpaid_amount_usd=float(row["unpaid_amount_usd"]),
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )

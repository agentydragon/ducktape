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
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import polars as pl

from augur.core.scenario_set import (
    FailureEvent,
    FailureEventType,
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

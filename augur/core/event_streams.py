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
surface stays — `ScenarioRunArrays` exposes `@cached_property` shims that
materialize records lazily from the underlying frame(s) so test access and
wire-response paths read unchanged.

See `augur/plans/event_stream_polars_refactor.md` for the migration plan and
target shape across all 9 streams. This module ships the infrastructure plus
the first migrated stream (`failure_events`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import polars as pl

from augur.core.scenario_set import FailureEvent, FailureEventType


class StreamFrameBuilder:
    """Accumulates per-recorder-call row-blocks (`dict[str, np.ndarray | list]`)
    and concatenates them into one `pl.DataFrame` on `build()`.

    Schema is declared up-front so empty builders still produce a frame with
    the right columns + dtypes, and individual `extend` calls don't have to
    care about column order.

    Builders are mutable; callers own the lifecycle (typically one builder
    per stream per scenario run, fed by recorders during the per-month loop,
    drained once at end-of-run)."""

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
        if not self._blocks:
            return pl.DataFrame(schema=self._schema)
        return pl.concat([pl.DataFrame(block, schema=self._schema) for block in self._blocks])


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


# -- failure_events ------------------------------------------------------------

FAILURE_EVENT_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "failure_event_id": pl.String,
    "failure_event_type": pl.String,
    "obligation_id": pl.String,
    "actor_id": pl.String,
    "unpaid_amount_usd": pl.Float64,
}

_FAILURE_EVENT_SORT_KEY: tuple[str, ...] = ("month_index", "rollout_index", "failure_event_type", "failure_event_id")


def sort_failure_events(df: pl.DataFrame) -> pl.DataFrame:
    """Polars equivalent of `_sorted_failure_events` over the Pydantic list.

    Sort columns match the legacy tuple key
    `(month_index, rollout_index, failure_event_type, failure_event_id)`."""

    return df.sort(list(_FAILURE_EVENT_SORT_KEY))


def materialize_failure_events(df: pl.DataFrame) -> Iterator[FailureEvent]:
    """Lazily reconstruct `FailureEvent` instances from the long-format frame
    so `ScenarioRunArrays.failure_events` keeps returning the Pydantic tuple
    the wire schema + test surface expect."""

    for row in df.iter_rows(named=True):
        yield FailureEvent(
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            failure_event_id=row["failure_event_id"],
            failure_event_type=FailureEventType(row["failure_event_type"]),
            obligation_id=row["obligation_id"],
            actor_id=row["actor_id"],
            unpaid_amount_usd=float(row["unpaid_amount_usd"]),
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )


def empty_failure_events_frame() -> pl.DataFrame:
    """Frame shape used when a scenario emits zero failure events. Same column
    schema + identity columns as a populated post-join frame so consumers
    (sort, materialize, `rollout_statuses` aggregation) don't have to branch
    on emptiness."""

    schema = FAILURE_EVENT_SCHEMA | dict.fromkeys(_IDENTITY_COLUMN_NAMES, pl.String)
    return pl.DataFrame(schema=schema)

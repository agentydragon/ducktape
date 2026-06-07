"""Shared helpers used by per-domain decoders in `augur.sim.codec`.

These are the codec-side counterparts to the string table + slot maps the compiler
emits: every decoder lifts integer codes back to string IDs, derives flat (month,
rollout, slot) index columns from state buffers, and builds Polars frames that
match the event/state-frame schemas declared in `augur.sim.state` + `augur.sim.events`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import polars as pl

from augur.sim.compiler import CompiledSimulation
from augur.sim.fixed_point import cents_array_to_usd


@dataclass(frozen=True)
class CodeColumn:
    """A string output column carried as integer codes + a (small) category table.

    The frame builders materialize it via a single Arrow dict-gather (`categories[codes]`)
    instead of constructing a `pl.Utf8` column from a NumPy `object` array of Python `str`
    (the GIL-bound `new_str` path that dominated decode at scale). `codes < 0` → null.
    """

    codes: np.ndarray
    categories: Sequence[str | None]


def code_column(plan: CompiledSimulation, codes: np.ndarray) -> CodeColumn:
    """A `CodeColumn` for ids interned in `plan.strings` (agents, accounts, lots, causes, …)."""
    return CodeColumn(codes=codes, categories=plan.strings)


def asset_code_column(plan: CompiledSimulation, codes: np.ndarray) -> CodeColumn:
    """A `CodeColumn` mapping asset codes to their wire ids (the frontend's `asset_id`)."""
    return CodeColumn(codes=codes, categories=[asset.wire_id for asset in plan.assets])


def _gather_str(name: str, column: CodeColumn) -> pl.Series:
    # Prepend a null category so code -1 (NO_CODE) maps to null after the +1 shift; clamp any
    # other negative code to that null slot too.
    categories = pl.Series("", [None, *column.categories], dtype=pl.Utf8())
    codes = np.asarray(column.codes, dtype=np.int64)
    indices = np.where(codes < 0, -1, codes) + 1
    return categories.gather(indices).rename(name)


def _columns_to_polars(columns: dict[str, Any]) -> dict[str, Any]:
    return {
        name: (_gather_str(name, value) if isinstance(value, CodeColumn) else value) for name, value in columns.items()
    }


def _column_size(value: Any) -> int:
    return value.codes.size if isinstance(value, CodeColumn) else value.size


def text(plan: CompiledSimulation, code: int) -> str | None:
    if code < 0:
        return None
    return plan.strings[code]


def codes_to_strings(plan: CompiledSimulation, codes: np.ndarray) -> np.ndarray:
    """Vectorize `text` over an int-code array; preserves the input shape.

    Output dtype is `object` (str | None entries). Polars will infer pl.Utf8 on
    DataFrame construction; None becomes null."""

    flat = np.asarray(codes, dtype=np.int64).reshape(-1)
    out = np.empty(flat.size, dtype=object)
    strings = plan.strings
    for i in range(flat.size):
        code = int(flat[i])
        out[i] = strings[code] if code >= 0 else None
    return out.reshape(np.asarray(codes).shape)


def codes_to_asset_wire_ids(plan: CompiledSimulation, codes: np.ndarray) -> np.ndarray:
    """Vectorize asset-code → wire-id over an int-code array; preserves shape.

    Asset codes index `plan.assets` (typed `AssetKey`); the output is each asset's wire-id
    string (or None for NO_CODE) for the `asset_id` output column + cause-id interpolation —
    the output-frame string contract the frontend reads. Decode that needs the *type* reads
    `plan.assets[code]` directly instead."""

    flat = np.asarray(codes, dtype=np.int64).reshape(-1)
    out = np.empty(flat.size, dtype=object)
    assets = plan.assets
    for i in range(flat.size):
        code = int(flat[i])
        out[i] = assets[code].wire_id if code >= 0 else None
    return out.reshape(np.asarray(codes).shape)


def r_first_view(state: np.ndarray) -> np.ndarray:
    """Move R from the trailing axis (the layout the JAX scan emits) to axis 1, so decoders can
    iterate `(h1, r, count[, ...])` row-major over the resulting flat buffer."""

    return np.moveaxis(state, -1, 1)


def usd_column(cents: Any) -> np.ndarray:
    return cents_array_to_usd(cents)


def quantity_column(quanta: Any, scales: Any) -> np.ndarray:
    return np.asarray(quanta, dtype=np.float64) / np.asarray(scales, dtype=np.float64)


def lot_quantity_column(plan: CompiledSimulation, lots: np.ndarray, quanta: Any) -> np.ndarray:
    return quantity_column(quanta, plan.lot_quantity_scale[np.asarray(lots, dtype=np.int64)])


def state_axes(h1: int, r: int, s: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Ravelled (month, rollout, slot) index columns for a state buffer of shape `(h1, r, s)`.

    Order is row-major over `(month, rollout, slot)` — matches the iteration order the
    old list-of-dicts decoders used so the resulting frame's row order is preserved.
    """

    months = np.broadcast_to(np.arange(h1, dtype=np.int64)[:, None, None], (h1, r, s)).ravel()
    rollouts = np.broadcast_to(np.arange(r, dtype=np.int64)[None, :, None], (h1, r, s)).ravel()
    slots = np.broadcast_to(np.arange(s, dtype=np.int64)[None, None, :], (h1, r, s)).ravel()
    return months, rollouts, slots


def state_history_frame_from_columns(columns: dict[str, Any], spec: Any) -> pl.DataFrame:
    """Build a state-history frame from pre-built numpy column arrays. State-history specs
    don't carry `month_index` in their schema (the cross-section is one month wide); decode
    adds month_index in front of every column the spec declares, so this helper threads
    `rollout_index`, `month_index`, and the spec's remaining columns in the expected order.
    Empty input produces a correctly-typed empty frame."""

    state_schema = pl.Schema(
        {
            "rollout_index": pl.Int64(),
            "month_index": pl.Int64(),
            **{name: dtype for name, dtype in spec.schema.items() if name != "rollout_index"},
        }
    )
    n = _column_size(next(iter(columns.values())))
    if n == 0:
        return state_schema.to_frame()
    return pl.DataFrame(_columns_to_polars(columns), schema=state_schema).select(list(state_schema.names()))


def frame_from_columns(spec: Any, **columns: Any) -> pl.DataFrame:
    """Materialize an event frame from numpy column arrays. Empty input produces a
    correctly-typed empty frame matching the spec's schema. Polars cast/infer is driven by
    `spec.schema` so object-dtype numpy arrays of Python strings become `pl.Utf8` (rather
    than `pl.Object`, which breaks downstream concat between dense and empty frames)."""

    n = _column_size(next(iter(columns.values())))
    if n == 0:
        return cast(pl.DataFrame, spec.empty())
    df: pl.DataFrame = pl.DataFrame(_columns_to_polars(columns), schema=spec.schema)
    return cast(pl.DataFrame, df.select(spec.schema.names()))

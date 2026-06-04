"""Slice a batched DenseSimulationResult into single-rollout (R=1) results."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

import numpy as np
import polars as pl

from augur.model.private_equity_bundle import PrivateEquityBundle
from augur.sim.buffers import SimulationBuffers, TaxLiabilityChange, TaxLiabilityChangeLog
from augur.sim.codec.plan import DenseSimulationResult
from augur.sim.external_series import ExternalSeriesContext


def slice_dense_results(dense: DenseSimulationResult, rollout_indices: Sequence[int]) -> list[DenseSimulationResult]:
    """Slice a batched result into one R=1 result per requested rollout.

    Each dense array field is split across all requested rollouts in a single pass
    (`_split_dc`/`_split_array`), so the buffer dataclass structure is walked once
    rather than once per rollout. The exogenous Polars frames (`series_values`, the PE
    bundle) are likewise partitioned by `rollout_index` once up front, keeping the whole
    operation O(R) instead of the O(R²) of filtering the batch frame per rollout."""
    indices = list(rollout_indices)
    series_by_rollout = _partition_by_rollout(dense.external_series.series_values)
    pe_by_rollout = (
        None
        if dense.external_series.private_equity.is_empty()
        else _partition_by_rollout(dense.external_series.private_equity.frame)
    )
    series_schema = dense.external_series.series_values.clear()

    external_values = _split_array(dense.plan.external_values, axis=1, indices=indices)
    pe_channels = _split_dc(dense.plan.pe_channels, axis=1, indices=indices)
    state = _split_dc(dense.buffers.state, axis=-1, indices=indices)
    transfers = _split_dc(dense.buffers.transfers, axis=-1, indices=indices)
    properties = _split_dc(dense.buffers.properties, axis=-1, indices=indices)
    lot_dispositions = _split_dc(dense.buffers.lot_dispositions, axis=-1, indices=indices)
    private_equity_opportunities = _split_dc(dense.buffers.private_equity_opportunities, axis=-1, indices=indices)
    taxes = _split_dc(dense.buffers.taxes, axis=-1, indices=indices)
    obligations = _split_dc(dense.buffers.obligations, axis=-1, indices=indices)
    primary_residence = _split_dc(dense.buffers.primary_residence, axis=-1, indices=indices)
    lifecycle = _split_dc(dense.buffers.lifecycle, axis=-1, indices=indices)

    results: list[DenseSimulationResult] = []
    for pos, rollout_index in enumerate(indices):
        plan = dataclasses.replace(
            dense.plan,
            rollout_count=1,
            slot_plan=dataclasses.replace(dense.plan.slot_plan, rollout_count=1),
            external_values=external_values[pos],
            pe_channels=pe_channels[pos],
        )
        buffers = SimulationBuffers(
            state=state[pos],
            transfers=transfers[pos],
            properties=properties[pos],
            lot_dispositions=lot_dispositions[pos],
            private_equity_opportunities=private_equity_opportunities[pos],
            taxes=taxes[pos],
            obligations=obligations[pos],
            primary_residence=primary_residence[pos],
            lifecycle=lifecycle[pos],
            tax_liability_changes=_slice_tax_liability_changes(dense.buffers.tax_liability_changes, rollout_index),
        )
        series_values = series_by_rollout.get(rollout_index, series_schema).with_columns(
            rollout_index=pl.lit(0, dtype=pl.Int64)
        )
        if pe_by_rollout is None:
            private_equity = dense.external_series.private_equity
        else:
            pe_frame = pe_by_rollout.get(rollout_index, dense.external_series.private_equity.frame.clear())
            private_equity = PrivateEquityBundle(frame=pe_frame.with_columns(rollout_index=pl.lit(0, dtype=pl.Int64)))
        external_series = ExternalSeriesContext(series_values=series_values, private_equity=private_equity)
        results.append(DenseSimulationResult(plan=plan, buffers=buffers, external_series=external_series))
    return results


def slice_dense_result(dense: DenseSimulationResult, *, rollout_index: int) -> DenseSimulationResult:
    """Return an R=1 DenseSimulationResult for one rollout of a batched result.

    The cached slice owns its own contiguous memory (via `.copy()` on every array) so the
    source batch can be released and the per-rollout LRU can evict each slice independently."""
    return slice_dense_results(dense, (rollout_index,))[0]


def _partition_by_rollout(frame: pl.DataFrame) -> dict[int, pl.DataFrame]:
    """Group a `rollout_index`-keyed frame into one sub-frame per rollout, in a single pass."""
    return {
        int(part.get_column("rollout_index")[0]): part
        for part in frame.partition_by("rollout_index")
        if part.height > 0
    }


def _slice_tax_liability_changes(log: TaxLiabilityChangeLog, rollout_index: int) -> TaxLiabilityChangeLog:
    """Restrict each tax-liability change block to one rollout (keeping the slot axis)."""
    return TaxLiabilityChangeLog(
        changes=[
            TaxLiabilityChange(
                snapshot_month=change.snapshot_month,
                slots=change.slots.copy(),
                amount=change.amount[:, rollout_index : rollout_index + 1].copy(),
                active=change.active[:, rollout_index : rollout_index + 1].copy(),
            )
            for change in log.changes
        ]
    )


def _split_array(arr: np.ndarray, *, axis: int, indices: Sequence[int]) -> list[np.ndarray]:
    """Split `arr` along `axis` into one owning, contiguous R=1 array per requested rollout.

    Uses basic slicing (`arr[..., i : i + 1, ...]`, a view) plus a single `.copy()`. This is
    both cheaper than a fancy `np.take(arr, [i], axis=...)` (basic slicing avoids the
    fancy-index slow path) and drops the redundant second copy that `np.take(...).copy()`
    performed — `take` already returns a fresh owning array."""
    selector: list[Any] = [slice(None)] * arr.ndim
    chunks: list[np.ndarray] = []
    for rollout_index in indices:
        selector[axis] = slice(rollout_index, rollout_index + 1)
        chunks.append(arr[tuple(selector)].copy())
    return chunks


def _split_dc[T](obj: T, *, axis: int, indices: Sequence[int]) -> list[T]:
    """Split every array field of a (possibly nested) dataclass along `axis`, returning one
    reconstructed R=1 dataclass per requested rollout. Walks the field structure once."""
    field_chunks: dict[str, list[Any]] = {}
    for field in dataclasses.fields(obj):  # type: ignore[arg-type]
        val = getattr(obj, field.name)
        if dataclasses.is_dataclass(val) and not isinstance(val, type):
            field_chunks[field.name] = _split_dc(val, axis=axis, indices=indices)
        else:
            field_chunks[field.name] = _split_array(val, axis=axis, indices=indices)
    cls = type(obj)
    return [cls(**{name: chunks[pos] for name, chunks in field_chunks.items()}) for pos in range(len(indices))]

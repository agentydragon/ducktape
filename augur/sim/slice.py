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

    The exogenous Polars frames (`series_values`, the PE bundle) are partitioned by
    `rollout_index` **once** up front, so slicing all R rollouts is O(R) rather than the
    O(R²) of filtering the whole batch frame per rollout."""
    series_by_rollout = _partition_by_rollout(dense.external_series.series_values)
    pe_by_rollout = (
        None
        if dense.external_series.private_equity.is_empty()
        else _partition_by_rollout(dense.external_series.private_equity.frame)
    )
    series_schema = dense.external_series.series_values.clear()
    return [
        _slice_one(dense, rollout_index, series_by_rollout, pe_by_rollout, series_schema)
        for rollout_index in rollout_indices
    ]


def slice_dense_result(dense: DenseSimulationResult, *, rollout_index: int) -> DenseSimulationResult:
    """Return an R=1 DenseSimulationResult for one rollout of a batched result.

    The cached slice owns its own memory (via `.copy()` on every array) so the
    source batch can be released."""
    return slice_dense_results(dense, (rollout_index,))[0]


def _slice_one(
    dense: DenseSimulationResult,
    rollout_index: int,
    series_by_rollout: dict[int, pl.DataFrame],
    pe_by_rollout: dict[int, pl.DataFrame] | None,
    series_schema: pl.DataFrame,
) -> DenseSimulationResult:
    sliced_pe_channels = _take_dc(dense.plan.pe_channels, rollout_index, axis=1)
    plan = dataclasses.replace(
        dense.plan,
        rollout_count=1,
        slot_plan=dataclasses.replace(dense.plan.slot_plan, rollout_count=1),
        external_values=dense.plan.external_values[:, rollout_index : rollout_index + 1, :].copy(),
        pe_channels=sliced_pe_channels,
    )
    buffers = SimulationBuffers(
        state=_take_dc(dense.buffers.state, rollout_index, axis=-1),
        transfers=_take_dc(dense.buffers.transfers, rollout_index, axis=-1),
        properties=_take_dc(dense.buffers.properties, rollout_index, axis=-1),
        lot_dispositions=_take_dc(dense.buffers.lot_dispositions, rollout_index, axis=-1),
        private_equity_opportunities=_take_dc(dense.buffers.private_equity_opportunities, rollout_index, axis=-1),
        taxes=_take_dc(dense.buffers.taxes, rollout_index, axis=-1),
        obligations=_take_dc(dense.buffers.obligations, rollout_index, axis=-1),
        primary_residence=_take_dc(dense.buffers.primary_residence, rollout_index, axis=-1),
        lifecycle=_take_dc(dense.buffers.lifecycle, rollout_index, axis=-1),
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
    return DenseSimulationResult(plan=plan, buffers=buffers, external_series=external_series)


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


def _take_dc[T](obj: T, rollout_index: int, *, axis: int) -> T:
    fields = dataclasses.fields(obj)  # type: ignore[arg-type]
    sliced: dict[str, Any] = {}
    for field in fields:
        val = getattr(obj, field.name)
        if dataclasses.is_dataclass(val) and not isinstance(val, type):
            sliced[field.name] = _take_dc(val, rollout_index, axis=axis)
        else:
            sliced[field.name] = np.take(val, [rollout_index], axis=axis).copy()
    return type(obj)(**sliced)

"""Slice a batched DenseSimulationResult into a single-rollout (R=1) result."""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import polars as pl

from augur.model.private_equity_bundle import PrivateEquityBundle
from augur.sim.buffers import SimulationBuffers, TaxLiabilityChange, TaxLiabilityChangeLog
from augur.sim.codec.plan import DenseSimulationResult
from augur.sim.external_series import ExternalSeriesContext


def slice_dense_result(dense: DenseSimulationResult, *, rollout_index: int) -> DenseSimulationResult:
    """Return an R=1 DenseSimulationResult for one rollout of a batched result.

    The cached slice owns its own memory (via `.copy()` on every array) so the
    source batch can be released."""
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
    external_series = ExternalSeriesContext(
        series_values=(
            dense.external_series.series_values.filter(pl.col("rollout_index") == rollout_index).with_columns(
                rollout_index=pl.lit(0, dtype=pl.Int64)
            )
        ),
        private_equity=_slice_pe_bundle(dense.external_series.private_equity, rollout_index=rollout_index),
    )
    return DenseSimulationResult(plan=plan, buffers=buffers, external_series=external_series)


def _slice_pe_bundle(pe: PrivateEquityBundle, *, rollout_index: int) -> PrivateEquityBundle:
    """Restrict a PE bundle to one rollout, remapping rollout indices to 0."""

    if pe.is_empty():
        return pe
    sliced = pe.frame.filter(pl.col("rollout_index") == rollout_index).with_columns(
        rollout_index=pl.lit(0, dtype=pl.Int64)
    )
    return PrivateEquityBundle(frame=sliced)


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

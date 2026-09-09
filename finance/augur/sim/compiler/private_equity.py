"""Quantize typed private-equity paths into the simulation engine's input channels."""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
from jaxtyping import Int64

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import PrivateEquityEventKindCode
from finance.augur.sim.compiler.helpers import NO_CODE
from finance.augur.sim.fixed_point import sampled_array_to_quanta


class PEExecutionChannels[ArrayT](NamedTuple):
    """Per-issuer channel arrays consumed by the simulation engine.

    Shape: `(issuer, rollout, month + 1)` for each channel. Built from the
    typed `PrivateEquityBundle` at compile time so the engine reads PE state
    by field access instead of going through `external_values[series_index]`.
    """

    mark_quanta: ArrayT
    regime_codes: ArrayT
    sale_opportunity_active: ArrayT
    sale_capacity_fractions: ArrayT
    eligible_fractions: ArrayT
    forced_sale_fractions: ArrayT
    liquidity_blocked: ArrayT
    forced_recovery_cashout_quanta: ArrayT


@dataclass(frozen=True)
class PEChannels:
    """Engine channels and their corresponding private-equity event kinds."""

    execution: PEExecutionChannels[np.ndarray]
    event_kind_codes: Int64[np.ndarray, " issuer rollout snapshot"]


def compile_pe_channels(
    issuer_ids: tuple[str, ...],
    *,
    private_equity: PrivateEquityBundle,
    rollout_count: int,
    horizon_months: int,
    currency_quantum: object,
) -> PEChannels:
    """Materialize per-issuer PE channel arrays from the typed `PrivateEquityBundle`.

    Returns shape `(issuer, rollout, month + 1)` for each channel. The bundle's
    `from_issuer_arrays` already validates ranges, dtypes, and known code values;
    this just slices the typed columns per issuer into dense ndarrays for the
    engine to read by field access.
    """

    issuer_count = len(issuer_ids)
    snapshot_months = horizon_months + 1
    mark_quanta = np.zeros((issuer_count, rollout_count, snapshot_months), dtype=np.int64)
    regime_codes = np.full((issuer_count, rollout_count, snapshot_months), NO_CODE, dtype=np.int64)
    event_kind_codes = np.full(
        (issuer_count, rollout_count, snapshot_months), int(PrivateEquityEventKindCode.NONE), dtype=np.int64
    )
    sale_opportunity_active = np.zeros((issuer_count, rollout_count, snapshot_months), dtype=np.bool_)
    sale_capacity_fractions = np.ones((issuer_count, rollout_count, snapshot_months), dtype=np.float64)
    eligible_fractions = np.ones((issuer_count, rollout_count, snapshot_months), dtype=np.float64)
    forced_sale_fractions = np.zeros((issuer_count, rollout_count, snapshot_months), dtype=np.float64)
    liquidity_blocked = np.zeros((issuer_count, rollout_count, snapshot_months), dtype=np.bool_)
    forced_recovery_cashout_quanta = np.zeros((issuer_count, rollout_count, snapshot_months), dtype=np.int64)
    for issuer_idx, issuer_id in enumerate(issuer_ids):
        if issuer_id not in private_equity.issuer_ids():
            raise ValueError(f"private-equity bundle missing required issuer {issuer_id!r}")
        mark_values = private_equity.issuer_float_matrix(
            issuer_id, "mark_usd_per_unit", rollout_count=rollout_count, horizon_months=horizon_months
        )
        # Check executable marks before quantization can round small negative values
        # to zero. Terminal marks remain informative only; execution uses months 0..H-1.
        executable_marks = mark_values[:, :horizon_months]
        if executable_marks.size and (not np.isfinite(executable_marks).all() or (executable_marks < 0.0).any()):
            raise ValueError(
                f"private-equity mark series for issuer {issuer_id!r} produced a negative or non-finite value"
            )
        mark_quanta[issuer_idx] = sampled_array_to_quanta(mark_values, quantum=currency_quantum)
        regime_codes[issuer_idx] = private_equity.issuer_int_matrix(
            issuer_id, "regime_code", rollout_count=rollout_count, horizon_months=horizon_months
        )
        event_kind_codes[issuer_idx] = private_equity.issuer_int_matrix(
            issuer_id, "event_kind_code", rollout_count=rollout_count, horizon_months=horizon_months
        )
        sale_opportunity_active[issuer_idx] = private_equity.issuer_bool_matrix(
            issuer_id, "sale_opportunity_active", rollout_count=rollout_count, horizon_months=horizon_months
        )
        sale_capacity_fractions[issuer_idx] = private_equity.issuer_float_matrix(
            issuer_id, "sale_capacity_fraction", rollout_count=rollout_count, horizon_months=horizon_months
        )
        eligible_fractions[issuer_idx] = private_equity.issuer_float_matrix(
            issuer_id, "eligible_fraction", rollout_count=rollout_count, horizon_months=horizon_months
        )
        forced_sale_fractions[issuer_idx] = private_equity.issuer_float_matrix(
            issuer_id, "forced_sale_fraction", rollout_count=rollout_count, horizon_months=horizon_months
        )
        liquidity_blocked[issuer_idx] = private_equity.issuer_bool_matrix(
            issuer_id, "liquidity_blocked", rollout_count=rollout_count, horizon_months=horizon_months
        )
        forced_recovery_values = private_equity.issuer_float_matrix(
            issuer_id, "forced_recovery_cashout_usd", rollout_count=rollout_count, horizon_months=horizon_months
        )
        executable_recovery = forced_recovery_values[:, :horizon_months]
        if executable_recovery.size and (executable_recovery < 0.0).any():
            raise ValueError("private-equity forced-recovery cashout series produced a negative value")
        forced_recovery_cashout_quanta[issuer_idx] = sampled_array_to_quanta(
            forced_recovery_values, quantum=currency_quantum
        )
    return PEChannels(
        execution=PEExecutionChannels(
            mark_quanta=mark_quanta,
            regime_codes=regime_codes,
            sale_opportunity_active=sale_opportunity_active,
            sale_capacity_fractions=sale_capacity_fractions,
            eligible_fractions=eligible_fractions,
            forced_sale_fractions=forced_sale_fractions,
            liquidity_blocked=liquidity_blocked,
            forced_recovery_cashout_quanta=forced_recovery_cashout_quanta,
        ),
        event_kind_codes=event_kind_codes,
    )

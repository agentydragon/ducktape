"""Quantize typed private-equity paths into the integer series a composed world reads."""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial

import numpy as np

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import IssuerId
from finance.augur.sim.fixed_point import round_ppb, sampled_array_to_quanta
from finance.augur.sim.market_path import Series
from finance.augur.sim.money import Currency


def compile_private_equity_series(
    issuer_ids: Sequence[IssuerId],
    bundle: PrivateEquityBundle,
    *,
    rollout_count: int,
    horizon_months: int,
    currency: Currency,
) -> tuple[Series, ...]:
    """The ten per-issuer private-equity channels, in the execution input's typed integer units.

    Each issuer's marks and forced-recovery cashouts are checked before they are quantized.
    """

    floats = partial(bundle.issuer_float_matrix, rollout_count=rollout_count, horizon_months=horizon_months)
    ints = partial(bundle.issuer_int_matrix, rollout_count=rollout_count, horizon_months=horizon_months)
    bools = partial(bundle.issuer_bool_matrix, rollout_count=rollout_count, horizon_months=horizon_months)
    bundle_issuer_ids = bundle.issuer_ids()
    series: list[Series] = []
    for issuer_id in issuer_ids:
        if issuer_id not in bundle_issuer_ids:
            raise ValueError(f"private-equity bundle missing required issuer {issuer_id!r}")
        marks = floats(issuer_id, "mark_usd_per_unit")
        # Check executable marks before quantization can round small negative values
        # to zero. Terminal marks remain informative only; execution uses months 0..H-1.
        executable_marks = marks[:, :horizon_months]
        if executable_marks.size and (not np.isfinite(executable_marks).all() or (executable_marks < 0.0).any()):
            raise ValueError(
                f"private-equity mark series for issuer {issuer_id!r} produced a negative or non-finite value"
            )
        mark_quanta = sampled_array_to_quanta(marks, quantum=currency.quantum)
        recoveries = floats(issuer_id, "forced_recovery_cashout_usd")
        executable_recoveries = recoveries[:, :horizon_months]
        if executable_recoveries.size and (executable_recoveries < 0.0).any():
            raise ValueError("private-equity forced-recovery cashout series produced a negative value")
        channels = {
            "mark": mark_quanta,
            "regime": ints(issuer_id, "regime_code"),
            "event_kind": ints(issuer_id, "event_kind_code"),
            "sale_opportunity": bools(issuer_id, "sale_opportunity_active").astype(np.int64),
            "sale_capacity": round_ppb(floats(issuer_id, "sale_capacity_fraction")),
            "eligible": round_ppb(floats(issuer_id, "eligible_fraction")),
            "forced_sale": round_ppb(floats(issuer_id, "forced_sale_fraction")),
            "liquidity_blocked": bools(issuer_id, "liquidity_blocked").astype(np.int64),
            "forced_recovery": sampled_array_to_quanta(recoveries, quantum=currency.quantum),
            "company_valuation": sampled_array_to_quanta(
                floats(issuer_id, "company_valuation_usd"), quantum=currency.quantum
            ),
        }
        series.extend(
            Series(
                series_id=f"private_equity_{channel}:{issuer_id}",
                snapshots=horizon_months + 1,
                values=tuple(int(value) for value in path.reshape(-1)),
            )
            for channel, path in channels.items()
        )
    return tuple(series)

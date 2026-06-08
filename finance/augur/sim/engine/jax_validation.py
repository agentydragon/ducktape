"""Host-side validation for sampled inputs consumed by the JAX simulation engine."""

from __future__ import annotations

import numpy as np

from finance.augur.sim.compiler import CompiledSimulation


def validate_seed_dependent_inputs(plan: CompiledSimulation) -> None:
    """Validate sampled numeric inputs whose bad values would otherwise enter compiled JAX code."""

    # PE-channel validation is seed-dependent (the channels are sampled), so it runs every call on the
    # concrete plan. The in-scan path can't raise. Only months 0..H-1 are executable sim months; the
    # terminal H snapshot exists for level-series lookups but is not validated by the eager loop.
    pe_channels = plan.pe_channels
    for issuer_idx, issuer_code in enumerate(plan.pe_issuers.codes):
        if int(issuer_code) < 0:
            continue
        marks = pe_channels.marks[issuer_idx, :, : plan.horizon_months]
        if marks.size and (not np.isfinite(marks).all() or (marks < 0.0).any()):
            raise ValueError(
                f"private-equity mark series for issuer {plan.strings[int(issuer_code)]!r} "
                "produced a negative or non-finite value"
            )
        forced_recovery = pe_channels.forced_recovery_cashout_cents[issuer_idx, :, : plan.horizon_months]
        if forced_recovery.size and (forced_recovery < 0).any():
            raise ValueError("private-equity forced-recovery cashout series produced a negative value")

    harvest = plan.harvest_policies
    for policy_idx in range(harvest.gain_profile_index.shape[0]):
        if int(harvest.gain_profile_index[policy_idx]) < 0 or not harvest.lot_mask[policy_idx].any():
            continue
        series_index = int(harvest.series_index[policy_idx])
        price = plan.external_values[series_index, :, : plan.horizon_months]
        if not np.isfinite(price).all() or (price < 0.0).any():
            raise ValueError(f"harvest policy {policy_idx} index series produced a negative or non-finite price")

"""Private-equity protocol bundle helpers."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from augur.model.private_equity_bundle import PrivateEquityBundle
from augur.model.series import PrivateEquityEventKindCode, PrivateEquityRegimeCode

BoolMatrix = npt.NDArray[np.bool_]
FloatMatrix = npt.NDArray[np.float64]


def observed_private_equity_mark_matrix(latent_mark: FloatMatrix, update_events: BoolMatrix) -> FloatMatrix:
    """Expose private marks only at observed update events.

    Before public-market trading exists, the sampled latent issuer value is an internal
    driver. The user-visible PE mark is the last observed admin/tender value, forward
    filled between sparse update events.
    """

    if latent_mark.shape != update_events.shape:
        raise ValueError(
            f"private-equity latent mark matrix has shape {latent_mark.shape}; "
            f"expected update event shape {update_events.shape}"
        )
    if not np.isfinite(latent_mark).all() or np.any(latent_mark <= 0.0):
        raise ValueError("private-equity latent mark matrix must be finite and positive")

    observed = np.empty_like(latent_mark, dtype=np.float64)
    observed[:, 0] = latent_mark[:, 0]
    for month in range(1, latent_mark.shape[1]):
        observed[:, month] = np.where(update_events[:, month], latent_mark[:, month], observed[:, month - 1])
    return observed


def neutral_private_equity_issuer_bundle(
    issuer_id: str, *, observed_mark: FloatMatrix, tender_events: BoolMatrix, rollout_count: int, horizon_months: int
) -> PrivateEquityBundle:
    """Build a single-issuer `PrivateEquityBundle` with v1 neutral protocol defaults.

    Private operating regime; tender event kind on tender months, `NONE` otherwise;
    full sale capacity and eligibility; no forced sales; no liquidity block; no forced
    recovery cashout. The opt-in M2 company-valuation channel is off here (all-zeros):
    this protocol has no market-cap concept, only a per-unit mark.
    """

    expected_shape = (rollout_count, horizon_months + 1)
    if tender_events.shape != expected_shape:
        raise ValueError(f"tender event matrix has shape {tender_events.shape}; expected {expected_shape}")
    event_kind = np.where(
        tender_events, int(PrivateEquityEventKindCode.TENDER), int(PrivateEquityEventKindCode.NONE)
    ).astype(np.int64)
    return PrivateEquityBundle.from_issuer_arrays(
        issuer_id,
        mark_usd_per_unit=observed_mark.astype(np.float64),
        regime_code=np.full(expected_shape, int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64),
        event_kind_code=event_kind,
        sale_opportunity_active=tender_events.astype(np.bool_),
        sale_capacity_fraction=np.ones(expected_shape, dtype=np.float64),
        eligible_fraction=np.ones(expected_shape, dtype=np.float64),
        forced_sale_fraction=np.zeros(expected_shape, dtype=np.float64),
        liquidity_blocked=np.zeros(expected_shape, dtype=np.bool_),
        forced_recovery_cashout_usd=np.zeros(expected_shape, dtype=np.float64),
        company_valuation_usd=np.zeros(expected_shape, dtype=np.float64),
        rollout_count=rollout_count,
        horizon_months=horizon_months,
    )

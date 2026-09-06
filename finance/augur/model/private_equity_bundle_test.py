"""A protocol channel carrying a code no vocabulary defines is rejected at construction.

`from_issuer_arrays` is the boundary between a sampled exogenous bundle and everything that
reads it, and it is the only place the codes are still plain integers. Letting an unknown one
past here would hand the simulator a regime or event kind it has to dispatch on and cannot
name, so the failure belongs at the boundary rather than deep in an engine.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import PrivateEquityEventKindCode, PrivateEquityRegimeCode

HORIZON_MONTHS = 12
SHAPE = (1, HORIZON_MONTHS + 1)
BAD_CODE = 999


def _bundle(*, regime_code: np.ndarray, event_kind_code: np.ndarray) -> PrivateEquityBundle:
    """One issuer whose every channel is inert, so only the codes under test can fail."""

    return PrivateEquityBundle.from_issuer_arrays(
        "acme",
        mark_usd_per_unit=np.full(SHAPE, 100.0),
        regime_code=regime_code,
        event_kind_code=event_kind_code,
        sale_opportunity_active=np.zeros(SHAPE, dtype=np.bool_),
        sale_capacity_fraction=np.full(SHAPE, 1.0),
        eligible_fraction=np.full(SHAPE, 1.0),
        forced_sale_fraction=np.zeros(SHAPE),
        liquidity_blocked=np.zeros(SHAPE, dtype=np.bool_),
        forced_recovery_cashout_usd=np.zeros(SHAPE),
        company_valuation_usd=np.zeros(SHAPE),
        rollout_count=1,
        horizon_months=HORIZON_MONTHS,
    )


def _codes(value: int, *, bad_month: int | None = None) -> np.ndarray:
    codes = np.full(SHAPE, value, dtype=np.int64)
    if bad_month is not None:
        codes[:, bad_month] = BAD_CODE
    return codes


def test_an_undefined_regime_code_is_refused() -> None:
    with pytest.raises(ValueError, match=rf"channel 'regime_code' has unknown code\(s\) \[{BAD_CODE}\]"):
        _bundle(
            regime_code=_codes(int(PrivateEquityRegimeCode.PRIVATE_OPERATING), bad_month=5),
            event_kind_code=_codes(int(PrivateEquityEventKindCode.NONE)),
        )


def test_an_undefined_event_kind_code_is_refused() -> None:
    with pytest.raises(ValueError, match=rf"channel 'event_kind_code' has unknown code\(s\) \[{BAD_CODE}\]"):
        _bundle(
            regime_code=_codes(int(PrivateEquityRegimeCode.PRIVATE_OPERATING)),
            event_kind_code=_codes(int(PrivateEquityEventKindCode.NONE), bad_month=5),
        )


if __name__ == "__main__":
    pytest_bazel.main()

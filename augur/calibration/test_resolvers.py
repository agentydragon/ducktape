"""Unit tests for per-rollout market resolvers against synthetic trajectories."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest_bazel

from augur.calibration.resolvers import (
    Resolution,
    RolloutTrajectory,
    resolve_ipo_by_date,
    resolve_market,
    resolve_pre_ipo_failure,
)
from augur.model.series import PrivateEquityEventKindCode, PrivateEquityRegimeCode

AS_OF = date(2026, 5, 27)


def _traj(horizon: int = 24, *, events: dict[int, int] | None = None) -> RolloutTrajectory:
    marks = np.ones(horizon + 1, dtype=np.float64)
    event_kind = np.zeros(horizon + 1, dtype=np.int64)
    regime = np.full(horizon + 1, int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64)
    for month, code in (events or {}).items():
        event_kind[month] = int(code)
    return RolloutTrajectory(mark_usd_per_unit=marks, event_kind_code=event_kind, regime_code=regime, as_of=AS_OF)


def test_ipo_by_date_yes_no_unresolved() -> None:
    ipo_at_5 = _traj(events={5: PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN})
    assert resolve_ipo_by_date(ipo_at_5, by_month=5) is Resolution.YES  # boundary inclusive
    assert resolve_ipo_by_date(ipo_at_5, by_month=4) is Resolution.NO  # IPO later than deadline
    no_ipo = _traj(horizon=24)
    assert resolve_ipo_by_date(no_ipo, by_month=24) is Resolution.NO  # whole window simulated
    assert resolve_ipo_by_date(no_ipo, by_month=999) is Resolution.UNRESOLVED  # deadline past horizon


def test_pre_ipo_failure_race() -> None:
    fail_first = _traj(
        events={3: PrivateEquityEventKindCode.COLLAPSE, 7: PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN}
    )
    assert resolve_pre_ipo_failure(fail_first) is Resolution.YES
    ipo_first = _traj(events={3: PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN, 7: PrivateEquityEventKindCode.COLLAPSE})
    assert resolve_pre_ipo_failure(ipo_first) is Resolution.NO
    acquired = _traj(events={5: PrivateEquityEventKindCode.ACQUISITION_CASHOUT})
    assert resolve_pre_ipo_failure(acquired) is Resolution.YES  # acquisition is a pre-IPO exit
    survives = _traj(horizon=24)
    assert resolve_pre_ipo_failure(survives) is Resolution.UNRESOLVED  # still private at end of horizon


def test_month_on_or_before() -> None:
    traj = _traj(horizon=120)
    assert traj.month_on_or_before(AS_OF) == 0
    assert traj.month_on_or_before(date(2027, 1, 1)) == 7
    assert traj.month_on_or_before(date(2030, 1, 1)) == 43


def test_resolve_market_dispatch() -> None:
    traj = _traj(horizon=120, events={7: PrivateEquityEventKindCode.PUBLIC_MARKET_OPEN})
    assert resolve_market(traj, mapping_kind="ipo_by_date", params={"by_date": "2027-01-01"}) is Resolution.YES
    assert resolve_market(traj, mapping_kind="ipo_by_date", params={"by_date": "2026-09-01"}) is Resolution.NO


def test_resolve_market_refuses_unmodeled_kind() -> None:
    """Valuation is not modeled by augur; it must not be silently scored."""
    traj = _traj()
    try:
        resolve_market(traj, mapping_kind="valuation_threshold", params={"threshold_usd": 1e12})
    except ValueError:
        return
    raise AssertionError("expected ValueError for a non-cleanly-resolvable mapping_kind")


if __name__ == "__main__":
    pytest_bazel.main()

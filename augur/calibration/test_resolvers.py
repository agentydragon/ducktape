"""Unit tests for per-rollout market resolvers against synthetic trajectories."""

from __future__ import annotations

from datetime import date

import numpy as np
import numpy.typing as npt
import pytest_bazel

from augur.calibration.platform import Direction
from augur.calibration.resolvers import (
    Resolution,
    RolloutTrajectory,
    bucket_model_counts,
    inflation_yoy_counts,
    level_by_date_counts,
    level_threshold_counts,
    resolve_ipo_by_date,
    resolve_pre_ipo_failure,
    resolve_valuation_by_date,
)
from augur.model.series import PrivateEquityEventKindCode, PrivateEquityRegimeCode

AS_OF = date(2026, 5, 27)


def _traj(
    horizon: int = 24, *, events: dict[int, int] | None = None, valuation: npt.NDArray[np.float64] | None = None
) -> RolloutTrajectory:
    marks = np.ones(horizon + 1, dtype=np.float64)
    event_kind = np.zeros(horizon + 1, dtype=np.int64)
    regime = np.full(horizon + 1, int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64)
    for month, code in (events or {}).items():
        event_kind[month] = int(code)
    return RolloutTrajectory(
        mark_usd_per_unit=marks,
        event_kind_code=event_kind,
        regime_code=regime,
        as_of=AS_OF,
        company_valuation_usd=valuation,
    )


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


def test_valuation_by_date_yes_no_unresolved() -> None:
    # V climbs 1e11 -> 1.2e12 over 24 months; crosses 1e12 at month 20.
    valuation = np.linspace(1e11, 1.2e12, 25)
    traj = _traj(horizon=24, valuation=valuation)
    assert resolve_valuation_by_date(traj, threshold_usd=1e12, by_month=20) is Resolution.YES  # crosses exactly here
    assert (
        resolve_valuation_by_date(traj, threshold_usd=1e12, by_month=24) is Resolution.YES
    )  # later deadline still YES
    assert resolve_valuation_by_date(traj, threshold_usd=1e12, by_month=19) is Resolution.NO  # not reached by deadline
    # Threshold never reached anywhere in the simulated window -> NO (whole window covered).
    assert resolve_valuation_by_date(traj, threshold_usd=5e12, by_month=24) is Resolution.NO
    # Deadline beyond the simulated horizon and not yet reached -> UNRESOLVED (might cross later).
    assert resolve_valuation_by_date(traj, threshold_usd=5e12, by_month=999) is Resolution.UNRESOLVED


def test_valuation_by_date_channel_off_is_unresolved() -> None:
    """An issuer with no valuation channel (`company_valuation_usd is None`) is never scoreable."""
    traj = _traj(horizon=24)  # valuation defaults to None
    assert resolve_valuation_by_date(traj, threshold_usd=1.0, by_month=24) is Resolution.UNRESOLVED
    assert resolve_valuation_by_date(traj, threshold_usd=1e12, by_month=999) is Resolution.UNRESOLVED


def test_month_on_or_before() -> None:
    traj = _traj(horizon=120)
    assert traj.month_on_or_before(AS_OF) == 0
    assert traj.month_on_or_before(date(2027, 1, 1)) == 7
    assert traj.month_on_or_before(date(2030, 1, 1)) == 43


def test_level_threshold_counts_point_in_time() -> None:
    # 4 rollouts, S&P at month 7 = [7000, 7600, 8000, 5000]; threshold 7500 ABOVE -> 2 YES.
    matrix = np.zeros((4, 13), dtype=np.float64)
    matrix[:, 7] = [7000.0, 7600.0, 8000.0, 5000.0]
    above = level_threshold_counts(matrix, threshold=7500.0, direction=Direction.ABOVE, at_month=7, horizon_months=12)
    assert (above.yes, above.no, above.unresolved) == (2, 2, 0)
    assert above.p_model == 0.5
    # BELOW is the exact complement.
    below = level_threshold_counts(matrix, threshold=7500.0, direction=Direction.BELOW, at_month=7, horizon_months=12)
    assert (below.yes, below.no) == (2, 2)
    # at_month past the simulated horizon -> every rollout UNRESOLVED (no p_model).
    beyond = level_threshold_counts(matrix, threshold=7500.0, direction=Direction.ABOVE, at_month=99, horizon_months=12)
    assert (beyond.yes, beyond.no, beyond.unresolved) == (0, 0, 4)
    assert beyond.p_model is None


def test_inflation_yoy_counts_window() -> None:
    # index grows 100 -> 103.5 over 12 months (3.5% YoY) for all rollouts; threshold 3% ABOVE -> all YES.
    matrix = np.ones((3, 13), dtype=np.float64) * 100.0
    matrix[:, 12] = 103.5
    yes = inflation_yoy_counts(matrix, threshold=0.03, direction=Direction.ABOVE, at_month=12, horizon_months=12)
    assert (yes.yes, yes.no) == (3, 0)
    # A trailing window reaching before month 0 (as_of) is not covered by the sample -> UNRESOLVED.
    early = inflation_yoy_counts(matrix, threshold=0.03, direction=Direction.ABOVE, at_month=6, horizon_months=12)
    assert (early.yes, early.no, early.unresolved) == (0, 0, 3)


def test_inflation_yoy_counts_with_pre_as_of_history() -> None:
    # Near-term YoY (at_month=2) looks back 10 months into real pre-as_of history.
    matrix = np.full((3, 13), 330.0, dtype=np.float64)
    matrix[:, 2] = 340.0  # numerator
    history = np.full(12, 300.0, dtype=np.float64)  # history[-10] == 300 -> yoy = 340/300 - 1 = 13.3%
    scored = inflation_yoy_counts(
        matrix, threshold=0.03, direction=Direction.ABOVE, at_month=2, horizon_months=12, history=history
    )
    assert (scored.yes, scored.no, scored.unresolved) == (3, 0, 0)
    # Without history the same near-term market is UNRESOLVED (the look-back precedes as_of).
    no_hist = inflation_yoy_counts(matrix, threshold=0.03, direction=Direction.ABOVE, at_month=2, horizon_months=12)
    assert no_hist.unresolved == 3
    # History too short to cover the look-back -> still UNRESOLVED.
    short = inflation_yoy_counts(
        matrix, threshold=0.03, direction=Direction.ABOVE, at_month=2, horizon_months=12, history=np.full(5, 300.0)
    )
    assert short.unresolved == 3


def test_level_by_date_counts_touch() -> None:
    # 4 rollouts start at 100; reach 150 at months {0:3, 1:10, 3:12}; rollout 2 never reaches.
    matrix = np.full((4, 13), 100.0, dtype=np.float64)
    matrix[0, 3:] = 150.0
    matrix[1, 10:] = 150.0
    matrix[3, 12] = 150.0
    # by month 5: only rollout 0 has touched 150 within the window.
    early = level_by_date_counts(matrix, threshold=150.0, direction=Direction.ABOVE, by_month=5, horizon_months=12)
    assert (early.yes, early.no, early.unresolved) == (1, 3, 0)
    # by month 12 (whole horizon): rollouts 0,1,3 touched -> YES; rollout 2 NO.
    full = level_by_date_counts(matrix, threshold=150.0, direction=Direction.ABOVE, by_month=12, horizon_months=12)
    assert (full.yes, full.no, full.unresolved) == (3, 1, 0)
    # deadline beyond the horizon: the un-touched rollout is UNRESOLVED (might cross later).
    beyond = level_by_date_counts(matrix, threshold=150.0, direction=Direction.ABOVE, by_month=99, horizon_months=12)
    assert (beyond.yes, beyond.no, beyond.unresolved) == (3, 0, 1)


def test_bucket_model_counts_tile() -> None:
    # value at month 5 = [50, 150, 250, 950]; buckets (-inf,100),[100,900),[900,+inf) -> [1,2,1].
    matrix = np.zeros((4, 13), dtype=np.float64)
    matrix[:, 5] = [50.0, 150.0, 250.0, 950.0]
    counts = bucket_model_counts(
        matrix, lows=[None, 100.0, 900.0], highs=[100.0, 900.0, None], at_month=5, horizon_months=12
    )
    assert counts is not None
    assert list(counts) == [1, 2, 1]
    # Beyond the horizon -> unscoreable (None).
    assert bucket_model_counts(matrix, lows=[None], highs=[None], at_month=99, horizon_months=12) is None


if __name__ == "__main__":
    pytest_bazel.main()

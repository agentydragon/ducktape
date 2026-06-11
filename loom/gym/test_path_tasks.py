from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pytest_bazel

from loom.gym.monthly_series import MonthlySeries, add_months
from loom.gym.path_tasks import path_tasks, tasks_for_path
from loom.gym.task import CategoricalOutcome, CategoricalQuestion

ANCHOR = date(2020, 1, 1)


def _series(series_id: str, levels: Sequence[float | None]) -> MonthlySeries:
    """Monthly levels from 2020-01; None makes a hole (unobserved month)."""
    return MonthlySeries(
        series_id=series_id,
        description=f"test {series_id}",
        unit="units",
        provenance="synthetic",
        values={add_months(ANCHOR, n): level for n, level in enumerate(levels) if level is not None},
    )


# 14 months 2020-01..2021-02: peak 110 in 2020-03, trough 95 in 2020-04
# ((110 - 95) / 110 = 13.6% drawdown), then a recovery that stays below the peak.
DIP = _series(
    "sp500", [100.0, 105.0, 110.0, 95.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0]
)


def test_dip_drawdown_and_first_cross_buckets() -> None:
    tasks = {task.task_id: task for task in tasks_for_path(DIP, anchor=ANCHOR)}
    assert set(tasks) == {"sp500-bundle-2020-01-drawdown", "sp500-bundle-2020-01-first-cross"}
    assert all(task.bundle_id == "sp500-bundle-2020-01" for task in tasks.values())
    assert all(task.as_of == date(2020, 2, 1) for task in tasks.values())
    assert all(task.resolution_date == date(2021, 1, 31) for task in tasks.values())
    drawdown = tasks["sp500-bundle-2020-01-drawdown"]
    assert drawdown.outcome == CategoricalOutcome(category="10.0% to under 20.0%")
    drawdown_question = drawdown.question
    assert isinstance(drawdown_question, CategoricalQuestion)
    assert drawdown_question.ordered
    assert drawdown_question.categories == (
        "under 5.0%",
        "5.0% to under 10.0%",
        "10.0% to under 20.0%",
        "at or above 20.0%",
    )
    # 2020-03 (window month 2) is the first month at or above the 1.10x threshold (110.00).
    assert tasks["sp500-bundle-2020-01-first-cross"].outcome == CategoricalOutcome(category="months 1-3")


def test_ramp_first_cross_and_never() -> None:
    # The ramp from 100 by +2/month first reaches the 1.10x threshold (110.00) at window month 5.
    ramp = _series("sp500", [100.0 + 2 * n for n in range(14)])
    tasks = {task.task_id: task for task in tasks_for_path(ramp, anchor=ANCHOR)}
    assert tasks["sp500-bundle-2020-01-first-cross"].outcome == CategoricalOutcome(category="months 4-6")
    # A monotone ramp never declines from its running peak.
    assert tasks["sp500-bundle-2020-01-drawdown"].outcome == CategoricalOutcome(category="under 5.0%")

    flat = _series("sp500", [100.0] * 14)
    flat_tasks = {task.task_id: task for task in tasks_for_path(flat, anchor=ANCHOR)}
    assert flat_tasks["sp500-bundle-2020-01-first-cross"].outcome == CategoricalOutcome(category="never")


def test_yoy_from_12m_apart_values() -> None:
    # YoY needs only the two months 12 apart: 102.5 / 100.0 → +2.5%, even though
    # every month in between is missing (so no other path task is emitted).
    cpi = _series("cpi", [100.0] + [None] * 11 + [102.5])
    (task,) = tasks_for_path(cpi, anchor=ANCHOR)
    assert task.task_id == "cpi-bundle-2020-01-yoy"
    assert task.bundle_id == "cpi-bundle-2020-01"
    assert task.outcome == CategoricalOutcome(category="2.0% to under 3.0%")
    question = task.question
    assert isinstance(question, CategoricalQuestion)
    assert question.ordered
    assert question.categories == ("under 2.0%", "2.0% to under 3.0%", "3.0% to under 4.0%", "at or above 4.0%")
    assert "2021-01 vs 2020-01" in question.text


def test_window_hole_skips_drawdown_and_first_cross() -> None:
    # 2020-07 (window month 6) is missing → the order-sensitive path statistics are uncomputable.
    holed = _series("sp500", [100.0, 105.0, 110.0, 95.0, 100.0, 101.0, None, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0])
    assert tasks_for_path(holed, anchor=ANCHOR) == []


def test_missing_target_month_skips_yoy() -> None:
    assert tasks_for_path(_series("cpi", [100.0, 101.0]), anchor=ANCHOR) == []


def test_path_tasks_iterates_anchor_grid() -> None:
    # Only the 2020-01 anchor has a full 12-month window within DIP's 14 months.
    tasks = path_tasks([DIP], anchor_start=ANCHOR, anchor_step_months=3)
    assert {task.task_id for task in tasks} == {"sp500-bundle-2020-01-drawdown", "sp500-bundle-2020-01-first-cross"}


if __name__ == "__main__":
    pytest_bazel.main()

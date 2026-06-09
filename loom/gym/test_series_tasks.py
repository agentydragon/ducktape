from __future__ import annotations

from datetime import date

import pytest_bazel

from loom.gym.monthly_series import MonthlySeries, add_months, month_end
from loom.gym.series_tasks import SeriesTaskSpec, series_tasks, tasks_for_spec
from loom.gym.task import BinaryOutcome, ScalarOutcome

# Linear ramp 100, 102, ... over 2020-01..2020-12 with a hole at 2020-05
# (mimics the BLS Oct-2025 CPI gap).
RAMP = MonthlySeries(
    series_id="ramp",
    description="test ramp",
    unit="units",
    provenance="synthetic",
    values={add_months(date(2020, 1, 1), n): 100.0 + 2 * n for n in range(12) if n != 4},
)


def test_month_arithmetic() -> None:
    assert add_months(date(2024, 11, 1), 2) == date(2025, 1, 1)
    assert month_end(date(2024, 2, 1)) == date(2024, 2, 29)


def test_max_observed_skips_holes() -> None:
    # Window (2020-03, 2020-06] contains the 2020-05 hole; max is over observed months only.
    assert RAMP.max_observed_between(after=date(2020, 3, 1), through=date(2020, 6, 1)) == 110.0


def test_generated_outcomes_match_series() -> None:
    spec = SeriesTaskSpec(series=RAMP, binary_thresholds=((6, 1.05), (6, 1.5)), scalar_horizons=(6,))
    tasks = {task.task_id: task for task in tasks_for_spec(spec, anchor_start=date(2020, 1, 1), anchor_step_months=3)}
    assert len(tasks) == len(set(tasks))

    # Anchor 2020-01 (level 100): +6m level is 112; ramp reaches 112 ≥ 105 (YES) but never 150 (NO).
    scalar = tasks["ramp-level-2020-01-h6"]
    assert scalar.outcome == ScalarOutcome(value=112.0)
    assert scalar.as_of == date(2020, 2, 1)
    assert scalar.resolution_date == date(2020, 7, 31)
    assert "100.00" in scalar.question.text
    assert tasks["ramp-ge-1.05x-2020-01-h6"].outcome == BinaryOutcome(value=True)
    assert tasks["ramp-ge-1.5x-2020-01-h6"].outcome == BinaryOutcome(value=False)


def test_windows_beyond_data_are_not_emitted() -> None:
    spec = SeriesTaskSpec(series=RAMP, binary_thresholds=((6, 1.05),), scalar_horizons=(6,))
    tasks = tasks_for_spec(spec, anchor_start=date(2020, 1, 1), anchor_step_months=3)
    # Last month is 2020-12; anchors 2020-07/2020-10 have no +6m data, so only 2020-01/2020-04 yield tasks.
    assert {task.as_of for task in tasks} == {date(2020, 2, 1), date(2020, 5, 1)}


def test_default_series_tasks_generate_at_scale() -> None:
    tasks = series_tasks()
    assert len(tasks) > 100
    assert len({task.task_id for task in tasks}) == len(tasks)
    # The glm-4.5 probed-cutoff window (as_of ≥ 2024-07-01) must be non-empty.
    assert sum(task.as_of >= date(2024, 7, 1) for task in tasks) >= 20


def test_known_history_spot_checks() -> None:
    # The S&P closed 2024 at 5881.63 after an anchor (2024-06) close of 5460.48:
    # the 1.05× six-month threshold (5733.50) was crossed (Nov 6032.38).
    tasks = {task.task_id: task for task in series_tasks()}
    assert tasks["sp500-ge-1.05x-2024-06-h6"].outcome == BinaryOutcome(value=True)
    assert tasks["sp500-level-2024-06-h6"].outcome == ScalarOutcome(value=5881.63)


if __name__ == "__main__":
    pytest_bazel.main()

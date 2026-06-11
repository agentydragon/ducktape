from __future__ import annotations

from datetime import date

import pytest_bazel

from loom.gym.monthly_series import MonthlySeries, add_months, month_end
from loom.gym.series_tasks import SeriesTaskSpec, tasks_for_spec
from loom.gym.task import BinaryOutcome, GridCoordinates, GridShape, ScalarOutcome, Task

# Linear ramp 100, 102, ... over 2020-01..2020-12 with a hole at 2020-05
# (mimics the BLS Oct-2025 CPI gap).
RAMP = MonthlySeries(
    series_id="ramp",
    description="test ramp",
    unit="units",
    provenance="synthetic",
    values={add_months(date(2020, 1, 1), n): 100.0 + 2 * n for n in range(12) if n != 4},
)

# Linear decline 100, 98, ... over 2020-01..2020-12.
DECLINE = MonthlySeries(
    series_id="decline",
    description="test decline",
    unit="units",
    provenance="synthetic",
    values={add_months(date(2020, 1, 1), n): 100.0 - 2 * n for n in range(12)},
)

FLAT = MonthlySeries(
    series_id="flat",
    description="test flat",
    unit="units",
    provenance="synthetic",
    values={add_months(date(2020, 1, 1), n): 100.0 for n in range(12)},
)


def test_month_arithmetic() -> None:
    assert add_months(date(2024, 11, 1), 2) == date(2025, 1, 1)
    assert month_end(date(2024, 2, 1)) == date(2024, 2, 29)


def test_max_observed_skips_holes() -> None:
    # Window (2020-03, 2020-06] contains the 2020-05 hole; max is over observed months only.
    assert RAMP.max_observed_between(after=date(2020, 3, 1), through=date(2020, 6, 1)) == 110.0


def test_min_observed_skips_holes() -> None:
    # Window (2020-03, 2020-06] contains the 2020-05 hole; min is over observed months only.
    assert RAMP.min_observed_between(after=date(2020, 3, 1), through=date(2020, 6, 1)) == 106.0


def test_floor_threshold_outcomes() -> None:
    # The decline reaches 88 by +6m, at or below the 0.9x floor (90) → YES; the flat series never dips → NO.
    def floor_task(series: MonthlySeries) -> Task:
        spec = SeriesTaskSpec(
            series=series, binary_thresholds=(), binary_floor_thresholds=((6, 0.9),), scalar_horizons=()
        )
        (task,) = tasks_for_spec(spec, anchor_start=date(2020, 1, 1), anchor_step_months=12)
        return task

    decline_task = floor_task(DECLINE)
    assert decline_task.task_id == "decline-le-0.9x-2020-01-h6"
    assert "at or below 90.00" in decline_task.question.text
    assert decline_task.outcome == BinaryOutcome(value=True)
    assert floor_task(FLAT).outcome == BinaryOutcome(value=False)


def test_scalar_horizon_emission() -> None:
    spec = SeriesTaskSpec(series=RAMP, binary_thresholds=(), scalar_horizons=(3,))
    (task,) = tasks_for_spec(spec, anchor_start=date(2020, 1, 1), anchor_step_months=12)
    assert task.task_id == "ramp-level-2020-01-h3"
    assert task.outcome == ScalarOutcome(value=106.0)
    # The generator stamps its grid point onto the task — the structured form
    # the id above is merely rendered from.
    assert task.grid == GridCoordinates(shape=GridShape.SCALAR_LEVEL, anchor=date(2020, 1, 1), horizon_months=3)


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


if __name__ == "__main__":
    pytest_bazel.main()
